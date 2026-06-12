"""
local_state.py — filesystem layout, hashing, manifest, and config I/O.

Directory structure
-------------------
~/.study/
    config.json          — active workspace config (token, server URL, …)
    manifest.json        — local state: {file_path: {version, checksum}}
    workspaces/
        <workspace_name>/
            …            — local copies of synced files (mirror of remote)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Root paths
# ---------------------------------------------------------------------------

STUDY_DIR: Path = Path.home() / ".study"
CONFIG_PATH: Path = STUDY_DIR / "config.json"
MANIFEST_PATH: Path = STUDY_DIR / "manifest.json"
WORKSPACES_DIR: Path = STUDY_DIR / "workspaces"


def ensure_dirs() -> None:
    """Create the ~/.study/ directory tree if it does not already exist."""
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

HASH_CHUNK_SIZE = 65_536  # 64 KiB


def sha256_file(path: Path) -> str:
    """
    Return the lowercase SHA-256 hex digest of the file at *path*.

    Reads in chunks to handle arbitrarily large files without loading
    everything into memory.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Config  (~/.study/config.json)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Return the config dict, or {} if it has not been created yet."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_config(config: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def get_config_value(key: str) -> Optional[str]:
    return load_config().get(key)


# ---------------------------------------------------------------------------
# Manifest  (~/.study/manifest.json)
# ---------------------------------------------------------------------------
#
# Schema:
# {
#   "src/main.py": {"version": 3, "checksum": "abcdef..."},
#   "docs/README.md": {"version": 1, "checksum": "123456..."}
# }

def load_manifest() -> dict:
    """Return the manifest dict, or {} if not yet initialised."""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_manifest(manifest: dict) -> None:
    ensure_dirs()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def get_manifest_entry(file_path: str) -> Optional[dict]:
    """Return the manifest entry for *file_path*, or None if untracked."""
    return load_manifest().get(file_path)


def update_manifest_entry(file_path: str, version: int, checksum: str) -> None:
    """
    Upsert a single manifest entry after a successful push or pull.
    Thread-safety note: for a single-user CLI this is fine; no locking needed.
    """
    manifest = load_manifest()
    manifest[file_path] = {"version": version, "checksum": checksum}
    save_manifest(manifest)


def remove_manifest_entry(file_path: str) -> None:
    """Remove a file from the manifest (e.g. after a local delete)."""
    manifest = load_manifest()
    manifest.pop(file_path, None)
    save_manifest(manifest)


# ---------------------------------------------------------------------------
# Workspace file paths
# ---------------------------------------------------------------------------

def workspace_root(workspace_name: str) -> Path:
    """Return (and create) the local directory for the given workspace."""
    p = WORKSPACES_DIR / workspace_name
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_file_path(workspace_name: str, file_path: str) -> Path:
    """
    Translate a workspace-relative *file_path* (e.g. "src/main.py") to its
    absolute path on disk (e.g. ~/.study/workspaces/my-ws/src/main.py).
    """
    return workspace_root(workspace_name) / file_path


def to_relative_path(workspace_name: str, absolute_path: Path) -> str:
    """
    Convert an absolute path inside the workspace directory to a
    workspace-relative POSIX string (e.g. "src/main.py").
    """
    root = workspace_root(workspace_name)
    return absolute_path.relative_to(root).as_posix()


def all_local_files(workspace_name: str) -> list[Path]:
    """Return absolute Paths for every file currently in the local workspace."""
    root = workspace_root(workspace_name)
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


def normalize_file_path(raw: str) -> str:
    """
    Normalise a user-supplied path into a workspace-relative POSIX string.

    Strips leading slashes, collapses '..' (no escapes above root), and
    converts backslashes to forward slashes.
    """
    p = Path(raw.replace("\\", "/"))
    parts: list[str] = []
    for seg in p.parts:
        if seg in ("", ".", "/"):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)
