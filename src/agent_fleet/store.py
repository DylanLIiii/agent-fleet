from __future__ import annotations

import fcntl
import json
import os
import shlex
import socket
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from agent_fleet.model import FleetError, Instance, runner_id, valid_name


def safe_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise FleetError(f"Refusing symbolic-link path: {part}")


def private_read(path: Path) -> str:
    safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise FleetError(f"File must be owned by you with permissions 600: {path}")
            if info.st_size > 1024 * 1024:
                raise FleetError(f"Protected configuration or credential file is too large: {path}")
            return stream.read()
    except OSError as exc:
        raise FleetError(f"Cannot read protected file: {path}") from exc


def private_write(path: Path, text: str) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".fleet-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_legacy(text: str) -> dict[str, str]:
    """Parse literal Bash assignments, never source or evaluate a shell file."""
    values = {}
    allowed = {
        "PROVIDER",
        "REPO_URL",
        "REPO_NAME",
        "RUNNER_ID",
        "DROID_DIR",
        "WORKER_COUNT",
        "ACTIVE_WORKERS",
        "GPU_SPLIT",
        "OUTPOST_NAME",
        "USE_SYSTEMD",
        "EXTRA_DIRS",
        "DISCOVER_DIRS",
        "DISCOVER_DEPTH",
        "DISCOVER_EXCLUDES",
    }
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in allowed or "$" in value or "`" in value:
            raise FleetError("Legacy config must contain literal assignments, not shell code.")
        try:
            parts = shlex.split(value, comments=False)
        except ValueError as exc:
            raise FleetError(
                "Cannot parse legacy config. Convert it to literal assignments."
            ) from exc
        if len(parts) > 1:
            raise FleetError("Legacy values must be quoted or shell-escaped.")
        if key in values:
            raise FleetError(f"Duplicate legacy field: {key}")
        values[key] = parts[0] if parts else ""
    return values


def legacy_list(values: dict[str, str], key: str) -> tuple[str, ...]:
    """Read a JSON-encoded legacy list, written as one quoted shell word."""
    text = values.get(key, "")
    if not text:
        return ()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise FleetError(f"Invalid legacy setting: {key}") from exc
    if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
        raise FleetError(f"Invalid legacy setting: {key}")
    return tuple(data)


