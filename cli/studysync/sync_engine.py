"""
sync_engine.py — Networking, OCC logic, and S3 streaming.

All HTTP calls to the StudySync server and all direct S3 streaming live here.
The Typer commands in main.py are thin wrappers that call into SyncEngine.

Key design decisions
--------------------
* Streaming uploads:  We open the local file as a binary file object and wrap
  it in a progress-tracking shim.  `requests` reads the wrapper's `.read()`
  method and forwards the Content-Length header we set explicitly, which is
  required for S3 presigned PUT URLs.
* Streaming downloads: `requests` streaming GET + chunk-writing gives us a
  constant memory footprint regardless of file size.
* No retries: This is v1.  Production systems should add exponential backoff
  with `tenacity` or `urllib3.Retry`.
"""

from __future__ import annotations

import os
import shutil
import sys

from .constants import PRODUCTION_SERVER_URL
from pathlib import Path
from typing import Optional

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from .local_state import (
    all_local_files,
    load_config,
    load_manifest,
    local_file_path,
    sha256_file,
    to_relative_path,
    update_manifest_entry,
    workspace_root,
    WORKSPACES_DIR,
)

console = Console(stderr=False)

UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024    # 8 MiB
DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
HTTP_TIMEOUT = 20                       # seconds for metadata calls
STREAM_TIMEOUT = 600                    # seconds for S3 streaming


# ---------------------------------------------------------------------------
# Progress-aware file wrapper for streaming uploads
# ---------------------------------------------------------------------------

class _ProgressReader:
    """
    File-like wrapper that reports bytes read to a Rich Progress task.

    requests calls `.read(size)` on whatever you pass as `data=`, so this
    wrapper intercepts those reads and advances the progress bar.  Exposing
    `__len__` lets requests set the Content-Length header automatically, which
    is mandatory for S3 presigned PUT URLs.
    """

    def __init__(
        self,
        fh,
        total: int,
        progress: Progress,
        task_id,
    ) -> None:
        self._fh = fh
        self._total = total
        self._progress = progress
        self._task_id = task_id

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._progress.update(self._task_id, advance=len(chunk))
        return chunk

    def __len__(self) -> int:
        return self._total


# ---------------------------------------------------------------------------
# SyncEngine
# ---------------------------------------------------------------------------

