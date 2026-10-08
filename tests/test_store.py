from dataclasses import replace
from pathlib import Path

import pytest

from agent_fleet.model import FleetError, gpu_group
from agent_fleet.store import Store, parse_legacy, private_read, private_write


def test_private_roundtrip(store, config):
    store.save(config)
    assert store.load("test") == config
    path = store.location("test") / "fleet.json"
    assert path.stat().st_mode & 0o777 == 0o600
    store.save_secret(replace(config, provider="cursor"), "test-key")
    assert store.secret(replace(config, provider="cursor")) == "test-key"


def test_private_permissions_and_symlinks(tmp_path):
    path = tmp_path / "data"
    path.write_text("private")
    path.chmod(0o644)
    with pytest.raises(FleetError, match="600"):
        private_read(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(FleetError, match="symbolic"):
        private_write(link, "changed")
    assert path.read_text() == "private"


def test_symlinked_parent_refused(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    link = tmp_path / "link"
    link.symlink_to(parent)
    with pytest.raises(FleetError, match="symbolic"):
        Store(link / "fleet")


def test_lock_serializes_and_dry_run_does_not_write(store):
    with store.lock(dry_run=True):
        assert not store.home.exists()
    with store.lock(), pytest.raises(FleetError, match="Another"):
        with store.lock():
            pass


@pytest.mark.parametrize("home", [Path("/"), Path("/tmp/.."), Path("relative")])
def test_bad_home(home):
    with pytest.raises(FleetError):
        Store(home)


@pytest.mark.parametrize(
    "text",
    [
        "PROVIDER=$(touch /tmp/should-not-exist)",
        "PROVIDER=`echo amp`",
        "echo unsafe",
        "PROVIDER=amp\nPROVIDER=cursor",
        "REPO_URL=unquoted spaces",
    ],
)
def test_legacy_does_not_execute(text):
    with pytest.raises(FleetError):
        parse_legacy(text)


def test_legacy_in_place(store, config):
    private_write(
        store.home / "fleet.env",
        (
            f"PROVIDER=amp\nREPO_URL={config.repo_url}\nREPO_NAME=source\n"
            "RUNNER_ID=test-runner\nWORKER_COUNT=2\nACTIVE_WORKERS=1,2\n"
            "GPU_SPLIT=no\nUSE_SYSTEMD=no\nDROID_DIR=''\nOUTPOST_NAME=''\n"
        ),
    )
    legacy = store.load("amp")
    assert legacy.runner_id == config.runner_id
    assert store.location("amp") == store.home
    store.save(replace(legacy, active_workers=(2,)))
    assert store.load("amp").active_workers == (2,)
    assert not (store.home / "fleet.json").exists()


def test_legacy_directory_fields_roundtrip(store, config):
    private_write(
        store.home / "fleet.env",
        (
            f"PROVIDER=amp\nREPO_URL={config.repo_url}\nREPO_NAME=source\n"
            "RUNNER_ID=test-runner\nWORKER_COUNT=2\nACTIVE_WORKERS=1,2\n"
            "GPU_SPLIT=no\nUSE_SYSTEMD=no\n"
            'EXTRA_DIRS=\'["/tmp/one", "/tmp/two"]\'\n'
            "DISCOVER_DIRS='[\"/tmp/code\"]'\n"
            "DISCOVER_DEPTH=3\n"
            "DISCOVER_EXCLUDES='[\"dotfiles\"]'\n"
        ),
    )
    legacy = store.load("amp")
    assert legacy.extra_dirs == ("/tmp/one", "/tmp/two")
    assert legacy.discover_dirs == ("/tmp/code",)
    assert legacy.discover_depth == 3
    assert legacy.discover_excludes == ("dotfiles",)
    store.save(replace(legacy, extra_dirs=("/tmp/one",)))
    assert store.load("amp").extra_dirs == ("/tmp/one",)
    assert not (store.home / "fleet.json").exists()


def test_malformed_native(store, config):
    store.save(config)
    path = store.location("test") / "fleet.json"
    private_write(path, '{"provider": "amp"}')
    with pytest.raises(FleetError, match="Invalid configuration"):
        store.load("test")


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "../escape"},
        {"worker_count": 0},
        {"worker_count": 129},
        {"active_workers": (1, 1)},
        {"active_workers": (3,)},
        {"repo_name": ".."},
        {"runner_id": "bad/id"},
        {"provider": "other"},
        {"gpu_split": "yes"},
    ],
)
def test_invalid_config(config, changes):
    with pytest.raises(FleetError):
        replace(config, **changes).validate()


def test_shared_target_semantics(config):
    assert config.process_indices("2") == (1,)
    assert config.targets("test-runner") == (1, 2)
    cursor = replace(config, provider="cursor")
    assert cursor.process_indices("2") == (2,)
    assert cursor.targets(cursor.worker_name(1)) == (1,)


def test_gpu_split():
    assert [gpu_group(i, 5, 2) for i in (1, 2)] == ["0,1", "2,3,4"]
    assert [gpu_group(i, 2, 4) for i in range(1, 5)] == ["0", "1", "", ""]
    assert gpu_group(1, 0, 4) == ""


def test_dangling_config_symlink_is_not_hidden(store):
    directory = store.home / "instances" / "broken"
    directory.mkdir(parents=True)
    (directory / "fleet.json").symlink_to(directory / "missing")
    with pytest.raises(FleetError, match="symbolic"):
        store.all()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/repo.git",
        "https://example.com/repo.git?token=example",
        "ext::command",
        "custom://example.com/repo.git",
    ],
)
def test_unsafe_repository_urls(config, url):
    with pytest.raises(FleetError):
        replace(config, repo_url=url).validate()
