"""Capture the read-only sample dashboard for the README."""

import asyncio
from pathlib import Path

from agent_fleet.manager import Manager
from agent_fleet.store import Store
from agent_fleet.ui import FleetApp


async def capture() -> None:
    app = FleetApp(Manager(Store(Path.home() / ".agent-fleet")), demo=True)
    async with app.run_test(size=(130, 42)) as pilot:
        await pilot.pause()
        assets = Path(__file__).resolve().parents[1] / "assets"
        assets.mkdir(exist_ok=True)
        screenshot = assets / "dashboard.svg"
        app.save_screenshot(str(screenshot))
        screenshot.write_text(
            "\n".join(line.rstrip() for line in screenshot.read_text().splitlines()) + "\n"
        )


if __name__ == "__main__":
    asyncio.run(capture())
