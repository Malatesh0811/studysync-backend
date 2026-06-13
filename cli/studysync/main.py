"""
main.py -- Rich-enhanced Typer CLI entry point for the StudySync `study` command.
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

try:
    __version__ = _pkg_version("study_sync")
except PackageNotFoundError:
    __version__ = "dev"

console = Console()
DEFAULT_SERVER = PRODUCTION_SERVER_URL

app = typer.Typer(
    name="study",
    help="StudySync -- offline-first CLI workspace synchronisation.",
    rich_markup_mode="rich",
    invoke_without_command=True,
    add_completion=True,
)

workspace_app = typer.Typer(
    help="Manage workspaces.",
    invoke_without_command=True,
    no_args_is_help=True,
)
app.add_typer(workspace_app, name="workspace")


def _print_help_panel() -> None:
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

    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold blue", padding=(0, 2))
    tbl.add_column("Command", style="cyan", no_wrap=True)
    tbl.add_column("Description")

    for cmd, desc in [
        ("study workspace create [NAME]", "Create a new workspace and print its share token"),
        ("study join [TOKEN or NAME]",    "Join a workspace using its token or alias"),
        ("study pull",                    "Download new/changed files from the remote workspace"),
        ("study push [FILE]",             "Upload a local file to the remote workspace"),
        ("study status",                  "Show the sync state of every tracked file"),
        ("study config",                  "Display active workspace configuration"),
    ]:
        tbl.add_row(cmd, desc)

    console.print(Rule("[bold]Commands[/bold]", style="dim"))
    console.print(Padding(tbl, (0, 0)))
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
    if ctx.invoked_subcommand is None:
        _print_help_panel()
        raise typer.Exit()


@workspace_app.command("create")
def workspace_create(
    name: str = typer.Argument(..., help="Unique workspace name to create on the server."),
    server: str = typer.Option(
        DEFAULT_SERVER, "--server", "-s",
        help="Base URL of the StudySync server.",
        envvar="STUDYSYNC_SERVER", show_default=False,
    ),
) -> None:
    """Create a new workspace and receive a shareable access token."""
    ensure_dirs()
    engine = SyncEngine(server_url=server)

    with console.status(f"[blue]Creating workspace [bold]{name!r}[/bold]...[/blue]", spinner="dots"):
        result = engine.create_workspace(name)

    token: str = result["access_token"]
    workspace_id: str = result["workspace_id"]

    save_config({
        "server_url": server,
        "workspace_name": name,
        "workspace_id": workspace_id,
        "workspace_token": token,
    })
    workspace_root(name)

    console.print(
        Panel(
            "[bold]Workspace[/bold]  " + name + "\n"
            "[bold]Token    [/bold]  [yellow]" + token + "[/yellow]\n\n"
            "Share with collaborators:\n"
            "  [cyan]study join " + token + "[/cyan]\n"
            "  [cyan]study join " + name + "[/cyan]  (alias also works)",
            title="[bold green]Workspace created[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


@app.command()
def join(
    token_or_alias: str = typer.Argument(..., help="Workspace access token (UUID) or workspace name."),
    server: str = typer.Option(
        DEFAULT_SERVER, "--server", "-s",
        help="Base URL of the StudySync server.",
        envvar="STUDYSYNC_SERVER", show_default=False,
    ),
) -> None:
    """Join an existing workspace using its access token or name."""
    ensure_dirs()
    engine = SyncEngine(server_url=server)

    with console.status(f"[blue]Resolving '{token_or_alias}'...[/blue]", spinner="dots"):
        resolved = engine.resolve_input(token_or_alias)

    token: str = resolved["access_token"]
    workspace_name: str = resolved["name"]
    workspace_id: str = resolved["workspace_id"]
    from_alias: bool = resolved["resolved_from_alias"]

    save_config({
        "server_url": server,
        "workspace_name": workspace_name,
        "workspace_id": workspace_id,
        "workspace_token": token,
    })
    workspace_root(workspace_name)

    alias_note = (
        "\n[dim]Resolved alias '" + token_or_alias + "' to " + token + "[/dim]"
        if from_alias else ""
    )

    console.print(
        Panel(
            "[bold]Workspace[/bold]  " + workspace_name + "\n"
            "[bold]Server   [/bold]  " + server + "\n"
            "[bold]Token    [/bold]  " + token
            + alias_note
            + "\n\nRun [cyan]study pull[/cyan] to download all files.",
            title="[bold green]Joined workspace[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


@app.command()
def pull() -> None:
    """Download all new or updated files from the remote workspace."""
    console.rule("[bold blue]study pull[/bold blue]", style="dim blue")
    SyncEngine().pull()


@app.command()
def push(
    file_path: str = typer.Argument(..., help="Path to the local file to upload."),
    server: str = typer.Option(
        None, "--server",
        help="Override the server URL for this push only.",
        envvar="STUDYSYNC_SERVER", show_default=False,
    ),
) -> None:
    """Upload a local file to the remote workspace."""
    console.rule("[bold blue]study push[/bold blue]", style="dim blue")
    SyncEngine(server_url=server).push(file_path)


@app.command()
def status() -> None:
    """Show the sync status of every file in the workspace."""
    console.rule("[bold blue]study status[/bold blue]", style="dim blue")
    SyncEngine().status()


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
                title="[bold yellow]Not configured[/bold yellow]",
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
    body = "\n".join(
        "[bold]" + k.ljust(14) + "[/bold] " + str(v) for k, v in rows
    )

    console.print(
        Panel(
            body,
            title="[bold blue]StudySync Config[/bold blue]  (~/.study/config.json)",
            border_style="blue",
            padding=(1, 2),
        )
    )


if __name__ == "__main__":
    app()
