"""Detached supervisor. Credentials are loaded here, never stored in service units."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent_fleet.model import FleetError
from agent_fleet.providers import build_command, gpu_count
from agent_fleet.store import Store, safe_path


def supervise(home: Path, name: str, index: int) -> int:
    os.umask(0o077)
    store = Store(home)
    config = store.load(name)
    config.targets(str(index))
    directory = store.location(name)
    logfile = directory / "logs" / f"{config.key(index)}.log"
    safe_path(logfile)
    for rotated in logfile.parent.glob(logfile.name + ".*"):
        safe_path(rotated)
    logfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(logfile, maxBytes=10 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    logger = logging.getLogger("agent-fleet.worker")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    child = None
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        secret = store.secret(config)
        command = build_command(config, directory, index, secret=secret, gpus=gpu_count())
        if config.provider == "devin" and not secret:
            raise FleetError("Missing Devin token. Save it with agent-fleet credential.")
        if config.provider == "cursor":
            data = directory / "data" / config.worker_name(index)
            safe_path(data)
            data.mkdir(parents=True, exist_ok=True, mode=0o700)
        logger.info(
            "Starting %s. Remote task availability must be confirmed on-platform.",
            config.key(index),
        )
        if stopping:
            return 0
        child = subprocess.Popen(
            command.argv,
            cwd=command.cwd,
            env={**os.environ, **command.env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        if stopping:
            stop(signal.SIGTERM, None)
        if child.stdout is not None:
            for line in child.stdout:
                logger.info(
                    "%s", line.rstrip().replace(secret, "[REDACTED]") if secret else line.rstrip()
                )
        code = child.wait()
        logger.info("Worker exited (code %s).", code)
        return 0 if stopping else code
    except (FleetError, OSError):
        logger.error(
            "Worker startup failed. Check provider installation, credentials and working directory."
        )
        return 1
    finally:
        if child is not None and child.poll() is None:
            stop(signal.SIGTERM, None)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        handler.close()
        logger.removeHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--worker", type=int, required=True)
    args = parser.parse_args()
    try:
        code = supervise(args.home, args.instance, args.worker)
    except (FleetError, OSError):
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
