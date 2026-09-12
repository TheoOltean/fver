"""Work out how a repository builds, so `fver scan` can capture exact
compiler flags without the user configuring anything."""

from __future__ import annotations

from pathlib import Path

from fver.core.config import BuildConfig


def detect_build(repo_root: Path) -> tuple[BuildConfig, list[str]]:
    """Build settings inferred from files in the repo, plus notes for the user.
    Used at scan time whenever [build] in config.toml names neither a
    compile_commands.json nor a capture command."""
    notes: list[str] = []
    build = BuildConfig()
    for cand in ("compile_commands.json", "build/compile_commands.json"):
        if (repo_root / cand).exists():
            build.compile_commands = cand
            notes.append(f"build: using {cand}")
            return build, notes
    if (repo_root / "CMakeLists.txt").exists():
        build.compile_commands = "build/compile_commands.json"
        notes.append(
            "build: CMake project; run `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build` "
            "once so build/compile_commands.json exists before `fver scan`"
        )
    elif (repo_root / "meson.build").exists():
        build.compile_commands = "build/compile_commands.json"
        notes.append("build: Meson project; run `meson setup build` once before `fver scan`")
    elif any((repo_root / m).exists() for m in ("Makefile", "makefile", "GNUmakefile")):
        build.capture_command = "bear -- make"
        notes.append("build: Makefile project; `fver scan` captures the build with `bear -- make`")
    else:
        notes.append(
            "build: no build system found; every .c file is compiled with build.fallback_flags"
        )
    return build, notes


def resolve_build(repo_root: Path, configured: BuildConfig) -> tuple[BuildConfig, list[str]]:
    """The configured settings if the user chose a source of flags, else the
    detected ones (keeping the user's include/exclude/fallback_flags)."""
    if configured.compile_commands or configured.capture_command:
        return configured, []
    detected, notes = detect_build(repo_root)
    merged = configured.model_copy(
        update={
            "compile_commands": detected.compile_commands,
            "capture_command": detected.capture_command,
        }
    )
    return merged, notes
