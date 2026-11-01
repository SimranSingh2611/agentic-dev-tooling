import ast
import json
import sqlite3
from contextlib import contextmanager
from graphlib import TopologicalSorter
from typing import TypedDict

from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from engineering.agents import Agents
from engineering.clarification import ClarificationLimitExceeded, decision_text, load_history
from engineering.workspace import PolicyError, Workspace, tree_hash
from runner.executor import Validator


class RunState(TypedDict, total=False):
    run_id: str
    revision: int
    requirement: str
    scenario: str
    mode: str
    clarification: str
    clarification_history: list[dict]
    clarification_rounds: int
    plan: dict
    attempt: int
    model_calls: int
    candidate_hash: str
    tests_result: dict
    review_result: dict
    documentation: str
    failures: list[str]
    approval: dict
    outcome: str
    restored_hash: str
    release_path: str


class Cancelled(RuntimeError):
    pass


@contextmanager
def checkpointer(config):
    url = config.checkpoint_database_url
    if url.startswith("sqlite:///"):
        from pathlib import Path

        from langgraph.checkpoint.sqlite import SqliteSaver

        path = Path(url.removeprefix("sqlite:///"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path), check_same_thread=False, timeout=20) as connection:
            yield SqliteSaver(connection)
    else:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(
            url.replace("postgresql+psycopg://", "postgresql://")
        ) as saver:
            saver.setup()
            yield saver


