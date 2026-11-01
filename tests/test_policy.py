from graphlib import CycleError

import pytest

from engineering.agents import demo_plan
from engineering.schemas import FileChange, Patch, Plan
from engineering.workspace import PolicyError, Workspace, allowed, tree_hash
from runner.executor import static_checks


@pytest.mark.parametrize(
    "path",
    [
        "../.env",
        "/etc/passwd",
        "engineering/api.py",
        "acceptance/test_contract.py",
        "tests/conftest.py",
        "shortener/../../runner/api.py",
        "shortener\\main.py",
        "pyproject.toml",
    ],
)
def test_agent_cannot_edit_control_plane_or_escape_workspace(path):
    assert not allowed(path)


def test_patch_is_validated_before_any_writes(config):
    work = Workspace(config, "test", 1)
    work.prepare("brownfield")
    before = tree_hash(work.root)
    patch = Patch(
        rationale="bad batch",
        files=[
            FileChange(path="shortener/main.py", content="x = 1\n"),
            FileChange(path="../escaped.py", content="x = 2\n"),
        ],
    )
    with pytest.raises(PolicyError):
        work.apply(patch)
    assert tree_hash(work.root) == before


def test_symlinks_and_credentials_are_rejected(config):
    work = Workspace(config, "test", 1)
    work.prepare("brownfield")
    (work.root / "tests/test_link.py").symlink_to(config.source_root / "shortener/main.py")
    with pytest.raises(PolicyError):
        tree_hash(work.root)
    (work.root / "tests/test_link.py").unlink()
    with pytest.raises(PolicyError):
        work.apply(
            Patch(
                rationale="secret", files=[FileChange(path="README.md", content="sk-" + "a" * 30)]
            )
        )


def test_plan_rejects_cycles_and_unknown_dependencies():
    plan = demo_plan("greenfield", "").model_dump()
    plan["tasks"][0]["depends_on"] = ["implement"]
    with pytest.raises((ValueError, CycleError)):
        Plan.model_validate(plan)
    plan["tasks"][0]["depends_on"] = ["nonexistent"]
    with pytest.raises(ValueError):
        Plan.model_validate(plan)


def test_static_policy_detects_dynamic_execution(tmp_path):
    (tmp_path / "bad.py").write_text("import subprocess\neval('1+1')\n")
    result = static_checks(tmp_path)
    assert not result["passed"]
    assert len(result["findings"]) == 2


def test_rollback_removes_unapproved_files_without_reading_symlink_targets(config, tmp_path):
    work = Workspace(config, "policy-recovery", 1)
    work.prepare("brownfield")
    outside = tmp_path / "private-file"
    outside.write_text("not candidate source")
    (work.root / "tests/conftest.py").symlink_to(outside)
    work.save_artifacts({"run_id": "policy-recovery"})
    assert "unsafe files were not read" in (work.artifacts / "change.diff").read_text()
    assert work.rollback() == tree_hash(work.baseline)
    assert not (work.root / "tests/conftest.py").exists()
    assert outside.read_text() == "not candidate source"
