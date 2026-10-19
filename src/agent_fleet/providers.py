from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from agent_fleet.model import FleetError, Instance, gpu_group

GUIDANCE = {
    "cursor": "Install Cursor CLI, then run cursor-agent login. Dispatch via Cursor → My Machines.",
    "devin": (
        "Install and log in to Devin CLI, then attach one Outpost worker "
        "to your existing project directories."
    ),
    "amp": "Install Amp CLI and run amp login. Dispatch via Amp → Runner → directory.",
    "droid": (
        "Install Droid CLI, run droid /login, and register with droid computer register. "
        "Dispatch via Factory → Droid Computers."
    ),
}

DROID_LOGIN_HINT = (
    "Factory login is missing or has expired. "
    "Run `droid` and sign in (/login), then restart the worker."
)


def run(
    args: list[str], *, timeout: float = 10, cwd: Path | None = None, check: bool = True
) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FleetError(
            f"Could not run {Path(args[0]).name}. Check installation and connectivity."
        ) from exc
    if result.returncode and check:
        # Child errors can include URL credentials or tokens. Never echo them.
        raise FleetError(f"{Path(args[0]).name} exited with code {result.returncode}.")
    return result.stdout.strip()


def binary(provider: str) -> str:
    names = ("cursor-agent", "agent") if provider == "cursor" else (provider,)
    for name in names:
        if path := shutil.which(name):
            return path
    raise FleetError(GUIDANCE[provider])


def systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        run(["systemctl", "--user", "show-environment"], timeout=3)
        return True
    except FleetError:
        return False


