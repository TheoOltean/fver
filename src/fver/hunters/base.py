"""The Hunter protocol and helpers shared by all bug finders."""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from pathlib import Path
from typing import Protocol, runtime_checkable

from fver.core.config import HuntersConfig
from fver.core.models import Finding, FunctionInfo, ToolStatus, TranslationUnit

# Kinds that represent an actual class of undefined behaviour. Everything
# else a hunter reports (bounds reached, unsupported constructs) is
# informational and must not turn into a BUG_FOUND claim.
UB_KINDS: frozenset[str] = frozenset(
    {
        "out_of_bounds",
        "use_after_free",
        "double_free",
        "null_or_invalid_deref",
        "signed_overflow",
        "division_by_zero",
        "undefined_shift",
        "pointer_overflow",
        "uninitialised_read",
        "memory_leak",
        "invalid_pointer_arith",
        "data_race",
        "undefined_behaviour",
    }
)


@runtime_checkable
class Hunter(Protocol):
    name: str

    def doctor(self) -> list[ToolStatus]: ...

    def run(
        self,
        tus: list[TranslationUnit],
        functions: list[FunctionInfo],
        repo_root: Path,
        workdir: Path,
        config: HuntersConfig,
    ) -> list[Finding]: ...


class FunctionLocator:
    """Map (source_path, line) to the FunctionInfo whose range contains it."""

    def __init__(self, functions: list[FunctionInfo]):
        by_file: dict[str, list[FunctionInfo]] = defaultdict(list)
        for f in functions:
            by_file[f.source_path].append(f)
        self._by_file: dict[str, tuple[list[int], list[FunctionInfo]]] = {}
        for path, fns in by_file.items():
            fns.sort(key=lambda f: f.start_line)
            self._by_file[path] = ([f.start_line for f in fns], fns)
        self._by_name: dict[str, list[FunctionInfo]] = defaultdict(list)
        for f in functions:
            self._by_name[f.name].append(f)

    def locate(self, source_path: str, line: int | None) -> FunctionInfo | None:
        entry = self._by_file.get(source_path)
        if entry is None or line is None:
            return None
        starts, fns = entry
        i = bisect.bisect_right(starts, line) - 1
        if i >= 0 and fns[i].start_line <= line <= fns[i].end_line:
            return fns[i]
        return None

    def by_name(self, name: str, source_path: str | None = None) -> FunctionInfo | None:
        cands = self._by_name.get(name, [])
        if source_path is not None:
            for f in cands:
                if f.source_path == source_path:
                    return f
        return cands[0] if cands else None


def normalise_source_path(path: str, repo_root: Path, tu_dir: str | None = None) -> str:
    """Turn a path a tool printed into the repo-relative posix form used as
    the canonical file identifier. Returns the input unchanged when it does
    not point inside the repo."""
    p = Path(path)
    if not p.is_absolute() and tu_dir:
        p = Path(tu_dir) / p
    if not p.is_absolute():
        p = repo_root / p
    try:
        return p.resolve().relative_to(repo_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path


_KEEP_FLAG = re.compile(
    r"^-(I|D|U|std=|include|isystem|iquote|idirafter|f(?:no-)?ms-extensions|m32|m64)"
)
_KEEP_WITH_ARG = {"-I", "-D", "-U", "-include", "-isystem", "-iquote", "-idirafter"}


def preprocessor_flags(arguments: list[str]) -> list[str]:
    """Extract the -I/-D/-U/-std/-include family from a compile argv and
    drop everything else (the compiler name, -c, -o, warnings, optimisation)."""
    out: list[str] = []
    it = iter(arguments[1:] if arguments else [])
    for a in it:
        if a in _KEEP_WITH_ARG:
            nxt = next(it, None)
            if nxt is not None:
                out.extend([a, nxt])
        elif _KEEP_FLAG.match(a):
            out.append(a)
    return out


def truncate_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"


def builtin_hunters() -> dict[str, type]:
    from fver.hunters.cbmc import CbmcHunter
    from fver.hunters.sanitizers import SanitizerHunter

    return {
        CbmcHunter.name: CbmcHunter,
        SanitizerHunter.name: SanitizerHunter,
    }


def enabled_hunters(config: HuntersConfig, only: list[str] | None = None) -> list[Hunter]:
    """CBMC always runs; the sanitizer hunter runs when the project has a
    test command to drive. `only` restricts to the named hunters."""
    table = builtin_hunters()
    names = [n for n in (only or table) if n in table]
    out: list[Hunter] = []
    for n in names:
        if n == "sanitizers" and only is None and not config.test_command:
            continue
        out.append(table[n]())
    return out
