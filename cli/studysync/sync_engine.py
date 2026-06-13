"""sync_engine.py -- All HTTP calls and sync logic for StudySync CLI."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

import requests
from requests import HTTPError, Session
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from .constants import PRODUCTION_SERVER_URL
from .local_state import (
    all_local_files,
    ensure_dirs,
    load_config,
    load_manifest,
    local_file_path,
    sha256_file,
    update_manifest_entry,
    workspace_root,
    WORKSPACES_DIR,
)

console = Console()


class _ProgressReader:
    def __init__(self, path: Path, task_id: Any, progress: Progress) -> None:
        self._fh = open(path, "rb")
        self._task_id = task_id
        self._progress = progress
        self._size = path.stat().st_size

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._progress.update(self._task_id, advance=len(chunk))
        return chunk

    def __len__(self) -> int:
        return self._size

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SyncEngine:
    def __init__(self, server_url: Optional[str] = None) -> None:
        ensure_dirs()
        config = load_config()
        raw_url = server_url or config.get("server_url", PRODUCTION_SERVER_URL)
        self.base_url: str = raw_url.rstrip("/")
        self.workspace_id: str = config.get("workspace_id", "")
        self.workspace_name: str = config.get("workspace_name", "")
        self.token: str = config.get("workspace_token", "")
        self.session: Session = requests.Session()
        if self.token:
            self.session.headers["Authorization"] = "Bearer " + self.token
        self.session.headers["User-Agent"] = "studysync-cli/1.0"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _raise_for_status(self, resp: requests.Response) -> None:
        if resp.ok:
            return
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPError("HTTP " + str(resp.status_code) + ": " + str(detail), response=resp)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", 90)
        try:
            return self.session.request(method, url, **kwargs)
        except requests.ReadTimeout:
            console.print("[yellow]Server is waking up (cold start) -- retrying...[/yellow]")
            kwargs["timeout"] = 120
            return self.session.request(method, url, **kwargs)

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def _require_workspace(self) -> None:
        if not self.workspace_id or not self.token:
            console.print(
                Panel(
                    "No workspace configured.\n\n"
                    "Create: [cyan]study workspace create <name>[/cyan]\n"
                    "Join:   [cyan]study join <token-or-name>[/cyan]",
                    title="[bold red]Not configured[/bold red]",
                    border_style="red",
                    padding=(1, 2),
                )
            )
            sys.exit(1)

    # ------------------------------------------------------------------
    # Workspace management
    # ------------------------------------------------------------------

    def create_workspace(self, name: str) -> dict:
        resp = self._post(self.base_url + "/workspaces", json={"name": name})
        self._raise_for_status(resp)
        data = resp.json()
        return {
            "workspace_id": data["workspace_id"],
            "access_token": data["access_token"],
            "name": data["name"],
        }

    def resolve_input(self, token_or_alias: str) -> dict:
        try:
            resp = self._get(self.base_url + "/resolve/" + token_or_alias)
            self._raise_for_status(resp)
            return resp.json()
        except HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            try:
                detail = exc.response.json().get("detail", str(exc))
            except Exception:
                detail = str(exc)
            console.print(
                Panel(
                    "Could not resolve '" + token_or_alias + "'\n\n" + str(detail),
                    title="[bold red]HTTP " + str(code) + " -- Not found[/bold red]",
                    border_style="red",
                    padding=(1, 2),
                )
            )
            sys.exit(1)
        except requests.ConnectionError:
            console.print(
                Panel(
                    "Cannot reach the server at:\n  " + self.base_url,
                    title="[bold red]Connection error[/bold red]",
                    border_style="red",
                    padding=(1, 2),
                )
            )
            sys.exit(1)

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull(self) -> None:
        self._require_workspace()
        cwd = Path(os.getcwd())

        with console.status("[blue]Fetching workspace state...[/blue]", spinner="dots"):
            try:
                resp = self._get(self.base_url + "/sync/state/" + self.token)
                self._raise_for_status(resp)
            except (HTTPError, requests.ConnectionError) as exc:
                console.print(Panel(str(exc), title="[bold red]Pull failed[/bold red]",
                                    border_style="red", padding=(1, 2)))
                sys.exit(1)

        remote_files = {f["file_path"]: f for f in resp.json().get("files", [])}

        if not remote_files:
            console.print("[green]Workspace is empty -- nothing to pull.[/green]")
            return

        manifest = load_manifest()
        to_download = []

        for fp, rf in remote_files.items():
            remote_ver = rf.get("latest_version", 0)
            local_entry = manifest.get(fp, {})
            local_ver = local_entry.get("version", 0)
            file_on_disk = (cwd / fp).exists()
            if not file_on_disk or local_ver < remote_ver:
                to_download.append(rf)

        if not to_download:
            console.print("[green]Everything is up to date.[/green]")
            return

        console.print("[blue]Downloading " + str(len(to_download)) + " file(s)...[/blue]")

        errors = []
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            for rf in to_download:
                fp = rf["file_path"]
                remote_ver = rf.get("latest_version", 0)
                remote_cksum = rf.get("latest_checksum", "")
                size = rf.get("size_bytes") or 0

                task_id = progress.add_task(fp, total=size if size > 0 else None)

                try:
                    dl = self._get(
                        self.base_url + "/sync/download-request",
                        params={"workspace_token": self.token, "file_path": fp},
                    )
                    self._raise_for_status(dl)
                except (HTTPError, requests.ConnectionError) as exc:
                    errors.append(fp + ": " + str(exc))
                    progress.update(task_id, visible=False)
                    continue

                presigned_url = dl.json()["presigned_url"]

                vault_path = local_file_path(self.workspace_name, fp)
                vault_path.parent.mkdir(parents=True, exist_ok=True)

                try:
                    with requests.get(presigned_url, stream=True, timeout=120) as r:
                        r.raise_for_status()
                        with open(vault_path, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                                if chunk:
                                    fh.write(chunk)
                                    progress.update(task_id, advance=len(chunk))
                except Exception as exc:
                    errors.append(fp + ": " + str(exc))
                    progress.update(task_id, visible=False)
                    continue

                actual = sha256_file(vault_path)
                if remote_cksum and actual != remote_cksum:
                    errors.append(fp + ": checksum mismatch")
                    vault_path.unlink(missing_ok=True)
                    progress.update(task_id, visible=False)
                    continue

                update_manifest_entry(fp, remote_ver, actual)

                dest = cwd / fp
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(vault_path, dest)

        if errors:
            for e in errors:
                console.print("[red]Failed: " + e + "[/red]")
        ok = len(to_download) - len(errors)
        console.print(
            "[green]Pull complete -- " + str(ok) + "/" + str(len(to_download))
            + " file(s) written to " + str(cwd) + "[/green]"
        )

    # ------------------------------------------------------------------
    # push
    # ------------------------------------------------------------------

    def push(self, file_path_arg: str) -> None:
        self._require_workspace()

        src = Path(file_path_arg).expanduser().resolve()
        if not src.exists():
            console.print(Panel("File not found: " + file_path_arg,
                                title="[bold red]File not found[/bold red]",
                                border_style="red", padding=(1, 2)))
            sys.exit(1)

        file_path = src.name
        checksum = sha256_file(src)
        size = src.stat().st_size

        manifest = load_manifest()
        local_entry = manifest.get(file_path, {})

        if local_entry.get("checksum") == checksum:
            console.print("[yellow]No changes detected in '" + file_path + "' -- nothing to push.[/yellow]")
            return

        base_version = local_entry.get("version", 0)

        with console.status("[blue]Requesting upload slot...[/blue]", spinner="dots"):
            try:
                req = self._post(
                    self.base_url + "/sync/upload-request",
                    json={
                        "workspace_token": self.token,
                        "file_path": file_path,
                        "checksum": checksum,
                        "size_bytes": size,
                        "base_version": base_version,
                    },
                )

                if req.status_code == 409:
                    try:
                        detail = req.json().get("detail", "")
                    except Exception:
                        detail = req.text
                    console.print(
                        Panel(
                            "'" + file_path + "' has been updated on the server since your last pull.\n\n"
                            "Your local base version: " + str(base_version) + "\n\n"
                            "Run [cyan]study pull[/cyan] first, then push again.\n\n"
                            + str(detail),
                            title="[bold red]Conflict -- pull required[/bold red]",
                            border_style="red",
                            padding=(1, 2),
                        )
                    )
                    sys.exit(1)

                self._raise_for_status(req)

            except HTTPError as exc:
                console.print(Panel(str(exc), title="[bold red]Upload request failed[/bold red]",
                                    border_style="red", padding=(1, 2)))
                sys.exit(1)

        data = req.json()
        presigned_url = data["presigned_url"]
        upload_id = data["upload_id"]
        new_version = data["new_version"]

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task("Uploading " + file_path, total=size)
            with open(src, "rb") as fh:
                reader = _ProgressReader(src, task_id, progress)
                put_resp = self.session.put(
                    presigned_url,
                    data=reader,
                    headers={
                        "Content-Length": str(size),
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=300,
                )
                put_resp.raise_for_status()

        with console.status("[blue]Committing...[/blue]", spinner="dots"):
            try:
                commit = self._post(self.base_url + "/sync/commit-upload",
                                    json={"upload_id": upload_id})
                self._raise_for_status(commit)
            except (HTTPError, requests.ConnectionError) as exc:
                console.print(Panel(str(exc), title="[bold red]Commit failed[/bold red]",
                                    border_style="red", padding=(1, 2)))
                sys.exit(1)

        update_manifest_entry(file_path, new_version, checksum)
        console.print("[green]Pushed '" + file_path + "' -> v" + str(new_version) + "[/green]")

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> None:
        self._require_workspace()
        cwd = Path(os.getcwd())

        with console.status("[blue]Fetching server state...[/blue]", spinner="dots"):
            try:
                resp = self._get(self.base_url + "/sync/state/" + self.token)
                self._raise_for_status(resp)
                remote_files = {f["file_path"]: f for f in resp.json().get("files", [])}
            except (HTTPError, requests.ConnectionError):
                remote_files = {}

        manifest = load_manifest()

        all_paths = set(remote_files) | set(manifest)
        for p in cwd.rglob("*"):
            if p.is_file() and not str(p).startswith(str(Path.home() / ".study")):
                try:
                    rel = p.relative_to(cwd).as_posix()
                    all_paths.add(rel)
                except ValueError:
                    pass

        if not all_paths:
            console.print("[yellow]No files tracked. Run [cyan]study push <file>[/cyan] to start.[/yellow]")
            return

        table = Table(show_header=True, header_style="bold blue")
        table.add_column("File", style="cyan", no_wrap=True)
        table.add_column("Status", justify="center")
        table.add_column("Local ver", justify="right", style="dim")
        table.add_column("Server ver", justify="right", style="dim")

        for fp in sorted(all_paths):
            remote_info = remote_files.get(fp)
            local_entry = manifest.get(fp, {})
            file_on_disk = (cwd / fp).exists()
            local_ver = str(local_entry.get("version", "-")) if local_entry else "-"
            server_ver = str(remote_info.get("latest_version", "-")) if remote_info else "-"
            local_cksum = local_entry.get("checksum", "") if local_entry else ""

            if remote_info and not file_on_disk and not local_entry:
                status_cell = "[cyan]NOT PULLED[/cyan]"
            elif remote_info and not file_on_disk and local_entry:
                status_cell = "[red]MISSING[/red]"
            elif remote_info and file_on_disk:
                disk_cksum = sha256_file(cwd / fp)
                if disk_cksum == remote_info.get("latest_checksum", ""):
                    status_cell = "[green]SYNCED[/green]"
                elif disk_cksum == local_cksum:
                    status_cell = "[cyan]NOT PULLED[/cyan]"
                else:
                    status_cell = "[yellow]MODIFIED[/yellow]"
            elif not remote_info and file_on_disk:
                status_cell = "[dim]LOCAL[/dim]"
            else:
                status_cell = "[dim]UNKNOWN[/dim]"

            table.add_row(fp, status_cell, local_ver, server_ver)

        console.print(table)
