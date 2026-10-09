from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from agent_fleet.manager import DirStatus, Manager, WorkerStatus, make_instance
from agent_fleet.model import PROVIDERS, FleetError, Instance, valid_name
from agent_fleet.providers import GUIDANCE

HELP = """
# Your fleet, at a glance

Select an instance on the left and a worker in the table.
Start, stop and restart affect the **selected worker**.
Use the command line with `all` to control every worker in an instance.

**Amp and Droid share one process.** A row action controls the entire runner.
Amp serves several directories; Droid uses one existing directory.
Amp also serves directories you add yourself (`e`) and Git checkouts it
discovers under the roots you choose.

## Keys

| Key | Action |
| --- | --- |
| `n` | Create an instance |
| `s` / `x` | Start / stop selected worker |
| `ctrl+r` | Restart selected worker |
| `a` | Add workers |
| `e` | Manage Amp directories |
| `delete` | Remove selected worker, with confirmation |
| `r` | Refresh readiness |
| `l` / `d` | Logs / diagnostics |
| `p` | Toggle preview mode (no changes) |
| `ctrl+p` | Search commands |
| `q` | Quit; background workers keep running |

## Readiness is evidence, not a promise

**Ready** means Cursor's local `/readyz` passed. **Local ready** means Amp's
runner listing or Droid's local daemon diagnostic passed.
**Unverified** means Devin is alive but has no worker-specific readiness probe.
**Legacy running** means an older Cursor PID record matches a live worker, but
the process is read-only. **Unverified PID** means a legacy record cannot prove
the worker's identity. Use the original Bash manager for these workers.
Always confirm remote task dispatch on the provider's platform.
Process checks update automatically every 10 seconds. Press `r` to refresh
readiness evidence; automatic checks do not repeatedly probe provider endpoints.

## Safe by default

Setup never starts agents automatically. Provider installation and login
remain explicit steps you perform outside this app. Credentials are masked,
stored with permissions `600`, and omitted from configuration and service units.
Removal keeps checkouts by default. Optional deletion requires verified ownership,
a clean checkout (including ignored files), and no unpushed branch commits.
"""


def split_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class FleetHeader(Header):
    def _on_click(self, event: Click) -> None:
        event.stop()


