from pathlib import Path

import pytest
from textual.widgets import Input, Select, Static

from agent_fleet.manager import Manager, make_instance
from agent_fleet.model import FleetError
from agent_fleet.providers import run
from agent_fleet.ui import ConfirmScreen, FleetApp, SetupScreen


def planned_droid(manager, source):
    return make_instance(
        manager.store,
        name="droid-project",
        provider="droid",
        source=source.as_uri(),
        count=4,
        backend="process",
    )


def test_droid_git_url_clones_to_managed_workspace(manager, source):
    config = planned_droid(manager, source)
    directory = manager.store.home / "instances/droid-project/checkouts/source-w1"
    assert config.worker_count == 1
    assert config.repo_url == source.as_uri()
    assert Path(config.droid_dir) == directory
    assert not directory.exists()
    messages = manager.setup(config)
    assert (directory / ".git").is_dir()
    assert run(["git", "-C", str(directory), "remote", "get-url", "origin"]) == source.as_uri()
    assert manager.store.load(config.name) == config
    assert not manager.running(config, 1)
    assert any("Clone" in message for message in messages)
    manager.remove(config.name, delete_checkouts=True)
    assert directory.exists()


def test_droid_git_preview_never_creates_files(manager, source):
    preview = Manager(manager.store, dry_run=True)
    config = planned_droid(preview, source)
    messages = preview.setup(config)
    assert any("Clone" in message for message in messages)
    assert not manager.store.home.exists()


def test_droid_clone_failure_does_not_save_configuration(manager, source, monkeypatch):
    import agent_fleet.manager as module

    config = planned_droid(manager, source)

    def fail_clone(*args, **kwargs):
        raise FleetError("Clone failed")

    monkeypatch.setattr(module, "run", fail_clone)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(FleetError, match="Clone failed"):
        manager.setup(config)
    assert manager.store.locations() == {}
    assert not Path(config.droid_dir).exists()
    assert not list(manager.store.home.rglob(".fleet-clone-*"))


def test_droid_clone_never_overwrites_existing_directory(manager, source):
    config = planned_droid(manager, source)
    directory = Path(config.droid_dir)
    directory.mkdir(parents=True)
    user_file = directory / "valuable.txt"
    user_file.write_text("keep this")
    with pytest.raises(FleetError, match="checkout"):
        manager.setup(config)
    assert user_file.read_text() == "keep this"
    assert manager.store.locations() == {}


async def test_droid_form_checks_directory_before_review(store):
    app = FleetApp(Manager(store), setup=True)
    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#provider", Select).value = "droid"
        await pilot.pause()
        screen.query_one("#instance-name", Input).value = "droid-project"
        screen.query_one("#source", Input).value = "relative-folder"
        await pilot.click("#setup-submit")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert screen.query_one("#source", Input).value == "relative-folder"
        assert "absolute" in str(screen.query_one("#setup-error", Static).render())
        assert not store.home.exists()


async def test_droid_form_url_review_and_selected_instance(manager, source, config):
    manager.setup(config)
    app = FleetApp(manager, setup=True)
    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#provider", Select).value = "droid"
        await pilot.pause()
        placeholder = screen.query_one("#source", Input).placeholder
        assert "/path/" in placeholder and "https://" in placeholder
        screen.query_one("#instance-name", Input).value = "droid-project"
        screen.query_one("#source", Input).value = source.as_uri()
        await pilot.click("#setup-submit")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert "checkouts/source-w1" in app.screen.message
        await pilot.click("#confirm")
        for _ in range(50):
            await pilot.pause(0.1)
            if app.selected == "droid-project" and app.visible_rows():
                break
        assert app.selected == "droid-project"
        assert app.visible_rows()[0].provider == "droid"
        assert manager.store.load("droid-project").repo_url == source.as_uri()
        assert not manager.running(manager.store.load("droid-project"), 1)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/project",
        "git@github.com:example/project.git",
    ],
)
def test_droid_github_sources_are_valid_clone_plans(manager, url):
    config = make_instance(
        manager.store,
        name="droid-project",
        provider="droid",
        source=url,
        preview=True,
    )
    assert config.repo_name == "project"
    assert config.repo_url == url
    assert config.worker_count == 1
    assert Path(config.droid_dir).parent.name == "checkouts"
    assert not manager.store.home.exists()


def test_droid_existing_directory_is_used_without_clone(manager, tmp_path, monkeypatch):
    local = tmp_path / "existing-project"
    local.mkdir()
    config = make_instance(
        manager.store,
        name="droid-project",
        provider="droid",
        source=str(local),
        preview=True,
    )
    monkeypatch.setattr(
        "agent_fleet.manager.run",
        lambda *a, **kw: pytest.fail("Cloned an existing Droid directory"),
    )
    manager.setup(config)
    assert config.repo_url == ""
    assert Path(config.droid_dir) == local
    assert manager.store.load(config.name) == config


def test_droid_clone_plan_cannot_write_outside_instance(manager, source, tmp_path):
    from dataclasses import replace

    config = replace(planned_droid(manager, source), droid_dir=str(tmp_path / "outside"))
    with pytest.raises(FleetError, match="managed checkouts"):
        manager.setup(config)
    assert not (tmp_path / "outside").exists()
    assert manager.store.locations() == {}


def test_droid_daemon_uses_cloned_working_directory(manager, source, tmp_path, monkeypatch):
    import os
    import sys

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "droid"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "if sys.argv[1:2] == ['doctor']:\n"
        "    print(json.dumps({'results': [{'id': 'daemon.loopback', 'status': 'pass'}]}))\n"
        "else:\n"
        "    open(os.environ['TEST_DROID_CWD'], 'w').write(os.getcwd())\n"
        "    while True: time.sleep(0.1)\n"
    )
    executable.chmod(0o700)
    marker = tmp_path / "daemon-cwd"
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("TEST_DROID_CWD", str(marker))
    config = planned_droid(manager, source)
    manager.setup(config)
    try:
        manager.control("start", config.name)
        assert marker.read_text() == config.droid_dir
        assert manager.running(config, 1)
    finally:
        manager.control("stop", config.name)
