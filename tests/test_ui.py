import pytest
from textual.widgets import Checkbox, DataTable, Input, Select, Static, TabbedContent

from agent_fleet.manager import Manager
from agent_fleet.ui import ConfirmScreen, FleetApp, HelpScreen, SetupScreen


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
