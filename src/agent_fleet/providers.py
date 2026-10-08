from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from agent_fleet.model import FleetError, Instance, gpu_group

GUIDANCE = {
    "cursor": "Install Cursor CLI, then run cursor-agent login. Dispatch via Cursor → My Machines.",
    "devin": "Install Devin CLI. Create an Outpost and token in Devin Cloud → Settings → Outposts.",
    "amp": "Install Amp CLI and run amp login. Dispatch via Amp → Runner → directory.",
    "droid": "Install Droid CLI and run droid /login. Dispatch via Factory → Droid Computers.",
}


def run(args: list[str], *, timeout: float = 10, cwd: Path | None = None) -> str:
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
    if result.returncode:
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
    env: dict[str, str]
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
            args.append(f"--token={secret}")
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
        args = [executable, "daemon", "--remote-access"]
    return Command(args, env, directory)


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


def readiness(config: Instance, index: int) -> str:
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
            return (
                "local ready"
                if any(
                    x.get("id") == "daemon.loopback" and x.get("status") == "pass"
                    for x in data.get("results", [])
                )
                else "starting"
            )
        return "unverified"
    except (FleetError, ValueError, TypeError, AttributeError, urllib.error.URLError, OSError):
        return "starting"
