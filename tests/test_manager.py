import os
import time
from dataclasses import replace

import pytest

from agent_fleet.manager import Manager, make_instance, process_birth
from agent_fleet.model import FleetError
from agent_fleet.providers import build_command
from agent_fleet.store import private_write


def test_preview_never_writes(store, config):
    manager = Manager(store, dry_run=True)
    assert manager.setup(config)
    assert not store.home.exists()


def test_prepare_scale_and_retain(manager, config):
    manager.setup(config)
    directory = manager.store.location(config.name)
    repo = config.repo_dir(directory, 1)
    assert (repo / ".git").is_dir()
    manager.add(config.name, 1)
    updated = manager.store.load(config.name)
    assert updated.active_workers == (1, 2, 3)
    manager.remove(config.name, "1")
    assert repo.exists()
    assert manager.store.load(config.name).active_workers == (2, 3)
    with pytest.raises(FleetError):
        manager.store.load(config.name).targets("1")


def test_setup_never_overwrites(manager, config):
    manager.setup(config)
    with pytest.raises(FleetError, match="already exists"):
        manager.setup(config)
    assert manager.store.load(config.name) == config


def test_repair_preserves_existing_data(manager, config):
    manager.setup(config)
    repo = config.repo_dir(manager.store.location(config.name), 1)
    user_file = repo / "precious.txt"
    user_file.write_text("keep me")
    manager.repair(config.name)
    assert user_file.read_text() == "keep me"


def test_deletion_rejects_user_files(manager, config):
    manager.setup(config)
    repo = config.repo_dir(manager.store.location(config.name), 1)
    (repo / "notes.txt").write_text("user work")
    with pytest.raises(FleetError, match="untracked"):
        manager.remove(config.name, "1", delete_checkouts=True)
    assert manager.store.load(config.name) == config
    assert (repo / "notes.txt").exists()


def test_deletion_rejects_ignored_files(manager, config):
    manager.setup(config)
    repo = config.repo_dir(manager.store.location(config.name), 1)
    (repo / ".git/info/exclude").write_text("cache\n")
    (repo / "cache").write_text("valuable")
    with pytest.raises(FleetError, match="ignored"):
        manager.assert_deletable(config, 1)


def test_deletion_rejects_unpushed_commits(manager, config, monkeypatch):
    manager.setup(config)
    import agent_fleet.manager as module

    original = module.run
    monkeypatch.setattr(
        module,
        "run",
        lambda args, **kw: "unpublished commit" if "log" in args else original(args, **kw),
    )
    with pytest.raises(FleetError, match="unpushed"):
        manager.assert_deletable(config, 1)


def test_clean_owned_checkout_can_be_deleted(manager, config):
    manager.setup(config)
    repo = config.repo_dir(manager.store.location(config.name), 1)
    manager.remove(config.name, "1", delete_checkouts=True)
    assert not repo.exists()


def test_pid_reuse_is_never_signaled(manager, config, monkeypatch):
    manager.setup(config)
    pidfile = manager.paths(config, 1)[0]
    private_write(pidfile, f"{os.getpid()} 1\n")
    signaled = []
    monkeypatch.setattr(os, "kill", lambda *args: signaled.append(args))
    with pytest.raises(FleetError, match="reused"):
        manager.control("stop", config.name)
    assert not signaled
    assert pidfile.exists()


def test_dead_pid_record_can_be_cleared(manager, config):
    manager.setup(config)
    pidfile = manager.paths(config, 1)[0]
    private_write(pidfile, "999999999 1\n")
    manager.control("stop", config.name)
    assert not pidfile.exists()


def test_provider_commands_and_empty_gpu(config, tmp_path):
    cursor = replace(config, provider="cursor", gpu_split=True)
    command = build_command(cursor, tmp_path, 2, gpus=1, executable="/bin/cursor-agent")
    assert command.env["CUDA_VISIBLE_DEVICES"] == ""
    assert "--data-dir" in command.argv
    assert f"127.0.0.1:{cursor.port(2)}" in command.argv
    amp = build_command(config, tmp_path, 1, executable="/bin/amp")
    assert amp.argv.count("--dir") == 2
    assert "--no-serve-cwd" in amp.argv
    devin = build_command(
        replace(config, provider="devin"),
        tmp_path,
        1,
        executable="/bin/devin",
        secret="synthetic-test-secret",
    )
    assert "--token=synthetic-test-secret" in devin.argv
    assert "synthetic-test-secret" not in repr(devin)
    assert devin.env["DEVIN_WORKDIR"] == str(tmp_path / "devin-workers/w1")


