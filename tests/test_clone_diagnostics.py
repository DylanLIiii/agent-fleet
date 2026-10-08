import os
import shutil
import sys
import time

import pytest

from agent_fleet.manager import Manager, make_instance, process_birth
from agent_fleet.model import FleetError


def fake_git(tmp_path, monkeypatch, body):
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "git"
    executable.write_text(f"#!{sys.executable}\n{body}")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")


def droid_plan(manager, source):
    return make_instance(
        manager.store,
        name="clone-check",
        provider="droid",
        source=source.as_uri(),
        backend="process",
    )


def test_clone_reports_authentication_without_echoing_credentials(
    manager, source, tmp_path, monkeypatch
):
    events = []
    manager.progress = events.append
    config = droid_plan(manager, source)
    fake_git(
        tmp_path,
        monkeypatch,
        "import sys\n"
        'sys.stderr.write("fatal: Authentication failed for '
        "'https://user:synthetic-credential@example.com/project.git'\\n\")\n"
        "sys.exit(128)\n",
    )
    monkeypatch.setattr("agent_fleet.manager.time.sleep", lambda _: None)
    with pytest.raises(FleetError) as error:
        manager.setup(config)
    assert "authentication" in str(error.value).lower()
    assert "synthetic-credential" not in str(error.value)
    assert "credential helper" in str(error.value).lower()
    assert manager.store.locations() == {}
    assert len([event for event in events if "attempt" in event]) == 1


@pytest.mark.parametrize(
    "stderr, reason",
    [
        ("fatal: unable to access URL: Could not resolve host: example.com", "DNS"),
        (
            "error: RPC failed; curl 92 HTTP/2 stream was not closed cleanly\nfatal: early EOF",
            "transfer",
        ),
        ("fatal: cannot create directory: No space left on device", "disk"),
        ("fatal: SSL certificate problem with synthetic-secret", "TLS"),
        ("fatal: unexpected opaque Git failure with synthetic-secret", "unclassified"),
    ],
)
def test_clone_failure_reason_is_safe(manager, source, tmp_path, monkeypatch, stderr, reason):
    events = []
    manager.progress = events.append
    config = droid_plan(manager, source)
    fake_git(tmp_path, monkeypatch, f"import sys\nsys.stderr.write({stderr!r})\nsys.exit(128)\n")
    monkeypatch.setattr("agent_fleet.manager.time.sleep", lambda _: None)
    with pytest.raises(FleetError) as error:
        manager.setup(config)
    assert reason.lower() in str(error.value).lower()
    assert "synthetic-secret" not in str(error.value)
    assert manager.store.locations() == {}
    attempts = len([event for event in events if "attempt" in event])
    assert attempts == (3 if reason in ("DNS", "transfer") else 1)


def test_clone_progress_is_streamed_and_redacted(store, source, tmp_path, monkeypatch):
    real_git = shutil.which("git")
    fake_git(
        tmp_path,
        monkeypatch,
        "import subprocess, sys\n"
        "sys.stderr.write('Receiving objects: 25% (1/4) synthetic-secret\\r')\n"
        "sys.stderr.flush()\n"
        f"sys.exit(subprocess.run([{real_git!r}, *sys.argv[1:]]).returncode)\n",
    )
    events = []
    manager = Manager(store, progress=events.append)
    manager.setup(droid_plan(manager, source))
    assert any("Receiving objects" in event and "25%" in event for event in events)
    assert all("synthetic-secret" not in event for event in events)
    assert any("completed" in event.lower() for event in events)


