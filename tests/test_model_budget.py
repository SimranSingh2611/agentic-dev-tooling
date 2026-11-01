import pytest
from langgraph.checkpoint.memory import InMemorySaver

from engineering.agents import Agents, demo_patch, demo_plan
from engineering.schemas import FileChange, ModelBudgetExceeded, Patch, Plan, Review, Task
from engineering.worker import work_once
from engineering.workflow import build_graph
from engineering.workspace import Workspace, tree_hash
from runner.executor import Validator


def create_run(api):
    response = api.post(
        "/engineering/runs",
        json={
            "requirement": "Add optional expiration dates to links.",
            "scenario": "brownfield",
            "mode": "live",
        },
    )
    assert response.status_code == 202
    return response.json()["id"]


def four_task_plan():
    plan = demo_plan("brownfield", "")
    previous = "implement"
    for number in range(2, 5):
        task_id = f"implement-{number}"
        plan.tasks.append(
            Task(
                id=task_id,
                description="Finish the candidate change",
                stage="implementation",
                depends_on=[previous],
            )
        )
        previous = task_id
    return Plan.model_validate(plan.model_dump())


def test_model_budget_reservations_survive_a_stale_checkpoint(store, api):
    run_id = create_run(api)
    store.claim("test-worker", 30)
    assert store.reserve_model_call(run_id, 1, 2, "first", 0) == 1
    assert store.reserve_model_call(run_id, 1, 2, "second", 0) == 2
    with pytest.raises(ModelBudgetExceeded):
        store.reserve_model_call(run_id, 1, 2, "third", 0)
    assert store.model_calls_used(run_id, 1, 0) == 2
    assert store.audit_valid(run_id)


def test_failed_implementation_keeps_all_reserved_calls_in_reported_count(config, store, api):
    class FailingMidway(Agents):
        def plan(self, state, files):
            return four_task_plan()

        def implement(self, state, files, task):
            if task["id"] == "implement-3":
                raise RuntimeError("Injected provider failure")
            if task["id"] == "implement":
                return Patch(
                    rationale="Partial documentation change",
                    files=[
                        FileChange(path="README.md", content="# Partially implemented candidate\n")
                    ],
                )
            return Patch(rationale="Task already satisfied", files=[])

    run_id = create_run(api)
    graph = build_graph(config, store, InMemorySaver(), agents=FailingMidway(config))
    work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "stopped"
    assert run.state["model_calls"] == 4, (
        "Plan and all three attempted implementation calls must survive the failed node"
    )
    assert sum(event.kind == "model.call_reserved" for event in store.events(run_id)) == 4
    work = Workspace(config, run_id, 1)
    assert run.state["candidate_hash"] == tree_hash(work.root.parent / "1-failed")
    assert run.state["restored_hash"] == tree_hash(work.baseline)
    assert run.state["candidate_hash"] != run.state["restored_hash"]


def test_budget_exhaustion_is_reported_explicitly(config, store, api):
    config.max_model_calls = 2

    class NoChangeTasks(Agents):
        def plan(self, state, files):
            return four_task_plan()

        def implement(self, state, files, task):
            return Patch(rationale="Already satisfied", files=[])

    run_id = create_run(api)
    work_once(
        config, store, build_graph(config, store, InMemorySaver(), agents=NoChangeTasks(config))
    )
    run = store.get(run_id)
    assert run.status == "stopped"
    assert run.state["error"]["type"] == "ModelBudgetExceeded"
    assert run.state["model_calls"] == 2
    assert "budget exhausted" in run.state["error"]["message"]