def gpu_count() -> int:
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        return len(run(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"]).splitlines())
    except FleetError:
        return 0


@dataclass
class Command:
    argv: list[str] = field(repr=False)
    env: dict[str, str] = field(repr=False)
    cwd: Path


def build_command(
    config: Instance,
    home: Path,
    index: int,
    *,
    secret: str = "",
    gpus: int = 0,
    executable: str | None = None,
) -> Command:
    executable = executable or binary(config.provider)
    directory = config.worker_dir(home, index)
    env = {}
    if config.gpu_split:
        # One Amp process serves all checkouts; per-checkout GPU isolation is impossible.
        group = (
            ",".join(map(str, range(gpus)))
            if config.shared
            else gpu_group(index, gpus, config.worker_count)
        )
        env["CUDA_VISIBLE_DEVICES"] = group
    if config.provider == "cursor":
        args = [
            executable,
            "worker",
            "start",
            "--name",
            config.worker_name(index),
            "--worker-dir",
            str(directory),
            "--data-dir",
            str(home / "data" / config.worker_name(index)),
            "--management-addr",
            f"127.0.0.1:{config.port(index)}",
        ]
        if secret:
            args += ["--api-key", secret]
    elif config.provider == "devin":
        env["DEVIN_WORKDIR"] = str(directory)
        args = [executable, "worker", "start", f"--outpost={config.outpost_name}"]
        if secret:
            env["DEVIN_OUTPOSTS_TOKEN"] = secret
    elif config.provider == "amp":
        args = [executable, "--no-tui", "--runner-id", config.runner_id, "--no-serve-cwd"]
        # Managed checkouts are startup flags; extra directories are applied with
        # `amp runner dirs add` so they can also change while the runner is up.
        for worker in config.active_workers:
            args += ["--dir", str(config.worker_dir(home, worker))]
        if config.discover_dirs:
            for root in config.discover_dirs:
                args.append(f"--discover-dirs={root}")
            if config.discover_depth:
                args += ["--discover-depth", str(config.discover_depth)]
            for pattern in config.discover_excludes:
                args += ["--discover-exclude", pattern]
    else:
        args = [executable, "daemon", "--remote-control"]
    return Command(args, env, directory)


_DROID_RELAY_RE = re.compile(r"\(Computer:\s*([^,)\r\n]+),\s*ID:\s*([^)\r\n]+)\)")
_DROID_VERSION_RE = re.compile(r"^\s*Version:\s*(\S+)\s*$")


def droid_log_info(logfile: Path | None) -> dict[str, str]:
    """Inspect a Droid worker log for readiness, errors, computer identity and version."""
    info = {"state": "unknown", "error": "", "name": "", "id": "", "version": ""}
    if logfile is None or logfile.is_symlink() or not logfile.is_file():
        return info
    try:
        lines = logfile.read_text(errors="replace").splitlines()
    except OSError:
        return info
    for line in lines:
        if match := _DROID_RELAY_RE.search(line):
            info["name"], info["id"] = match.group(1).strip(), match.group(2).strip()
    start_idx = 0
    for idx, line in enumerate(lines):
        if "Starting droid-" in line or (
            "Starting " in line and "Remote task availability" in line
        ):
            start_idx = idx + 1
    current = lines[start_idx:] if start_idx else lines
    info["state"] = "starting"
    for line in current:
        text = line.split("  ", 1)[-1].strip() if "  " in line else line.strip()
        if match := _DROID_VERSION_RE.match(text):
            info["version"] = match.group(1)
        if "[droid daemon] Daemon is running" in text or "Relay: wss://" in text:
            info["state"] = "ready"
        elif text.startswith("Startup failed:"):
            info["state"] = "failed"
            info["error"] = text.removeprefix("Startup failed:").strip()
        elif "requires a registered computer" in text and not info["error"]:
            info["state"] = "failed"
            info["error"] = text
        elif text.startswith("Worker exited (code ") and info["state"] != "ready":
            info["state"] = "failed"
    return info


def droid_registration(
    logfile: Path | None = None, *, probe_cli: bool = False
) -> dict[str, str] | None:
    """Return Factory Remote Control registration metadata when available."""
    log = droid_log_info(logfile)
    computer_id = log["id"]
    computer_name = log["name"]
    factory_dir = Path.home() / ".factory"
    for filename, key_path in (
        ("computer.json", ("computerId",)),
        ("host.json", ("computerRegistration", "computerId")),
    ):
        candidate = factory_dir / filename
        if not candidate.is_symlink() and candidate.is_file():
            try:
                node = json.loads(candidate.read_text(errors="replace"))
                for part in key_path:
                    node = node.get(part) if isinstance(node, dict) else None
                if isinstance(node, str) and node.strip():
                    computer_id = node.strip()
                    break
            except (OSError, ValueError, TypeError):
                pass
    if probe_cli:
        try:
            listing = run([binary("droid"), "computer", "list"], timeout=5)
            for line in listing.splitlines():
                if "(this machine)" in line:
                    cleaned = line.replace("(this machine)", "").strip()
                    parts = cleaned.split()
                    if len(parts) >= 2:
                        computer_name = parts[0].strip()
                        computer_id = parts[1].strip()
                    elif parts:
                        computer_name = parts[0].strip()
                    break
        except FleetError:
            pass
    if not computer_id and not computer_name:
        return None
    return {
        "id": computer_id,
        "name": computer_name,
        "version": log["version"],
    }


def droid_cli_version() -> str | None:
    try:
        output = run([binary("droid"), "--version"], timeout=3)
    except FleetError:
        return None
    first = output.splitlines()[0].strip() if output else ""
    return first.removeprefix("v") if first else None


def droid_login_state() -> str:
    """Return "ok", "login required", or "unknown" when Droid's auth check is unclear.

    Doctor exits nonzero for unrelated failures such as being offline, so only its JSON counts.
    The verdict has no machine-readable reason, so a signed-out CLI is recognized by its wording;
    if that wording ever changes this degrades to "unknown" rather than blaming the login.
    """
    try:
        output = run(
            [binary("droid"), "doctor", "--checks", "auth", "--json", "--timeout", "3000"],
            timeout=10,
            check=False,
        )
        results = json.loads(output)["results"]
        verdict = next((item for item in results if item.get("id") == "auth.verify"), {})
    except (FleetError, ValueError, KeyError, TypeError, AttributeError):
        return "unknown"
    if verdict.get("status") == "pass":
        return "ok"
    return "login required" if "not logged in" in str(verdict.get("detail")) else "unknown"


def droid_startup_error(logfile: Path | None) -> str:
    """Why a Droid worker failed to start, naming a missing or expired Factory login."""
    error = droid_log_info(logfile)["error"]
    if droid_login_state() != "login required":
        return error
    return f"{DROID_LOGIN_HINT} ({error})" if error else DROID_LOGIN_HINT


def amp_runner(config: Instance) -> dict | None:
    """The running Amp runner entry for this instance, or None when it is not running."""
    try:
        data = json.loads(run([binary("amp"), "runner", "list", "--json"], timeout=5))
    except (FleetError, ValueError, TypeError, AttributeError):
        return None
    for entry in data.get("runners", []):
        if isinstance(entry, dict) and entry.get("runnerId") == config.runner_id:
            return entry
    return None


def amp_served_dirs(config: Instance) -> list[str] | None:
    """Directories the running runner serves, or None when it is not running."""
    entry = amp_runner(config)
    if entry is None:
        return None
    return [
        item["path"]
        for item in entry.get("directories", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("path")
    ]


def amp_dirs_command(config: Instance, verb: str, paths: list[str]) -> list[str]:
    if verb not in ("add", "remove"):
        raise FleetError("Unknown Amp directory action.")
    return [binary("amp"), "runner", "dirs", verb, "--runner-id", config.runner_id, *paths]


def readiness(config: Instance, index: int, logfile: Path | None = None) -> str:
    """Return readiness evidence, never infer remote availability from a live PID."""
    try:
        if config.provider == "cursor":
            # Local management traffic must not go through proxy environment variables.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(
                f"http://127.0.0.1:{config.port(index)}/readyz", timeout=2
            ) as response:
                return "ready" if response.status == 200 else "starting"
        if config.provider == "amp":
            return "local ready" if amp_runner(config) is not None else "starting"
        if config.provider == "droid":
            log = droid_log_info(logfile)
            if log["state"] == "failed":
                return "starting"
            data = json.loads(
                run(
                    [
                        binary("droid"),
                        "doctor",
                        "--daemon",
                        "--json",
                        "--timeout",
                        "2000",
                    ],
                    timeout=5,
                )
            )
            loopback_ok = any(
                x.get("id") == "daemon.loopback" and x.get("status") == "pass"
                for x in data.get("results", [])
            )
            if not loopback_ok:
                return "starting"
            if logfile is not None and logfile.is_file() and log["state"] != "ready":
                return "starting"
            return "local ready"
        return "unverified"
    except (FleetError, ValueError, TypeError, AttributeError, urllib.error.URLError, OSError):
        return "starting"
