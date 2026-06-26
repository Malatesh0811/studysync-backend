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
from rich.prompt import Prompt
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
                "\nOffline-first, distributed workspace sync for students.\n",
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
        ("study register",                   "Create a new account"),
        ("study login",                      "Login to your account"),
        ("study workspace create [NAME]",    "Create a new workspace"),
        ("study invite [EMAIL]",             "Invite a friend to your workspace"),
        ("study workspace remove [EMAIL]",   "Remove a member from your workspace"),
        ("study join [TOKEN or NAME]",       "Join a workspace using its token or name"),
        ("study push [FILE]",                "Upload a file to the workspace"),
        ("study pull",                       "Download new/changed files from workspace"),
        ("study status",                     "Show sync state of every tracked file"),
        ("study config",                     "Display active workspace configuration"),
    ]:
        tbl.add_row(cmd, desc)

    console.print(Rule("[bold]Commands[/bold]", style="dim"))
    console.print(Padding(tbl, (0, 0)))
    console.print(Rule("[bold]Quick start[/bold]", style="dim"))
    console.print(
        Padding(
            "[dim]1.[/dim]  [cyan]study register[/cyan]\n"
            "[dim]2.[/dim]  [cyan]study workspace create mygroup[/cyan]\n"
            "[dim]3.[/dim]  [cyan]study invite friend@gmail.com[/cyan]\n"
            "[dim]4.[/dim]  [cyan]study push notes.pdf[/cyan]\n\n"
            "Type [cyan]study <command> --help[/cyan] for per-command options.",
            (0, 4, 1, 4),
        )
    )


@app.callback()
def _root_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _print_help_panel()
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Auth commands
# ---------------------------------------------------------------------------

