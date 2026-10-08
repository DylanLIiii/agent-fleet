"""Bounded clone streaming without exposing provider output or credentials."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Event

from agent_fleet.model import FleetError

_PROGRESS = re.compile(
    r"^(?:remote:\s*)?"
    r"(Enumerating objects|Counting objects|Compressing objects|Receiving objects|"
    r"Resolving deltas|Updating files|Checking out files):(?:\s*(\d{1,3})%)?"
)


class CloneError(FleetError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _reason(text: str) -> tuple[str, bool]:
    text = text.lower()
    if any(
        x in text
        for x in (
            "authentication failed",
            "could not read username",
            "permission denied (publickey)",
            "repository not found",
            "access denied",
            "requested url returned error: 403",
            "requested url returned error: 401",
        )
    ):
        return (
            "Authentication or repository access was rejected. "
            "Check repository permission and configure Git's credential helper or SSH login.",
            False,
        )
    if "could not resolve" in text:
        return "DNS or proxy name resolution failed. Check your network and proxy settings.", True
    if "no space left on device" in text or "disk quota exceeded" in text:
        return (
            "The destination disk is full or its quota was exceeded. Free space and retry.",
            False,
        )
    if "permission denied" in text:
        return "Local file permission was denied. Check access to the Fleet directory.", False
    if any(
        x in text for x in ("certificate problem", "certificate verification", "ssl certificate")
    ):
        return "TLS certificate verification failed. Check your certificates and proxy.", False
    if any(
        x in text
        for x in (
            "rpc failed",
            "early eof",
            "http/2",
            "connection reset",
            "failed to connect",
            "connection timed out",
            "operation timed out",
            "remote end hung up",
            "could not fetch",
        )
    ):
        return "Git network transfer failed or was interrupted. Check connectivity and retry.", True
    return (
        "Unclassified Git failure. Run git clone manually to inspect its local error; "
        "redact credentials before sharing it.",
        False,
    )


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A transport child may still hold the output pipe after Git itself has exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def clone_repo(
    url: str,
    destination: Path,
    *,
    timeout: float,
    progress: Callable[[str], None] | None = None,
    cancel: Event | None = None,
) -> None:
    if cancel is not None and cancel.is_set():
        raise CloneError("Git clone cancelled. No instance configuration was saved.")
    try:
        process = subprocess.Popen(
            ["git", "clone", "--progress", "--", url, str(destination)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError as exc:
        raise CloneError(
            "Cannot start Git. Check installation and executable permissions."
        ) from exc
    completed = False
    tail: deque[str] = deque(maxlen=40)
    buffer = ""
    last_progress = ""
    deadline = time.monotonic() + timeout

    def remaining_time() -> float:
        if cancel is not None and cancel.is_set():
            raise CloneError("Git clone cancelled. No instance configuration was saved.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CloneError(
                f"Git clone timed out after {timeout:g}s. "
                "For large repositories, increase CLONE_TIMEOUT; check connectivity too."
            )
        return min(remaining, 0.25)

    def consume(line: str) -> None:
        nonlocal last_progress
        tail.append(line[-2048:])
        match = _PROGRESS.match(line.strip())
        if match and progress:
            phase, percent = match.groups()
            message = f"Git: {phase}" + (
                f" {percent}%" if percent is not None and int(percent) <= 100 else "…"
            )
            if message != last_progress:
                # Only allowlisted phase names and percentages leave this module.
                progress(message)
                last_progress = message

    try:
        if process.stdout is None:
            raise CloneError("Git clone output stream is unavailable.")
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                events = selector.select(remaining_time())
                if not events:
                    continue
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                buffer += chunk.decode(errors="replace")
                lines = re.split(r"[\r\n]", buffer)
                buffer = lines.pop()
                for line in lines:
                    if line:
                        consume(line)
                # Prevent an unterminated third-party output line from growing without bounds.
                buffer = buffer[-4096:]
        if buffer:
            consume(buffer)
        while True:
            try:
                code = process.wait(timeout=remaining_time())
                break
            except subprocess.TimeoutExpired:
                continue
        completed = code == 0
        if code:
            reason, retryable = _reason("\n".join(tail))
            raise CloneError(f"Git clone failed (exit {code}): {reason}", retryable=retryable)
        if progress:
            progress("Git clone completed.")
    except OSError as exc:
        raise CloneError(
            "Git clone I/O failed. Check the local filesystem and executable."
        ) from exc
    finally:
        if not completed:
            _stop(process)
        if process.stdout is not None:
            process.stdout.close()
