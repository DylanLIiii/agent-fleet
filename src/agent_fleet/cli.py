from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from agent_fleet.manager import Manager, make_instance
from agent_fleet.model import FleetError
from agent_fleet.store import Store

app = typer.Typer(
    help="Your agents. One calm control room. Run without a command to open the dashboard.",
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)
console = Console()
DEFAULT_HOME = Path.home() / ".agent-fleet"


def dashboard(manager: Manager, demo: bool = False, *, setup: bool = False) -> None:
    from agent_fleet.ui import FleetApp

    FleetApp(manager, demo=demo, setup=setup).run()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    home: Annotated[
        Path, typer.Option("--home", envvar="FLEET_HOME", help="Private fleet directory.")
    ] = DEFAULT_HOME,
    instance: Annotated[
        str | None, typer.Option("--instance", "-i", help="Instance to manage.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview without changing files or processes.")
    ] = False,
    demo: Annotated[
        bool, typer.Option("--demo", help="Open a read-only sample dashboard.")
    ] = False,
) -> None:
    if not sys.platform.startswith("linux"):
        raise FleetError("Agent Fleet currently supports Linux (including WSL2).")
    try:
        timeout = float(os.environ.get("READY_TIMEOUT", "20"))
    except ValueError as exc:
        raise FleetError("READY_TIMEOUT must be a positive number of seconds.") from exc
    manager = Manager(Store(home), dry_run=dry_run, ready_timeout=timeout)
    ctx.obj = (manager, instance)
    if ctx.invoked_subcommand is None:
        dashboard(manager, demo)
    elif demo:
        raise FleetError("--demo opens the dashboard; do not combine it with a subcommand.")


def output(messages: list[str], *, preview: bool = False) -> None:
    for message in messages:
        console.print(Text(("[preview] " if preview else "") + message))


@app.command()
def tui(ctx: typer.Context, demo: bool = False) -> None:
    """Open the interactive Textual dashboard."""
    dashboard(ctx.obj[0], demo)


@app.command()
def setup(
    ctx: typer.Context,
    provider: Annotated[str | None, typer.Option(help="cursor, devin, amp or droid.")] = None,
    name: Annotated[str | None, typer.Option(help="New instance name.")] = None,
    source: Annotated[str | None, typer.Option(help="Git URL or local directory.")] = None,
    count: Annotated[int, typer.Option(min=1, max=128)] = 4,
    label: str = "",
    backend: str = "auto",
    outpost: str = "",
    gpu_split: bool = False,
) -> None:
    """Create an instance. Omit flags for the interactive setup form."""
    manager, selected = ctx.obj
    if provider is None:
        if selected:
            output(manager.repair(selected), preview=manager.dry_run)
        else:
            dashboard(manager, setup=True)
        return
    if not source:
        raise FleetError("Provide --source with a Git URL or existing local directory.")
    config = make_instance(
        manager.store,
        name=name or provider,
        provider=provider,
        source=source,
        count=count,
        label=label,
        backend=backend,
        outpost=outpost,
        gpu_split=gpu_split,
        preview=manager.dry_run,
    )
    secret = ""
    if provider == "devin" and not manager.dry_run:
        secret = typer.prompt("Devin Outpost token", hide_input=True)
    output(manager.setup(config, secret=secret), preview=manager.dry_run)


@app.command()
def repair(ctx: typer.Context) -> None:
    """Prepare missing checkouts without replacing configuration or existing data."""
    manager, selected = ctx.obj
    output(manager.repair(selected), preview=manager.dry_run)


@app.command("list")
def list_instances(ctx: typer.Context) -> None:
    """List configured workspaces without running provider probes."""
    manager, selected = ctx.obj
    show_status(manager, selected, probe=False)


