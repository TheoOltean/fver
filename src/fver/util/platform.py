"""What system is this, and where are the tools `fver setup` installs."""

from __future__ import annotations

import functools
import os
from pathlib import Path

from fver.util.proc import which

# Package manager -> install command prefix. The first one found wins.
_MANAGERS: list[tuple[str, str]] = [  # the ones `fver setup` supports
    ("brew", "brew install"),
    ("apt-get", "sudo apt-get install -y"),
    ("dnf", "sudo dnf install -y"),
    ("pacman", "sudo pacman -S --needed"),
]

OPAM_SWITCH = "fver"  # the switch `fver setup` creates


@functools.lru_cache(maxsize=1)
def package_manager() -> tuple[str, str] | None:
    """(manager name, install command prefix) for the first manager on PATH."""
    for name, prefix in _MANAGERS:
        if which(name):
            return name, prefix
    return None


def switch_bin() -> Path:
    """Where the proof toolchain's executables are."""
    root = os.environ.get("OPAMROOT") or str(Path.home() / ".opam")
    return Path(root) / OPAM_SWITCH / "bin"


def tool(name: str) -> str:
    """The executable to run for `name`: the copy in the fver opam switch when
    there is one, else the bare name (PATH)."""
    p = switch_bin() / name
    return str(p) if p.exists() else name
