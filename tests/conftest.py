from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_fleet.manager import Manager
from agent_fleet.model import Instance
from agent_fleet.store import Store, private_write


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "fleet")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    directory = tmp_path / "source.git"
    subprocess.run(["git", "init", "--bare", str(directory)], check=True, capture_output=True)
    return directory


@pytest.fixture
def config(source: Path) -> Instance:
    return Instance("test", "amp", str(source), "source", "test-runner", 2, (1, 2))


@pytest.fixture
def manager(store: Store) -> Manager:
    return Manager(store, ready_timeout=3)


@pytest.fixture
def fake_amp(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "amp"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "if sys.argv[1:3] == ['runner', 'list']:\n"
        "    print(json.dumps({'runners': [{'runnerId': 'test-runner'}]}))\n"
        "else:\n"
        "    print('fake worker started', flush=True)\n"
        "    open(os.environ['TEST_CHILD_PID'], 'w').write(str(os.getpid()))\n"
        "    while True: time.sleep(0.1)\n"
    )
    script.chmod(0o700)
    import os

    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    marker = tmp_path / "child.pid"
    monkeypatch.setenv("TEST_CHILD_PID", str(marker))
    return marker


@pytest.fixture
def legacy_cursor(manager: Manager, source: Path, tmp_path: Path):
    home = manager.store.location("old-cursor")
    private_write(
        home / "fleet.env",
        (
            f"PROVIDER=cursor\nREPO_URL={source}\nREPO_NAME=source\n"
            "WORKER_COUNT=1\nACTIVE_WORKERS=1\nGPU_SPLIT=no\nUSE_SYSTEMD=no\n"
        ),
    )
    config = manager.store.load("old-cursor")
    workspace = config.worker_dir(home, 1)
    workspace.mkdir(parents=True)
    executable = tmp_path / "cursor-agent"
    executable.write_text(f"#!{sys.executable}\nimport time\nwhile True: time.sleep(0.1)\n")
    executable.chmod(0o700)
    process = subprocess.Popen(
        [
            str(executable),
            "worker",
            "start",
            "--name",
            "source-w1",
            "--worker-dir",
            str(workspace),
        ]
    )
    record = home / "run/source-w1.pid"
    record.parent.mkdir()
    record.write_text(str(process.pid) + "\n")
    record.chmod(0o644)
    try:
        # Wait until /proc contains the exec'd provider rather than the spawning interpreter.
        import time

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if b"worker\x00start" in Path(f"/proc/{process.pid}/cmdline").read_bytes():
                break
            time.sleep(0.01)
        yield manager, config, record, process
    finally:
        process.terminate()
        process.wait(timeout=3)
