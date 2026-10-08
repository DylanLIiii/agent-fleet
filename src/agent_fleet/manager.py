from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from agent_fleet.model import FleetError, Instance, runner_id
from agent_fleet.providers import binary, gpu_count, readiness, run, systemd_available
from agent_fleet.store import Store, private_read, private_write, safe_path


def make_instance(
    store: Store,
    *,
    name: str,
    provider: str,
    source: str,
    count: int = 4,
    label: str = "",
    backend: str = "auto",
    outpost: str = "",
    gpu_split: bool = False,
    preview: bool = False,
) -> Instance:
    if backend not in ("auto", "process", "systemd"):
        raise FleetError("Choose auto, process or systemd as the backend.")
    if provider == "droid":
        repo_url, droid_dir = "", str(Path(source).expanduser())
        repo_name = Path(droid_dir).name
        count = 1
    else:
        droid_dir = ""
        local = Path(source).expanduser()
        repo_url = source
        if local.is_dir():
            try:
                repo_url = run(["git", "-C", str(local), "remote", "get-url", "origin"])
            except FleetError:
                if run(["git", "-C", str(local), "rev-parse", "--is-bare-repository"]) != "true":
                    raise FleetError("Local checkout needs an origin remote.") from None
        repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        # SCP-style remotes can omit the owner segment: host:repository.git.
        repo_name = repo_name.rsplit(":", 1)[-1]
    systemd = backend == "systemd" or (backend == "auto" and not preview and systemd_available())
    config = Instance(
        name,
        provider,
        repo_url,
        repo_name,
        runner_id(store.home / "instances" / name, provider, label),
        count,
        tuple(range(1, count + 1)),
        droid_dir=droid_dir,
        outpost_name=outpost,
        gpu_split=gpu_split,
        use_systemd=systemd,
    )
    config.validate()
    return config


def process_birth(pid: int) -> str | None:
    if pid <= 1:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


@dataclass(frozen=True)
class WorkerStatus:
    instance: str
    provider: str
    index: int
    name: str
    state: str
    directory: Path
    backend: str


