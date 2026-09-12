"""AddressSanitizer / UndefinedBehaviorSanitizer via the project's own tests.

Only runs when the config enables it and names a test command. The build is
driven by the user's own build system with CC/CFLAGS/LDFLAGS pointing at
sanitized flags; fver writes nothing into the user tree itself, but the
user's build will put instrumented objects wherever it normally does.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fver.core.config import HuntersConfig
from fver.core.models import Finding, FunctionInfo, ToolStatus, TranslationUnit
from fver.hunters.base import FunctionLocator, normalise_source_path, truncate_lines
from fver.util import proc

log = logging.getLogger(__name__)

SAN_FLAGS = "-fsanitize=address,undefined -fno-omit-frame-pointer -g"

_ASAN_HEAD = re.compile(r"==\d+==\s*ERROR: (?P<san>\w+Sanitizer): (?P<what>[\w-]+)")
_UBSAN_LINE = re.compile(r"(?P<file>[^\s:]+):(?P<line>\d+)(?::\d+)?: runtime error: (?P<what>.+)")
_FRAME = re.compile(r"#\d+ 0x[0-9a-f]+ in (?P<func>[\w.]+) (?P<file>[^\s:]+):(?P<line>\d+)")

_ASAN_KINDS: list[tuple[str, str]] = [
    ("heap-buffer-overflow", "out_of_bounds"),
    ("stack-buffer-overflow", "out_of_bounds"),
    ("global-buffer-overflow", "out_of_bounds"),
    ("heap-use-after-free", "use_after_free"),
    ("stack-use-after-return", "use_after_free"),
    ("stack-use-after-scope", "use_after_free"),
    ("double-free", "double_free"),
    ("attempting free on address which was not malloc", "null_or_invalid_deref"),
    ("SEGV", "null_or_invalid_deref"),
    ("LeakSanitizer", "memory_leak"),
    ("detected memory leaks", "memory_leak"),
    ("use-of-uninitialized-value", "uninitialised_read"),
]
_UBSAN_KINDS: list[tuple[str, str]] = [
    ("signed integer overflow", "signed_overflow"),
    ("division by zero", "division_by_zero"),
    ("shift exponent", "undefined_shift"),
    ("shift", "undefined_shift"),
    ("null pointer", "null_or_invalid_deref"),
    ("misaligned address", "null_or_invalid_deref"),
    ("index", "out_of_bounds"),
    ("out of bounds", "out_of_bounds"),
    ("pointer index expression", "pointer_overflow"),
    ("applying non-zero offset", "pointer_overflow"),
    ("load of value", "uninitialised_read"),
]


def _classify(table: list[tuple[str, str]], text: str) -> str:
    low = text.lower()
    for needle, kind in table:
        if needle.lower() in low:
            return kind
    return "undefined_behaviour"


def parse_sanitizer_output(text: str, repo_root: Path) -> list[dict]:
    """Return records {kind, file, line, message, witness}."""
    lines = text.splitlines()
    out: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _ASAN_HEAD.search(line)
        if m:
            # block extends until a blank line following the summary or ~60 lines
            j = i + 1
            while j < len(lines) and j - i < 200 and not lines[j].startswith("SUMMARY:"):
                j += 1
            block = "\n".join(lines[i : min(j + 1, len(lines))])
            file, lineno = "", None
            for fm in _FRAME.finditer(block):
                rel = normalise_source_path(fm.group("file"), repo_root)
                if not rel.startswith("/") and not rel.startswith(".."):
                    file, lineno = rel, int(fm.group("line"))
                    break
            out.append(
                {
                    "kind": _classify(
                        _ASAN_KINDS, f"{m.group('san')} {m.group('what')} {block[:400]}"
                    ),
                    "file": file,
                    "line": lineno,
                    "message": f"{m.group('san')}: {m.group('what')}",
                    "witness": truncate_lines(block, 60),
                }
            )
            i = j + 1
            continue
        m2 = _UBSAN_LINE.search(line)
        if m2:
            j = i + 1
            while j < len(lines) and j - i < 60 and lines[j].strip().startswith("#"):
                j += 1
            block = "\n".join(lines[i:j])
            out.append(
                {
                    "kind": _classify(_UBSAN_KINDS, m2.group("what")),
                    "file": normalise_source_path(m2.group("file"), repo_root),
                    "line": int(m2.group("line")),
                    "message": f"UBSan: {m2.group('what')}",
                    "witness": truncate_lines(block, 60),
                }
            )
            i = j
            continue
        i += 1
    return out


class SanitizerHunter:
    name = "sanitizers"

    def doctor(self) -> list[ToolStatus]:
        from fver.util.platform import compiler_supports, find_compiler

        cc = os.environ.get("CC") or find_compiler()
        ok = bool(cc) and compiler_supports(cc, tuple(SAN_FLAGS.split()))
        return [
            ToolStatus(
                name="sanitizers",
                found=ok,
                path=proc.which(cc) if cc else None,
                version=cc,
                required=True,
                hint=""
                if ok
                else "the C compiler does not accept -fsanitize=address,undefined "
                "(install clang or a gcc with libasan/libubsan)",
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
        if not config.test_command:
            log.info("sanitizer hunter has nothing to run: set hunters.test_command")
            return []
        workdir.mkdir(parents=True, exist_ok=True)
        log.warning(
            "running the project's tests with sanitizer flags; instrumented build products "
            "will land wherever the project's build normally writes them"
        )
        from fver.util.platform import compiler_supports, find_compiler

        env = dict(os.environ)
        cc = env.get("CC") or find_compiler() or "cc"
        if not compiler_supports(cc, tuple(SAN_FLAGS.split())):
            log.warning("%s does not accept %s; skipping sanitizer hunt", cc, SAN_FLAGS)
            return []
        env["CC"] = cc
        env["CFLAGS"] = (env.get("CFLAGS", "") + " " + SAN_FLAGS).strip()
        env["LDFLAGS"] = (env.get("LDFLAGS", "") + " -fsanitize=address,undefined").strip()
        env.setdefault("ASAN_OPTIONS", "detect_leaks=1:halt_on_error=0")
        env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=0")
        r = proc.run_shell(
            config.test_command, cwd=repo_root, timeout=config.cbmc_timeout_seconds * 4, env=env
        )
        (workdir / "tests.log").write_text(
            r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8"
        )
        locator = FunctionLocator(functions)
        findings: list[Finding] = []
        for rec in parse_sanitizer_output(r.stdout + "\n" + r.stderr, repo_root):
            fi = locator.locate(rec["file"], rec["line"]) if rec["file"] else None
            findings.append(
                Finding(
                    function_id=fi.id if fi else None,
                    source_path=rec["file"] or "<unknown>",
                    line=rec["line"],
                    kind=rec["kind"],
                    tool="sanitizers",
                    message=rec["message"],
                    witness=rec["witness"],
                )
            )
        return findings