def test_droid_directory_never_deleted(manager, config, tmp_path):
    directory = tmp_path / "existing"
    directory.mkdir()
    droid = replace(
        config,
        provider="droid",
        worker_count=1,
        active_workers=(1,),
        droid_dir=str(directory),
        repo_url="",
    )
    manager.setup(droid)
    with pytest.raises(FleetError, match="Only one"):
        manager.setup(replace(droid, name="second"))
    manager.remove(droid.name, delete_checkouts=True)
    assert directory.exists()


def test_systemd_ownership_refusal(manager, config, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = replace(config, use_systemd=True)
    manager.setup(config)
    unit = manager.paths(config, 1)[2]
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=unowned\n")
    with pytest.raises(FleetError, match="unowned"):
        manager.control("stop", config.name)
    assert unit.exists()


def test_real_detached_worker_lifecycle(manager, config, fake_amp):
    manager.setup(config)
    child_pid = None
    try:
        manager.control("start", config.name, "2")
        assert manager.running(config, 1)
        assert manager.paths(config, 1)[0] == manager.paths(config, 2)[0]
        deadline = time.monotonic() + 3
        while not fake_amp.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(fake_amp.read_text())
        assert process_birth(child_pid) is not None
        with pytest.raises(FleetError, match="Stop all"):
            manager.add(config.name)
        assert "fake worker started" in manager.logs(config.name)
        manager.control("restart", config.name)
        assert manager.running(config, 1)
    finally:
        manager.control("stop", config.name)
    assert not manager.running(config, 1)
    if child_pid:
        assert process_birth(child_pid) is None


def test_preview_control_never_calls_provider(manager, config, monkeypatch):
    manager.setup(config)
    preview = Manager(manager.store, dry_run=True)
    monkeypatch.setattr(
        "agent_fleet.manager.binary", lambda _: pytest.fail("Provider lookup in preview")
    )
    before = (manager.store.location(config.name) / "fleet.json").read_bytes()
    preview.control("restart", config.name)
    preview.remove(config.name, "1")
    assert (manager.store.location(config.name) / "fleet.json").read_bytes() == before


def test_make_instance_from_local_remote(store, source):
    from agent_fleet.providers import run

    run(["git", "-C", str(source), "remote", "add", "origin", "git@github.com:you/project.git"])
    config = make_instance(store, name="local", provider="cursor", source=str(source), preview=True)
    assert config.repo_url == "git@github.com:you/project.git"
    assert config.repo_name == "project"


def test_start_failure_stops_supervisor_and_child(manager, config, fake_amp, monkeypatch):
    manager.setup(config)
    manager.ready_timeout = 0.5
    monkeypatch.setattr("agent_fleet.manager.readiness", lambda *_: "starting")
    with pytest.raises(FleetError, match="timed out"):
        manager.control("start", config.name)
    assert not manager.running(config, 1)
    assert not manager.paths(config, 1)[0].exists()
    assert process_birth(int(fake_amp.read_text())) is None


def test_supervisor_redacts_credential(manager, config, tmp_path, monkeypatch):
    import sys

    directory = tmp_path / "devin-bin"
    directory.mkdir()
    executable = directory / "devin"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "print(next(a for a in sys.argv if a.startswith('--token=')), flush=True)\n"
        "while True: time.sleep(0.1)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    config = replace(config, provider="devin", outpost_name="test-outpost")
    secret = "synthetic-credential-for-test"
    manager.setup(config, secret=secret)
    try:
        manager.control("start", config.name, "1")
        logfile = manager.paths(config, 1)[1]
        text = logfile.read_text()
        assert secret not in text
        assert "[REDACTED]" in text
        assert logfile.stat().st_mode & 0o777 == 0o600
    finally:
        manager.control("stop", config.name)


def test_generated_systemd_unit_contains_no_secret(manager, config, tmp_path, monkeypatch):
    import agent_fleet.manager as module

    monkeypatch.setenv("HOME", str(tmp_path))
    config = replace(config, provider="cursor", use_systemd=True)
    secret = "synthetic-systemd-secret"
    manager.setup(config, secret=secret)
    original_run = module.run
    active = False

    def fake_run(args, **kwargs):
        nonlocal active
        if args[0] != "systemctl":
            return original_run(args, **kwargs)
        if "is-active" in args and not active:
            raise FleetError("Inactive test service")
        if "enable" in args:
            active = True
        if "disable" in args:
            active = False
        return ""

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "binary", lambda _: "/opt/fake-cursor")
    monkeypatch.setattr(module, "readiness", lambda *_: "ready")
    manager.control("start", config.name, "1")
    unit = manager.paths(config, 1)[2]
    assert secret not in unit.read_text()
    assert "-m" in unit.read_text() and "agent_fleet.runtime" in unit.read_text()
    assert unit.stat().st_mode & 0o777 == 0o600
    manager.control("stop", config.name, "1")


