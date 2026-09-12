"""Preprocess each translation unit, keeping line markers so diagnostics
map back to source."""

from __future__ import annotations

import logging
from pathlib import Path

from fver.core.models import TranslationUnit
from fver.core.workspace import Workspace
from fver.util.proc import run

log = logging.getLogger("fver.index")

# Flags that take a separate argument and must be dropped together with it.
_DROP_WITH_ARG = {"-o", "-MF", "-MT", "-MQ"}
# Flags to drop outright (compilation / dependency / output related).
_DROP = {"-c", "-S", "-E", "-M", "-MM", "-MD", "-MMD", "-MP", "-MG", "-P"}
_DROP_PREFIX = ("-o",)  # -ofoo
_DROP_PREFIX_EXACT_LEN = 2


def preprocess_argv(tu: TranslationUnit) -> list[str]:
    """Rewrite the compile argv into a preprocess-only argv writing to stdout."""
    argv = list(tu.arguments)
    if not argv:
        raise ValueError("empty argv")
    out: list[str] = [argv[0]]
    skip = False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a in _DROP_WITH_ARG:
            skip = True
            continue
        if a in _DROP:
            continue
        if a.startswith("-o") and len(a) > _DROP_PREFIX_EXACT_LEN and not a.startswith("-O"):
            continue
        out.append(a)
    out.extend(["-E", "-C", "-x", "c"])
    return out


def preprocess_tu(ws: Workspace, tu: TranslationUnit, timeout: float = 300.0) -> str | None:
    """Preprocess one TU into its work dir. Returns an error string on failure."""
    try:
        argv = preprocess_argv(tu)
    except ValueError as e:
        return str(e)
    src = Path(tu.source_path)
    directory = Path(tu.directory)
    if tu.source_path not in argv:
        abs_src = (ws.repo_root / src).resolve()
        argv.append(str(abs_src))
    r = run(argv, cwd=directory, timeout=timeout)
    if r.timed_out:
        return f"preprocessing timed out after {timeout:.0f}s"
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines()[-8:]
        return f"{argv[0]} exited {r.returncode}: " + " | ".join(tail)
    outdir = ws.tu_work_dir(tu.id)
    outpath = outdir / "preprocessed.i"
    ws.assert_not_user_file(outpath)
    outpath.write_text(r.stdout, encoding="utf-8")
    tu.preprocessed_path = outpath.relative_to(ws.root).as_posix()
    return None


def preprocess_all(
    ws: Workspace, tus: list[TranslationUnit], timeout: float = 300.0
) -> dict[str, str]:
    """Preprocess every TU. Returns {tu_id: error} for the ones that failed."""
    errors: dict[str, str] = {}
    for tu in tus:
        err = preprocess_tu(ws, tu, timeout=timeout)
        if err:
            errors[tu.id] = err
            log.warning("preprocess %s: %s", tu.source_path, err)
    return errors
