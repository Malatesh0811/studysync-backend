"""local_state.py -- filesystem layout, hashing, manifest, config and outbox I/O."""
from __future__ import annotations
import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STUDY_DIR: Path = Path.home() / ".study"
CONFIG_PATH: Path = STUDY_DIR / "config.json"
MANIFEST_PATH: Path = STUDY_DIR / "manifest.json"
OUTBOX_PATH: Path = STUDY_DIR / "outbox.json"
WORKSPACES_DIR: Path = STUDY_DIR / "workspaces"


def ensure_dirs() -> None:
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_config(config: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# Manifest
# ------------------------------------------------------------------

def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_manifest(manifest: dict) -> None:
    ensure_dirs()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def update_manifest_entry(
    file_path: str,
    version: int,
    checksum: str,
    pending: bool = False,
) -> None:
    manifest = load_manifest()
    entry: dict = {"version": version, "checksum": checksum}
    if pending:
        entry["pending"] = True
    manifest[file_path] = entry
    save_manifest(manifest)


# ------------------------------------------------------------------
# Outbox  (queued offline pushes)
# ------------------------------------------------------------------

def load_outbox() -> list:
    if not OUTBOX_PATH.exists():
        return []
    try:
        return json.loads(OUTBOX_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_outbox(outbox: list) -> None:
    ensure_dirs()
    OUTBOX_PATH.write_text(json.dumps(outbox, indent=2), encoding="utf-8")


def add_to_outbox(
    file_path: str,
    checksum: str,
    base_version: int,
    vault_path: str,
    size_bytes: int,
) -> None:
    """Add or replace an outbox entry for file_path."""
    outbox = load_outbox()
    outbox = [e for e in outbox if e["file_path"] != file_path]
    outbox.append({
        "file_path": file_path,
        "checksum": checksum,
        "base_version": base_version,
        "vault_path": vault_path,
        "size_bytes": size_bytes,
        "staged_at": datetime.now(timezone.utc).isoformat(),
    })
    save_outbox(outbox)


def remove_from_outbox(file_path: str) -> None:
    outbox = load_outbox()
    save_outbox([e for e in outbox if e["file_path"] != file_path])


# ------------------------------------------------------------------
# Workspace helpers
# ------------------------------------------------------------------

def workspace_root(workspace_name: str) -> Path:
    p = WORKSPACES_DIR / workspace_name
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_file_path(workspace_name: str, file_path: str) -> Path:
    return workspace_root(workspace_name) / file_path


def all_local_files(workspace_name: str) -> list:
    root = workspace_root(workspace_name)
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]