def test_legacy_cursor_live_process_is_read_only_not_stopped(legacy_cursor):
    manager, config, record, process = legacy_cursor
    before = record.read_bytes(), record.stat().st_mode
    assert manager.status(config.name, probe=False)[0].state == "legacy running"
    assert process.poll() is None
    assert (record.read_bytes(), record.stat().st_mode) == before
    assert not manager.paths(config, 1)[0].exists()


@pytest.mark.parametrize("action", ["start", "stop", "restart", "remove"])
def test_legacy_cursor_cannot_be_duplicated_or_controlled(legacy_cursor, monkeypatch, action):
    manager, config, record, process = legacy_cursor
    monkeypatch.setattr(
        "agent_fleet.manager.binary", lambda _: pytest.fail("Provider execution for legacy worker")
    )
    with pytest.raises(FleetError, match="legacy"):
        if action == "remove":
            manager.remove(config.name)
        else:
            manager.control(action, config.name)
    assert process.poll() is None
    assert record.exists()
    assert manager.store.load(config.name) == config


def test_legacy_pid_does_not_identify_an_unrelated_process(legacy_cursor):
    manager, config, record, _ = legacy_cursor
    record.write_text(str(os.getpid()) + "\n")
    assert manager.status(config.name, probe=False)[0].state == "unverified PID"


def test_dead_legacy_record_is_not_reported_running(legacy_cursor):
    manager, config, record, _ = legacy_cursor
    record.write_text("999999999\n")
    assert manager.status(config.name, probe=False)[0].state == "stopped"


def test_legacy_symlink_record_is_rejected(legacy_cursor, tmp_path):
    manager, config, record, _ = legacy_cursor
    target = tmp_path / "outside-record"
    target.write_text(record.read_text())
    record.unlink()
    record.symlink_to(target)
    with pytest.raises(FleetError, match="symbolic"):
        manager.status(config.name, probe=False)


def test_legacy_record_writable_by_others_is_rejected(legacy_cursor):
    manager, config, record, _ = legacy_cursor
    record.chmod(0o666)
    with pytest.raises(FleetError, match="Unsafe legacy"):
        manager.status(config.name, probe=False)


def test_legacy_record_with_wrong_process_birth_is_unverified(legacy_cursor):
    manager, config, record, process = legacy_cursor
    record.write_text(f"{process.pid} 1\n")
    assert manager.status(config.name, probe=False)[0].state == "unverified PID"


