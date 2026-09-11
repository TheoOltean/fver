"""Cerberus: a strict ISO C front-end and interpreter.

Two passes: elaboration (static, per TU: UB the front-end can already see,
plus constructs it cannot handle) and execution (TUs with a `main`, run
under the interpreter, which reports UB the moment it happens).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fver.core.config import HuntersConfig
from fver.core.models import Finding, FunctionInfo, ToolStatus, TranslationUnit
from fver.hunters.base import (
    FunctionLocator,
    normalise_source_path,
    preprocessor_flags,
    truncate_lines,
)
from fver.util import proc

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "opam install cerberus (needs opam: see `fver doctor`) | "
    "docker: ghcr.io/rems-project/cerberus | https://github.com/rems-project/cerberus"
)

# Cerberus output shapes seen in practice:
#   file.c:12:5: undefined behaviour: UB043_indirection_invalid_value
#   Undefined behaviour: UB036_exceptional_condition (at file.c:12:5)
#   file.c:3:1: error: unsupported ...
_LOC_RE = re.compile(r"(?P<file>[^\s:]+\.[ch]):(?P<line>\d+)(?::(?P<col>\d+))?")
_UB_LINE_RE = re.compile(
    r"undefined behaviou?r[:\s]+(?P<code>UB\d+[A-Za-z0-9_]*)?(?P<rest>.*)", re.IGNORECASE
)
_ERR_RE = re.compile(
    r"\b(error|fatal error|unsupported|not supported|unimplemented)\b", re.IGNORECASE
)

# ISO C annex J numbering as Cerberus labels them -> our kinds
_UB_KIND_TABLE: list[tuple[str, str]] = [
    ("UB036", "signed_overflow"),
    ("exceptional_condition", "signed_overflow"),
    ("UB045", "division_by_zero"),
    ("division_by_zero", "division_by_zero"),
    ("UB051", "undefined_shift"),
    ("UB052", "undefined_shift"),
    ("shift", "undefined_shift"),
    ("UB043", "null_or_invalid_deref"),
    ("indirection", "null_or_invalid_deref"),
    ("UB046", "out_of_bounds"),
    ("array_subscript", "out_of_bounds"),
    ("out_of_bound", "out_of_bounds"),
    ("UB010", "uninitialised_read"),
    ("UB011", "uninitialised_read"),
    ("uninit", "uninitialised_read"),
    ("indeterminate", "uninitialised_read"),
    ("UB047", "pointer_overflow"),
    ("UB049", "invalid_pointer_arith"),
    ("pointer_arith", "invalid_pointer_arith"),
    ("UB025", "data_race"),
    ("race", "data_race"),
    ("dead", "use_after_free"),
    ("lifetime", "use_after_free"),
    ("free", "double_free"),
]


def classify_ub(code: str, rest: str) -> str:
    text = f"{code} {rest}".lower()
    for needle, kind in _UB_KIND_TABLE:
        if needle.lower() in text:
            return kind
    return "undefined_behaviour"


def parse_cerberus_output(
    text: str, repo_root: Path, tu_dir: str | None, default_file: str
) -> list[dict]:
    """Return records {kind, file, line, message, informational}."""
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m_ub = _UB_LINE_RE.search(line)
        loc = _LOC_RE.search(line)
        file = normalise_source_path(loc.group("file"), repo_root, tu_dir) if loc else default_file
        lineno = int(loc.group("line")) if loc else None
        if m_ub:
            code = m_ub.group("code") or ""
            rest = m_ub.group("rest") or ""
            out.append(
                {
                    "kind": classify_ub(code, rest),
                    "file": file,
                    "line": lineno,
                    "message": line,
                    "informational": False,
                }
            )
        elif _ERR_RE.search(line):
            out.append(
                {
                    "kind": "cerberus_unsupported",
                    "file": file,
                    "line": lineno,
                    "message": line,
                    "informational": True,
                }
            )
    return out


class CerberusHunter:
    name = "cerberus"

    def doctor(self) -> list[ToolStatus]:
        path = proc.which("cerberus")
        return [
            ToolStatus(
                name="cerberus",
                found=path is not None,
                path=path,
                version=proc.version_of(["cerberus", "--version"]) if path else None,
                required=False,
                hint=INSTALL_HINT,
            )
        ]

    def run(
        self,
        tus: list[TranslationUnit],
        functions: list[FunctionInfo],
        repo_root: Path,
        workdir: Path,
        config: HuntersConfig,
    ) -> list[Finding]:
        if proc.which("cerberus") is None:
            log.warning("cerberus not found on PATH; skipping (%s)", INSTALL_HINT)
            return []
        workdir.mkdir(parents=True, exist_ok=True)
        locator = FunctionLocator(functions)
        has_main = {f.tu_id for f in functions if f.name == "main"}
        findings: list[Finding] = []
        for tu in tus:
            flags = preprocessor_flags(tu.arguments)
            source_abs = str((repo_root / tu.source_path).resolve())
            passes: list[tuple[str, list[str]]] = [("elab", ["cerberus", *flags, source_abs])]
            if tu.id in has_main:
                passes.append(("exec", ["cerberus", "--exec", "--batch", *flags, source_abs]))
            for tag, argv in passes:
                r = proc.run(argv, cwd=Path(tu.directory), timeout=config.cbmc_timeout_seconds)
                (workdir / f"{tu.id}_{tag}.txt").write_text(
                    r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8"
                )
                if r.timed_out:
                    log.info("cerberus %s pass timed out on %s", tag, tu.source_path)
                    continue
                if r.returncode == 127:
                    return findings
                for rec in parse_cerberus_output(
                    r.stdout + "\n" + r.stderr, repo_root, tu.directory, tu.source_path
                ):
                    fi = locator.locate(rec["file"], rec["line"])
                    findings.append(
                        Finding(
                            function_id=fi.id if fi else None,
                            source_path=rec["file"],
                            line=rec["line"],
                            kind=rec["kind"],
                            tool=f"cerberus-{tag}",
                            message=rec["message"],
                            witness=truncate_lines(r.stdout, 40)
                            if tag == "exec" and not rec["informational"]
                            else "",
                        )
                    )
        return findings