def build_graph(config, store, saver, agents=None, validator=None):
    agents = agents or Agents(config)
    validator = validator or Validator(config)

    def workspace(state):
        return Workspace(config, state["run_id"], state["revision"])

    def gate(state, node):
        run = store.get(state["run_id"])
        if not run or run.status == "cancelled" or run.revision != state["revision"]:
            raise Cancelled("Run cancelled or requirement superseded")
        owner = get_config()["configurable"].get("worker_owner")
        if owner and run.owner != owner:
            raise Cancelled("Worker lease was superseded")
        store.event(
            state["run_id"], "stage.entered", {"stage": node, "revision": state["revision"]}
        )

    def budget(state, purpose):
        if state["mode"] != "live":
            return state.get("model_calls", 0)
        return store.reserve_model_call(
            state["run_id"],
            state["revision"],
            config.max_model_calls,
            purpose,
            state.get("model_calls", 0),
        )

    def prepare(state):
        gate(state, "prepare")
        workspace(state).prepare(state["scenario"])
        return {
            "attempt": 0,
            "model_calls": 0,
            "failures": [],
            "clarification_history": [],
            "clarification_rounds": 0,
        }

    def plan(state):
        gate(state, "plan")
        calls = budget(state, "planning")
        history = load_history(state, store)
        clarification = decision_text(history, state.get("clarification", ""))
        planning_state = {
            **state,
            "clarification": clarification,
            "clarification_history": history,
            "clarification_rounds": len(history),
        }
        result = agents.plan(planning_state, workspace(state).snapshot())
        store.event(state["run_id"], "plan.created", result.model_dump())
        update = {
            "plan": result.model_dump(),
            "model_calls": calls,
            "clarification": clarification,
            "clarification_history": history,
            "clarification_rounds": len(history),
        }
        if result.questions and len(history) >= config.max_clarification_rounds:
            store.event(
                state["run_id"],
                "clarification.limit_reached",
                {
                    "rounds": len(history),
                    "limit": config.max_clarification_rounds,
                    "unresolved_questions": result.questions,
                },
            )
            # Return the final plan/evidence to the checkpoint, then stop in a
            # separate node so unresolved blockers and the consumed call survive.
            return {**update, "outcome": "clarification_limit"}
        return update

    def clarify(state):
        answer = interrupt(
            {
                "kind": "clarification",
                "questions": state["plan"]["questions"],
                "revision": state["revision"],
            }
        )
        gate(state, "clarify")
        if not isinstance(answer, str) or not answer.strip():
            raise PolicyError("Clarification must contain an answer")
        answer = answer.strip()
        history = load_history(state, store)
        entry = {"questions": list(state["plan"]["questions"]), "answer": answer}
        history.append(entry)
        store.event(state["run_id"], "requirement.clarified", {**entry, "round": len(history)})
        return {
            "clarification": decision_text(history, state.get("clarification", "")),
            "clarification_history": history,
            "clarification_rounds": len(history),
        }

    def clarification_limit(state):
        gate(state, "clarification_limit")
        raise ClarificationLimitExceeded(
            "Unresolved blockers remain after the clarification round limit"
        )

    def implement(state):
        gate(state, "implement")
        work = workspace(state)
        tasks = {task["id"]: task for task in state["plan"]["tasks"]}
        order = list(
            TopologicalSorter(
                {key: task["depends_on"] for key, task in tasks.items()}
            ).static_order()
        )
        pending = [
            tasks[task_id] for task_id in order if tasks[task_id]["stage"] == "implementation"
        ]
        if state["mode"] == "live" and not state.get("attempt", 0):
            # Stage labels are model output. Reconcile every requested deliverable
            # before validation, including edits mislabeled design or validation.
            pending.append(
                {
                    "id": "workflow:complete-deliverables",
                    "stage": "implementation",
                    "description": "Complete the whole requested change before validation. Inspect every planned task, regardless of its stage label, against the current source. Implement any missing model/source changes, write meaningful executable regression tests under tests/test_*.py, and replace the placeholder README.md with feature usage, error behavior, and any required existing-database migration instructions. Do not execute migrations. Preserve already-correct changes. Return complete contents only for changed files; files=[] is valid only when all these deliverables already exist.",
                    "depends_on": [task["id"] for task in pending],
                }
            )
        if state.get("attempt", 0):
            pending = [
                {
                    "id": f"repair-attempt-{state['attempt'] + 1}",
                    "stage": "implementation",
                    "description": "Repair the current candidate using all reported validation failures. Fix failing source or test fixtures while preserving already-working behavior and meaningful coverage. Change only files needed for the failures; do not replay the original tasks.",
                    "depends_on": [task["id"] for task in pending],
                }
            ]
            store.event(
                state["run_id"],
                "repair.targeted",
                {
                    "attempt": state["attempt"] + 1,
                    "strategy": "One candidate-wide repair driven by recorded validation failures",
                },
            )
        calls = state.get("model_calls", 0)
        for task in pending:
            task_id = task["id"]
            if any(
                tasks[dep]["stage"] not in {"design", "implementation"}
                for dep in task["depends_on"]
            ):
                raise PolicyError(
                    "Implementation cannot depend on a downstream validation/documentation task"
                )
            calls = budget({**state, "model_calls": calls}, "implementation:" + task_id)
            patch = agents.implement(state, work.snapshot(), task)
            gate(state, "apply_patch")
            if patch.files:
                digest = work.apply(patch)
            else:
                # An already-satisfied task may legitimately require no edit.
                # Independent validation still decides whether the feature works.
                digest = tree_hash(work.root)
                store.event(
                    state["run_id"],
                    "task.no_change",
                    {"task": task_id, "rationale": patch.rationale, "candidate_hash": digest},
                )
            store.event(
                state["run_id"],
                "task.completed",
                {
                    "task": task_id,
                    "candidate_hash": digest,
                    "rationale": patch.rationale,
                    "files": [change.path for change in patch.files],
                },
            )
        return {
            "attempt": state.get("attempt", 0) + 1,
            "model_calls": calls,
            "candidate_hash": tree_hash(work.root),
            "approval": {},
        }

    def tests(state):
        gate(state, "tests")
        result = validator.check(state, "tests")
        if state["mode"] == "live":
            work = workspace(state)
            files = work.snapshot()
            has_tests = any(
                name.startswith("tests/test_")
                and any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test_")
                    for node in ast.walk(ast.parse(content))
                )
                for name, content in files.items()
            )
            readme = files.get("README.md", "").strip()
            baseline_readme = (work.baseline / "README.md").read_text().strip()
            missing = []
            if not has_tests:
                missing.append("candidate regression tests under tests/test_*.py")
            if not readme or readme == baseline_readme:
                missing.append(
                    "candidate README.md with feature usage and required migration instructions"
                )
            if missing:
                result = {
                    **result,
                    "passed": False,
                    "missing_deliverables": missing,
                    "output": result.get("output", "")
                    + "\nMissing requested deliverables: "
                    + "; ".join(missing),
                }
        store.event(state["run_id"], "validation.tests", result)
        return {"tests_result": result}

    def review(state):
        gate(state, "review")
        static = validator.check(state, "security")
        calls = budget(state, "review")
        review_result = agents.review(state, workspace(state).snapshot(), static)
        result = {
            "passed": static["passed"] and review_result.passed,
            "static": static,
            "findings": review_result.findings,
            "candidate_hash": state["candidate_hash"],
        }
        store.event(state["run_id"], "validation.review", result)
        return {"review_result": result, "model_calls": calls}

    def document(state):
        gate(state, "document")
        document = "\n".join(
            [
                "# Engineering change",
                "",
                state["requirement"],
                "",
                "## Architecture",
                state["plan"]["architecture"],
                "## Assumptions",
                *[f"- {value}" for value in state["plan"]["assumptions"]],
                "## Risks",
                *[f"- {value}" for value in state["plan"]["risks"]],
                "## Release boundary",
                "Approval prepares a source bundle. Deployments and existing-database migrations require a separate reviewed change.",
                "## Limitations",
                "Demo mode uses fixed recipes. Local validation is not a security sandbox. Static policy checks do not prove security or compliance.",
            ]
        )
        return {"documentation": document}

    def evaluate(state):
        gate(state, "evaluate")
        digest = tree_hash(workspace(state).root)
        failures = []
        for name in ["tests_result", "review_result"]:
            result = state[name]
            if result.get("candidate_hash") != digest or digest != state["candidate_hash"]:
                raise PolicyError("Validation evidence is stale")
            if not result["passed"]:
                failures.append(json.dumps({"stage": name, "result": result}))
        workspace(state).save_artifacts({**state, "failures": failures})
        store.event(
            state["run_id"],
            "validation.gate",
            {"passed": not failures, "attempt": state["attempt"], "candidate_hash": digest},
        )
        return {"failures": failures}

    def repair(state):
        gate(state, "repair")
        store.event(
            state["run_id"],
            "repair.scheduled",
            {"attempt": state["attempt"] + 1, "maximum_attempts": config.max_repairs + 1},
        )
        return {}

    def approval(state):
        decision = interrupt(
            {
                "kind": "approval",
                "revision": state["revision"],
                "candidate_hash": state["candidate_hash"],
                "tests": state["tests_result"],
                "review": state["review_result"],
                "action": "Prepare an immutable source bundle for human-controlled release",
            }
        )
        gate(state, "approval")
        if (
            not isinstance(decision, dict)
            or decision.get("candidate_hash") != tree_hash(workspace(state).root)
            or decision.get("revision") != state["revision"]
        ):
            raise PolicyError("Approval does not match the current revision and candidate")
        store.event(state["run_id"], "approval.recorded", decision)
        return {"approval": decision}

    def release(state):
        gate(state, "release")
        work = workspace(state)
        if not state["approval"].get("approved") or tree_hash(work.root) != state["candidate_hash"]:
            raise PolicyError("Release requires approval of the exact validated candidate")
        bundle = work.artifacts / "bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        for name, content in work.snapshot().items():
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if tree_hash(bundle) != state["candidate_hash"]:
            raise PolicyError("Release bundle integrity check failed")
        (work.artifacts / "SUMMARY.md").write_text(
            state["documentation"]
            + f"\n\nCandidate: `{state['candidate_hash']}`\nValidation: passed\nReviewer: {state['approval']['reviewer']}\nMode: {state['mode']}\n"
        )
        store.event(
            state["run_id"],
            "release.prepared",
            {"candidate_hash": state["candidate_hash"], "bundle": str(bundle)},
        )
        return {"outcome": "completed", "release_path": str(bundle)}

    def rollback(state):
        gate(state, "rollback")
        work = workspace(state)
        work.save_artifacts(state)
        restored = work.rollback()
        store.event(
            state["run_id"],
            "workspace.rolled_back",
            {
                "restored_hash": restored,
                "failed_candidate_hash": state.get("candidate_hash"),
                "reason": "rejected" if state.get("approval") else "validation budget exhausted",
            },
        )
        return {
            "outcome": "rejected" if state.get("approval") else "stopped",
            "restored_hash": restored,
        }

    def report(state):
        gate(state, "report")
        workspace(state).save_artifacts(state)
        return {}

    graph = StateGraph(RunState)
    for name, node in {
        "prepare": prepare,
        "plan": plan,
        "clarify": clarify,
        "clarification_limit": clarification_limit,
        "implement": implement,
        "tests": tests,
        "review": review,
        "document": document,
        "evaluate": evaluate,
        "repair": repair,
        "approval": approval,
        "release": release,
        "rollback": rollback,
        "report": report,
    }.items():
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "plan")
    graph.add_conditional_edges(
        "plan",
        lambda state: (
            "clarification_limit"
            if state.get("outcome") == "clarification_limit"
            else ("clarify" if state["plan"]["questions"] else "implement")
        ),
        ["clarify", "implement", "clarification_limit"],
    )
    graph.add_edge("clarify", "plan")
    for node in ["tests", "review", "document"]:
        graph.add_edge("implement", node)
    graph.add_edge(["tests", "review", "document"], "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        lambda state: (
            "approval"
            if not state["failures"]
            else ("repair" if state["attempt"] <= config.max_repairs else "rollback")
        ),
        ["approval", "repair", "rollback"],
    )
    graph.add_edge("repair", "implement")
    graph.add_conditional_edges(
        "approval",
        lambda state: "release" if state["approval"].get("approved") else "rollback",
        ["release", "rollback"],
    )
    graph.add_edge("release", "report")
    graph.add_edge("rollback", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=saver)