class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            with VerticalScroll():
                yield Markdown(HELP)
            yield Button("Back to fleet", id="close-help", variant="primary")

    @on(Button.Pressed, "#close-help")
    def close_help(self) -> None:
        self.app.pop_screen()


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, message: str, *, destructive: bool = False):
        super().__init__()
        self.heading = title
        self.message = message
        self.destructive = destructive

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.heading, classes="dialog-title")
            yield Static(self.message, id="confirm-message", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Confirm",
                    id="confirm",
                    variant="error" if self.destructive else "primary",
                )

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class AddScreen(ModalScreen[int | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="add-dialog"):
            yield Label("Grow your fleet", classes="dialog-title")
            yield Static("Existing workspaces stay untouched. New workers start only when you ask.")
            yield Input("1", type="integer", id="add-count")
            yield Static("", id="add-error", classes="error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Add workers", id="add-submit", variant="primary")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            try:
                count = int(self.query_one("#add-count", Input).value)
                if not 1 <= count <= 128:
                    raise ValueError
                self.dismiss(count)
            except ValueError:
                self.query_one("#add-error", Static).update("Enter a number between 1 and 128.")

    def action_cancel(self) -> None:
        self.dismiss(None)


class RemoveScreen(ModalScreen[bool | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, config: Instance, target: int):
        super().__init__()
        self.config = config
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="remove-dialog"):
            yield Label("Remove this worker?", classes="dialog-title")
            yield Static(
                f"{self.config.name} / worker {self.target}\n\n"
                "The worker will stop and leave the fleet. Logs and data stay."
                + (
                    "\nAmp's shared runner will stop for all directories."
                    if self.config.shared
                    else ""
                ),
                markup=False,
            )
            if self.config.provider != "droid":
                yield Checkbox("Also delete its clean, Fleet-owned checkout", id="delete-checkout")
                yield Static("Deletion is refused if any changed, ignored or unpushed work exists.")
            else:
                yield Static("Your Droid working directory is never deleted.")
            with Horizontal(classes="dialog-actions"):
                yield Button("Keep worker", id="cancel")
                yield Button("Remove worker", id="remove-submit", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            delete = self.query("#delete-checkout")
            self.dismiss(bool(delete and delete.first(Checkbox).value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class DirsScreen(ModalScreen[tuple[str, dict[str, Any]] | None]):
    """Manage the directories an Amp runner serves, live when it runs."""

    BINDINGS = [("escape", "cancel", "Close")]

    def __init__(self, config: Instance):
        super().__init__()
        self.config = config
        self.rows: list[DirStatus] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dirs-dialog"):
            yield Label(f"{self.config.name} · served directories", classes="dialog-title")
            yield Static(
                "Directories you serve join a running runner at once. Discovery roots apply "
                "when it starts. Directories added outside Agent Fleet are dropped at the "
                "next start.",
                classes="muted",
            )
            yield DataTable(id="dir-list", cursor_type="row")
            yield Input(placeholder="/absolute/path", id="dir-path")
            with Horizontal(classes="dialog-actions"):
                yield Button("Close", id="cancel")
                yield Button("Remove selected", id="dir-remove")
                yield Button("Discover here", id="dir-discover")
                yield Button("Serve directory", id="dir-add", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#dir-list", DataTable)
        table.add_columns("Kind", "Served", "Path")
        self.refresh_rows()
        self.query_one("#dir-path", Input).focus()

    @work(exclusive=True, group="dir-list")
    async def refresh_rows(self) -> None:
        try:
            rows = await asyncio.to_thread(self.app.manager.list_dirs, self.config.name)
        except (FleetError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.rows = rows
        table = self.query_one("#dir-list", DataTable)
        table.clear()
        for row in rows:
            served = "—" if row.live is None else ("yes" if row.live else "not yet")
            table.add_row(row.kind, served, row.path)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id in ("dir-add", "dir-discover"):
            path = self.query_one("#dir-path", Input).value.strip()
            if not path:
                self.notify("Enter an absolute directory first.", severity="warning")
                return
            self.dismiss(
                ("add_dirs", {"paths": [path]})
                if event.button.id == "dir-add"
                else ("edit_discovery", {"add": [path]})
            )
            return
        table = self.query_one("#dir-list", DataTable)
        row = self.rows[table.cursor_row] if 0 <= table.cursor_row < len(self.rows) else None
        if row is None:
            self.notify("Select a directory first.", severity="warning")
        elif row.kind == "discovered":
            self.notify("Found by Amp discovery; remove its discovery root instead.")
        elif row.kind == "runtime":
            self.notify(
                "Added outside Agent Fleet; it is dropped at the next start.", severity="warning"
            )
        else:
            self.dismiss(("remove_dirs", {"paths": [row.path]}))

    def action_cancel(self) -> None:
        self.dismiss(None)


class SetupScreen(ModalScreen[tuple[dict[str, Any], str, bool] | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, preview: bool = False):
        super().__init__()
        self.preview = preview
        self._current_provider: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-dialog"):
            yield Label("A new home for your agents", classes="dialog-title")
            yield Static(
                "01  Choose a provider    /    02  Workspace    /    03  Review", classes="muted"
            )
            with VerticalScroll(id="setup-fields"):
                yield Label("Provider")
                yield Select(
                    [(p.capitalize(), p) for p in PROVIDERS],
                    value="cursor",
                    allow_blank=False,
                    id="provider",
                )
                yield Static(GUIDANCE["cursor"], id="provider-guidance", classes="muted")
                yield Label("Instance name")
                yield Input(placeholder="e.g. cursor-personal", id="instance-name")
                yield Label("Runner display name (optional)")
                yield Input(placeholder="Stable identity gets a unique suffix", id="runner-label")
                yield Label("Git URL or existing local directory", id="source-label")
                yield Input(placeholder="git@github.com:you/project.git", id="source")
                with Horizontal(id="capacity-row"):
                    with Vertical():
                        yield Label("Workers / checkouts")
                        yield Input("4", type="integer", id="worker-count")
                    with Vertical():
                        yield Label("Background backend")
                        yield Select(
                            [
                                ("Auto-detect", "auto"),
                                ("Detached process", "process"),
                                ("systemd user service", "systemd"),
                            ],
                            value="auto",
                            allow_blank=False,
                            id="backend",
                        )
                with Vertical(id="dirs-fields"):
                    yield Label("Also serve these directories (comma-separated)")
                    yield Input(
                        placeholder="/home/you/worktrees/one, /home/you/worktrees/two",
                        id="extra-dirs",
                    )
                    yield Label("Let Amp discover Git checkouts under (comma-separated)")
                    yield Input(
                        placeholder="/home/you/code · leave empty to disable", id="discover-dirs"
                    )
                    with Horizontal(id="discover-row"):
                        with Vertical():
                            yield Label("Discovery depth (1–10)")
                            yield Input(placeholder="Amp default", id="discover-depth")
                        with Vertical():
                            yield Label("Discovery excludes (comma-separated)")
                            yield Input(placeholder="dotfiles, work/legacy", id="discover-exclude")
                yield Checkbox("Split NVIDIA GPUs between workers", id="gpu-split")
                with Vertical(id="outpost-fields"):
                    yield Label("Devin Outpost name")
                    yield Input(placeholder="Your existing Outpost", id="outpost")
                with Vertical(id="credential-fields"):
                    yield Label("Credential (optional for Cursor CLI login)", id="credential-label")
                    yield Input(
                        placeholder="Stored privately, never shown in previews",
                        password=True,
                        id="secret",
                    )
                    yield Static(
                        "Credentials are cleared when you switch providers.",
                        classes="muted",
                    )
                yield Checkbox(
                    "Preview only, do not create anything", value=self.preview, id="setup-preview"
                )
                yield Static("", id="setup-error", classes="error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Review setup", id="setup-submit", variant="primary")

    def on_mount(self) -> None:
        self.update_provider("cursor")
        self.query_one("#instance-name", Input).focus()

    @on(Select.Changed, "#provider")
    def provider_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.update_provider(event.value)

    def update_provider(self, provider: str) -> None:
        if self._current_provider is not None and provider != self._current_provider:
            self.query_one("#secret", Input).value = ""
            self.query_one("#outpost", Input).value = ""
        self._current_provider = provider
        self.query_one("#provider-guidance", Static).update(GUIDANCE[provider])
        self.query_one("#instance-name", Input).placeholder = f"e.g. {provider}-personal"
        self.query_one("#outpost-fields").display = provider == "devin"
        self.query_one("#credential-fields").display = provider in ("cursor", "devin")
        self.query_one("#dirs-fields").display = provider == "amp"
        self.query_one("#credential-label", Label).update(
            "Outpost token (required)"
            if provider == "devin"
            else "API key (optional if CLI is logged in)"
        )
        self.query_one("#source-label", Label).update(
            "Git URL to clone, or existing local directory (absolute path)"
            if provider == "droid"
            else "Git URL or existing local Git directory"
        )
        self.query_one("#source", Input).placeholder = (
            "https://github.com/you/project.git or /path/to/project"
            if provider == "droid"
            else "git@github.com:you/project.git"
        )
        self.query_one("#worker-count", Input).disabled = provider == "droid"
        self.query_one("#worker-count", Input).value = "1" if provider == "droid" else "4"
        self.query_one("#gpu-split", Checkbox).disabled = provider in ("amp", "droid")
        if provider in ("amp", "droid"):
            self.query_one("#gpu-split", Checkbox).value = False

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        try:
            name = valid_name(self.query_one("#instance-name", Input).value.strip())
            count = int(self.query_one("#worker-count", Input).value)
            if not 1 <= count <= 128:
                raise FleetError("Use between 1 and 128 workers.")
            source = self.query_one("#source", Input).value.strip()
            if not source:
                raise FleetError("Enter a repository or local directory.")
            provider = str(self.query_one("#provider", Select).value)
            payload = {
                "name": name,
                "provider": provider,
                "source": source,
                "count": count,
                "label": self.query_one("#runner-label", Input).value.strip(),
                "backend": str(self.query_one("#backend", Select).value),
                "outpost": (
                    self.query_one("#outpost", Input).value.strip() if provider == "devin" else ""
                ),
                "gpu_split": self.query_one("#gpu-split", Checkbox).value,
            }
            if provider == "amp":
                depth = self.query_one("#discover-depth", Input).value.strip()
                if depth and not depth.isdigit():
                    raise FleetError("Discovery depth must be a whole number between 1 and 10.")
                payload |= {
                    "extra_dirs": split_values(self.query_one("#extra-dirs", Input).value),
                    "discover_dirs": split_values(self.query_one("#discover-dirs", Input).value),
                    "discover_depth": int(depth) if depth else 0,
                    "discover_excludes": split_values(
                        self.query_one("#discover-exclude", Input).value
                    ),
                }
            if provider in ("amp", "droid"):
                # Validate paths and the clone plan before dismissing the user's form.
                make_instance(self.app.manager.store, **payload, preview=True)
            preview = self.query_one("#setup-preview", Checkbox).value
            secret = self.query_one("#secret", Input).value
            if payload["provider"] == "devin" and not secret and not preview:
                raise FleetError("Enter the Outpost token, or choose preview only.")
            if payload["provider"] not in ("cursor", "devin"):
                secret = ""
            self.dismiss((payload, secret, preview))
        except (FleetError, ValueError) as exc:
            self.query_one("#setup-error", Static).update(
                str(exc) if isinstance(exc, FleetError) else "Worker count must be a whole number."
            )

    def action_cancel(self) -> None:
        self.dismiss(None)


class FleetApp(App):
    TITLE = "Agent Fleet"
    SUB_TITLE = "Your agents. One calm control room."
    ENABLE_COMMAND_PALETTE = True
    CSS_PATH = "fleet.tcss"
    BINDINGS = [
        Binding("n", "new", "New instance"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+r", "restart", "Restart", show=False),
        Binding("a", "add", "Add workers", show=False),
        Binding("e", "dirs", "Directories", show=False),
        Binding("delete", "remove", "Remove worker", show=False),
        Binding("l", "logs", "Logs", show=False),
        Binding("d", "doctor", "Diagnostics", show=False),
        Binding("p", "preview", "Toggle preview", show=False),
    ]

    def __init__(self, manager: Manager, *, demo: bool = False, setup: bool = False):
        super().__init__()
        self.manager = manager
        self.demo = demo
        self.setup_on_launch = setup
        self.configs: dict[str, Instance] = {}
        self.selected: str | None = None
        self.rows: list[WorkerStatus] = []
        self.busy = False
        self.refreshing = False
        self._refresh_requested = False
        self._last_probe_at: datetime | None = None
        self._loaded_signature: tuple = ()

    def compose(self) -> ComposeResult:
        yield FleetHeader(show_clock=True)
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Label("FLEET / INSTANCES", id="sidebar-title")
                yield ListView(id="instances")
                yield Button("+ New instance", id="new-instance", variant="primary")
                yield Static("No Bash. No clutter.\nJust your fleet.", id="sidebar-note")
            with Vertical(id="main"):
                with Horizontal(id="metrics"):
                    yield Static("0\nINSTANCES", id="metric-instances", classes="metric")
                    yield Static("0\nWORKSPACES", id="metric-workers", classes="metric")
                    yield Static("0\nLIVE PROCESSES", id="metric-ready", classes="metric")
                with Horizontal(id="section-heading"):
                    yield Label("Your control room", id="fleet-heading")
                    yield Select(
                        [], prompt="Select instance", allow_blank=True, id="compact-instance"
                    )
                    yield Checkbox("Preview mode", value=self.manager.dry_run, id="preview")
                    yield Button("+ New", id="compact-new-instance", variant="primary")
                yield Static(
                    "Select a worker. Shared Amp/Droid runners are controlled as one process.",
                    id="context",
                    markup=False,
                )
                yield DataTable(id="workers", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="actions"):
                    yield Button("Start", id="start-worker", variant="success")
                    yield Button("Stop", id="stop-worker")
                    yield Button("Restart", id="restart-worker")
                    yield Button("Add", id="add-workers")
                    yield Button("Dirs", id="dirs")
                    yield Button("Remove", id="remove-worker", variant="error")
                with TabbedContent(id="details"):
                    with TabPane("Activity", id="activity-tab"):
                        yield RichLog(id="activity", wrap=True, markup=False)
                    with TabPane("Logs", id="logs-tab"):
                        yield RichLog(id="logs", wrap=True, markup=False)
                    with TabPane("Health", id="health-tab"):
                        yield RichLog(id="health", wrap=True, markup=False)
                yield Static("Loading your fleet…", id="statusbar", markup=False)
        yield Static(
            "n New  s Start  x Stop  r Refresh  ? Help  q Quit",
            id="compact-hints",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#workers", DataTable).add_columns(
            "Worker", "Provider", "State", "Workspace"
        )
        self.activity("Welcome. Create an instance with [n], or select one to get started.")
        if self.demo:
            self.activity("DEMO · Read-only sample fleet. No commands or network probes will run.")
        self.action_refresh()
        if self.setup_on_launch:
            self.call_after_refresh(self.action_new)
        self.set_interval(10, self._refresh_timer)

    def on_resize(self, event) -> None:
        for screen in self.screen_stack:
            screen.set_class(event.size.width < 100, "compact")
            screen.set_class(event.size.width < 80 or event.size.height < 25, "small")
        if self.configs:
            self.populate_table()

    def activity(self, message: str) -> None:
        self.query_one("#activity", RichLog).write(Text(message))

    def _refresh_timer(self) -> None:
        if not self.busy and len(self.screen_stack) == 1:
            self.action_refresh(probe=False, manual=False)

    def _sample(self) -> tuple[list[Instance], list[WorkerStatus]]:
        configs = [
            Instance(
                "cursor-lab",
                "cursor",
                "git@github.com:you/atlas.git",
                "atlas",
                "studio-cursor-7c4a",
                3,
                (1, 2, 3),
            ),
            Instance(
                "amp-studio",
                "amp",
                "git@github.com:you/orbit.git",
                "orbit",
                "studio-amp-91bf",
                2,
                (1, 2),
            ),
            Instance(
                "devin-build",
                "devin",
                "git@github.com:you/signal.git",
                "signal",
                "studio-devin-2e18",
                1,
                (1,),
                outpost_name="build-room",
            ),
        ]
        rows = []
        for config in configs:
            for i in config.active_workers:
                state = (
                    "local ready"
                    if config.provider == "amp"
                    else (
                        "unverified"
                        if config.provider == "devin"
                        else "ready"
                        if i < 3
                        else "stopped"
                    )
                )
                rows.append(
                    WorkerStatus(
                        config.name,
                        config.provider,
                        i,
                        config.worker_name(i),
                        state,
                        Path(
                            f"/home/you/.agent-fleet/instances/{config.name}/checkouts/{config.repo_name}-w{i}"
                        ),
                        "systemd",
                    )
                )
        return configs, rows

    @work(group="refresh")
    async def action_refresh(self, *, probe: bool = True, manual: bool = True) -> None:
        statusbar = self.query_one("#statusbar", Static)
        if self.busy:
            if manual:
                statusbar.update("Action in progress; fleet status will refresh when it completes.")
            return
        if self.refreshing:
            if manual:
                self._refresh_requested = True
                statusbar.update("Refresh queued; waiting for the current status check…")
            return
        self.refreshing = True
        probe_readiness = probe and not self.manager.dry_run and not self.demo
        statusbar.update(
            "Refreshing processes and readiness…" if probe_readiness else "Checking process status…"
        )
        try:
            if self.demo:
                configs, rows = self._sample()
            else:
                configs = await asyncio.to_thread(self.manager.store.all)
                rows = await asyncio.to_thread(self.manager.status, probe=probe_readiness)
                if not probe_readiness:
                    rows = self._preserve_cached_readiness(rows)
            if self.busy:
                return
            self.configs = {c.name: c for c in configs}
            if self.selected not in self.configs:
                self.selected = configs[0].name if configs else None
            signature = tuple((c.name, c.provider, len(c.active_workers)) for c in configs)
            listing = self.query_one("#instances", ListView)
            if signature != self._loaded_signature:
                self._loaded_signature = signature
                await listing.clear()
                for config in configs:
                    await listing.append(
                        ListItem(
                            Label(
                                f"{config.name}\n{config.provider.upper()} · "
                                f"{len(config.active_workers)} workspaces"
                            ),
                            name=config.name,
                        )
                    )
            if configs:
                listing.index = [c.name for c in configs].index(self.selected)
            self._sync_instance_picker()
            self.rows = rows
            self.populate_table()
            self.query_one("#metric-instances", Static).update(f"{len(configs)}\nINSTANCES")
            self.query_one("#metric-workers", Static).update(f"{len(rows)}\nWORKSPACES")
            process_keys = {
                (row.instance, 0 if self.configs[row.instance].shared else row.index)
                for row in rows
                if row.state not in ("stopped", "unverified PID")
            }
            self.query_one("#metric-ready", Static).update(f"{len(process_keys)}\nLIVE PROCESSES")
            if probe_readiness:
                self._last_probe_at = datetime.now()
            statusbar.update(self._status_message())
        except (FleetError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            statusbar.update("Could not load fleet. Fix configuration, then press r.")
        finally:
            self.refreshing = False
            if self._refresh_requested and not self.busy:
                self._refresh_requested = False
                self.call_after_refresh(self.action_refresh)

    def _preserve_cached_readiness(self, rows: list[WorkerStatus]) -> list[WorkerStatus]:
        readiness_states = {"ready", "local ready", "unverified", "starting"}
        previous = {(row.instance, row.index): row.state for row in self.rows}
        return [
            replace(row, state=previous[(row.instance, row.index)])
            if row.state == "running"
            and previous.get((row.instance, row.index)) in readiness_states
            else row
            for row in rows
        ]

    def _sync_instance_picker(self) -> None:
        picker = self.query_one("#compact-instance", Select)
        picker.set_options(
            [
                (f"{config.name} · {config.provider.upper()}", config.name)
                for config in self.configs.values()
            ]
        )
        if self.selected in self.configs:
            picker.value = self.selected
        else:
            picker.clear()

    def _status_message(self) -> str:
        if self.demo:
            return "DEMO / READ ONLY"
        if self.manager.dry_run:
            if self.screen.has_class("small"):
                return "PREVIEW / No changes · readiness probes disabled"
            return "PREVIEW / No changes · process checks every 10s · readiness probes disabled"
        readiness = (
            f"readiness checked {self._last_probe_at:%H:%M}"
            if self._last_probe_at is not None
            else "press r to check readiness"
        )
        if self.screen.has_class("small"):
            return f"Live checks every 10s · {readiness}"
        return f"Fleet home: {self.manager.store.home} · Live checks every 10s · {readiness}"

    def populate_table(self) -> None:
        table = self.query_one("#workers", DataTable)
        old_row = table.cursor_row
        table.clear()
        colors = {
            "ready": "#76ddab",
            "local ready": "#76ddab",
            "unverified": "#eccb7a",
            "starting": "#eccb7a",
            "stopped": "#8c99ae",
            "running": "#a6c9ff",
            "legacy running": "#eccb7a",
            "unverified PID": "#f3aab1",
        }
        for row in self.visible_rows():
            workspace = (
                row.directory.name if self.screen.has_class("compact") else str(row.directory)
            )
            table.add_row(
                f"{self.configs[row.instance].repo_name} / w{row.index}",
                row.provider.capitalize(),
                Text(row.state, style=colors.get(row.state, "white")),
                workspace,
                key=str(row.index),
            )
        if table.row_count:
            table.move_cursor(row=min(old_row, table.row_count - 1))
        self.query_one("#fleet-heading", Label).update(self.selected or "Your control room")
        self.update_controls()

    @on(DataTable.RowHighlighted, "#workers")
    def worker_highlighted(self) -> None:
        self.update_controls()

    def update_controls(self) -> None:
        rows = self.visible_rows()
        index = self.query_one("#workers", DataTable).cursor_row
        row = rows[index] if 0 <= index < len(rows) else None
        readonly = row is not None and row.state in ("legacy running", "unverified PID")
        for selector in ("#start-worker", "#stop-worker", "#restart-worker", "#remove-worker"):
            button = self.query_one(selector, Button)
            button.disabled = row is None or readonly
            button.tooltip = (
                "Legacy worker: read-only. Use the original Bash manager." if readonly else None
            )
        config = self.configs.get(self.selected)
        dirs_button = self.query_one("#dirs", Button)
        dirs_button.disabled = config is None or config.provider != "amp" or readonly
        dirs_button.tooltip = (
            "Amp only: serve extra directories or discover Git checkouts."
            if dirs_button.disabled
            else None
        )
        if readonly:
            self.query_one("#context", Static).update(
                "Legacy PID detected · read-only. Use the original Bash manager."
            )
        else:
            config = self.configs.get(self.selected)
            self.query_one("#context", Static).update(
                f"{config.runner_id} · "
                + (
                    "One shared process; a row action affects the whole runner."
                    + (
                        f" {len(config.extra_dirs)} extra directories, "
                        f"{len(config.discover_dirs)} discovery roots."
                        if config.provider == "amp"
                        else ""
                    )
                    if config.shared
                    else "Independent worker processes."
                )
                if config is not None
                else "Your fleet is empty. Press n to create your first instance. Press ? for help."
            )

    def visible_rows(self) -> list[WorkerStatus]:
        return [row for row in self.rows if row.instance == self.selected]

    def selected_worker(self) -> WorkerStatus | None:
        rows = self.visible_rows()
        cursor = self.query_one("#workers", DataTable).cursor_row
        if not rows or cursor >= len(rows):
            self.notify("Create or select a worker first.", severity="warning")
            return None
        return rows[cursor]

    @on(ListView.Selected, "#instances")
    def instance_selected(self, event: ListView.Selected) -> None:
        self.selected = event.item.name
        self.query_one("#compact-instance", Select).value = self.selected
        self.populate_table()

    @on(Select.Changed, "#compact-instance")
    def compact_instance_changed(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str) or event.value not in self.configs:
            return
        if event.value == self.selected:
            return
        self.selected = event.value
        listing = self.query_one("#instances", ListView)
        listing.index = [config.name for config in self.configs.values()].index(self.selected)
        self.populate_table()

    @on(Checkbox.Changed, "#preview")
    def preview_changed(self, event: Checkbox.Changed) -> None:
        self.manager.dry_run = event.value
        self.activity(
            "Preview mode on: no configuration, process or remote changes."
            if event.value
            else "Preview mode off: confirmed actions will make changes."
        )
        self.action_refresh()

    def action_preview(self) -> None:
        checkbox = self.query_one("#preview", Checkbox)
        checkbox.value = not checkbox.value

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_new(self) -> None:
        if self.demo:
            self.notify("Demo is read-only. Launch without --demo to create your fleet.")
            return
        self.push_screen(SetupScreen(preview=self.manager.dry_run), self._setup_result)

    def _setup_result(self, result) -> None:
        if result is None:
            return
        payload, secret, preview = result
        backend_names = {
            "auto": "Auto-detect",
            "process": "Detached process",
            "systemd": "systemd user service",
        }
        summary_lines = [
            f"Provider: {payload['provider'].capitalize()}",
            f"Instance: {payload['name']}",
            f"Workspace source: {payload['source']}",
            f"Workers / checkouts: {payload['count']}",
            f"Runner label: {payload.get('label') or 'Default for this host'}",
            f"Backend preference: {backend_names[payload['backend']]}",
            f"GPU split: {'enabled' if payload.get('gpu_split') else 'off'}",
        ]
        if payload["provider"] == "devin":
            summary_lines.append(f"Outpost: {payload['outpost']}")
        if payload["provider"] == "amp":
            summary_lines.extend(
                (
                    "Extra directories: " + (", ".join(payload.get("extra_dirs", [])) or "none"),
                    "Discovery roots: " + (", ".join(payload.get("discover_dirs", [])) or "none"),
                    "Discovery depth: "
                    + (
                        str(payload["discover_depth"])
                        if payload.get("discover_depth")
                        else "Amp default"
                    ),
                    "Discovery excludes: "
                    + (", ".join(payload.get("discover_excludes", [])) or "none"),
                )
            )
        summary_lines.extend(
            (
                "",
                "Preview only. No files, credentials or processes will change."
                if preview
                else "Prepare workspaces and save configuration. Nothing starts automatically.",
            )
        )
        summary = "\n".join(summary_lines)
        if payload["provider"] == "droid":
            try:
                config = make_instance(self.manager.store, **payload, preview=True)
            except FleetError as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            summary += f"\n\nLocal workspace: {config.droid_dir}"
            if config.repo_url:
                summary += "\nClone first; save the instance only if cloning succeeds."

        def confirmed(yes: bool) -> None:
            if yes:
                self._perform("setup", payload=payload, secret=secret, preview=preview)

        self.push_screen(ConfirmScreen("Review your new instance", summary), confirmed)

    def _control(self, action: str) -> None:
        if self.demo:
            self.notify("Demo is read-only. No processes were changed.")
            return
        row = self.selected_worker()
        if row is None:
            return
        if row.state in ("legacy running", "unverified PID"):
            self.notify(
                "Legacy worker is read-only. Use the original Bash manager.", severity="warning"
            )
            return
        preview = self.manager.dry_run
        message = f"{action.capitalize()} {row.instance} / worker {row.index}?"
        if self.configs[row.instance].shared:
            message += "\nThis controls the shared runner for ALL its directories."
        if preview:
            message += "\nPreview only, no changes."
        self.push_screen(
            ConfirmScreen(action.capitalize() + " worker", message),
            lambda yes: (
                self._perform(action, name=row.instance, target=str(row.index), preview=preview)
                if yes
                else None
            ),
        )

    def action_start(self) -> None:
        self._control("start")

    def action_stop(self) -> None:
        self._control("stop")

    def action_restart(self) -> None:
        self._control("restart")

    def action_add(self) -> None:
        if self.demo:
            self.notify("Demo is read-only.")
            return
        if self.selected is None:
            self.notify("Select an instance first.")
            return
        name = self.selected
        preview = self.manager.dry_run
        self.push_screen(
            AddScreen(),
            lambda count: (
                self._perform("add", name=name, count=count, preview=preview) if count else None
            ),
        )

    def action_dirs(self) -> None:
        if self.demo:
            self.notify("Demo is read-only.")
            return
        config = self.configs.get(self.selected)
        if config is None:
            self.notify("Select an instance first.")
            return
        if config.provider != "amp":
            self.notify("Only Amp instances manage extra directories.", severity="warning")
            return

        def done(request) -> None:
            if request:
                action, kwargs = request
                self._perform(action, name=config.name, **kwargs)

        self.push_screen(DirsScreen(config), done)

    def action_remove(self) -> None:
        if self.demo:
            self.notify("Demo is read-only.")
            return
        row = self.selected_worker()
        if row:
            if row.state in ("legacy running", "unverified PID"):
                self.notify(
                    "Legacy worker is read-only. Use the original Bash manager.", severity="warning"
                )
                return
            preview = self.manager.dry_run
            self.push_screen(
                RemoveScreen(self.configs[row.instance], row.index),
                lambda delete: (
                    self._perform(
                        "remove",
                        name=row.instance,
                        target=str(row.index),
                        delete_checkouts=delete,
                        preview=preview,
                    )
                    if delete is not None
                    else None
                ),
            )

    @on(TabbedContent.TabActivated, "#details")
    def details_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "logs-tab":
            self._load_logs()
        elif event.pane.id == "health-tab":
            self._load_doctor()

    def action_logs(self) -> None:
        details = self.query_one("#details", TabbedContent)
        if details.active == "logs-tab":
            self._load_logs()
        else:
            details.active = "logs-tab"

    @work(exclusive=True, group="read-details")
    async def _load_logs(self) -> None:
        row = self.selected_worker()
        if row is None:
            return
        log = self.query_one("#logs", RichLog)
        log.clear()
        try:
            text = (
                "09:41:02  Worker started\n09:41:03  Connected to local management endpoint\n"
                "09:41:03  Ready. Confirm remote dispatch on-platform.\n"
                "09:42:11  Waiting for your next task."
                if self.demo
                else await asyncio.to_thread(
                    self.manager.logs,
                    row.instance,
                    str(row.index),
                )
            )
            log.write(Text(text))
        except (FleetError, OSError) as exc:
            log.write(Text(str(exc), style="red"))

    def action_doctor(self) -> None:
        details = self.query_one("#details", TabbedContent)
        if details.active == "health-tab":
            self._load_doctor()
        else:
            details.active = "health-tab"

    @work(exclusive=True, group="read-details")
    async def _load_doctor(self) -> None:
        log = self.query_one("#health", RichLog)
        log.clear()
        try:
            checks = (
                [("pass", "Demo", "Sample diagnostics; no network or provider calls.")]
                if self.demo
                else await asyncio.to_thread(self.manager.doctor, self.selected)
            )
            for state, title, detail in checks:
                log.write(
                    Text(
                        f"{state.upper():4}  {title}: {detail}",
                        style="red"
                        if state == "fail"
                        else "#76ddab"
                        if state == "pass"
                        else "#a6c9ff",
                    )
                )
        except (FleetError, OSError) as exc:
            log.write(Text(str(exc), style="red"))

    @work(group="mutation")
    async def _perform(self, action: str, **kwargs) -> None:
        if self.busy:
            self.notify("An action is already running. Please wait.", severity="warning")
            return
        self.busy = True
        preview = kwargs.setdefault("preview", self.manager.dry_run)
        clone_cancel = Event()
        kwargs["_clone_cancel"] = clone_cancel
        self.query_one("#details", TabbedContent).active = "activity-tab"
        self.query_one("#statusbar", Static).update(f"Working: {action}…")
        self.activity(f"{action.capitalize()} requested.")
        try:
            messages = await asyncio.to_thread(self._execute, action, kwargs)
            if action == "setup" and not preview:
                self.selected = kwargs["payload"]["name"]
            for message in messages:
                self.activity(message)
            self.notify("Preview complete." if preview else f"{action.capitalize()} complete.")
        except asyncio.CancelledError:
            clone_cancel.set()
            raise
        except (FleetError, OSError) as exc:
            self.activity(f"Could not complete action: {exc}")
            self.notify(str(exc), severity="error", timeout=8)
        finally:
            self.busy = False
            self._refresh_requested = False
            self.action_refresh()

    def _execute(self, action: str, kwargs) -> list[str]:
        # Snapshot preview state so toggling the UI cannot alter an action halfway through.
        manager = Manager(
            self.manager.store,
            dry_run=kwargs.pop("preview"),
            ready_timeout=self.manager.ready_timeout,
            clone_timeout=self.manager.clone_timeout,
            progress=lambda message: self.call_from_thread(self.activity, message),
            clone_cancel=kwargs.pop("_clone_cancel"),
        )
        if action == "setup":
            payload = kwargs["payload"]
            preview = manager.dry_run
            config = make_instance(manager.store, **payload, preview=preview)
            return manager.setup(config, secret="" if preview else kwargs["secret"])
        if action in ("start", "stop", "restart"):
            return manager.control(action, **kwargs)
        if action == "add":
            return manager.add(**kwargs)
        if action == "add_dirs":
            return manager.add_dirs(**kwargs)
        if action == "remove_dirs":
            return manager.remove_dirs(**kwargs)
        if action == "edit_discovery":
            return manager.edit_discovery(**kwargs)
        return manager.remove(**kwargs)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "new-instance": self.action_new,
            "compact-new-instance": self.action_new,
            "start-worker": self.action_start,
            "stop-worker": self.action_stop,
            "restart-worker": self.action_restart,
            "add-workers": self.action_add,
            "dirs": self.action_dirs,
            "remove-worker": self.action_remove,
        }
        if callback := actions.get(event.button.id):
            callback()
