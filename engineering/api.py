import hmac
import statistics
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select, text

from engineering.config import Settings, settings
from engineering.schemas import ApprovalRequest, ReplanRequest, ResumeRequest, RunCreate
from engineering.store import Event, Run, Store, append_event
from engineering.workspace import PolicyError, Workspace, tree_hash


def run_view(run):
    return {
        "id": run.id,
        "requirement": run.requirement,
        "scenario": run.scenario,
        "mode": run.mode,
        "revision": run.revision,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "state": run.state,
    }


def create_app(config: Settings | None = None):
    config = config or settings()
    store = Store(config.engineering_database_url)

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize()
        yield
        store.engine.dispose()

    app = FastAPI(
        title="Agentic Engineering Control Plane",
        version="0.1.0",
        description="Create demo/live engineering runs, inspect evidence, clarify requirements, and approve an exact candidate. Demo mode uses deterministic fixtures. Approval prepares source artifacts; it does not deploy them.",
        lifespan=lifespan,
    )
    app.state.store = store

    def authenticate(x_api_key: Annotated[str | None, Header()] = None):
        if not x_api_key or not hmac.compare_digest(x_api_key, config.engineering_api_key):
            raise HTTPException(401, "A valid engineering X-API-Key is required")

    router = APIRouter(prefix="/engineering", dependencies=[Depends(authenticate)])

    def existing(db, run_id):
        run = db.scalar(select(Run).where(Run.id == str(run_id)).with_for_update())
        if not run:
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/health")
    def health():
        with store.engine.connect() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "service": "engineering-api"}

    @router.post("/runs", status_code=202)
    def create_run(body: RunCreate):
        with store.sessions.begin() as db:
            run = Run(**body.model_dump())
            db.add(run)
            db.flush()
            append_event(
                db,
                run,
                "run.created",
                {"requirement": run.requirement, "mode": run.mode, "scenario": run.scenario},
            )
            return run_view(run)

    @router.get("/runs/{run_id}")
    def get_run(run_id: uuid.UUID):
        run = store.get(str(run_id))
        if not run:
            raise HTTPException(404, "Run not found")
        return {
            **run_view(run),
            "audit": {
                "verified": store.audit_valid(str(run_id)),
                "events": run.audit_seq,
                "head": run.audit_head,
            },
        }

    @router.get("/runs/{run_id}/events")
    def events(
        run_id: uuid.UUID,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        if not store.get(str(run_id)):
            raise HTTPException(404, "Run not found")
        return [
            {
                "sequence": event.sequence,
                "revision": event.revision,
                "kind": event.kind,
                "payload": event.payload,
                "occurred_at": event.occurred_at,
                "previous_hash": event.previous_hash,
                "hash": event.hash,
            }
            for event in store.events(str(run_id))
            if event.sequence > after
        ][:limit]

    @router.post("/runs/{run_id}/clarification", status_code=202)
    def clarify(run_id: uuid.UUID, body: ResumeRequest):
        with store.sessions.begin() as db:
            run = existing(db, run_id)
            if run.status != "awaiting_clarification" or run.revision != body.revision:
                raise HTTPException(409, "Run is not awaiting clarification at that revision")
            run.action = {"gate": "clarify", "value": body.answer}
            run.status = "queued"
            append_event(db, run, "clarification.submitted", {"answer": body.answer})
            return run_view(run)

    @router.post("/runs/{run_id}/approval", status_code=202)
    def approve(run_id: uuid.UUID, body: ApprovalRequest):
        with store.sessions.begin() as db:
            run = existing(db, run_id)
            if run.status != "awaiting_approval" or run.revision != body.revision:
                raise HTTPException(409, "Run is not awaiting approval at that revision")
            work = Workspace(config, run.id, run.revision)
            try:
                actual_hash = tree_hash(work.root)
            except PolicyError as exc:
                raise HTTPException(409, "Candidate failed workspace integrity checks") from exc
            if (
                body.candidate_hash != run.state.get("candidate_hash")
                or actual_hash != body.candidate_hash
            ):
                raise HTTPException(409, "Candidate changed; approval is stale")
            run.action = {"gate": "approval", "value": body.model_dump()}
            run.status = "queued"
            append_event(db, run, "approval.submitted", body.model_dump())
            return run_view(run)

    @router.post("/runs/{run_id}/replan", status_code=202)
    def replan(run_id: uuid.UUID, body: ReplanRequest):
        with store.sessions.begin() as db:
            run = existing(db, run_id)
            if run.status in {"running", "queued"} or run.revision != body.revision:
                raise HTTPException(
                    409, "Pause/completion and the current revision are required for replanning"
                )
            previous = {
                "revision": run.revision,
                "requirement": run.requirement,
                "candidate_hash": run.state.get("candidate_hash"),
            }
            run.revision += 1
            run.requirement = body.requirement
            run.state = {}
            run.action = None
            run.status = "queued"
            append_event(
                db,
                run,
                "requirement.replanned",
                {
                    "previous": previous,
                    "requirement": body.requirement,
                    "invalidated": [
                        "plan",
                        "implementation",
                        "validation",
                        "documentation",
                        "approval",
                    ],
                    "policy": "Conservative full replan from source baseline; prior revisions and their artifacts are retained",
                },
            )
            return run_view(run)

    @router.post("/runs/{run_id}/cancel")
    def cancel(run_id: uuid.UUID):
        with store.sessions.begin() as db:
            run = existing(db, run_id)
            if run.status in {"completed", "rejected", "stopped", "cancelled"}:
                raise HTTPException(409, "Run is already terminal")
            was_running = run.status == "running"
            run.status = "cancelled"
            run.action = None
            append_event(db, run, "run.cancelled", {"cooperative": True, "running": was_running})
            if not was_running:
                work = Workspace(config, run.id, run.revision)
                if work.baseline.exists():
                    work.save_artifacts(run.state)
                    restored = work.rollback()
                    run.state = {**run.state, "outcome": "cancelled", "restored_hash": restored}
                    work.save_artifacts(run.state)
                    append_event(
                        db,
                        run,
                        "workspace.rolled_back",
                        {"reason": "cancelled", "restored_hash": restored},
                    )
            return run_view(run)

    @router.get("/runs/{run_id}/artifacts/{artifact}")
    def artifacts(
        run_id: uuid.UUID, artifact: str, revision: int | None = Query(default=None, ge=1)
    ):
        run = store.get(str(run_id))
        if not run:
            raise HTTPException(404, "Run not found")
        names = {"diff": "change.diff", "summary": "SUMMARY.md", "state": "run.json"}
        if artifact not in names or (revision and revision > run.revision):
            raise HTTPException(404, "Artifact not found")
        path = Workspace(config, run.id, revision or run.revision).artifacts / names[artifact]
        if not path.is_file():
            raise HTTPException(404, "Artifact is not available yet")
        return FileResponse(path, filename=names[artifact])

    @router.get("/metrics")
    def metrics():
        with store.sessions() as db:
            runs = list(db.scalars(select(Run)))
            events = list(db.scalars(select(Event).order_by(Event.id)))
        terminal = [
            run for run in runs if run.status in {"completed", "rejected", "stopped", "cancelled"}
        ]
        retries = [event for event in events if event.kind == "repair.scheduled"]
        rollbacks = [event for event in events if event.kind == "workspace.rolled_back"]
        recovery, failures, active, processing = [], {}, {}, []
        for event in events:
            stamp = datetime.fromisoformat(event.occurred_at)
            key = (event.run_id, event.revision)
            if event.kind == "validation.gate":
                if not event.payload["passed"]:
                    failures.setdefault(key, stamp)
                elif key in failures:
                    recovery.append((stamp - failures.pop(key)).total_seconds())
            if event.kind == "worker.claimed":
                active[key] = stamp
            elif event.kind == "run.status" and key in active:
                processing.append((stamp - active.pop(key)).total_seconds())

        def aware(value):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value

        latency = [
            (aware(run.updated_at) - aware(run.created_at)).total_seconds() for run in terminal
        ]
        return {
            "runs": len(runs),
            "terminal_runs": len(terminal),
            "completed_runs": sum(run.status == "completed" for run in runs),
            "success_rate": sum(run.status == "completed" for run in terminal) / len(terminal)
            if terminal
            else None,
            "repair_count": len(retries),
            "rollback_count": len(rollbacks),
            "repair_frequency": len(retries) / len(runs) if runs else 0,
            "rollback_frequency": len(rollbacks) / len(runs) if runs else 0,
            "mttr_seconds": statistics.mean(recovery) if recovery else None,
            "mean_end_to_end_seconds": statistics.mean(latency) if latency else None,
            "mean_worker_segment_seconds": statistics.mean(processing) if processing else None,
            "definitions": {
                "success": "Current run status completed / terminal runs, including explicit rejections and cancellations",
                "mttr": "First failed validation gate to next passed gate for a recovered revision",
                "end_to_end": "Run creation to terminal status, including human waiting time",
                "worker_segment": "Claim to next persisted pause/completion; excludes human waiting time",
            },
        }

    app.include_router(router)
    return app
