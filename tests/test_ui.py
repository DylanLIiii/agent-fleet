import asyncio
from threading import Event

import pytest
from textual.command import CommandPalette
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    RichLog,
    Select,
    Static,
    TabbedContent,
)

from agent_fleet.manager import Manager
from agent_fleet.ui import ConfirmScreen, DirsScreen, FleetApp, HelpScreen, SetupScreen


@pytest.mark.parametrize("size", [(120, 40), (90, 28), (70, 22)])
async def test_demo_layout_keyboard_and_read_only(store, size):
    app = FleetApp(Manager(store), demo=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.query_one("#workers", DataTable).row_count == 3
        assert app.selected == "cursor-lab"
        statusbar = app.query_one("#statusbar", Static)
        assert statusbar.region.bottom <= size[1] - 1
        assert app.query_one("#actions").region.bottom <= size[1] - 1
        assert app.query_one("#actions").region.right <= size[0]
        if size[0] < 80:
            assert app.query_one("#sidebar").region.width == 0
            picker = app.query_one("#compact-instance", Select)
            assert picker.region.width > 0
            assert app.query_one("#compact-hints").region.right <= size[0]
            assert app.query_one("#workers", DataTable).get_row_at(0)[3] == "atlas-w1"
            picker.value = "amp-studio"
            await pilot.pause()
            assert app.selected == "amp-studio"
        await pilot.press("l")
        await pilot.pause()
        assert app.query_one("#details", TabbedContent).active == "logs-tab"
        await pilot.press("d")
        await pilot.pause()
        assert app.query_one("#details", TabbedContent).active == "health-tab"
        await pilot.press("s", "n", "delete")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert not store.home.exists()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert len(app.screen_stack) == 1


async def test_empty_state_setup_provider_fields_and_cancel(store):
    app = FleetApp(Manager(store))
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        assert "empty" in str(app.query_one("#context", Static).render())
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        app.screen.query_one("#provider", Select).value = "droid"
        await pilot.pause()
        assert app.screen.query_one("#worker-count", Input).value == "1"
        assert app.screen.query_one("#worker-count", Input).disabled
        assert not app.screen.query_one("#credential-fields").display
        await pilot.press("escape")
        assert not store.home.exists()


async def test_small_layout_keeps_new_instance_button_available(store):
    app = FleetApp(Manager(store))
    async with app.run_test(size=(70, 22)) as pilot:
        await pilot.pause()
        assert await pilot.click("#compact-new-instance")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)


async def test_setup_preview_review_and_no_writes(store, source):
    app = FleetApp(Manager(store, dry_run=True))
    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#instance-name", Input).value = "preview"
        screen.query_one("#source", Input).value = str(source)
        assert screen.query_one("#setup-preview", Checkbox).value
        await pilot.click("#setup-submit")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm")
        await pilot.pause(0.5)
        assert not store.home.exists()
        assert not app.busy


async def test_setup_review_includes_provider_specific_settings(store):
    app = FleetApp(Manager(store))
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#provider", Select).value = "devin"
        await pilot.pause()
        screen.query_one("#instance-name", Input).value = "devin-preview"
        screen.query_one("#runner-label", Input).value = "team-runner"
        screen.query_one("#source", Input).value = "https://example.com/project.git"
        screen.query_one("#outpost", Input).value = "build-room"
        screen.query_one("#secret", Input).value = "must-not-appear-in-review"
        screen.query_one("#backend", Select).value = "process"
        screen.query_one("#gpu-split", Checkbox).value = True

        await pilot.click("#setup-submit")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmScreen)
        summary = str(app.screen.query_one("#confirm-message", Static).render())
        assert "Runner label: team-runner" in summary
        assert "Outpost: build-room" in summary
        assert "Backend preference: Detached process" in summary
        assert "GPU split: enabled" in summary
        assert "must-not-appear-in-review" not in summary


async def test_amp_setup_review_includes_discovery_options(store, tmp_path):
    root = tmp_path / "discover"
    root.mkdir()
    app = FleetApp(Manager(store))
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#provider", Select).value = "amp"
        await pilot.pause()
        screen.query_one("#instance-name", Input).value = "amp-preview"
        screen.query_one("#source", Input).value = "https://example.com/project.git"
        screen.query_one("#discover-dirs", Input).value = str(root)
        screen.query_one("#discover-depth", Input).value = "3"
        screen.query_one("#discover-exclude", Input).value = "dotfiles, work/legacy"

        await pilot.click("#setup-submit")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmScreen)
        summary = str(app.screen.query_one("#confirm-message", Static).render())
        assert f"Discovery roots: {root}" in summary
        assert "Discovery depth: 3" in summary
        assert "Discovery excludes: dotfiles, work/legacy" in summary


async def test_start_confirmation_cancel_preserves_state(manager, config):
    manager.setup(config)
    app = FleetApp(manager)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        assert not manager.running(config, 1)


async def test_setup_command_opens_form(store):
    app = FleetApp(Manager(store), setup=True)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        await pilot.press("escape")
        assert not store.home.exists()


async def test_confirmed_preview_does_not_turn_into_real_action(manager, config, monkeypatch):
    manager.setup(config)
    manager.dry_run = True
    monkeypatch.setattr(
        "agent_fleet.manager.binary", lambda _: pytest.fail("Real start in preview")
    )
    app = FleetApp(manager)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        manager.dry_run = False
        await pilot.click("#confirm")
        await pilot.pause(0.5)
        assert not manager.running(config, 1)


async def test_header_click_never_opens_palette_but_keyboard_does(store):
    app = FleetApp(Manager(store), demo=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for offset in ((0, 0), (1, 0), (25, 0), (60, 0)):
            await pilot.click("Header", offset=offset)
            await pilot.pause()
            assert not isinstance(app.screen, CommandPalette)
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)


