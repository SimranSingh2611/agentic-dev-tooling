import difflib
import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath

from engineering.schemas import Patch, PolicyError


def allowed(path: str) -> bool:
    value = PurePosixPath(path)
    return (
        str(value) == path
        and bool(
            re.fullmatch(
                r"(?:shortener/[A-Za-z_][A-Za-z0-9_]*\.py|tests/test_[A-Za-z0-9_]+\.py|README\.md)",
                path,
            )
        )
        and not value.is_absolute()
        and ".." not in value.parts
        and "\\" not in path
        and (
            (len(value.parts) == 2 and value.parts[0] == "shortener" and value.suffix == ".py")
            or (
                len(value.parts) == 2
                and value.parts[0] == "tests"
                and value.name.startswith("test_")
                and value.suffix == ".py"
            )
            or path == "README.md"
        )
    )


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PolicyError("Workspace symlinks are forbidden")
        if path.is_file():
            if not allowed(path.relative_to(root).as_posix()):
                raise PolicyError("Workspace contains an unapproved file")
            name = path.relative_to(root).as_posix().encode()
            content = path.read_bytes()
            digest.update(
                len(name).to_bytes(8, "big") + name + len(content).to_bytes(8, "big") + content
            )
    return digest.hexdigest()


class Workspace:
    def __init__(self, config, run_id, revision):
        self.config = config
        self.root = config.workspace_dir.resolve() / run_id / str(revision)
        self.baseline = self.root.parent / f"{revision}-baseline"
        self.artifacts = config.data_dir.resolve() / "artifacts" / run_id / str(revision)

    def prepare(self, scenario):
        if self.baseline.exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            self.config.source_root / "shortener",
            self.root / "shortener",
            ignore=shutil.ignore_patterns("__pycache__"),
            dirs_exist_ok=True,
        )
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "README.md").write_text(
            "# Candidate URL shortener\n\nGenerated changes are pending validation and review.\n"
        )
        if scenario == "greenfield":
            (self.root / "shortener" / "main.py").write_text(
                'from fastapi import FastAPI\n\ndef create_app(database_url=None, api_key=None, rate_limit=60):\n    """Implement the service contract supplied in the requirement."""\n    return FastAPI(title="URL Shortener Skeleton")\n'
            )
        shutil.copytree(self.root, self.baseline)

    def snapshot(self):
        tree_hash(self.root)
        return {
            p.relative_to(self.root).as_posix(): p.read_text()
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and allowed(p.relative_to(self.root).as_posix())
        }

    def apply(self, patch: Patch):
        if not patch.files or len(patch.files) > 15:
            raise PolicyError("A patch must contain 1-15 files")
        paths = [change.path for change in patch.files]
        if len(set(paths)) != len(paths):
            raise PolicyError("Duplicate file changes are forbidden")
        for change in patch.files:
            if not allowed(change.path):
                raise PolicyError(f"File is outside the permitted change scope: {change.path}")
            target = self.root / change.path
            if target.is_symlink() or target.parent.is_symlink():
                raise PolicyError("Symlinks are forbidden")
            if len(change.content.encode()) > 100_000:
                raise PolicyError("File exceeds the 100 KB change limit")
            if re.search(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}", change.content):
                raise PolicyError("Possible API credential in generated output")
            if target.suffix == ".py":
                compile(change.content, change.path, "exec")
        # Validate the entire batch before writing any file.
        for change in patch.files:
            target = self.root / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content)
        return tree_hash(self.root)

    def diff(self):
        result = []
        current = self.snapshot()
        paths = set(current) | {
            p.relative_to(self.baseline).as_posix() for p in self.baseline.rglob("*") if p.is_file()
        }
        for name in sorted(paths):
            before = (self.baseline / name).read_text() if (self.baseline / name).exists() else ""
            after = current.get(name, "")
            result.extend(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    fromfile=f"a/{name}",
                    tofile=f"b/{name}",
                )
            )
        return "".join(result)

    def save_artifacts(self, state):
        self.artifacts.mkdir(parents=True, exist_ok=True)
        if not state.get("restored_hash"):
            try:
                diff = self.diff()
            except (PolicyError, UnicodeError):
                diff = "Candidate failed workspace integrity policy; unsafe files were not read.\n"
            (self.artifacts / "change.diff").write_text(diff)
        (self.artifacts / "run.json").write_text(json.dumps(state, indent=2, default=str))
        summary = state.get(
            "documentation", "# Engineering change\n\n" + state.get("requirement", "")
        )
        summary += "\n\n## Execution evidence\n"
        for key in ["mode", "revision", "candidate_hash", "attempt", "outcome", "restored_hash"]:
            summary += f"- {key}: {state.get(key, 'pending')}\n"
        for key in ["tests_result", "review_result"]:
            summary += f"- {key}: {state.get(key, {}).get('passed', 'pending')}\n"
        summary += f"- Approval: {json.dumps(state.get('approval', {}))}\n"
        if state.get("error"):
            summary += f"\nExecution stopped: {state['error']['message']}\n"
        (self.artifacts / "SUMMARY.md").write_text(summary)

    def rollback(self):
        failed = self.root.parent / f"{self.root.name}-failed"
        if self.root.is_symlink():
            self.root.unlink()
        elif self.root.exists():
            shutil.copytree(self.root, failed, symlinks=True, dirs_exist_ok=True)
            shutil.rmtree(self.root)
        # Operate only inside the run's generated workspace; source code is never reset.
        shutil.copytree(self.baseline, self.root)
        return tree_hash(self.root)