class Store:
    def __init__(self, home: Path):
        if (
            not home.is_absolute()
            or home.resolve() == Path("/")
            or any(ord(c) < 32 for c in str(home))
        ):
            raise FleetError("FLEET_HOME must be an absolute, non-root path.")
        safe_path(home)
        self.home = home.resolve()

    @contextmanager
    def lock(self, dry_run: bool = False):
        if dry_run:
            yield
            return
        safe_path(self.home / ".lock")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.home / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FleetError(
                    "Another Agent Fleet command is making changes. Try again."
                ) from exc
            yield
        finally:
            os.close(fd)

    def locations(self) -> dict[str, Path]:
        result = {}
        legacy = self.home / "fleet.env"
        if legacy.exists() or legacy.is_symlink():
            values = parse_legacy(private_read(legacy))
            name = values.get("PROVIDER", "legacy")
            if name not in ("cursor", "devin", "amp", "droid"):
                name = "legacy"
            result[name] = self.home
        instances = self.home / "instances"
        safe_path(instances)
        if instances.exists():
            for directory in sorted(instances.iterdir()):
                safe_path(directory)
                if not directory.is_dir():
                    continue
                if any(
                    (directory / f).exists() or (directory / f).is_symlink()
                    for f in ("fleet.json", "fleet.env")
                ):
                    name = valid_name(directory.name)
                    if name in result:
                        raise FleetError(f"Duplicate instance name: {name}")
                    result[name] = directory
        return result

    def location(self, name: str) -> Path:
        if name != "legacy":
            valid_name(name)
        path = self.locations().get(name, self.home / "instances" / name)
        safe_path(path)
        return path

    def load(self, name: str) -> Instance:
        directory = self.location(name)
        native = directory / "fleet.json"
        legacy = directory / "fleet.env"
        if native.exists() and legacy.exists():
            raise FleetError(
                f"Both fleet.json and fleet.env exist in {directory}; resolve the conflict."
            )
        try:
            if native.exists() or native.is_symlink():
                data = json.loads(private_read(native))
                for key in ("active_workers", "extra_dirs", "discover_dirs", "discover_excludes"):
                    if key in data:
                        data[key] = tuple(data[key])
                config = Instance(**data)
                if config.name != name:
                    raise FleetError("Config name does not match its instance directory.")
            elif legacy.exists() or legacy.is_symlink():
                data = parse_legacy(private_read(legacy))
                count = int(data["WORKER_COUNT"])
                for flag in ("GPU_SPLIT", "USE_SYSTEMD"):
                    if data.get(flag) not in ("yes", "no"):
                        raise FleetError(f"Invalid legacy setting: {flag}")
                config = Instance(
                    name=name,
                    provider=data["PROVIDER"],
                    repo_url=data["REPO_URL"],
                    repo_name=data["REPO_NAME"],
                    runner_id=data.get("RUNNER_ID")
                    or runner_id(
                        directory,
                        data["PROVIDER"],
                        f"{socket.gethostname().split('.')[0]}-"
                        f"{data['PROVIDER']}-{data['REPO_NAME']}",
                    ),
                    worker_count=count,
                    active_workers=tuple(
                        int(i)
                        for i in data.get(
                            "ACTIVE_WORKERS", ",".join(map(str, range(1, count + 1)))
                        ).split(",")
                    ),
                    droid_dir=data.get("DROID_DIR", ""),
                    outpost_name=data.get("OUTPOST_NAME", ""),
                    gpu_split=data["GPU_SPLIT"] == "yes",
                    use_systemd=data["USE_SYSTEMD"] == "yes",
                    extra_dirs=legacy_list(data, "EXTRA_DIRS"),
                    discover_dirs=legacy_list(data, "DISCOVER_DIRS"),
                    discover_depth=int(data.get("DISCOVER_DEPTH") or 0),
                    discover_excludes=legacy_list(data, "DISCOVER_EXCLUDES"),
                )
            else:
                raise FleetError(f"Instance '{name}' is not configured.")
        except (KeyError, TypeError, ValueError) as exc:
            raise FleetError(
                f"Invalid configuration in {directory}. Check required fields."
            ) from exc
        try:
            config.validate()
        except (TypeError, ValueError, AttributeError) as exc:
            raise FleetError(f"Invalid configuration field types in {directory}.") from exc
        return config

    def all(self) -> list[Instance]:
        return [self.load(name) for name in self.locations()]

    def select(self, name: str | None) -> Instance:
        if name:
            return self.load(name)
        names = list(self.locations())
        if len(names) != 1:
            raise FleetError("Select an instance with --instance NAME, or create one with setup.")
        return self.load(names[0])

    def save(self, config: Instance) -> None:
        config.validate()
        directory = self.location(config.name)
        legacy = directory / "fleet.env"
        if legacy.exists():
            fields = {
                "PROVIDER": config.provider,
                "REPO_URL": config.repo_url,
                "REPO_NAME": config.repo_name,
                "RUNNER_ID": config.runner_id,
                "DROID_DIR": config.droid_dir,
                "WORKER_COUNT": str(config.worker_count),
                "ACTIVE_WORKERS": ",".join(map(str, config.active_workers)),
                "GPU_SPLIT": "yes" if config.gpu_split else "no",
                "OUTPOST_NAME": config.outpost_name,
                "USE_SYSTEMD": "yes" if config.use_systemd else "no",
                # Directory lists stay one quoted word: a JSON array survives the legacy parser.
                "EXTRA_DIRS": json.dumps(list(config.extra_dirs)),
                "DISCOVER_DIRS": json.dumps(list(config.discover_dirs)),
                "DISCOVER_DEPTH": str(config.discover_depth),
                "DISCOVER_EXCLUDES": json.dumps(list(config.discover_excludes)),
            }
            private_write(
                legacy, "\n".join(f"{k}={shlex.quote(v)}" for k, v in fields.items()) + "\n"
            )
        else:
            private_write(directory / "fleet.json", json.dumps(asdict(config), indent=2) + "\n")

    def remove_config(self, name: str) -> None:
        directory = self.location(name)
        for filename in ("fleet.json", "fleet.env"):
            path = directory / filename
            if path.exists():
                private_read(path)
                path.unlink()

    def secret(self, config: Instance) -> str:
        filename = {"cursor": "cursor-api-key", "devin": "devin-token"}.get(config.provider)
        if filename is None:
            return ""
        path = self.location(config.name) / filename
        if not path.exists() and not path.is_symlink():
            return ""
        return private_read(path).strip()

    def save_secret(self, config: Instance, secret: str) -> None:
        if not secret or "\n" in secret or "\r" in secret:
            raise FleetError("Credential must be a nonempty single line.")
        filename = {"cursor": "cursor-api-key", "devin": "devin-token"}.get(config.provider)
        if filename is None:
            raise FleetError("This provider uses its CLI login, not a Fleet credential.")
        private_write(self.location(config.name) / filename, secret)
