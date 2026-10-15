"""Which files the agent may read and write."""

from pathlib import Path, PurePosixPath

KEY_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
KEY_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pgpass", "credentials.json"}
PRIVATE_DIRS = {".git", ".janus", ".ssh"}


class PathError(Exception):
    pass


def is_secret_file(name: str) -> bool:
    """.env and every .env.* variant except .env.example, private keys, credential files."""
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name.endswith(".local.toml") or name.endswith(".local.yaml") or name.startswith("config.local."):
        return True
    return name in KEY_NAMES or PurePosixPath(name).suffix in KEY_SUFFIXES


def check_rel(rel: str) -> str:
    """Normalize a path given by the model and refuse anything outside the rules."""
    if not rel or "\\" in rel or "\0" in rel:
        raise PathError(f"invalid path: {rel!r}")
    path = PurePosixPath(rel)
    if path.is_absolute() or ".." in path.parts:
        raise PathError(f"path must stay inside the project: {rel}")
    if any(part in PRIVATE_DIRS for part in path.parts):
        raise PathError(f"path is in a private directory: {rel}")
    if is_secret_file(path.name):
        raise PathError(f"secret files are never read or written: {rel}")
    return path.as_posix()


def resolve(root: Path, rel: str) -> Path:
    rel = check_rel(rel)
    root = Path(root).resolve()
    full = (root / rel).resolve()
    if root != full and root not in full.parents:
        raise PathError(f"path escapes the project (symlink?): {rel}")
    return full


def read_text(root: Path, rel: str) -> str:
    return resolve(root, rel).read_text(encoding="utf-8")


def is_env_file(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name == ".env.example" or (name.endswith(".env") and name != ".env")
