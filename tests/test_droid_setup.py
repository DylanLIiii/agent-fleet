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

    monkeypatch.setattr(module, "clone_repo", fail_clone)
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
        "    print('[droid daemon] Daemon is running', flush=True)\n"
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


def test_droid_readiness_rejects_loopback_only_when_daemon_exits(
    manager, source, tmp_path, monkeypatch
):
    import os
    import sys

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "droid"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "if sys.argv[1:2] == ['doctor']:\n"
        "    print(json.dumps({'results': [{'id': 'daemon.loopback', 'status': 'pass'}]}))\n"
        "else:\n"
        "    time.sleep(0.6)\n"
        "    print('Startup failed: Authentication token expired.', flush=True)\n"
        "    sys.exit(1)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    config = planned_droid(manager, source)
    manager.setup(config)
    with pytest.raises(FleetError, match="Authentication token expired"):
        manager.control("start", config.name)
    assert not manager.running(config, 1)


def test_droid_auto_registers_computer_on_first_start(manager, source, tmp_path, monkeypatch):
    import os
    import sys

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    reg_marker = tmp_path / "registered"
    executable = binary_dir / "droid"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        f"reg = pathlib.Path({str(reg_marker)!r})\n"
        "if sys.argv[1:2] == ['doctor']:\n"
        "    print(json.dumps({'results': [{'id': 'daemon.loopback', 'status': 'pass'}]}))\n"
        "elif sys.argv[1:3] == ['computer', 'register']:\n"
        "    assert '-y' in sys.argv\n"
        "    reg.write_text('agent-fleet-box')\n"
        "    print('Registered computer agent-fleet-box', flush=True)\n"
        "else:\n"
        "    assert '--remote-control' in sys.argv\n"
        "    if not reg.exists():\n"
        "        print('Remote access requires a registered computer. "
        "Run `droid computer register` first.', flush=True)\n"
        "        sys.exit(1)\n"
        "    print('[droid daemon] Daemon is running', flush=True)\n"
        "    print('  Version: 0.240.0', flush=True)\n"
        "    print('  Relay: wss://relay.factory.ai/v0/computer/uuid-1/agent "
        "(Computer: agent-fleet-box, ID: uuid-1)', flush=True)\n"
        "    while True: time.sleep(0.1)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    config = planned_droid(manager, source)
    manager.setup(config)
    try:
        manager.control("start", config.name)
        assert reg_marker.exists()
        assert manager.running(config, 1)
        assert manager.status(config.name)[0].state == "local ready"
        assert "agent-fleet-box" in manager.droid_identity(config)
    finally:
        manager.control("stop", config.name)


def test_droid_doctor_reports_computer_and_version_drift(manager, source, tmp_path, monkeypatch):
    import os
    import sys

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "droid"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "if sys.argv[1:2] == ['--version']:\n"
        "    print('0.241.1')\n"
        "elif sys.argv[1:3] == ['computer', 'list']:\n"
        "    print('agent-fleet\\tuuid-1234 (this machine)')\n"
        "elif sys.argv[1:2] == ['doctor']:\n"
        "    print(json.dumps({'results': [{'id': 'daemon.loopback', 'status': 'pass'}]}))\n"
        "else:\n"
        "    print('[droid daemon] Daemon is running', flush=True)\n"
        "    print('  Version: 0.240.0', flush=True)\n"
        "    print('  Relay: wss://relay.factory.ai/v0/computer/uuid-1234/agent "
        "(Computer: agent-fleet, ID: uuid-1234)', flush=True)\n"
        "    while True: time.sleep(0.1)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    config = planned_droid(manager, source)
    manager.setup(config)
    try:
        manager.control("start", config.name)
        checks = manager.doctor(config.name)
        details = "\n".join(detail for _, _, detail in checks)
        assert "Registered Factory Computer: agent-fleet (uuid-1234)" in details
        assert "Running daemon is v0.240.0 while installed Droid CLI is v0.241.1" in details
    finally:
        manager.control("stop", config.name)


def test_droid_multi_project_directories_and_transition(manager, tmp_path):
    first = tmp_path / "proj-a"
    second = tmp_path / "proj-b"
    third = tmp_path / "proj-c"
    for directory in (first, second, third):
        directory.mkdir()
    config = make_instance(
        manager.store,
        name="droid-multi",
        provider="droid",
        project_dirs=[str(first), str(second)],
        backend="process",
    )
    manager.setup(config)
    worker_dir = config.worker_dir(manager.store.location(config.name), 1)
    assert (worker_dir / "projects/proj-a").resolve() == first.resolve()
    assert (worker_dir / "projects/proj-b").resolve() == second.resolve()
    assert len(manager.list_dirs(config.name)) == 2

    manager.add_dirs(config.name, [str(third)])
    assert len(manager.list_dirs(config.name)) == 3
    assert (worker_dir / "projects/proj-c").resolve() == third.resolve()

    manager.remove_dirs(config.name, [str(second)])
    assert len(manager.list_dirs(config.name)) == 2
    assert not (worker_dir / "projects/proj-b").exists()
    assert second.is_dir()
    manager.remove(config.name)
    assert first.is_dir() and third.is_dir()
