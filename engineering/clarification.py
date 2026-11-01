"""Carry human decisions across planning rounds, including older checkpoints."""


class ClarificationLimitExceeded(ValueError):
    """Planning still has blockers after the allowed human clarification rounds."""


def normalize_answer(answer: str) -> str:
    return answer.strip()


def decision_text(history: list[dict], legacy_text: str = "") -> str:
    """Deduplicate the prompt text without discarding the per-round audit history."""
    answers = []
    for answer in [legacy_text, *(entry["answer"] for entry in history)]:
        for paragraph in answer.splitlines():
            paragraph = normalize_answer(paragraph)
            if paragraph and paragraph not in answers:
                answers.append(paragraph)
    return "\n".join(answers)


def load_history(state: dict, store) -> list[dict]:
    if state.get("clarification_history"):
        return list(state["clarification_history"])
    if not state.get("clarification"):
        return []

    # Older graph checkpoints stored only a concatenated string. Recover the
    # questions paired with each accepted answer from the existing event journal.
    history, questions = [], []
    for event in store.events(state["run_id"]):
        if event.revision != state["revision"]:
            continue
        if event.kind == "plan.created":
            questions = event.payload.get("questions", [])
        elif event.kind == "requirement.clarified":
            history.append(
                {
                    "questions": event.payload.get("questions", questions),
                    "answer": event.payload["answer"],
                }
            )
    if not history:
        history = [
            {
                "questions": state.get("plan", {}).get("questions", []),
                "answer": state["clarification"],
            }
        ]
    return history