def test_repair_targets_failures_once_instead_of_replaying_four_tasks(
    config, store, api, live_patch
):
    tasks_seen = []

    class ScriptedAgents(Agents):
        def plan(self, state, files):
            return four_task_plan()

        def implement(self, state, files, task):
            tasks_seen.append(task["id"])
            return live_patch

        def review(self, state, files, validation):
            return Review(passed=True, findings=[])

    class FailFirstValidation(Validator):
        def check(self, state, kind):
            if state["attempt"] == 1 and kind == "tests":
                return {
                    "passed": False,
                    "candidate_hash": state["candidate_hash"],
                    "output": "Injected fixture failure requiring repair",
                }
            return super().check(state, kind)

    run_id = create_run(api)
    graph = build_graph(
        config,
        store,
        InMemorySaver(),
        agents=ScriptedAgents(config),
        validator=FailFirstValidation(config),
    )
    work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "awaiting_approval", run.state
    assert tasks_seen == [
        "implement",
        "implement-2",
        "implement-3",
        "implement-4",
        "workflow:complete-deliverables",
        "repair-attempt-2",
    ]
    assert run.state["model_calls"] == 9
    assert run.state["attempt"] == 2
    assert run.state["tests_result"]["passed"]
    assert store.audit_valid(run_id)


def test_no_change_patch_still_requires_real_acceptance_tests(config, store, api):
    config.max_repairs = 0

    class NoChanges(Agents):
        def plan(self, state, files):
            return demo_plan("brownfield", "")

        def implement(self, state, files, task):
            return Patch(rationale="Claims the feature is already present", files=[])

        def review(self, state, files, validation):
            return Review(passed=True, findings=[])

    run_id = create_run(api)
    work_once(config, store, build_graph(config, store, InMemorySaver(), agents=NoChanges(config)))
    run = store.get(run_id)
    assert run.status == "stopped"
    assert not run.state["tests_result"]["passed"], (
        "Unchanged baseline does not implement expiration"
    )
    assert "error" not in run.state, "A no-op is not an execution-policy failure"
    assert any(event.kind == "task.no_change" for event in store.events(run_id))
    assert not any(event.kind == "release.prepared" for event in store.events(run_id))


def test_completion_fulfills_edits_mislabeled_as_design_or_validation(
    config, store, api, live_patch
):
    seen = []

    class MislabeledPlan(Agents):
        def plan(self, state, files):
            plan = demo_plan("brownfield", "")
            plan.tasks[0].description = "Add the expiration column to the model."
            plan.tasks[2].description = "Write new expiration regression tests."
            return plan

        def implement(self, state, files, task):
            seen.append(task["id"])
            if task["id"] == "workflow:complete-deliverables":
                assert state["plan"]["tasks"][0]["stage"] == "design"
                assert state["plan"]["tasks"][2]["stage"] == "validation"
                return live_patch
            return Patch(rationale="Deferred changes to final integration", files=[])

        def review(self, state, files, validation):
            return Review(passed=True, findings=[])

    run_id = create_run(api)
    work_once(
        config, store, build_graph(config, store, InMemorySaver(), agents=MislabeledPlan(config))
    )
    run = store.get(run_id)
    assert run.status == "awaiting_approval", run.state
    assert seen == ["implement", "workflow:complete-deliverables"]
    assert run.state["tests_result"]["passed"]
    assert "8 passed" in run.state["tests_result"]["output"]


def test_contract_pass_cannot_hide_missing_requested_tests_and_docs(config, store, api):
    config.max_repairs = 0

    class MissingDeliverables(Agents):
        def plan(self, state, files):
            return demo_plan("brownfield", "")

        def implement(self, state, files, task):
            return demo_patch(config.source_root, "brownfield")

        def review(self, state, files, validation):
            return Review(passed=True, findings=[])

    run_id = create_run(api)
    work_once(
        config,
        store,
        build_graph(config, store, InMemorySaver(), agents=MissingDeliverables(config)),
    )
    run = store.get(run_id)
    assert run.status == "stopped"
    assert "7 passed" in run.state["tests_result"]["output"]
    assert not run.state["tests_result"]["passed"]
    assert len(run.state["tests_result"]["missing_deliverables"]) == 2
    assert not any(event.kind == "approval.recorded" for event in store.events(run_id))
