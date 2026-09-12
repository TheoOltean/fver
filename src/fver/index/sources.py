"""Which .c files to prove and how to read each one: no build involved.

Every included file is compiled with its own directory and every header
directory in the repository on the include path, and no macros defined.
"""

from __future__ import annotations

import re
from pathlib import Path

from fver.core.config import SourcesConfig
from fver.core.models import TranslationUnit

FLAGS = ["-std=c11"]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """A repo-relative glob with ** support, as a full-match regex. `**/`
    matches zero or more leading directories; `*` and `?` never cross `/`."""
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


def is_included(rel_path: str, sources: SourcesConfig) -> bool:
    """Exclude wins over include. Paths are repo-relative, posix."""
    if any(_glob_to_regex(p).match(rel_path) for p in sources.exclude):
        return False
    return any(_glob_to_regex(p).match(rel_path) for p in sources.include)


def header_dirs(repo_root: Path) -> list[str]:
    """Every directory in the repository that holds a header, repo-relative,
    shallowest first."""
    dirs: set[str] = set()
    for h in repo_root.rglob("*.h"):
        rel = h.relative_to(repo_root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        dirs.add(rel.parent.as_posix())
    return sorted(dirs, key=lambda d: (d.count("/"), d))


def list_sources(
    repo_root: Path, sources: SourcesConfig, compiler: str = "cc"
) -> list[TranslationUnit]:
    """One translation unit per included .c file."""
    repo_root = repo_root.resolve()
    tus: list[TranslationUnit] = []
    incs = header_dirs(repo_root)
    for file in sorted(repo_root.rglob("*.c")):
        rel_parts = file.relative_to(repo_root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        rel = file.relative_to(repo_root).as_posix()
        if not is_included(rel, sources):
            continue
        own = file.parent.relative_to(repo_root).as_posix()
        order = [own] + [d for d in incs if d != own]
        argv = [compiler, *FLAGS, *[f"-I{d}" for d in order], "-c", rel]
        tus.append(
            TranslationUnit(
                id=TranslationUnit.make_id(rel, argv),
                source_path=rel,
                directory=str(repo_root),
                arguments=argv,
            )
        )
    return tus
