import pytest
from langgraph.checkpoint.memory import InMemorySaver

from engineering.store import Event
from engineering.worker import work_once
from engineering.workflow import build_graph, checkpointer
from engineering.workspace import Workspace, tree_hash


def test_parallel_validation_paths_join_before_approval(config, store, api):
    import threading

    barrier = threading.Barrier(2)

    class ParallelValidator:
        def check(self, state, kind):
            barrier.wait(timeout=3)
            return {
                "passed": True,
                "output": "Concurrency probe",
                "candidate_hash": state["candidate_hash"],
                "findings": [],
            }

    run_id = create_run(api)
    graph = build_graph(config, store, InMemorySaver(), validator=ParallelValidator())
    work_once(config, store, graph)
    assert store.get(run_id).status == "awaiting_approval"
    kinds = [event.kind for event in store.events(run_id)]
    assert kinds.index("validation.tests") < kinds.index("validation.gate")
    assert kinds.index("validation.review") < kinds.index("validation.gate")


def test_repair_recovers_and_metrics_record_recovery(config, store, api):
    from runner.executor import Validator

    class EventuallyPassing(Validator):
        def check(self, state, kind):
            if kind == "tests" and state["attempt"] == 1:
                return {
                    "passed": False,
                    "output": "Injected first-attempt failure",
                    "candidate_hash": state["candidate_hash"],
                }
            return super().check(state, kind)

    run_id = create_run(api)
    graph = build_graph(config, store, InMemorySaver(), validator=EventuallyPassing(config))
    work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "awaiting_approval", run.state
    assert run.state["attempt"] == 2
    assert approve(api, run_id).status_code == 202
    work_once(config, store, graph)
    metrics = api.get("/engineering/metrics").json()
    assert metrics["success_rate"] == 1
    assert metrics["repair_count"] == 1
    assert metrics["mttr_seconds"] > 0


def test_concurrent_workers_cannot_claim_same_run(config, store, api):
    from concurrent.futures import ThreadPoolExecutor

    run_id = create_run(api)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda owner: store.claim(owner, 30), ["worker-a", "worker-b"]))
    claimed = [run for run in claims if run is not None]
    assert len(claimed) == 1
    assert claimed[0].id == run_id
    assert store.audit_valid(run_id)


