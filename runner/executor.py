import ast
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from engineering.workspace import PolicyError, tree_hash


def run_command(command, root, timeout=90, extra_env=None):
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "CANDIDATE_ROOT": str(root),
        "SCENARIO": "greenfield",
    }
    environment.update(extra_env or {})
    started = time.monotonic()
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = 124
        finally:
            # Kill descendants even when their parent exited normally.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        output.seek(0)
        text = output.read(24000).decode(errors="replace")
    return {
        "passed": code == 0,
        "exit_code": code,
        "output": text,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def static_checks(root):
    findings = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any(
                    module.split(".")[0] in {"subprocess", "socket", "ctypes", "pickle", "pty"}
                    for module in modules
                ):
                    findings.append(
                        f"{path.relative_to(root)}:{node.lineno}: prohibited process/network/unsafe-deserialization module"
                    )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "__import__"}
            ):
                findings.append(
                    f"{path.relative_to(root)}:{node.lineno}: prohibited dynamic execution"
                )
    return {
        "passed": not findings,
        "findings": findings,
        "output": "\n".join(findings) or "Static execution policy checks passed",
    }


def validate(root: Path, kind: str, scenario: str, expected_hash: str, trusted_root: Path):
    if tree_hash(root) != expected_hash:
        raise PolicyError("Candidate changed after validation was scheduled")
    if kind == "security":
        result = static_checks(root)
    elif kind == "tests":
        policy = static_checks(root)
        if not policy["passed"]:
            return {**policy, "candidate_hash": expected_hash, "kind": kind}
        result = run_command(
            [
                sys.executable,
                "-B",
                "-I",
                "-m",
                "pytest",
                "-p",
                "runner.bootstrap",
                "-c",
                str(trusted_root / "runner/pytest.ini"),
                str(trusted_root / "acceptance"),
                str(root / "tests"),
            ],
            root,
            extra_env={"SCENARIO": scenario},
        )
    else:
        raise PolicyError("Unknown validation kind")
    if tree_hash(root) != expected_hash:
        raise PolicyError("Candidate mutated while validation was running")
    return {**result, "candidate_hash": expected_hash, "kind": kind}


class Validator:
    def __init__(self, config):
        self.config = config

    def check(self, state, kind):
        root = self.config.workspace_dir.resolve() / state["run_id"] / str(state["revision"])
        if self.config.runner_backend == "local":
            return validate(
                root, kind, state["scenario"], state["candidate_hash"], self.config.source_root
            )
        if self.config.runner_backend != "http":
            raise PolicyError("RUNNER_BACKEND must be http or explicitly opted-in local")
        with httpx.Client(timeout=120) as client:
            response = client.post(
                self.config.runner_url + "/validate",
                headers={"X-Runner-Token": self.config.runner_token},
                json={
                    "run_id": state["run_id"],
                    "revision": state["revision"],
                    "kind": kind,
                    "scenario": state["scenario"],
                    "candidate_hash": state["candidate_hash"],
                },
            )
            response.raise_for_status()
            return response.json()
