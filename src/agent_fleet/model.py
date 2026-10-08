from __future__ import annotations

import hashlib
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

PROVIDERS = ("cursor", "devin", "amp", "droid")


class FleetError(Exception):
    """An actionable error safe to display to the user."""


def valid_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value)
        or value == "legacy"
    ):
        raise FleetError("Use 1–32 letters, numbers, hyphens or underscores. 'legacy' is reserved.")
    return value


def runner_id(home: Path, provider: str, label: str = "") -> str:
    label = label or f"{socket.gethostname().split('.')[0]}-{provider}"
    label = re.sub(r"[^A-Za-z0-9-]", "-", label).strip("-") or "agent-fleet"
    return f"{label}-{hashlib.sha256(str(home).encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class Instance:
    name: str
    provider: str
    repo_url: str
    repo_name: str
    runner_id: str
    worker_count: int
    active_workers: tuple[int, ...]
    droid_dir: str = ""
    outpost_name: str = ""
    gpu_split: bool = False
    use_systemd: bool = False

    def validate(self) -> None:
        if self.name != "legacy":
            valid_name(self.name)
        if self.provider not in PROVIDERS:
            raise FleetError("Choose Cursor, Devin, Amp or Droid.")
        if type(self.worker_count) is not int or not 1 <= self.worker_count <= 128:
            raise FleetError("Worker count must be between 1 and 128.")
        if not self.active_workers or any(
            type(i) is not int or not 1 <= i <= self.worker_count for i in self.active_workers
        ):
            raise FleetError("Active workers must be nonempty and within the configured range.")
        if len(set(self.active_workers)) != len(self.active_workers):
            raise FleetError("Active worker numbers must be unique.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.repo_name) or self.repo_name in (".", ".."):
            raise FleetError("Repository name must contain only letters, numbers, '.', '_' or '-'.")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.runner_id):
            raise FleetError("Runner identity must contain only letters, numbers or hyphens.")
        if type(self.gpu_split) is not bool or type(self.use_systemd) is not bool:
            raise FleetError("GPU split and systemd settings must be booleans.")
        for value in (self.repo_url, self.droid_dir, self.outpost_name):
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise FleetError("Configuration fields cannot contain control characters.")
        if self.provider == "droid":
            if self.worker_count != 1 or not Path(self.droid_dir).is_absolute():
                raise FleetError("Droid requires one worker and an absolute local directory.")
            if not Path(self.droid_dir).is_dir() and not self.repo_url:
                raise FleetError("The Droid working directory does not exist.")
        elif not self.repo_url or self.repo_url.startswith("-"):
            raise FleetError("Enter a Git URL or an existing local repository.")
        if self.repo_url and "://" in self.repo_url:
            parsed = urlsplit(self.repo_url)
            if parsed.scheme not in ("https", "http", "ssh", "git", "file"):
                raise FleetError("Use a standard Git transport, not a custom remote helper.")
            if (
                parsed.password
                or parsed.query
                or (parsed.scheme in ("https", "http") and parsed.username)
            ):
                raise FleetError(
                    "Do not embed credentials in repository URLs. Use Git's credential helper."
                )
        if self.repo_url.startswith("ext::"):
            raise FleetError("Git external command transports are not supported.")
        if self.provider == "devin" and not self.outpost_name:
            raise FleetError("Devin requires an Outpost name.")

    @property
    def shared(self) -> bool:
        return self.provider in ("amp", "droid")

    def targets(self, target: str = "all") -> tuple[int, ...]:
        if target == "all" or (self.shared and target == self.runner_id):
            return self.active_workers
        for i in self.active_workers:
            if target in (str(i), self.worker_name(i)):
                return (i,)
        raise FleetError(f"Worker '{target}' is not active. Use 'agent-fleet list' to see workers.")

    def process_indices(self, target: str = "all") -> tuple[int, ...]:
        targets = self.targets(target)
        return (self.active_workers[0],) if self.shared else targets

    def worker_name(self, index: int) -> str:
        return f"{self.runner_id}-{self.repo_name}-w{index}"

    def key(self, index: int) -> str:
        suffix = "" if self.shared else f"-w{index}"
        return f"{self.provider}-{self.runner_id}{suffix}"

    def port(self, index: int) -> int:
        digest = hashlib.sha256(self.key(index).encode()).hexdigest()[:4]
        return 20000 + int(digest, 16) % 20000

    def worker_dir(self, home: Path, index: int) -> Path:
        if self.provider == "droid":
            return Path(self.droid_dir)
        if self.provider == "devin":
            return home / "devin-workers" / f"w{index}"
        return home / "checkouts" / f"{self.repo_name}-w{index}"

    def repo_dir(self, home: Path, index: int) -> Path:
        directory = self.worker_dir(home, index)
        return directory / "repos" / self.repo_name if self.provider == "devin" else directory


def gpu_group(index: int, total: int, count: int) -> str:
    per = max(1, total // count)
    start = (index - 1) * per
    end = total if index == count else min(total, start + per)
    return ",".join(str(i) for i in range(start, end))