@pytest.mark.parametrize("size", [(120, 40), (90, 28), (70, 22)])
async def test_mouse_controls_keep_target_after_refresh(manager, config, size):
    manager.setup(config)
    app = FleetApp(manager)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for target in ("#start-worker", "#stop-worker", "#restart-worker"):
            for _ in range(2):
                app.action_refresh()
                await pilot.pause()
                assert await pilot.click(target)
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen)
                await pilot.press("escape")
        assert not manager.running(config, 1)


async def test_legacy_worker_actions_are_read_only_in_dashboard(legacy_cursor):
    manager, config, record, process = legacy_cursor
    before = record.read_bytes()
    app = FleetApp(manager)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.rows[0].state == "legacy running"
        for selector in ("#start-worker", "#stop-worker", "#restart-worker", "#remove-worker"):
            assert app.query_one(selector, Button).disabled
        await pilot.press("s", "x", "ctrl+r", "delete")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert process.poll() is None
        assert record.read_bytes() == before
        assert "read-only" in str(app.query_one("#context", Static).render())


async def test_setup_amp_directory_fields(store):
    app = FleetApp(Manager(store))
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SetupScreen)
        assert not screen.query_one("#dirs-fields").display
        screen.query_one("#provider", Select).value = "amp"
        await pilot.pause()
        assert screen.query_one("#dirs-fields").display
        screen.query_one("#provider", Select).value = "droid"
        await pilot.pause()
        assert not screen.query_one("#dirs-fields").display
        await pilot.press("escape")


async def test_provider_switch_clears_provider_credentials(store):
    app = FleetApp(Manager(store))
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#provider", Select).value = "devin"
        await pilot.pause()
        screen.query_one("#secret", Input).value = "old-provider-secret"
        screen.query_one("#outpost", Input).value = "old-outpost"

        screen.query_one("#provider", Select).value = "amp"
        await pilot.pause()
        assert screen.query_one("#secret", Input).value == ""
        assert screen.query_one("#outpost", Input).value == ""
        assert not screen.query_one("#credential-fields").display

        screen.query_one("#provider", Select).value = "cursor"
        await pilot.pause()
        assert screen.query_one("#secret", Input).value == ""
        await pilot.press("escape")


async def test_mouse_details_tabs_load_content(store):
    app = FleetApp(Manager(store), demo=True)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        details = app.query_one("#details", TabbedContent)

        assert await pilot.click(details.get_tab("logs-tab"))
        await pilot.pause(0.1)
        assert details.active == "logs-tab"
        assert app.query_one("#logs", RichLog).lines

        assert await pilot.click(details.get_tab("health-tab"))
        await pilot.pause(0.1)
        assert details.active == "health-tab"
        assert app.query_one("#health", RichLog).lines


async def test_auto_refresh_skips_readiness_probes_but_manual_refresh_runs_them(
    manager, config, monkeypatch
):
    manager.setup(config)
    original_status = manager.status
    probes = []

    def track_status(name=None, *, probe=True):
        probes.append(probe)
        return original_status(name, probe=probe)

    monkeypatch.setattr(manager, "status", track_status)
    app = FleetApp(manager)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        probes.clear()

        app._refresh_timer()
        await pilot.pause()
        assert probes and all(not probe for probe in probes)

        probes.clear()
        await pilot.press("r")
        await pilot.pause()
        assert probes and any(probe for probe in probes)


async def test_manual_refresh_is_queued_during_an_in_flight_refresh(manager, config, monkeypatch):
    manager.setup(config)
    original_status = manager.status
    started = Event()
    release = Event()

    def slow_status(name=None, *, probe=True):
        if not started.is_set():
            started.set()
            release.wait(timeout=3)
        return original_status(name, probe=probe)

    monkeypatch.setattr(manager, "status", slow_status)
    app = FleetApp(manager)
    async with app.run_test(size=(100, 36)) as pilot:
        try:
            await asyncio.to_thread(started.wait, 1)
            await pilot.press("r")
            await pilot.pause()
            assert app._refresh_requested
            assert "queued" in str(app.query_one("#statusbar", Static).render()).lower()
        finally:
            release.set()
        await pilot.pause(0.3)
        assert not app._refresh_requested
        assert not app.refreshing


async def test_dirs_dialog_serves_directories(manager, config, tmp_path):
    manager.setup(config)
    extra = tmp_path / "worktree"
    extra.mkdir()
    root = tmp_path / "code"
    root.mkdir()
    app = FleetApp(manager)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        assert await pilot.click("#dirs")
        await pilot.pause(0.5)
        assert isinstance(app.screen, DirsScreen)
        screen = app.screen
        assert screen.query_one("#dir-list", DataTable).row_count == 2
        screen.query_one("#dir-path", Input).value = str(extra)
        assert await pilot.click("#dir-add")
        await pilot.pause(0.5)
        assert manager.store.load(config.name).extra_dirs == (str(extra.resolve()),)
        assert len(app.screen_stack) == 1
        await pilot.press("e")
        await pilot.pause(0.5)
        app.screen.query_one("#dir-path", Input).value = str(root)
        assert await pilot.click("#dir-discover")
        await pilot.pause(0.5)
        assert manager.store.load(config.name).discover_dirs == (str(root.resolve()),)
        await pilot.press("e")
        await pilot.pause(0.5)
        # Rows: two checkouts, then the extra directory, then the discovery root.
        app.screen.query_one("#dir-list", DataTable).move_cursor(row=2)
        assert await pilot.click("#dir-remove")
        await pilot.pause(0.5)
        assert manager.store.load(config.name).extra_dirs == ()
        assert manager.store.load(config.name).discover_dirs == (str(root.resolve()),)
