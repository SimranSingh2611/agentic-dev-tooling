import hmac
import os
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from engineering.workspace import PolicyError
from runner.executor import validate


class ValidationRequest(BaseModel):
    run_id: uuid.UUID
    revision: int = Field(ge=1)
    candidate_hash: str = Field(pattern="^[a-f0-9]{64}$")
    kind: Literal["tests", "security"]
    scenario: Literal["greenfield", "brownfield", "ambiguous"]


app = FastAPI(title="Candidate validation runner", docs_url=None, redoc_url=None)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/validate")
def check(body: ValidationRequest, x_runner_token: Annotated[str | None, Header()] = None):
    token = os.getenv("RUNNER_TOKEN", "local-runner-key")
    if not x_runner_token or not hmac.compare_digest(token, x_runner_token):
        raise HTTPException(401, "Invalid runner token")
    root = (
        Path(os.getenv("WORKSPACE_DIR", "workspaces")).resolve()
        / str(body.run_id)
        / str(body.revision)
    )
    if not root.is_dir() or root.is_symlink() or root.parent.is_symlink():
        raise HTTPException(404, "Candidate not found")
    try:
        return validate(
            root, body.kind, body.scenario, body.candidate_hash, Path(__file__).resolve().parents[1]
        )
    except (PolicyError, SyntaxError) as exc:
        raise HTTPException(409, str(exc)) from exc
