"""How fver reads a repository. By default: the source itself, with every
header directory in the tree on the include path and no macros defined. A
build is consulted only when the user points at one (an exported
compile_commands.json, or a capture command) because the project generates
headers or depends on build-time defines."""

from __future__ import annotations

from pathlib import Path

from fver.core.config import BuildConfig


def detect_build(repo_root: Path) -> tuple[BuildConfig, list[str]]:
    """Use an exported compile_commands.json if one is lying around; otherwise
    read the source directly. Returns (config, notes for the user)."""
    build = BuildConfig()
    for cand in ("compile_commands.json", "build/compile_commands.json"):
        if (repo_root / cand).exists():
            build.compile_commands = cand
            return build, [f"build: using the exported {cand} for exact compiler flags"]
    return build, [
        "source: read directly, with the repository's header directories on the include path"
    ]


def resolve_build(repo_root: Path, configured: BuildConfig) -> tuple[BuildConfig, list[str]]:
    """The configured settings if the user chose a source of flags, else the
    detected ones (keeping the user's include/exclude/fallback_flags)."""
    if configured.compile_commands or configured.capture_command:
        return configured, []
    detected, notes = detect_build(repo_root)
    merged = configured.model_copy(update={"compile_commands": detected.compile_commands})
    return merged, notes