@app.command()
def register(
    server: str = typer.Option(
        DEFAULT_SERVER, "--server", "-s",
        envvar="STUDYSYNC_SERVER", show_default=False,
    ),
) -> None:
    """Create a new StudySync account."""
    ensure_dirs()
    console.rule("[bold blue]study register[/bold blue]", style="dim blue")
    email = Prompt.ask("[cyan]Email[/cyan]")
    password = Prompt.ask("[cyan]Password[/cyan]", password=True)
    confirm = Prompt.ask("[cyan]Confirm password[/cyan]", password=True)
    if password != confirm:
        console.print("[red]Passwords do not match.[/red]")
        raise typer.Exit(1)

    engine = SyncEngine(server_url=server)
    with console.status("[blue]Creating account...[/blue]", spinner="dots"):
        result = engine.register(email, password)

    cfg = load_config()
    cfg["user_token"] = result["access_token"]
    cfg["user_email"] = result["email"]
    cfg["server_url"] = server
    save_config(cfg)

    console.print(
        Panel(
            "[bold]Email  [/bold]  " + result["email"] + "\n\n"
            "You are now logged in.\n"
            "Next: [cyan]study workspace create <name>[/cyan]",
            title="[bold green]Account created[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


@app.command()
def login(
    server: str = typer.Option(
        DEFAULT_SERVER, "--server", "-s",
        envvar="STUDYSYNC_SERVER", show_default=False,
    ),
) -> None:
    """Login to your StudySync account."""
    ensure_dirs()
    console.rule("[bold blue]study login[/bold blue]", style="dim blue")
    email = Prompt.ask("[cyan]Email[/cyan]")
    password = Prompt.ask("[cyan]Password[/cyan]", password=True)

    engine = SyncEngine(server_url=server)
    with console.status("[blue]Logging in...[/blue]", spinner="dots"):
        result = engine.login(email, password)

    cfg = load_config()
    cfg["user_token"] = result["access_token"]
    cfg["user_email"] = result["email"]
    cfg["server_url"] = server
    save_config(cfg)

    console.print(
        Panel(
            "Logged in as [bold]" + result["email"] + "[/bold]\n\n"
            "Next: [cyan]study workspace create <name>[/cyan]  or  [cyan]study join <token>[/cyan]",
            title="[bold green]Login successful[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Workspace commands
# ---------------------------------------------------------------------------

@workspace_app.command("create")
def workspace_create(
    name: str = typer.Argument(..., help="Unique workspace name."),
    server: str = typer.Option(
        DEFAULT_SERVER, "--server", "-s",
        envvar="STUDYSYNC_SERVER", show_default=False,
    ),
) -> None:
    """Create a new workspace. You become the owner."""
    ensure_dirs()
    engine = SyncEngine(server_url=server)

    with console.status("[blue]Creating workspace [bold]" + name + "[/bold]...[/blue]", spinner="dots"):
        result = engine.create_workspace(name)

    token: str = result["access_token"]
    workspace_id: str = result["workspace_id"]

    cfg = load_config()
    cfg.update({
        "server_url": server,
        "workspace_name": name,
        "workspace_id": workspace_id,
        "workspace_token": token,
    })
    save_config(cfg)
    workspace_root(name)

    console.print(
        Panel(
            "[bold]Workspace[/bold]  " + name + "\n"
            "[bold]Token    [/bold]  [yellow]" + token + "[/yellow]\n\n"
            "Invite friends:\n"
            "  [cyan]study invite friend@gmail.com[/cyan]\n\n"
            "Or share the token:\n"
            "  [cyan]study join " + token + "[/cyan]",
            title="[bold green]Workspace created[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


@workspace_app.command("remove")
def workspace_remove(
    email: str = typer.Argument(..., help="Email of the member to remove."),
) -> None:
    """Remove a member from your workspace. Owner only."""
    console.rule("[bold blue]study workspace remove[/bold blue]", style="dim blue")
    engine = SyncEngine()
    with console.status("[blue]Removing " + email + "...[/blue]", spinner="dots"):
        engine.remove_member(email)
    console.print("[green]" + email + " removed from workspace.[/green]")


# ---------------------------------------------------------------------------
# Invite
# ---------------------------------------------------------------------------

@app.command()
def invite(
    email: str = typer.Argument(..., help="Email address of the friend to invite."),
) -> None:
    """Invite a friend to your current workspace by email."""
    console.rule("[bold blue]study invite[/bold blue]", style="dim blue")
    engine = SyncEngine()
    with console.status("[blue]Inviting " + email + "...[/blue]", spinner="dots"):
        result = engine.invite_member(email)
    console.print(
        Panel(
            "[bold]" + email + "[/bold] has been added to [bold]" + result["workspace_name"] + "[/bold].\n\n"
            "They can now run:\n"
            "  [cyan]study join " + result["access_token"] + "[/cyan]\n"
            "  [cyan]study pull[/cyan]",
            title="[bold green]Invited[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

@app.command()
def join(
    token_or_alias: str = typer.Argument(..., help="Workspace access token (UUID) or workspace name."),
    server: str = typer.Option(
        DEFAULT_SERVER, "--server", "-s",
        envvar="STUDYSYNC_SERVER", show_default=False,
    ),
) -> None:
    """Join an existing workspace using its access token or name."""
    ensure_dirs()
    engine = SyncEngine(server_url=server)

    with console.status("[blue]Resolving '" + token_or_alias + "'...[/blue]", spinner="dots"):
        resolved = engine.resolve_input(token_or_alias)

    token: str = resolved["access_token"]
    workspace_name: str = resolved["name"]
    workspace_id: str = resolved["workspace_id"]
    from_alias: bool = resolved["resolved_from_alias"]

    cfg = load_config()
    cfg.update({
        "server_url": server,
        "workspace_name": workspace_name,
        "workspace_id": workspace_id,
        "workspace_token": token,
    })
    save_config(cfg)
    workspace_root(workspace_name)

    alias_note = (
        "\n[dim]Resolved alias '" + token_or_alias + "' → " + token + "[/dim]"
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


# ---------------------------------------------------------------------------
# Sync commands
# ---------------------------------------------------------------------------

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
                "Register:  [cyan]study register[/cyan]\n"
                "Or join:   [cyan]study join <token>[/cyan]",
                title="[bold yellow]Not configured[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        raise typer.Exit(1)

    workspace_name = cfg.get("workspace_name", "N/A")
    local_path = WORKSPACES_DIR / workspace_name if workspace_name != "N/A" else "N/A"

    rows = [
        ("User",         cfg.get("user_email", "N/A")),
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
