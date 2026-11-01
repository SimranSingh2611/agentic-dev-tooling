from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

from engineering.agents import Agents, demo_plan
from engineering.clarification import decision_text, load_history
from engineering.worker import work_once
from engineering.workflow import build_graph, checkpointer


def event(kind, payload, revision=1):
    return SimpleNamespace(kind=kind, payload=payload, revision=revision)


def test_legacy_checkpoint_recovers_question_answer_pairs_and_deduplicates_text():
    answer = 'Use UTC. Preserve the message "two  spaces".'
    events = [
        event("plan.created", {"questions": ["Which timezone?"]}),
        event("requirement.clarified", {"answer": answer}),
        event("plan.created", {"questions": ["Should dates use UTC?"]}),
        event("requirement.clarified", {"answer": answer}),
        event("requirement.clarified", {"answer": "another revision"}, revision=2),
    ]
    state = {"run_id": "legacy", "revision": 1, "clarification": "\n" + answer + "\n" + answer}
    history = load_history(state, SimpleNamespace(events=lambda _: events))
    assert [entry["questions"] for entry in history] == [
        ["Which timezone?"],
        ["Should dates use UTC?"],
    ]
    assert decision_text(history, state["clarification"]) == answer
    assert len(history) == 2, "Keep both human submissions in the decision lineage"


def test_saved_history_is_authoritative_without_reloading_events():
    history = [{"questions": ["Policy?"], "answer": "Keep existing access controls."}]
    assert load_history({"clarification_history": history}, None) == history


def test_later_human_corrections_are_retained_in_order():
    history = [
        {"questions": ["Retention?"], "answer": "Keep 30 days."},
        {"questions": ["Confirm change?"], "answer": "Change retention to 7 days."},
    ]
    assert decision_text(history) == "Keep 30 days.\nChange retention to 7 days."


def create_run(api, mode="demo"):
    response = api.post(
        "/engineering/runs",
        json={
            "requirement": "Make shortened links safer for users.",
            "scenario": "brownfield",
            "mode": mode,
        },
    )
    assert response.status_code == 202
    return response.json()["id"]


def answer(api, run_id, text):
    response = api.post(
        f"/engineering/runs/{run_id}/clarification", json={"revision": 1, "answer": text}
    )
    assert response.status_code == 202


class RepeatingPlanner(Agents):
    def plan(self, state, files):
        plan = demo_plan("brownfield", "")
        plan.questions = ["Which users may manage links?"]
        return plan


def test_repeated_clarification_is_bounded_and_does_not_bypass_blockers(config, store, api):
    config.max_clarification_rounds = 1
    run_id = create_run(api, mode="live")
    graph = build_graph(config, store, InMemorySaver(), agents=RepeatingPlanner(config))
    work_once(config, store, graph)
    assert store.get(run_id).status == "awaiting_clarification"
    answer(api, run_id, "Keep the current administrator-only policy.")
    work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "stopped"
    assert run.state["error"]["type"] == "ClarificationLimitExceeded"
    assert run.state["model_calls"] == 2
    assert run.state["plan"]["questions"] == ["Which users may manage links?"]
    assert "Clarification round limit" in run.state["error"]["message"]
    assert not any(entry.kind == "task.completed" for entry in store.events(run_id))
    assert any(entry.kind == "clarification.limit_reached" for entry in store.events(run_id))
    assert store.audit_valid(run_id)


def test_new_material_blocker_can_still_pause_after_first_answer(config, store, api):
    class FollowupPlanner(Agents):
        def plan(self, state, files):
            plan = demo_plan("brownfield", "")
            if not state.get("clarification_history"):
                plan.questions = ["Who may manage links?"]
            else:
                plan.questions = ["May administrators view other users' private analytics?"]
            return plan

    run_id = create_run(api)
    graph = build_graph(config, store, InMemorySaver(), agents=FollowupPlanner(config))
    work_once(config, store, graph)
    answer(api, run_id, "Only administrators may manage links.")
    work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "awaiting_clarification"
    assert run.state["clarification_rounds"] == 1
    assert run.state["interruption"]["questions"] == [
        "May administrators view other users' private analytics?"
    ]
    assert not any(entry.kind == "task.completed" for entry in store.events(run_id))


def test_clarified_run_resumes_after_restart_with_history_and_no_duplicate_text(config, store, api):
    seen = []

    class ContextAwarePlanner(Agents):
        def plan(self, state, files):
            seen.append(state)
            plan = demo_plan("brownfield", "")
            rounds = len(state.get("clarification_history", []))
            plan.questions = [["Should dates use UTC?"], ["Confirm analytics privacy?"], []][
                min(rounds, 2)
            ]
            return plan

    run_id = create_run(api)
    text = "Use UTC and preserve administrator-only analytics."
    with checkpointer(config) as saver:
        work_once(
            config, store, build_graph(config, store, saver, agents=ContextAwarePlanner(config))
        )
    answer(api, run_id, text)
    with checkpointer(config) as saver:
        work_once(
            config, store, build_graph(config, store, saver, agents=ContextAwarePlanner(config))
        )
    assert store.get(run_id).status == "awaiting_clarification"
    answer(api, run_id, text)
    with checkpointer(config) as saver:
        work_once(
            config, store, build_graph(config, store, saver, agents=ContextAwarePlanner(config))
        )
    run = store.get(run_id)
    assert run.status == "awaiting_approval", run.state
    assert run.state["clarification"] == text
    assert run.state["clarification_rounds"] == 2
    assert len(run.state["clarification_history"]) == 2
    assert seen[-1]["plan"]["questions"] == ["Confirm analytics privacy?"]
    assert seen[-1]["clarification_history"][0]["questions"] == ["Should dates use UTC?"]
    assert seen[-1]["clarification_history"][1]["questions"] == ["Confirm analytics privacy?"]
    assert not (config.data_dir / "artifacts" / run_id / "1" / "bundle").exists()
    assert store.audit_valid(run_id)