def test_amp_startup_flags_cover_discovery_only(config, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    root = tmp_path / "code"
    root.mkdir()
    amp = replace(
        config,
        extra_dirs=(str(extra),),
        discover_dirs=(str(root),),
        discover_depth=3,
        discover_excludes=("dotfiles",),
    )
    command = build_command(amp, tmp_path, 1, executable="/bin/amp")
    # Extra directories change while the runner runs, so they are not startup flags.
    assert str(extra) not in command.argv
    assert f"--discover-dirs={root}" in command.argv
    assert command.argv[command.argv.index("--discover-depth") + 1] == "3"
    assert command.argv[command.argv.index("--discover-exclude") + 1] == "dotfiles"
    assert "--no-serve-cwd" in command.argv


def test_make_instance_rejects_bad_directory_configuration(store, source, tmp_path):
    with pytest.raises(FleetError, match="Only Amp"):
        make_instance(
            store,
            name="cursor-dirs",
            provider="cursor",
            source=str(source),
            extra_dirs=[str(tmp_path)],
            preview=True,
        )
    with pytest.raises(FleetError, match="absolute"):
        make_instance(
            store,
            name="amp-relative",
            provider="amp",
            source=str(source),
            extra_dirs=["worktrees/one"],
            preview=True,
        )
    with pytest.raises(FleetError, match="not an existing directory"):
        make_instance(
            store,
            name="amp-missing",
            provider="amp",
            source=str(source),
            extra_dirs=[str(tmp_path / "missing")],
            preview=True,
        )
    with pytest.raises(FleetError, match="Discovery depth"):
        make_instance(
            store,
            name="amp-depth",
            provider="amp",
            source=str(source),
            discover_dirs=[str(tmp_path)],
            discover_depth=11,
            preview=True,
        )
    with pytest.raises(FleetError, match="discovery root"):
        make_instance(
            store,
            name="amp-exclude",
            provider="amp",
            source=str(source),
            discover_excludes=["dotfiles"],
            preview=True,
        )


def test_amp_extra_directories_apply_live_and_remove(
    manager, config, fake_amp, tmp_path, amp_calls
):
    extra = str((tmp_path / "worktree").resolve())
    (tmp_path / "worktree").mkdir()
    manager.setup(config)
    manager.control("start", config.name)
    try:
        messages = manager.add_dirs(config.name, [extra])
        assert any("running runner" in message for message in messages)
        rows = {row.path: row for row in manager.list_dirs(config.name)}
        assert rows[extra].kind == "extra" and rows[extra].live is True
        assert f"add {extra}" in amp_calls.read_text()
        assert manager.remove_dirs(config.name, [extra])
        rows = {row.path: row for row in manager.list_dirs(config.name)}
        assert extra not in rows
        assert manager.store.load(config.name).extra_dirs == ()
    finally:
        manager.control("stop", config.name)


def test_amp_start_converges_served_directories(
    manager, config, fake_amp, tmp_path, amp_calls, amp_state
):
    kept = tmp_path / "kept"
    kept.mkdir()
    stale = tmp_path / "stale"
    stale.mkdir()
    configured = replace(config, extra_dirs=(str(kept.resolve()),))
    manager.setup(configured)
    amp_state.write_text(f"{stale}\n")
    manager.control("start", configured.name)
    try:
        rows = {row.path: row for row in manager.list_dirs(configured.name)}
        assert rows[str(kept.resolve())].live is True
        assert str(stale) not in rows
    finally:
        manager.control("stop", configured.name)
    calls = amp_calls.read_text()
    assert f"add {kept.resolve()}" in calls
    assert f"remove {stale}" in calls


def test_amp_discovery_configuration(manager, config, tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    manager.setup(config)
    messages = manager.edit_discovery(
        config.name, add=[str(root)], depth=3, add_excludes=["dotfiles"]
    )
    assert any("starts" in message for message in messages)
    saved = manager.store.load(config.name)
    assert saved.discover_dirs == (str(root.resolve()),)
    assert saved.discover_depth == 3
    assert saved.discover_excludes == ("dotfiles",)
    with pytest.raises(FleetError, match="Discovery depth"):
        manager.edit_discovery(config.name, depth=11)
    manager.edit_discovery(config.name, clear=True)
    cleared = manager.store.load(config.name)
    assert (cleared.discover_dirs, cleared.discover_depth, cleared.discover_excludes) == ((), 0, ())


def test_directory_management_is_amp_only(manager, config, source, tmp_path):
    cursor = replace(config, provider="cursor", name="cursor-lab")
    manager.setup(cursor)
    with pytest.raises(FleetError, match="Only Amp"):
        manager.add_dirs("cursor-lab", [str(tmp_path)])
    with pytest.raises(FleetError, match="Only Amp"):
        manager.list_dirs("cursor-lab")


def test_removing_unconfigured_directory_is_refused(manager, config, tmp_path):
    manager.setup(config)
    with pytest.raises(FleetError, match="Not a configured directory"):
        manager.remove_dirs(config.name, [str(tmp_path)])
