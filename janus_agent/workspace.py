"""A temporary copy of the project where fixes are applied and verified.

The real project is only written at the end, after confirmation, and only if the files
the agent changed are still identical to what was copied.
"""

import hashlib
import shutil
import tempfile
from pathlib import Path

from janus_agent.scanners.scan_config import SKIP_DIRS
from janus_agent.security import paths

MAX_FILE_BYTES = 5_000_000


class StaleProject(Exception):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Workspace:
    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        self._tmp = tempfile.TemporaryDirectory(prefix="janus-agent-")
        self.path = Path(self._tmp.name) / self.project.name
        self.hashes = {}
        self._touched = {}
        self._copy()

    def _copy(self) -> None:
        self.path.mkdir()
        for src in self.project.rglob("*"):
            rel = src.relative_to(self.project)
            if any(part in SKIP_DIRS for part in rel.parts) or src.is_symlink():
                continue
            if src.is_dir():
                continue
            if paths.is_secret_file(src.name) or src.stat().st_size > MAX_FILE_BYTES:
                continue  # .env files and keys never enter the workspace
            dst = self.path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            self.hashes[rel.as_posix()] = _digest(src)

    def write(self, changes: dict) -> None:
        """Apply in-memory changes (rel -> (before, after)) to the workspace copy."""
        for rel, (before, after) in changes.items():
            self._touched.setdefault(rel, self._original_text(rel))
            target = paths.resolve(self.path, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8")

    def _original_text(self, rel: str):
        if rel not in self.hashes:
            return None
        return (self.project / rel).read_text(encoding="utf-8")

    def changed_files(self) -> dict:
        """Files the agent changed: rel -> (original text or None, new text).

        Only files written through write() count, so files created by the test run
        (caches, databases) never end up in the diff.
        """
        changes = {}
        for rel, before in self._touched.items():
            after = paths.resolve(self.path, rel).read_text(encoding="utf-8")
            if after != before:
                changes[rel] = (before, after)
        return changes

    def write_back(self, changes: dict) -> None:
        """Write the changes to the real project, refusing if it changed meanwhile."""
        for rel in changes:
            original = self.project / rel
            if rel in self.hashes:
                if not original.is_file() or _digest(original) != self.hashes[rel]:
                    raise StaleProject(f"{rel} changed during the run; nothing was written")
            elif original.exists():
                raise StaleProject(f"{rel} was created during the run; nothing was written")
        for rel, (_, after) in changes.items():
            target = paths.resolve(self.project, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8")

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
