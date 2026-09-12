"""Locate, capture or synthesise compile_commands.json and turn it into
TranslationUnits filtered by the project's include/exclude globs."""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from fver.core.config import BuildConfig
from fver.core.models import TranslationUnit
from fver.util.proc import run_shell

log = logging.getLogger("fver.build")

_CANDIDATES = ("compile_commands.json", "build/compile_commands.json")


@dataclass
class BuildCapture:
    tus: list[TranslationUnit]
    source: str  # "config" | "found:<path>" | "captured:<path>" | "synthesised"
    warnings: list[str] = field(default_factory=list)


def find_compile_commands(repo_root: Path, configured: str | None) -> Path | None:
    """Return the compile_commands.json to use, or None."""
    if configured:
        p = (repo_root / configured).resolve()
        return p if p.exists() else None
    for rel in _CANDIDATES:
        p = repo_root / rel
        if p.exists():
            return p
    for child in sorted(repo_root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            p = child / "compile_commands.json"
            if p.exists():
                return p
    return None


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a repo-relative glob with ** support into a full-match regex.

    `**/` matches zero or more leading directories, `/**` at the end matches
    anything below, `*` and `?` never cross a `/`.
    """
    pat = pattern.strip("/")
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_any(rel: str, patterns: list[str]) -> bool:
    return any(_glob_to_regex(p).match(rel) for p in patterns)


def is_included(rel_path: str, build: BuildConfig) -> bool:
    """Exclude wins over include. Paths are repo-relative, posix."""
    if _matches_any(rel_path, build.exclude):
        return False
    return _matches_any(rel_path, build.include)


def _entry_argv(entry: dict) -> list[str]:
    if entry.get("arguments"):
        return list(entry["arguments"])
    if entry.get("command"):
        return shlex.split(entry["command"])
    return []


def parse_compile_commands(
    path: Path, repo_root: Path, build: BuildConfig
) -> tuple[list[TranslationUnit], list[str]]:
    """Parse compile_commands.json into TranslationUnits (filtered, deduped)."""
    warnings: list[str] = []
    try:
        entries = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"cannot read {path}: {e}"]
    if not isinstance(entries, list):
        return [], [f"{path}: expected a JSON array"]

    tus: list[TranslationUnit] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or "file" not in entry:
            continue
        directory = Path(entry.get("directory") or repo_root)
        if not directory.is_absolute():
            directory = repo_root / directory
        file = Path(entry["file"])
        if not file.is_absolute():
            file = directory / file
        file = file.resolve()
        if file.suffix != ".c":
            continue
        try:
            rel = file.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            warnings.append(f"skipping {file}: outside repository")
            continue
        if not file.exists():
            warnings.append(f"skipping {rel}: file does not exist")
            continue
        if not is_included(rel, build):
            continue
        if rel in seen:
            continue
        argv = _entry_argv(entry)
        if not argv:
            warnings.append(f"skipping {rel}: entry has neither 'arguments' nor 'command'")
            continue
        seen.add(rel)
        tus.append(
            TranslationUnit(
                id=TranslationUnit.make_id(rel, argv),
                source_path=rel,
                directory=str(directory.resolve()),
                arguments=argv,
            )
        )
    return tus, warnings


def synthesise(repo_root: Path, build: BuildConfig, compiler: str) -> list[TranslationUnit]:
    """No build database: compile every included .c with the fallback flags."""
    tus: list[TranslationUnit] = []
    for file in sorted(repo_root.rglob("*.c")):
        if any(part.startswith(".") for part in file.relative_to(repo_root).parts):
            continue
        rel = file.relative_to(repo_root).as_posix()
        if not is_included(rel, build):
            continue
        argv = [compiler, *build.fallback_flags, "-c", rel]
        tus.append(
            TranslationUnit(
                id=TranslationUnit.make_id(rel, argv),
                source_path=rel,
                directory=str(repo_root.resolve()),
                arguments=argv,
            )
        )
    return tus


def _bear_with_output(cmd: str, output: Path) -> str:
    """`bear -- make` writes compile_commands.json into the cwd, i.e. the user's
    repository. Redirect it into fver's own work directory unless the user
    already chose an output path."""
    stripped = cmd.lstrip()
    if stripped.startswith("bear ") and "--output" not in stripped and "-o " not in stripped:
        return stripped.replace("bear ", f"bear --output {output} ", 1)
    return cmd


def capture_build(
    repo_root: Path,
    build: BuildConfig,
    compiler: str = "cc",
    work_dir: Path | None = None,
) -> BuildCapture:
    """The full policy: configured path, then discovery, then capture command,
    then synthesis. `work_dir` (normally <repo>/.fver/work) receives anything
    the capture command produces so the user's tree stays untouched."""
    from fver.build.detect import resolve_build

    repo_root = repo_root.resolve()
    warnings: list[str] = []
    build, notes = resolve_build(repo_root, build)
    for n in notes:
        log.info(n)
    path = find_compile_commands(repo_root, build.compile_commands)
    source = "config" if (path and build.compile_commands) else (f"found:{path}" if path else "")

    if path is None and build.capture_command:
        captured: Path | None = None
        kept: Path | None = None
        cmd = build.capture_command
        if work_dir is not None:
            work_dir.mkdir(parents=True, exist_ok=True)
            kept = work_dir / "compile_commands.json"
            captured = work_dir / "compile_commands.new.json"
            cmd = _bear_with_output(cmd, captured)
        log.info("running build capture command: %s", cmd)
        r = run_shell(cmd, cwd=repo_root, timeout=3600)
        if not r.ok:
            warnings.append(
                f"capture command exited {r.returncode}; using whatever it captured. "
                f"Last output: {r.stderr.strip()[-300:]}"
            )
        if captured is not None and captured.exists():
            path = captured
        else:
            path = find_compile_commands(repo_root, build.compile_commands)
        if path is not None:
            source = f"captured:{path}"
            try:
                empty = not json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                empty = True
            if empty:
                if kept is not None and kept.exists():
                    warnings.append(
                        "the capture command compiled nothing (build already up to date); "
                        f"reusing the previous capture at {kept}. Run the project's clean "
                        "target (e.g. `make clean`) and scan again to refresh it."
                    )
                    path = kept
                    source = f"captured(previous):{kept}"
                else:
                    warnings.append(
                        "the capture command produced an empty compile_commands.json: the build "
                        "was probably already up to date, so nothing was compiled. Run the "
                        "project's clean target (e.g. `make clean`) and scan again."
                    )
                    path = None
            elif kept is not None and path == captured:
                kept.write_text(
                    path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
                )
                path = kept
                source = f"captured:{kept}"
        if captured is not None and captured.exists():
            captured.unlink()

    if path is not None:
        tus, w = parse_compile_commands(path, repo_root, build)
        warnings.extend(w)
        return BuildCapture(tus=tus, source=source, warnings=warnings)

    warnings.append(
        "no build system found; compiling every included .c file with "
        f"fallback flags {build.fallback_flags}. If the project does build some other way, "
        "set build.capture_command (e.g. 'bear -- make') or build.compile_commands."
    )
    return BuildCapture(
        tus=synthesise(repo_root, build, compiler), source="synthesised", warnings=warnings
    )
