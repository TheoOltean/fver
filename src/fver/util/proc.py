"""Subprocess helpers with timeouts and captured output."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run(
    argv: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> ProcResult:
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input=input_text,
            check=False,
        )
        return ProcResult(argv, p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return ProcResult(argv, -1, out, err, time.monotonic() - t0, timed_out=True)
    except FileNotFoundError:
        return ProcResult(argv, 127, "", f"{argv[0]}: command not found", time.monotonic() - t0)


def run_shell(
    cmd: str,
    cwd: Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> ProcResult:
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return ProcResult([cmd], p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return ProcResult([cmd], -1, out, err, time.monotonic() - t0, timed_out=True)


def which(name: str) -> str | None:
    return shutil.which(name)


def version_of(argv: list[str], timeout: float = 15.0) -> str | None:
    """Best-effort: run `tool --version`-style command and return the first line."""
    if which(argv[0]) is None:
        return None
    r = run(argv, timeout=timeout)
    text = (r.stdout or r.stderr).strip()
    return text.splitlines()[0] if text else None