def show_status(
    manager: Manager, name: str | None, *, probe: bool, json_output: bool = False
) -> None:
    rows = manager.status(name, probe=probe)
    if json_output:
        # Machine-readable JSON must not be wrapped or colorized by Rich.
        typer.echo(json.dumps([asdict(row) for row in rows], default=str, indent=2))
        return
    if not rows:
        console.print("Your fleet is empty. Run agent-fleet to create your first instance.")
        return
    table = Table(title="Agent Fleet", border_style="blue")
    for column in ("Instance", "Worker", "Provider", "State", "Backend", "Workspace"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            Text(row.instance),
            str(row.index),
            row.provider,
            row.state,
            row.backend,
            Text(str(row.directory)),
        )
    console.print(table)


@app.command()
def status(
    ctx: typer.Context, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Show process and readiness evidence for your fleet."""
    manager, selected = ctx.obj
    show_status(manager, selected, probe=not manager.dry_run, json_output=json_output)


@app.command()
def start(ctx: typer.Context, target: str = "all") -> None:
    """Start all workers, a worker number, or a worker/runner name."""
    manager, selected = ctx.obj
    output(manager.control("start", selected, target), preview=manager.dry_run)


@app.command()
def stop(ctx: typer.Context, target: str = "all") -> None:
    """Stop workers after checking process or service ownership."""
    manager, selected = ctx.obj
    output(manager.control("stop", selected, target), preview=manager.dry_run)


@app.command()
def restart(ctx: typer.Context, target: str = "all") -> None:
    """Stop and start selected workers."""
    manager, selected = ctx.obj
    output(manager.control("restart", selected, target), preview=manager.dry_run)


@app.command()
def add(ctx: typer.Context, count: Annotated[int, typer.Argument(min=1, max=128)] = 1) -> None:
    """Add independent checkouts without overwriting existing workers."""
    manager, selected = ctx.obj
    output(manager.add(selected, count), preview=manager.dry_run)


@app.command()
def logs(
    ctx: typer.Context,
    target: str = "all",
    lines: Annotated[int, typer.Option("--lines", "-n", min=1, max=10000)] = 100,
) -> None:
    """Read recent logs; credentials are redacted."""
    manager, selected = ctx.obj
    console.print(Text(manager.logs(selected, target, lines)))


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check provider dependencies, credentials and workspaces."""
    manager, selected = ctx.obj
    checks = manager.doctor(selected)
    for state, title, detail in checks:
        console.print(
            Text(
                f"{state.upper():4}  {title}: {detail}",
                style="red" if state == "fail" else "green" if state == "pass" else "blue",
            )
        )
    if any(state == "fail" for state, _, _ in checks):
        raise typer.Exit(1)


@app.command()
def credential(ctx: typer.Context) -> None:
    """Save a Cursor API key or Devin token using a masked prompt."""
    manager, selected = ctx.obj
    if manager.dry_run:
        console.print(
            "Preview: would prompt for and save a private credential. Nothing was read or written."
        )
        return
    with manager.store.lock():
        config = manager.store.select(selected)
        if config.provider not in ("cursor", "devin"):
            raise FleetError("Amp and Droid use their CLI login. No Fleet credential is needed.")
        secret = typer.prompt("Credential", hide_input=True)
        manager.store.save_secret(config, secret)
    console.print("Credential saved privately (permissions 600).")


@app.command()
def remove(
    ctx: typer.Context,
    target: str = "all",
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Confirm removal without a prompt.")
    ] = False,
    delete_checkouts: Annotated[
        bool, typer.Option(help="Also delete verified clean, owned checkouts.")
    ] = False,
) -> None:
    """Remove selected workers. Keep all workspaces unless explicitly asked to delete."""
    manager, selected = ctx.obj
    if not manager.dry_run and not yes:
        typer.confirm("Stop and remove these workers from the fleet?", abort=True)
        if delete_checkouts:
            typer.confirm(
                "Also delete clean, Fleet-owned checkouts? This cannot be undone.", abort=True
            )
    output(
        manager.remove(selected, target, delete_checkouts=delete_checkouts), preview=manager.dry_run
    )


def main() -> None:
    try:
        app()
    except FleetError as exc:
        console.print(Text(f"Could not complete: {exc}", style="red"))
        raise SystemExit(1) from None
    except OSError:
        console.print(
            "Could not access a required file or process. Check permissions.", style="red"
        )
        raise SystemExit(1) from None
