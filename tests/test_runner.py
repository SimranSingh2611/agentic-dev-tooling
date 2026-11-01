import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from engineering.workspace import PolicyError, Workspace, tree_hash
from runner.api import app
from runner.executor import run_command, validate


def test_subprocess_environment_does_not_receive_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "never-pass-this-to-candidate")
    monkeypatch.setenv("RUNNER_TOKEN", "never-pass-this-either")
    result = run_command(
        [
            sys.executable,
            "-I",
            "-c",
            "import os; print(os.getenv('OPENAI_API_KEY')); print(os.getenv('RUNNER_TOKEN'))",
        ],
        tmp_path,
    )
    assert result["passed"]
    assert result["output"].splitlines() == ["None", "None"]


def test_subprocess_timeout_is_bounded(tmp_path):
    result = run_command(
        [sys.executable, "-I", "-c", "import time; time.sleep(10)"], tmp_path, timeout=0.05
    )
    assert result["exit_code"] == 124
    assert result["duration_seconds"] < 3


def test_runner_rejects_stale_candidate(config):
    work = Workspace(config, str(uuid.uuid4()), 1)
    work.prepare("brownfield")
    digest = tree_hash(work.root)
    (work.root / "README.md").write_text("tampered")
    with pytest.raises(PolicyError, match="changed"):
        validate(work.root, "security", "brownfield", digest, config.source_root)


def test_unapproved_configuration_cannot_bypass_candidate_hash(config):
    work = Workspace(config, str(uuid.uuid4()), 1)
    work.prepare("brownfield")
    (work.root / "tests/conftest.py").write_text("raise RuntimeError('unapproved config')")
    with pytest.raises(PolicyError, match="unapproved"):
        tree_hash(work.root)


def test_static_policy_blocks_tests_before_execution(config, tmp_path):
    work = Workspace(config, str(uuid.uuid4()), 1)
    work.prepare("brownfield")
    marker = tmp_path / "should-not-be-created"
    (work.root / "tests/test_side_effect.py").write_text(
        f"import subprocess\nopen({str(marker)!r}, 'w').write('executed')\n"
    )
    result = validate(work.root, "tests", "brownfield", tree_hash(work.root), config.source_root)
    assert not result["passed"]
    assert not marker.exists()


def test_runner_http_requires_token_and_validates_candidate(config, monkeypatch):
    run_id = str(uuid.uuid4())
    work = Workspace(config, run_id, 1)
    work.prepare("brownfield")
    monkeypatch.setenv("WORKSPACE_DIR", str(config.workspace_dir))
    monkeypatch.setenv("RUNNER_TOKEN", "runner-test-key")
    body = {
        "run_id": run_id,
        "revision": 1,
        "kind": "security",
        "scenario": "brownfield",
        "candidate_hash": tree_hash(work.root),
    }
    with TestClient(app) as client:
        assert client.post("/validate", json=body).status_code == 401
        result = client.post("/validate", headers={"X-Runner-Token": "runner-test-key"}, json=body)
        assert result.status_code == 200
        assert result.json()["passed"]
        body["run_id"] = "../../escape"
        assert (
            client.post(
                "/validate", headers={"X-Runner-Token": "runner-test-key"}, json=body
            ).status_code
            == 422
        )