class SyncEngine:
    """
    Encapsulates all network operations for the StudySync CLI.

    Parameters override config.json values — useful for workspace create/join
    where config hasn't been saved yet.
    """

    def __init__(
        self,
        server_url: Optional[str] = None,
        workspace_token: Optional[str] = None,
        workspace_name: Optional[str] = None,
    ) -> None:
        config = load_config()
        raw_url = server_url or config.get("server_url", PRODUCTION_SERVER_URL)
        self.server_url = raw_url.rstrip("/")
        self.workspace_token = workspace_token or config.get("workspace_token")
        self.workspace_name = workspace_name or config.get("workspace_name")

    # ------------------------------------------------------------------
    # Guard
    # ------------------------------------------------------------------

    def _require_workspace(self) -> None:
        if not self.workspace_token or not self.workspace_name:
            console.print(
                "[red]No active workspace.  "
                "Run [bold]study workspace create <name>[/bold] or "
                "[bold]study join <token>[/bold] first.[/red]"
            )
            sys.exit(1)

    # ------------------------------------------------------------------
    # Workspace management
    # ------------------------------------------------------------------

    def create_workspace(self, name: str) -> dict:
        resp = requests.post(
            f"{self.server_url}/workspaces",
            json={"name": name},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 409:
            console.print(
                f"[red]A workspace named '[bold]{name}[/bold]' already exists on the server.[/red]"
            )
            sys.exit(1)
        _raise_for_status(resp)
        return resp.json()

    def join_workspace(self, token: str) -> dict:
        resp = requests.post(
            f"{self.server_url}/workspaces/join",
            json={"token": token},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code in (401, 404):
            console.print("[red]Invalid token — workspace not found.[/red]")
            sys.exit(1)
        _raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------------
    # Remote state
    # ------------------------------------------------------------------

    def get_remote_state(self) -> list[dict]:
        self._require_workspace()
        resp = requests.get(
            f"{self.server_url}/sync/state/{self.workspace_token}",
            timeout=HTTP_TIMEOUT,
        )
        _raise_for_status(resp)
        return resp.json()["files"]

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull(self) -> None:
        """
        Compare remote state to the local manifest and download every file
        that is absent or outdated locally.

        A file is considered up-to-date if:
        1. Its manifest entry version matches the remote version, AND
        2. The physical file on disk still matches the manifest checksum.

        Condition 2 catches the edge case where someone manually edited the
        file without going through `study push`.
        """
        self._require_workspace()
        assert self.workspace_name  # guarded by _require_workspace

        console.print(
            f"[blue]Fetching remote state for workspace "
            f"'[bold]{self.workspace_name}[/bold]'…[/blue]"
        )

        remote_files = self.get_remote_state()
        if not remote_files:
            console.print("[green]Workspace is empty.  Nothing to pull.[/green]")
            return

        manifest = load_manifest()
        to_download: list[dict] = []

        for rf in remote_files:
            file_path: str = rf["file_path"]
            remote_version: int = rf["latest_version"]
            remote_checksum: str = rf.get("latest_checksum") or ""

            local_entry = manifest.get(file_path)
            dest = local_file_path(self.workspace_name, file_path)

            if local_entry and local_entry["version"] == remote_version:
                # Version matches — verify the physical file hasn't drifted.
                if dest.exists():
                    if sha256_file(dest) == remote_checksum:
                        continue  # Truly up to date
                    # File was modified locally without a push — pull wins.
                    console.print(
                        f"[yellow]  Overwriting locally-modified "
                        f"'[bold]{file_path}[/bold]' with remote v{remote_version}[/yellow]"
                    )

            to_download.append(rf)

        if not to_download:
            console.print("[green]✓ Everything is up to date.[/green]")
            return

        console.print(f"[blue]Downloading [bold]{len(to_download)}[/bold] file(s)…[/blue]")

        success = 0
        for rf in to_download:
            file_path = rf["file_path"]
            remote_version = rf["latest_version"]
            remote_checksum = rf.get("latest_checksum") or ""
            size_bytes: Optional[int] = rf.get("size_bytes")

            console.print(f"  → [cyan]{file_path}[/cyan]  [dim]v{remote_version}[/dim]")
            try:
                dl_info = self._request_download(file_path)
                dest = local_file_path(self.workspace_name, file_path)
                self._stream_download(dl_info["presigned_url"], dest, size_bytes)

                # Integrity check
                actual_checksum = sha256_file(dest)
                if actual_checksum != remote_checksum:
                    console.print(
                        f"  [red]✗ Checksum mismatch for '{file_path}'.  "
                        f"Expected {remote_checksum[:12]}… got {actual_checksum[:12]}…  "
                        "File deleted.[/red]"
                    )
                    dest.unlink(missing_ok=True)
                    continue

                update_manifest_entry(file_path, remote_version, remote_checksum)

                # ----------------------------------------------------------
                # Checkout phase: copy from vault → current working directory
                # ----------------------------------------------------------
                cwd = Path(os.getcwd())
                checkout_dest = cwd / file_path
                checkout_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, checkout_dest)

                console.print(
                    f"  [green]✓ {file_path}[/green]  "
                    f"[dim]→ ./{Path(file_path).as_posix()}[/dim]"
                )
                success += 1

            except Exception as exc:
                console.print(f"  [red]✗ Failed to download '{file_path}': {exc}[/red]")

        console.print(
            f"[green]Pull complete — [bold]{success}/{len(to_download)}[/bold] file(s) "
            f"updated in vault and checked out to [bold]{os.getcwd()}[/bold].[/green]"
        )

    # ------------------------------------------------------------------
    # push
    # ------------------------------------------------------------------

    def push(self, file_path_arg: str) -> None:
        """
        Push a single file to the remote workspace.

        Accepts either:
        * A workspace-relative path  ("src/main.py")
        * An absolute/relative OS path — the file is copied into the workspace
          directory if it lives outside it.

        OCC flow:
        1. Hash the local file.
        2. Read base_version from the manifest (0 if untracked/new).
        3. POST /sync/upload-request → 409 means remote has diverged → abort.
        4. Stream PUT to the presigned S3 URL.
        5. POST /sync/commit-upload.
        6. Update manifest.
        """
        self._require_workspace()
        assert self.workspace_name

        ws_root = workspace_root(self.workspace_name)

        # Resolve the file and its workspace-relative key
        abs_path, relative = self._resolve_push_path(file_path_arg, ws_root)

        console.print(f"[blue]Hashing [bold]{relative}[/bold]…[/blue]")
        checksum = sha256_file(abs_path)
        size_bytes = abs_path.stat().st_size

        # Check against manifest: skip if nothing changed
        manifest = load_manifest()
        local_entry = manifest.get(relative)
        if local_entry and local_entry["checksum"] == checksum:
            console.print(
                f"[yellow]No changes detected in '[bold]{relative}[/bold]'.  Nothing to push.[/yellow]"
            )
            return

        base_version = local_entry["version"] if local_entry else 0

        # --- OCC gate ---
        console.print(
            f"[blue]Requesting upload slot  "
            f"[dim](base_version={base_version})[/dim]…[/blue]"
        )
        resp = requests.post(
            f"{self.server_url}/sync/upload-request",
            json={
                "workspace_token": self.workspace_token,
                "file_path": relative,
                "base_version": base_version,
                "checksum": checksum,
                "size_bytes": size_bytes,
            },
            timeout=HTTP_TIMEOUT,
        )

        if resp.status_code == 409:
            detail = resp.json().get("detail", "Remote has diverged.")
            console.print(
                f"\n[bold red]⚠  CONFLICT — Remote has changes.  Pull first.[/bold red]\n"
                f"[red]{detail}[/red]\n"
                f"  Run: [cyan]study pull[/cyan]\n"
            )
            sys.exit(1)

        _raise_for_status(resp)
        upload_info = resp.json()

        upload_id: str = upload_info["upload_id"]
        presigned_url: str = upload_info["presigned_url"]
        new_version: int = upload_info["new_version"]

        # --- Stream to S3 ---
        console.print(f"[blue]Uploading to S3  [dim]({size_bytes:,} bytes)[/dim]…[/blue]")
        try:
            self._stream_upload(presigned_url, abs_path, size_bytes)
        except Exception as exc:
            console.print(f"[red]✗ S3 upload failed: {exc}[/red]")
            console.print(
                "[yellow]The upload slot has been allocated but the file was not written.  "
                "You can retry — the slot will expire automatically.[/yellow]"
            )
            sys.exit(1)

        # --- Commit ---
        console.print("[blue]Committing…[/blue]")
        try:
            commit_resp = requests.post(
                f"{self.server_url}/sync/commit-upload",
                json={"upload_id": upload_id},
                timeout=HTTP_TIMEOUT,
            )
            _raise_for_status(commit_resp)
        except Exception as exc:
            console.print(
                f"[red]✗ Commit failed: {exc}\n"
                "The file was uploaded to S3 but the server did not record it.  "
                "Contact your administrator with upload_id=[bold]{upload_id}[/bold].[/red]"
            )
            sys.exit(1)

        # --- Update manifest ---
        update_manifest_entry(relative, new_version, checksum)
        console.print(
            f"[green]✓ Pushed '[bold]{relative}[/bold]' → v{new_version}[/green]"
        )

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> None:
        """
        Compare every tracked file's on-disk SHA-256 against the manifest.

        States:
        CLEAN     — matches manifest checksum
        MODIFIED  — exists on disk but checksum differs
        DELETED   — tracked in manifest but missing on disk
        UNTRACKED — present on disk but not in manifest
        """
        self._require_workspace()
        assert self.workspace_name

        manifest = load_manifest()
        ws_root = workspace_root(self.workspace_name)

        table = Table(
            title=f"Workspace: [bold]{self.workspace_name}[/bold]",
            show_lines=False,
            header_style="bold",
        )
        table.add_column("File", style="cyan", no_wrap=True)
        table.add_column("Status", justify="center")
        table.add_column("Ver", justify="right", style="dim")
        table.add_column("Checksum", style="dim", no_wrap=True)

        tracked_paths: set[str] = set(manifest.keys())
        rows: list[tuple] = []

        for file_path, entry in manifest.items():
            dest = local_file_path(self.workspace_name, file_path)
            ver = str(entry["version"])
            if not dest.exists():
                rows.append((file_path, "[red]DELETED[/red]", ver, entry["checksum"][:14] + "…"))
            else:
                current = sha256_file(dest)
                if current == entry["checksum"]:
                    rows.append((file_path, "[green]CLEAN[/green]", ver, current[:14] + "…"))
                else:
                    rows.append((file_path, "[yellow]MODIFIED[/yellow]", ver, current[:14] + "…"))

        # Untracked files
        for abs_path in all_local_files(self.workspace_name):
            rel = to_relative_path(self.workspace_name, abs_path)
            if rel not in tracked_paths:
                rows.append((rel, "[blue]UNTRACKED[/blue]", "-", "-"))

        if not rows:
            console.print(
                f"[yellow]Workspace '[bold]{self.workspace_name}[/bold]' is empty locally.  "
                "Run [cyan]study pull[/cyan] to download files.[/yellow]"
            )
            return

        for row in sorted(rows, key=lambda r: r[0]):
            table.add_row(*row)

        console.print(table)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request_download(self, file_path: str) -> dict:
        resp = requests.get(
            f"{self.server_url}/sync/download-request",
            params={
                "workspace_token": self.workspace_token,
                "file_path": file_path,
            },
            timeout=HTTP_TIMEOUT,
        )
        _raise_for_status(resp)
        return resp.json()

    def _stream_download(
        self,
        presigned_url: str,
        dest: Path,
        file_size: Optional[int] = None,
    ) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)

        with requests.get(presigned_url, stream=True, timeout=STREAM_TIMEOUT) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", file_size or 0)) or None

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            ) as progress:
                task = progress.add_task(dest.name, total=total)
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if chunk:
                            fh.write(chunk)
                            progress.update(task, advance=len(chunk))

    def _stream_upload(
        self,
        presigned_url: str,
        local_path: Path,
        size_bytes: int,
    ) -> None:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(local_path.name, total=size_bytes)

            with open(local_path, "rb") as fh:
                reader = _ProgressReader(fh, size_bytes, progress, task)
                resp = requests.put(
                    presigned_url,
                    data=reader,
                    headers={
                        "Content-Length": str(size_bytes),
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=STREAM_TIMEOUT,
                )
                resp.raise_for_status()

    def _resolve_push_path(
        self,
        file_path_arg: str,
        ws_root: Path,
    ) -> tuple[Path, str]:
        """
        Return (absolute_path, workspace_relative_posix_key).

        Tries the arg as:
        1. A path relative to the workspace root.
        2. An absolute OS path — if the file is inside the workspace root,
           derive the relative key.  If outside, copy it into the workspace
           root (top-level, using the filename only).
        """
        import shutil

        # Attempt 1: relative to workspace root
        candidate = ws_root / file_path_arg
        if candidate.exists():
            return candidate, file_path_arg.replace("\\", "/")

        # Attempt 2: treat as an OS path
        os_path = Path(file_path_arg).expanduser().resolve()
        if not os_path.exists():
            console.print(f"[red]File not found: {file_path_arg}[/red]")
            sys.exit(1)

        try:
            # File is already inside the workspace directory
            relative = os_path.relative_to(ws_root).as_posix()
            return os_path, relative
        except ValueError:
            pass

        # File is outside the workspace — copy it in
        relative = os_path.name
        dest = ws_root / relative
        console.print(
            f"[yellow]File is outside the workspace.  "
            f"Copying to '[bold]{relative}[/bold]' inside workspace.[/yellow]"
        )
        shutil.copy2(os_path, dest)
        return dest, relative


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _raise_for_status(resp: requests.Response) -> None:
    """Raise with a helpful message on HTTP errors."""
    if not resp.ok:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise requests.HTTPError(
            f"HTTP {resp.status_code}: {detail}",
            response=resp,
        )
