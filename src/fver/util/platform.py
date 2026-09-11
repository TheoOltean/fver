"""Portable answers to "what system is this and how do I install X here".

fver targets every Unix-like system (Linux distributions, macOS, the BSDs).
Nothing else in the code base should special-case an operating system;
it should ask this module.
"""

from __future__ import annotations

import functools
import os
import platform as _platform
import subprocess
import sys
from pathlib import Path

from fver.util.proc import which

# Package manager -> command prefix. Order matters: the first one found wins.
_MANAGERS: list[tuple[str, str]] = [
    ("brew", "brew install"),
    ("apt-get", "sudo apt-get install -y"),
    ("dnf", "sudo dnf install -y"),
    ("yum", "sudo yum install -y"),
    ("pacman", "sudo pacman -S --needed"),
    ("zypper", "sudo zypper install -y"),
    ("apk", "sudo apk add"),
    ("pkg", "sudo pkg install -y"),  # FreeBSD
    ("pkgin", "sudo pkgin install"),  # NetBSD / pkgsrc
    ("pkg_add", "doas pkg_add"),  # OpenBSD
    ("nix-env", "nix-env -iA nixpkgs."),
]


@functools.lru_cache(maxsize=1)
def os_name() -> str:
    """'macos', 'linux', 'freebsd', 'openbsd', 'netbsd', or the lower-cased uname."""
    s = _platform.system().lower()
    return {"darwin": "macos"}.get(s, s)


@functools.lru_cache(maxsize=1)
def package_manager() -> tuple[str, str] | None:
    """(manager name, install command prefix) for the first manager on PATH."""
    for name, prefix in _MANAGERS:
        if which(name):
            return name, prefix
    return None


def install_hint(
    packages: str | dict[str, str],
    url: str | None = None,
    note: str | None = None,
) -> str:
    """Render a one-line install hint for this machine.

    `packages` is either one package name valid everywhere, or a mapping
    from manager name (or "*" for the default) to the package name(s) on that
    manager. Managers with no entry and no "*" are omitted.
    """
    pm = package_manager()
    parts: list[str] = []
    if pm is not None:
        name, prefix = pm
        pkg = packages if isinstance(packages, str) else packages.get(name, packages.get("*"))
        if pkg:
            parts.append(f"{prefix} {pkg}" if not prefix.endswith(".") else f"{prefix}{pkg}")
    if url:
        parts.append(url)
    if note:
        parts.append(note)
    if not parts:
        parts.append("install it with your package manager")
    return " | ".join(parts)


@functools.lru_cache(maxsize=1)
def find_compiler() -> str | None:
    """The first C compiler on PATH, in the order a Unix build would pick it."""
    for name in ("cc", "clang", "gcc"):
        if which(name):
            return name
    return None


@functools.lru_cache(maxsize=32)
def compiler_supports(compiler: str, flags: tuple[str, ...]) -> bool:
    """Whether `compiler` accepts `flags` when compiling an empty C file."""
    if which(compiler) is None:
        return False
    devnull = os.devnull
    try:
        r = subprocess.run(
            [compiler, *flags, "-x", "c", "-c", devnull, "-o", devnull],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def user_bin_dir() -> Path:
    """Where user-level installs put executables (uv, pipx, pip --user)."""
    return Path.home() / ".local" / "bin"


def on_path(directory: Path) -> bool:
    return str(directory) in os.environ.get("PATH", "").split(os.pathsep)


def python_ok() -> bool:
    return sys.version_info >= (3, 11)