def test_expired_worker_lease_is_recovered_without_reapplying_change(config, store, api):
    from datetime import UTC, datetime, timedelta

    from engineering.store import Run

    run_id = create_run(api)
    with checkpointer(config) as saver:
        work_once(config, store, build_graph(config, store, saver))
    assert approve(api, run_id).status_code == 202
    assert store.claim("dead-worker", 30).id == run_id
    with store.sessions.begin() as db:
        db.get(Run, run_id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
    with checkpointer(config) as saver:
        work_once(config, store, build_graph(config, store, saver))
    assert store.get(run_id).status == "completed"
    events = store.events(run_id)
    assert any(event.kind == "worker.claimed" and event.payload["recovered"] for event in events)
    assert sum(event.kind == "task.completed" for event in events) == 1


def test_demo_does_not_override_a_negative_clarification(config, store, api):
    run_id = create_run(api, "ambiguous")
    with checkpointer(config) as saver:
        graph = build_graph(config, store, saver)
        work_once(config, store, graph)
        api.post(
            f"/engineering/runs/{run_id}/clarification",
            json={"revision": 1, "answer": "No, do not block any domains."},
        )
        work_once(config, store, graph)
    assert store.get(run_id).status == "stopped"
    assert not any(event.kind == "task.completed" for event in store.events(run_id))


def test_cancellation_before_applying_patch_restores_baseline(config, store, api):
    from engineering.agents import Agents

    run_id = create_run(api)

    class CancellingAgents(Agents):
        def implement(self, state, files, task):
            assert api.post(f"/engineering/runs/{run_id}/cancel").status_code == 200
            return super().implement(state, files, task)

    graph = build_graph(config, store, InMemorySaver(), agents=CancellingAgents(config))
    work_once(config, store, graph)
    assert store.get(run_id).status == "cancelled"
    work = Workspace(config, run_id, 1)
    assert tree_hash(work.root) == tree_hash(work.baseline)


def test_unexpected_provider_errors_do_not_expose_credentials(config, store, api):
    from engineering.agents import Agents

    class FailingAgents(Agents):
        def plan(self, state, files):
            raise RuntimeError("secret-provider-credential")

    run_id = create_run(api)
    work_once(
        config, store, build_graph(config, store, InMemorySaver(), agents=FailingAgents(config))
    )
    assert "secret-provider-credential" not in str(store.get(run_id).state)
    assert "secret-provider-credential" not in str(
        [event.payload for event in store.events(run_id)]
    )


def create_run(api, scenario="brownfield"):
    response = api.post(
        "/engineering/runs",
        json={
            "requirement": "Add optional expiration dates to shortened links.",
            "scenario": scenario,
            "mode": "demo",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def approve(api, run_id, approved=True):
    run = api.get(f"/engineering/runs/{run_id}").json()
    return api.post(
        f"/engineering/runs/{run_id}/approval",
        json={
            "revision": run["revision"],
            "candidate_hash": run["state"]["candidate_hash"],
            "approved": approved,
            "reviewer": "test-reviewer",
        },
    )


@pytest.mark.parametrize("scenario", ["greenfield", "brownfield", "ambiguous"])
def test_three_scenarios_make_real_changes_and_require_approval(config, store, api, scenario):
    with checkpointer(config) as saver:
        graph = build_graph(config, store, saver)
        run_id = create_run(api, scenario)
        assert work_once(config, store, graph)
        run = store.get(run_id)
        if scenario == "ambiguous":
            assert run.status == "awaiting_clarification"
            response = api.post(
                f"/engineering/runs/{run_id}/clarification",
                json={
                    "revision": 1,
                    "answer": "Yes, block malware.test and its subdomains with HTTP 422.",
                },
            )
            assert response.status_code == 202
            assert work_once(config, store, graph)
            run = store.get(run_id)
        assert run.status == "awaiting_approval", run.state
        assert run.state["tests_result"]["passed"], run.state
        assert run.state["tests_result"]["candidate_hash"] == run.state["candidate_hash"]
        assert api.get(f"/engineering/runs/{run_id}/artifacts/diff").text
        assert not (Workspace(config, run_id, 1).artifacts / "bundle").exists()
        assert approve(api, run_id).status_code == 202
        assert work_once(config, store, graph)
        assert store.get(run_id).status == "completed"
        assert store.audit_valid(run_id)
        assert (Workspace(config, run_id, 1).artifacts / "bundle/shortener/main.py").is_file()


def test_approval_survives_worker_restart(config, store, api):
    run_id = create_run(api)
    with checkpointer(config) as saver:
        work_once(config, store, build_graph(config, store, saver))
    assert store.get(run_id).status == "awaiting_approval"
    assert approve(api, run_id).status_code == 202
    with checkpointer(config) as saver:
        work_once(config, store, build_graph(config, store, saver))
    assert store.get(run_id).status == "completed"


def test_stale_approval_and_replan_invalidates_evidence(config, store, api):
    run_id = create_run(api)
    with checkpointer(config) as saver:
        work_once(config, store, build_graph(config, store, saver))
    previous = store.get(run_id)
    response = api.post(
        f"/engineering/runs/{run_id}/replan",
        json={
            "revision": 1,
            "requirement": "Add expiration dates and document the migration clearly.",
        },
    )
    assert response.status_code == 202
    stale = api.post(
        f"/engineering/runs/{run_id}/approval",
        json={
            "revision": 1,
            "candidate_hash": previous.state["candidate_hash"],
            "approved": True,
            "reviewer": "test",
        },
    )
    assert stale.status_code == 409
    assert store.get(run_id).revision == 2
    with checkpointer(config) as saver:
        work_once(config, store, build_graph(config, store, saver))
    assert store.get(run_id).status == "awaiting_approval"
    assert store.get(run_id).state["revision"] == 2
    assert store.audit_valid(run_id)


def test_tampered_candidate_cannot_be_approved(config, store, api):
    run_id = create_run(api)
    with checkpointer(config) as saver:
        work_once(config, store, build_graph(config, store, saver))
    work = Workspace(config, run_id, 1)
    (work.root / "README.md").write_text("Changed after validation")
    assert approve(api, run_id).status_code == 409


def test_rejection_rolls_back_workspace_and_never_releases(config, store, api):
    run_id = create_run(api)
    with checkpointer(config) as saver:
        graph = build_graph(config, store, saver)
        work_once(config, store, graph)
        assert approve(api, run_id, approved=False).status_code == 202
        work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "rejected"
    work = Workspace(config, run_id, 1)
    assert tree_hash(work.root) == tree_hash(work.baseline)
    assert not (work.artifacts / "bundle").exists()
    assert (work.artifacts / "change.diff").read_text(), "Rollback must preserve the failed diff"


class FailingValidator:
    def check(self, state, kind):
        return {
            "passed": False,
            "output": "Injected validation failure",
            "candidate_hash": state["candidate_hash"],
            "findings": [],
        }


def test_failed_validation_has_bounded_repairs_and_rollback(config, store, api):
    run_id = create_run(api)
    config.max_repairs = 1
    graph = build_graph(config, store, InMemorySaver(), validator=FailingValidator())
    work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "stopped"
    assert run.state["attempt"] == 2
    assert sum(event.kind == "repair.scheduled" for event in store.events(run_id)) == 1
    assert sum(event.kind == "workspace.rolled_back" for event in store.events(run_id)) == 1


def test_cancelled_run_cannot_be_claimed(config, store, api):
    run_id = create_run(api)
    assert api.post(f"/engineering/runs/{run_id}/cancel").status_code == 200
    assert not work_once(config, store, build_graph(config, store, InMemorySaver()))
    assert store.get(run_id).status == "cancelled"


def test_hash_chain_detects_event_tampering(store, api):
    run_id = create_run(api)
    assert store.audit_valid(run_id)
    assert api.get(f"/engineering/runs/{run_id}").json()["audit"]["verified"]
    with store.sessions.begin() as db:
        event = db.query(Event).filter_by(run_id=run_id).first()
        event.payload = {"modified": True}
    assert not store.audit_valid(run_id)
    assert not api.get(f"/engineering/runs/{run_id}").json()["audit"]["verified"]


def test_live_run_without_key_stops_without_network_call(config, store, api):
    response = api.post(
        "/engineering/runs", json={"requirement": "Add expiration dates to links.", "mode": "live"}
    )
    run_id = response.json()["id"]
    work_once(config, store, build_graph(config, store, InMemorySaver()))
    assert store.get(run_id).status == "stopped"
    assert store.get(run_id).state["error"]["type"] == "ValueError"


def test_management_api_is_authenticated(api):
    api.headers.pop("X-API-Key")
    assert api.get("/engineering/metrics").status_code == 401
    assert api.post("/engineering/runs", json={"requirement": "Build a service"}).status_code == 401
