"""
main.py — Rich-enhanced Typer CLI entry point for the StudySync `study` command.

Install:
    cd cli && pip install -e .

Usage:
    study workspace create <name>
    study join <token>
    study pull
    study push <file_path>
    study status
    study config
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .constants import PRODUCTION_SERVER_URL
from .local_state import (
    WORKSPACES_DIR,
    ensure_dirs,
    load_config,
    save_config,
    workspace_root,
)
from .sync_engine import SyncEngine

# ---------------------------------------------------------------------------
# Version helper
# ---------------------------------------------------------------------------

try:
    __version__ = _pkg_version("study_sync")
except PackageNotFoundError:
    __version__ = "dev"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

console = Console()

# Points at the public production backend so users who install via
# `pip install study_sync` work immediately without needing --server.
DEFAULT_SERVER = PRODUCTION_SERVER_URL

# ---------------------------------------------------------------------------
# Typer app skeleton
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="study",
    help="StudySync — offline-first CLI workspace synchronisation.",
    rich_markup_mode="rich",
    # invoke_without_command=True so our callback can show the custom help panel
    invoke_without_command=True,
    add_completion=True,
)

workspace_app = typer.Typer(
    help="Manage workspaces.",
    invoke_without_command=True,
    no_args_is_help=True,
)
app.add_typer(workspace_app, name="workspace")


# ---------------------------------------------------------------------------
# Custom no-args help panel
# ---------------------------------------------------------------------------

def _print_help_panel() -> None:
    """Render a beautiful Rich help panel when `study` is called with no args."""

    # ── Header ──────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            Text.assemble(
                ("StudySync", "bold cyan"),
                ("  v" + __version__, "dim"),
                "\nOffline-first, distributed workspace sync for developers.\n",
                (DEFAULT_SERVER, "dim italic"),
            ),
            border_style="cyan",
            padding=(0, 2),
        )
    )

    # ── Commands table ───────────────────────────────────────────────────────
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold blue", padding=(0, 2))
    tbl.add_column("Command", style="cyan", no_wrap=True)
    tbl.add_column("Description")

    commands = [
        ("study workspace create [NAME]",  "Create a new workspace and print its share token"),
        ("study join [TOKEN]",             "Join a workspace using its share token"),
        ("study pull",                     "Download new/changed files from the remote workspace"),
        ("study push [FILE]",              "Upload a local file to the remote workspace"),
        ("study status",                   "Show the sync state of every tracked file"),
        ("study config",                   "Display active workspace configuration"),
    ]
    for cmd, desc in commands:
        tbl.add_row(cmd, desc)

    console.print(Rule("[bold]Commands[/bold]", style="dim"))
    console.print(Padding(tbl, (0, 0)))

    # ── Quick start ──────────────────────────────────────────────────────────
    console.print(Rule("[bold]Quick start[/bold]", style="dim"))
    console.print(
        Padding(
            "[dim]1.[/dim]  [cyan]pip install study_sync[/cyan]\n"
            "[dim]2.[/dim]  [cyan]study join <TOKEN>[/cyan]\n"
            "[dim]3.[/dim]  [cyan]study pull[/cyan]\n\n"
            "Type [cyan]study <command> --help[/cyan] for per-command options.",
            (0, 4, 1, 4),
        )
    )


@app.callback()
def _root_callback(ctx: typer.Context) -> None:
    """Show the help panel when `study` is run with no subcommand."""
    if ctx.invoked_subcommand is None:
        _print_help_panel()
        raise typer.Exit()


# ===========================================================================
# study workspace create <name>
# ===========================================================================

@workspace_app.command("create")
def workspace_create(
    name: str = typer.Argument(
        ...,
        help="Unique workspace name to create on the server.",
    ),
    server: str = typer.Option(
        DEFAULT_SERVER,
        "--server", "-s",
        help="Base URL of the StudySync server (default: production).",
        envvar="STUDYSYNC_SERVER",
        show_default=False,
    ),
) -> None:
    """
    Create a new workspace and receive a shareable access token.

    The printed token is the only credential for joining this workspace —
    copy it and share it with collaborators via [cyan]study join <TOKEN>[/cyan].
    """
    ensure_dirs()
    engine = SyncEngine(server_url=server)

    with console.status(
        f"[blue]Creating workspace [bold]{name!r}[/bold]…[/blue]",
        spinner="dots",
    ):
        result = engine.create_workspace(name)

    token: str = result["access_token"]
    workspace_id: str = result["workspace_id"]

    save_config(
        {
            "server_url": server,
            "workspace_name": name,
            "workspace_id": workspace_id,
            "workspace_token": token,
        }
    )
    workspace_root(name)

    console.print(
        Panel(
            f"[bold]Workspace[/bold]  {name}\n"
            f"[bold]Token    [/bold]  [yellow]{token}[/yellow]\n\n"
            f"Share with collaborators:\n"
            f"  [cyan]study join {token}[/cyan]",
            title="[bold green]✓ Workspace created[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ===========================================================================
# study join <token>
# ===========================================================================

@app.command()
def join(
    token: str = typer.Argument(
        ...,
        help="Workspace access token (UUID) shared by the workspace owner.",
    ),
    server: str = typer.Option(
        DEFAULT_SERVER,
        "--server", "-s",
        help="Base URL of the StudySync server (default: production).",
        envvar="STUDYSYNC_SERVER",
        show_default=False,
    ),
) -> None:
    """
    Join an existing workspace using its access token.

    Validates the token against the server, then saves the workspace
    credentials locally.  Run [cyan]study pull[/cyan] afterwards to
    download all files.
    """
    ensure_dirs()
    engine = SyncEngine(server_url=server)

    with console.status("[blue]Validating token…[/blue]", spinner="dots"):
        result = engine.join_workspace(token)

    workspace_name: str = result["name"]
    workspace_id: str = result["workspace_id"]

    save_config(
        {
            "server_url": server,
            "workspace_name": workspace_name,
            "workspace_id": workspace_id,
            "workspace_token": token,
        }
    )
    workspace_root(workspace_name)

    console.print(
        Panel(
            f"[bold]Workspace[/bold]  {workspace_name}\n"
            f"[bold]Server   [/bold]  {server}\n\n"
            f"Run [cyan]study pull[/cyan] to download all files.",
            title="[bold green]✓ Joined workspace[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ===========================================================================
# study pull
# ===========================================================================

@app.command()
def pull() -> None:
    """
    Download all new or updated files from the remote workspace.

    Compares remote file versions and checksums against the local manifest,
    downloads only what has changed, and verifies each file's SHA-256 hash.
    Updated files are written to the current directory.
    """
    console.rule("[bold blue]study pull[/bold blue]", style="dim blue")
    SyncEngine().pull()


# ===========================================================================
# study push <file_path>
# ===========================================================================

@app.command()
def push(
    file_path: str = typer.Argument(
        ...,
        help="Path to the local file to upload (relative or absolute).",
    ),
    server: str = typer.Option(
        None,
        "--server",
        help="Override the server URL for this push only.",
        envvar="STUDYSYNC_SERVER",
        show_default=False,
    ),
) -> None:
    """
    Upload a local file to the remote workspace.

    Computes a SHA-256 checksum and base version before uploading, so
    the server can reject concurrent conflicting writes (optimistic
    concurrency control).  If the file has not changed since the last push,
    the upload is skipped.
    """
    console.rule("[bold blue]study push[/bold blue]", style="dim blue")
    SyncEngine(server_url=server).push(file_path)


# ===========================================================================
# study status
# ===========================================================================

@app.command()
def status() -> None:
    """
    Show the sync status of every file in the local workspace.

    [green]CLEAN[/green]      — matches the last-known server version.
    [yellow]MODIFIED[/yellow]   — changed locally since last push/pull.
    [red]DELETED[/red]    — tracked but missing from disk.
    [blue]UNTRACKED[/blue]  — on disk but never pushed.
    """
    console.rule("[bold blue]study status[/bold blue]", style="dim blue")
    SyncEngine().status()


# ===========================================================================
# study config
# ===========================================================================

@app.command()
def config() -> None:
    """Display the active workspace configuration stored in ~/.study/config.json."""
    cfg = load_config()
    if not cfg:
        console.print(
            Panel(
                "No workspace is configured.\n\n"
                "Create one:  [cyan]study workspace create <name>[/cyan]\n"
                "Or join one: [cyan]study join <token>[/cyan]",
                title="[bold yellow]⚠ Not configured[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        raise typer.Exit(1)

    workspace_name = cfg.get("workspace_name", "N/A")
    local_path = WORKSPACES_DIR / workspace_name if workspace_name != "N/A" else "N/A"

    rows = [
        ("Workspace",    workspace_name),
        ("Server",       cfg.get("server_url", "N/A")),
        ("Token",        cfg.get("workspace_token", "N/A")),
        ("Workspace ID", cfg.get("workspace_id", "N/A")),
        ("Local path",   str(local_path)),
    ]
    body = "\n".join(f"[bold]{k:<14}[/bold] {v}" for k, v in rows)

    console.print(
        Panel(
            body,
            title="[bold blue]StudySync Config[/bold blue]  (~/.study/config.json)",
            border_style="blue",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
