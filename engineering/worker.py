import fcntl
import logging
import threading
import time
import uuid

from langgraph.types import Command

from engineering.clarification import ClarificationLimitExceeded
from engineering.config import settings
from engineering.schemas import ModelBudgetExceeded
from engineering.store import Store
from engineering.workflow import Cancelled, build_graph, checkpointer
from engineering.workspace import PolicyError, Workspace, tree_hash

log = logging.getLogger(__name__)


def execute_run(config, store, run, owner, graph):
    graph_config = {
        "configurable": {"thread_id": f"{run.id}:{run.revision}", "worker_owner": owner},
        "recursion_limit": 100,
    }
    try:
        checkpoint = graph.get_state(graph_config)
        if not checkpoint.values:
            value = {
                "run_id": run.id,
                "revision": run.revision,
                "requirement": run.requirement,
                "scenario": run.scenario,
                "mode": run.mode,
            }
        elif run.action and checkpoint.interrupts and run.action["gate"] in checkpoint.next:
            value = Command(resume=run.action["value"])
        else:
            value = None
        if value is not None or checkpoint.next:
            graph.invoke(value, graph_config)
        snapshot = graph.get_state(graph_config)
        state = dict(snapshot.values)
        if snapshot.interrupts:
            state["interruption"] = snapshot.interrupts[0].value
            status = (
                "awaiting_clarification"
                if state["interruption"]["kind"] == "clarification"
                else "awaiting_approval"
            )
        else:
            status = state.get("outcome", "stopped")
        store.finish(run.id, owner, state, status)
    except Cancelled:
        log.info("Run %s cancelled", run.id)
        current = store.get(run.id)
        if current and current.status == "cancelled":
            work = Workspace(config, run.id, run.revision)
            if work.baseline.exists():
                work.save_artifacts(dict(graph.get_state(graph_config).values))
                restored = work.rollback()
                store.event(
                    run.id,
                    "workspace.rolled_back",
                    {"reason": "cancelled", "restored_hash": restored},
                )
    except Exception as exc:
        # Provider errors may contain request metadata; persist an actionable type,
        # not an arbitrary exception string that could contain credentials.
        log.error("Run %s stopped (%s)", run.id, type(exc).__name__)
        state = dict(graph.get_state(graph_config).values)
        state["model_calls"] = store.model_calls_used(
            run.id, run.revision, state.get("model_calls", 0)
        )
        state["error"] = {
            "type": type(exc).__name__,
            "message": "Clarification round limit reached with unresolved blockers. Inspect state.plan.questions, update the requirement with the missing decisions, and use replan. No implementation or release was authorized."
            if isinstance(exc, ClarificationLimitExceeded)
            else "Model call budget exhausted. Inspect the recorded model.call_reserved events and validation failures, then replan. Model-call reservations are retained even when a later task fails."
            if isinstance(exc, ModelBudgetExceeded)
            else "OPENAI_API_KEY is missing from the worker environment. Configure it locally, restart the worker, and replan this run."
            if run.mode == "live" and not config.openai_api_key
            else "Execution stopped safely. Check model configuration, runner availability, and validation artifacts; replan to retry.",
        }
        work = Workspace(config, run.id, run.revision)
        if work.baseline.exists():
            try:
                state["candidate_hash"] = tree_hash(work.root)
            except PolicyError:
                state["candidate_integrity_failed"] = True
            work.save_artifacts(state)
            state["restored_hash"] = work.rollback()
            state["outcome"] = "stopped"
            work.save_artifacts(state)
            store.event(
                run.id,
                "workspace.rolled_back",
                {"reason": type(exc).__name__, "restored_hash": state["restored_hash"]},
            )
        store.event(run.id, "run.safe_stopped", state["error"])
        store.finish(run.id, owner, state, "stopped")


def work_once(config, store, graph, owner=None):
    owner = owner or str(uuid.uuid4())
    run = store.claim(owner, config.worker_lease_seconds)
    if not run:
        return False
    done = threading.Event()

    def renew():
        while not done.wait(max(1, config.worker_lease_seconds // 3)):
            if not store.heartbeat(run.id, owner, config.worker_lease_seconds):
                return

    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        locks = config.workspace_dir / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        # Fence concurrent processes on this host/shared filesystem, including
        # an old worker whose database lease expired during a slow model call.
        with (locks / f"{run.id}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            execute_run(config, store, run, owner, graph)
    finally:
        done.set()
        heartbeat.join(timeout=2)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = settings()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.engineering_database_url)
    store.initialize()
    with checkpointer(config) as saver:
        graph = build_graph(config, store, saver)
        owner = str(uuid.uuid4())
        while True:
            if not work_once(config, store, graph, owner):
                time.sleep(1)


if __name__ == "__main__":
    main()