def test_clone_timeout_kills_its_subprocesses_and_leaves_no_config(
    store, source, tmp_path, monkeypatch
):
    marker = tmp_path / "child.pid"
    fake_git(
        tmp_path,
        monkeypatch,
        "import subprocess, sys, time\n"
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "while True: time.sleep(0.1)\n",
    )
    manager = Manager(store, clone_timeout=0.2)
    with pytest.raises(FleetError, match="timed out"):
        manager.setup(droid_plan(manager, source))
    assert manager.store.locations() == {}
    assert not list(store.home.rglob(".fleet-clone-*"))
    pid = int(marker.read_text())
    deadline = time.monotonic() + 2
    while process_birth(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process_birth(pid) is None


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_clone_timeout_must_be_positive_and_finite(store, timeout):
    with pytest.raises(FleetError, match="Clone timeout"):
        Manager(store, clone_timeout=timeout)


def test_live_progress_cancellation_stops_clone_and_cleans_up(store, source, tmp_path, monkeypatch):
    marker = tmp_path / "git.pid"
    fake_git(
        tmp_path,
        monkeypatch,
        "import os, sys, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stderr.write('Receiving objects: 10% (1/10)\\r')\n"
        "sys.stderr.flush()\n"
        "while True: time.sleep(0.1)\n",
    )

    def cancel_after_live_progress(message):
        if message.startswith("Git: Receiving objects"):
            assert process_birth(int(marker.read_text())) is not None
            raise KeyboardInterrupt

    manager = Manager(store, progress=cancel_after_live_progress)
    with pytest.raises(KeyboardInterrupt):
        manager.setup(droid_plan(manager, source))
    assert process_birth(int(marker.read_text())) is None
    assert manager.store.locations() == {}
    assert not list(store.home.rglob(".fleet-clone-*"))


@pytest.mark.parametrize("close_output", [False, True])
def test_cancel_token_interrupts_a_silent_clone(store, source, tmp_path, monkeypatch, close_output):
    from threading import Event, Thread

    marker = tmp_path / "silent.pid"
    fake_git(
        tmp_path,
        monkeypatch,
        "import os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        + ("os.close(1)\nos.close(2)\n" if close_output else "")
        + "while True: time.sleep(0.1)\n",
    )
    cancel = Event()

    def request_cancel():
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    requester = Thread(target=request_cancel)
    requester.start()
    manager = Manager(store, clone_cancel=cancel)
    try:
        with pytest.raises(FleetError, match="cancelled"):
            manager.setup(droid_plan(manager, source))
    finally:
        cancel.set()
        requester.join(timeout=4)
    assert process_birth(int(marker.read_text())) is None
    assert manager.store.locations() == {}
    assert not list(store.home.rglob(".fleet-clone-*"))


async def test_dashboard_shows_live_clone_progress(store, source, tmp_path, monkeypatch):
    from textual.widgets import RichLog

    from agent_fleet.ui import FleetApp

    real_git = shutil.which("git")
    fake_git(
        tmp_path,
        monkeypatch,
        "import subprocess, sys\n"
        "sys.stderr.write('Receiving objects: 50% (1/2) synthetic-secret\\r')\n"
        "sys.stderr.flush()\n"
        f"sys.exit(subprocess.run([{real_git!r}, *sys.argv[1:]]).returncode)\n",
    )
    app = FleetApp(Manager(store, clone_timeout=17))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._perform(
            "setup",
            payload={
                "name": "clone-check",
                "provider": "droid",
                "source": source.as_uri(),
                "count": 1,
                "backend": "process",
                "label": "",
                "outpost": "",
                "gpu_split": False,
            },
            secret="",
            preview=False,
        )
        for _ in range(50):
            await pilot.pause(0.1)
            if store.locations() and not app.busy:
                break
        messages = "\n".join(line.text for line in app.query_one("#activity", RichLog).lines)
        assert "Receiving objects 50%" in messages
        assert "timeout 17s" in messages
        assert "synthetic-secret" not in messages
        assert store.load("clone-check").provider == "droid"


def test_cli_streams_safe_clone_progress(store, source, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agent_fleet.cli import app

    real_git = shutil.which("git")
    fake_git(
        tmp_path,
        monkeypatch,
        "import subprocess, sys\n"
        "sys.stderr.write('Receiving objects: 25% (1/4) synthetic-secret\\r')\n"
        "sys.stderr.flush()\n"
        f"sys.exit(subprocess.run([{real_git!r}, *sys.argv[1:]]).returncode)\n",
    )
    monkeypatch.setenv("CLONE_TIMEOUT", "42")
    result = CliRunner().invoke(
        app,
        [
            "--home",
            str(store.home),
            "setup",
            "--provider",
            "droid",
            "--name",
            "clone-check",
            "--source",
            source.as_uri(),
            "--backend",
            "process",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Receiving objects 25%" in result.output
    assert "timeout 42s" in result.output
    assert "synthetic-secret" not in result.output


async def test_dashboard_cancellation_stops_background_clone(store, source, tmp_path, monkeypatch):
    from agent_fleet.ui import FleetApp

    marker = tmp_path / "dashboard.pid"
    fake_git(
        tmp_path,
        monkeypatch,
        "import os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "while True: time.sleep(0.1)\n",
    )
    app = FleetApp(Manager(store))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        worker = app._perform(
            "setup",
            payload={
                "name": "clone-check",
                "provider": "droid",
                "source": source.as_uri(),
                "count": 1,
                "backend": "process",
                "label": "",
                "outpost": "",
                "gpu_split": False,
            },
            secret="",
            preview=False,
        )
        for _ in range(30):
            await pilot.pause(0.1)
            if marker.exists():
                break
        assert marker.exists()
        worker.cancel()
        pid = int(marker.read_text())
        for _ in range(30):
            await pilot.pause(0.1)
            if process_birth(pid) is None and not list(store.home.rglob(".fleet-clone-*")):
                break
        assert process_birth(pid) is None
        assert not list(store.home.rglob(".fleet-clone-*"))
        assert store.locations() == {}