class Manager:
    def __init__(self, store: Store, *, dry_run: bool = False, ready_timeout: float = 20):
        if not math.isfinite(ready_timeout) or ready_timeout <= 0:
            raise FleetError("Readiness timeout must be positive.")
        self.store = store
        self.dry_run = dry_run
        self.ready_timeout = ready_timeout

    def paths(self, config: Instance, index: int) -> tuple[Path, Path, Path]:
        home = self.store.location(config.name)
        key = config.key(index)
        return (
            home / "run" / f"{key}.pid",
            home / "logs" / f"{key}.log",
            Path.home() / ".config/systemd/user" / f"agent-fleet-{key}.service",
        )

    def _pid(self, path: Path) -> tuple[int, str] | None:
        if not path.exists() and not path.is_symlink():
            return None
        try:
            pid, birth = private_read(path).split()
            if not pid.isdigit() or not birth.isdigit() or int(pid) <= 1:
                raise ValueError
            return int(pid), birth
        except ValueError as exc:
            raise FleetError(f"Invalid PID record; inspect it manually: {path}") from exc

    def running(self, config: Instance, index: int) -> bool:
        pidfile, _, unit = self.paths(config, index)
        if config.use_systemd:
            try:
                run(["systemctl", "--user", "is-active", "--quiet", unit.name], timeout=3)
                return True
            except FleetError:
                return False
        identity = self._pid(pidfile)
        return identity is not None and process_birth(identity[0]) == identity[1]

    def status(self, name: str | None = None, *, probe: bool = True) -> list[WorkerStatus]:
        configs = [self.store.load(name)] if name else self.store.all()
        result = []

        def evidence(config: Instance, index: int) -> str:
            if not self.running(config, index):
                return "stopped"
            return readiness(config, index) if probe else "running"

        # Slow provider probes must not serialize hundreds of independent workers.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                (config.name, index): pool.submit(evidence, config, index)
                for config in configs
                for index in config.process_indices()
            }
            for config in configs:
                for index in config.active_workers:
                    process_index = config.active_workers[0] if config.shared else index
                    state = futures[(config.name, process_index)].result()
                    result.append(
                        WorkerStatus(
                            config.name,
                            config.provider,
                            index,
                            config.worker_name(index),
                            state,
                            config.worker_dir(self.store.location(config.name), index),
                            "systemd" if config.use_systemd else "process",
                        )
                    )
        return result

    def _assert_repo(self, config: Instance, path: Path) -> None:
        safe_path(path / ".git")
        if not (path / ".git").is_dir():
            raise FleetError(f"Not an independent Git checkout: {path}")
        if run(["git", "-C", str(path), "remote", "get-url", "origin"]) != config.repo_url:
            raise FleetError(f"Checkout origin does not match this instance: {path}")

    def _prepare(self, config: Instance) -> list[str]:
        home = self.store.location(config.name)
        actions = []
        if config.provider == "droid":
            return [f"Use existing directory {config.droid_dir}; never clone or delete it."]
        for index in config.active_workers:
            destination = config.repo_dir(home, index)
            safe_path(destination)
            if destination.exists():
                self._assert_repo(config, destination)
                actions.append(f"Keep existing checkout {destination}")
                continue
            actions.append(f"Clone an independent checkout → {destination}")
            if self.dry_run:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for attempt in range(3):
                temporary = Path(tempfile.mkdtemp(prefix=".fleet-clone-", dir=destination.parent))
                clone = temporary / "repo"
                try:
                    run(["git", "clone", "--", config.repo_url, str(clone)], timeout=300)
                    private_write(clone / ".git/agent-fleet-owner", config.runner_id + "\n")
                    safe_path(destination)
                    if destination.exists():
                        raise FleetError(f"Destination appeared during clone: {destination}")
                    # os.rename must never overwrite a preexisting directory, even if empty.
                    destination.mkdir(mode=0o700)
                    for child in clone.iterdir():
                        child.rename(destination / child.name)
                    clone.rmdir()
                    break
                except FleetError:
                    if attempt == 2:
                        raise
                    time.sleep(1)
                finally:
                    # Only delete the temporary directory created by this operation.
                    shutil.rmtree(temporary)
        return actions

    def setup(self, config: Instance, *, secret: str = "") -> list[str]:
        config.validate()
        with self.store.lock(self.dry_run):
            existing = self.store.locations()
            if config.name in existing:
                raise FleetError(
                    "This instance already exists. Use repair or add; setup never overwrites."
                )
            if config.provider == "droid" and any(x.provider == "droid" for x in self.store.all()):
                raise FleetError("Only one Droid Computer can be registered per machine.")
            if config.provider == "devin" and not secret and not self.dry_run:
                raise FleetError("Enter your Devin Outpost token.")
            actions = self._prepare(config)
            actions.append(f"Save private configuration for '{config.name}' (not started).")
            if not self.dry_run:
                if secret:
                    self.store.save_secret(config, secret)
                self.store.save(config)
            return actions

    def repair(self, name: str | None) -> list[str]:
        with self.store.lock(self.dry_run):
            return self._prepare(self.store.select(name))

    def add(self, name: str | None, count: int = 1) -> list[str]:
        with self.store.lock(self.dry_run):
            config = self.store.select(name)
            if config.provider == "droid":
                raise FleetError("Droid supports one Computer per machine, not multiple workers.")
            if count < 1 or config.worker_count + count > 128:
                raise FleetError("Add a positive number of workers, up to 128 total.")
            if config.shared or config.gpu_split:
                if any(self.running(config, i) for i in config.process_indices()):
                    raise FleetError(
                        "Stop all workers before changing Amp directories or GPU allocation."
                    )
            updated = replace(
                config,
                worker_count=config.worker_count + count,
                active_workers=config.active_workers
                + tuple(range(config.worker_count + 1, config.worker_count + count + 1)),
            )
            updated.validate()
            actions = self._prepare(updated)
            if not self.dry_run:
                self.store.save(updated)
            return actions + [f"Added {count} worker(s). Start them when you are ready."]

    def _unit_owned(self, config: Instance, index: int) -> Path:
        unit = self.paths(config, index)[2]
        safe_path(unit)
        if not unit.exists():
            raise FleetError(f"Cannot verify systemd service ownership: {unit}")
        info = unit.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise FleetError(f"Unsafe systemd unit ownership or permissions: {unit}")
        marker = f"# agent-fleet managed: {config.key(index)}"
        if marker not in unit.read_text().splitlines():
            raise FleetError(f"Refusing to manage an unowned systemd service: {unit}")
        return unit

    def _launcher(self, config: Instance, index: int) -> list[str]:
        return [
            sys.executable,
            "-m",
            "agent_fleet.runtime",
            "--home",
            str(self.store.home),
            "--instance",
            config.name,
            "--worker",
            str(index),
        ]

    def _start(self, config: Instance, index: int) -> str:
        home = self.store.location(config.name)
        if self.dry_run:
            backend = "systemd" if config.use_systemd else "process"
            return f"Would start {config.key(index)} using {backend}."
        binary(config.provider)
        if config.provider != "droid":
            for worker in config.active_workers if config.shared else (index,):
                self._assert_repo(config, config.repo_dir(home, worker))
        if config.provider == "devin" and not self.store.secret(config):
            raise FleetError("Missing Devin token. Use 'agent-fleet credential' to save one.")
        pidfile, logfile, unit = self.paths(config, index)
        if config.use_systemd:
            if unit.exists() or unit.is_symlink():
                self._unit_owned(config, index)
        if not self.running(config, index):
            if config.use_systemd:
                # ExecStart is systemd syntax, not shell syntax. Escape specifiers and expansions.
                def quote(value: str) -> str:
                    value = value.replace("\\", "\\\\").replace('"', '\\"')
                    value = value.replace("%", "%%").replace("$", "$$")
                    return f'"{value}"'

                command = " ".join(quote(x) for x in self._launcher(config, index))
                private_write(
                    unit,
                    (
                        f"# agent-fleet managed: {config.key(index)}\n[Unit]\n"
                        f"Description=Agent Fleet {config.provider}\nAfter=network-online.target\n"
                        f"[Service]\nType=simple\nExecStart={command}\nUMask=0077\n"
                        "Restart=on-failure\nRestartSec=5\nKillMode=control-group\n"
                        "TimeoutStopSec=15\n[Install]\nWantedBy=default.target\n"
                    ),
                )
                run(["systemctl", "--user", "daemon-reload"])
                run(["systemctl", "--user", "enable", "--now", unit.name])
            else:
                if self._pid(pidfile):
                    raise FleetError(f"Stale PID record. Run stop to inspect/clear it: {pidfile}")
                safe_path(logfile)
                logfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                pidfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # The supervisor records diagnostics without printing credentials here.
                process = subprocess.Popen(
                    self._launcher(config, index),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
                birth = process_birth(process.pid)
                if birth is None:
                    process.wait(timeout=5)
                    raise FleetError(
                        "Worker exited before its identity could be saved. Check logs."
                    )
                try:
                    private_write(pidfile, f"{process.pid} {birth}\n")
                except BaseException:
                    process.terminate()
                    process.wait(timeout=15)
                    raise
        deadline = time.monotonic() + self.ready_timeout
        try:
            # Allow the supervisor to exec the provider before querying readiness.
            time.sleep(min(0.3, self.ready_timeout))
            while time.monotonic() < deadline:
                if not self.running(config, index):
                    raise FleetError("Worker exited during startup. Check its logs.")
                evidence = readiness(config, index)
                if not self.running(config, index):
                    raise FleetError("Worker exited during its readiness probe. Check logs.")
                if evidence in ("ready", "local ready", "unverified"):
                    # Devin has no readiness probe; require a brief stable process lifetime.
                    if evidence == "unverified":
                        time.sleep(min(2, self.ready_timeout))
                        if not self.running(config, index):
                            raise FleetError("Worker exited during startup. Check its logs.")
                    return (
                        f"{config.key(index)}: {evidence}. "
                        "Remote dispatch must be confirmed on-platform."
                    )
                time.sleep(0.3)
            raise FleetError(
                "Readiness timed out. Check logs, authentication and provider connectivity."
            )
        except FleetError:
            self._stop(config, index)
            raise

    def _stop(self, config: Instance, index: int) -> str:
        if self.dry_run:
            return f"Would stop {config.key(index)}."
        pidfile, _, unit = self.paths(config, index)
        if config.use_systemd:
            if not unit.exists() and not unit.is_symlink():
                if self.running(config, index):
                    raise FleetError(
                        "Service is running without an ownership file; inspect it manually."
                    )
                return f"{config.key(index)} is already stopped."
            self._unit_owned(config, index)
            run(["systemctl", "--user", "disable", "--now", unit.name], timeout=20)
            if self.running(config, index):
                raise FleetError("Service did not stop; configuration has been kept.")
        else:
            identity = self._pid(pidfile)
            if identity is None:
                return f"{config.key(index)} is already stopped."
            pid, birth = identity
            current = process_birth(pid)
            if current is not None and current != birth:
                raise FleetError(
                    "PID was reused. Refusing to signal it or delete its identity record."
                )
            if current == birth:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 12
                while process_birth(pid) == birth and time.monotonic() < deadline:
                    time.sleep(0.1)
                if process_birth(pid) == birth:
                    raise FleetError(
                        "Worker did not stop. PID record and configuration have been kept."
                    )
            private_read(pidfile)
            pidfile.unlink()
        return f"Stopped {config.key(index)}."

    def control(self, action: str, name: str | None, target: str = "all") -> list[str]:
        if action not in ("start", "stop", "restart"):
            raise FleetError("Unknown process action.")
        with self.store.lock(self.dry_run):
            config = self.store.select(name)
            result = []
            for index in config.process_indices(target):
                if action in ("stop", "restart"):
                    result.append(self._stop(config, index))
                if action in ("start", "restart"):
                    result.append(self._start(config, index))
            return result

    def logs(self, name: str | None, target: str = "all", lines: int = 100) -> str:
        if not 1 <= lines <= 10000:
            raise FleetError("Choose between 1 and 10000 log lines.")
        config = self.store.select(name)
        index = config.targets(target)[0]
        _, logfile, _ = self.paths(config, index)
        safe_path(logfile)
        if config.use_systemd:
            # Native supervisor logs live in files even under systemd; legacy units use journald.
            if not logfile.exists():
                unit = self._unit_owned(config, index)
                text = run(
                    [
                        "journalctl",
                        "--user",
                        "-u",
                        unit.name,
                        "-n",
                        str(lines),
                        "--no-pager",
                        "-o",
                        "cat",
                    ]
                )
            else:
                text = self._tail(logfile, lines)
        else:
            text = (
                self._tail(logfile, lines)
                if logfile.exists()
                else "No logs yet. Start this worker."
            )
        secret = self.store.secret(config)
        return text.replace(secret, "[REDACTED]") if secret else text

    @staticmethod
    def _tail(path: Path, lines: int) -> str:
        private_read_check = path.stat()
        if private_read_check.st_uid != os.getuid() or private_read_check.st_mode & 0o077:
            raise FleetError(f"Log file must be private: {path}")
        with path.open(errors="replace") as stream:
            return "".join(deque(stream, maxlen=lines)).rstrip()

    def assert_deletable(self, config: Instance, index: int) -> Path:
        home = self.store.location(config.name)
        directory = config.repo_dir(home, index)
        if config.provider == "droid":
            raise FleetError("Droid directories are never deleted.")
        safe_path(directory)
        allowed = home / ("devin-workers" if config.provider == "devin" else "checkouts")
        if not directory.resolve().is_relative_to(allowed.resolve()):
            raise FleetError("Checkout is outside the managed workspace.")
        self._assert_repo(config, directory)
        marker = directory / ".git/agent-fleet-owner"
        if private_read(marker).strip() != config.runner_id:
            raise FleetError("Checkout ownership does not match this instance.")
        if run(
            [
                "git",
                "-C",
                str(directory),
                "status",
                "--porcelain",
                "--ignored",
                "--untracked-files=all",
            ]
        ):
            raise FleetError(
                f"Checkout has changed, untracked or ignored files. Keep it: {directory}"
            )
        if run(
            ["git", "-C", str(directory), "log", "--branches", "--not", "--remotes", "--oneline"]
        ):
            raise FleetError(f"Checkout contains unpushed commits. Keep it: {directory}")
        return directory

    def remove(
        self, name: str | None, target: str = "all", *, delete_checkouts: bool = False
    ) -> list[str]:
        with self.store.lock(self.dry_run):
            config = self.store.select(name)
            targets = config.targets(target)
            if delete_checkouts and config.provider != "droid":
                for index in targets:
                    self.assert_deletable(config, index)
            result = [self._stop(config, index) for index in config.process_indices(target)]
            for index in targets:
                if delete_checkouts and config.provider != "droid":
                    directory = self.assert_deletable(config, index)
                    result.append(
                        f"{'Would delete' if self.dry_run else 'Delete'} clean checkout {directory}"
                    )
                    if not self.dry_run:
                        shutil.rmtree(directory)
            remaining = tuple(i for i in config.active_workers if i not in targets)
            if not self.dry_run:
                if remaining:
                    self.store.save(replace(config, active_workers=remaining))
                else:
                    self.store.remove_config(config.name)
                if config.use_systemd:
                    for index in config.process_indices(target):
                        unit = self.paths(config, index)[2]
                        if unit.exists():
                            self._unit_owned(config, index).unlink()
                    run(["systemctl", "--user", "daemon-reload"])
            result.append("Removed selected workers. Logs, credentials and data are retained.")
            if config.provider == "amp" and remaining:
                result.append("Amp serves the remaining directories after you start it again.")
            return result

    def doctor(self, name: str | None = None) -> list[tuple[str, str, str]]:
        checks = []
        checks.append(("pass" if shutil.which("git") else "fail", "Git", "Required for checkouts."))
        checks.append(("info", "NVIDIA GPUs", str(gpu_count())))
        for config in [self.store.load(name)] if name else self.store.all():
            try:
                binary(config.provider)
                checks.append(("pass", config.name, "Provider CLI found."))
            except FleetError as exc:
                checks.append(("fail", config.name, str(exc)))
            for index in config.active_workers:
                try:
                    if config.provider != "droid":
                        self._assert_repo(
                            config, config.repo_dir(self.store.location(config.name), index)
                        )
                    checks.append(("pass", f"{config.name} / {index}", "Workspace verified."))
                except FleetError as exc:
                    checks.append(("fail", f"{config.name} / {index}", str(exc)))
            if config.provider == "devin":
                checks.append(
                    (
                        "pass" if self.store.secret(config) else "fail",
                        config.name,
                        "Devin token is saved."
                        if self.store.secret(config)
                        else "Devin token is missing.",
                    )
                )
            checks.append(
                ("info", config.name, "A live process does not prove remote task availability.")
            )
        return checks
