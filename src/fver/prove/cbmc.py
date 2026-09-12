"""CBMC: bounded model checking. Exhaustive up to a loop bound, no annotations."""

from __future__ import annotations

import bisect
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from fver.core.models import Finding, FunctionInfo, TranslationUnit
from fver.util import proc

log = logging.getLogger(__name__)

# ---- shared helpers (formerly hunters/base.py) ----
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


# ---- CBMC ----

CBMC_CHECKS = [
    "--bounds-check",
    "--pointer-check",
    "--pointer-overflow-check",
    "--signed-overflow-check",
    "--div-by-zero-check",
    "--undefined-shift-check",
]

MAX_FUNCTIONS_PER_TU = 20
INSTALL_HINT = "run `fver setup`"
CBMC_UNWIND = 8  # loop unwinding bound
CBMC_TIMEOUT_SECONDS = 300  # per entry point

# (substring of property class or description, lower-case) -> kind
_KIND_TABLE: list[tuple[str, str]] = [
    # Informational first: single-TU analysis cannot see other files' bodies.
    ("no body for callee", "missing_body"),
    ("no body for function", "missing_body"),
    ("unwinding assertion", "bound_reached"),
    ("array bounds", "out_of_bounds"),
    ("array `", "out_of_bounds"),
    ("upper bound", "out_of_bounds"),
    ("lower bound", "out_of_bounds"),
    ("bounds", "out_of_bounds"),
    ("deallocated", "use_after_free"),
    ("dead object", "use_after_free"),
    ("double free", "double_free"),
    ("free argument", "null_or_invalid_deref"),
    ("null", "null_or_invalid_deref"),
    ("pointer dereference", "null_or_invalid_deref"),
    ("dereference failure", "null_or_invalid_deref"),
    ("invalid pointer", "null_or_invalid_deref"),
    ("pointer outside", "pointer_overflow"),
    ("pointer arithmetic", "pointer_overflow"),
    ("pointer overflow", "pointer_overflow"),
    ("arithmetic overflow", "signed_overflow"),
    ("overflow", "signed_overflow"),
    ("division by zero", "division_by_zero"),
    ("shift", "undefined_shift"),
]


def classify(property_class: str, description: str) -> str:
    text = f"{property_class} {description}".lower()
    for needle, kind in _KIND_TABLE:
        if needle in text:
            return kind
    return "undefined_behaviour"


_STD_MAP = {
    "c89": "--c89",
    "c90": "--c89",
    "gnu89": "--c89",
    "gnu90": "--c89",
    "c99": "--c99",
    "gnu99": "--c99",
    "iso9899:1999": "--c99",
    "c11": "--c11",
    "gnu11": "--c11",
    "iso9899:2011": "--c11",
    "c17": "--c17",
    "c18": "--c17",
    "gnu17": "--c17",
    "gnu18": "--c17",
    "c23": "--c23",
    "c2x": "--c23",
    "gnu23": "--c23",
    "gnu2x": "--c23",
}


def cbmc_flags(arguments: list[str]) -> list[str]:
    """Translate a compiler argv into the subset of options CBMC accepts.

    CBMC understands -I/-D/-U, --include, --c89/--c99/--c11/--c17/--c23,
    --32/--64. Everything else from a real build line (-f*, -W*, -O*, -std=
    spelled the compiler way) makes it print its usage and exit, so those are
    translated or dropped here.
    """
    out: list[str] = []
    it = iter(arguments[1:] if arguments else [])
    for a in it:
        if a in ("-I", "-D", "-U"):
            nxt = next(it, None)
            if nxt is not None:
                out.extend([a, nxt])
        elif a in ("-isystem", "-iquote", "-idirafter"):
            nxt = next(it, None)
            if nxt is not None:
                out.extend(["-I", nxt])
        elif a == "-include":
            nxt = next(it, None)
            if nxt is not None:
                out.extend(["--include", nxt])
        elif a.startswith(("-I", "-D", "-U")) and len(a) > 2:
            out.append(a)
        elif a.startswith("-std="):
            mapped = _STD_MAP.get(a[5:].lower())
            if mapped:
                out.append(mapped)
        elif a == "-m32":
            out.append("--32")
        elif a == "-m64":
            out.append("--64")
    return out


def tool_error(stdout: str, stderr: str, returncode: int) -> str | None:
    """Return the first error line if CBMC failed to run the analysis at all
    (bad option, parse error, missing header), else None."""
    text = stdout + "\n" + stderr
    if "Usage error" in text or "unknown option" in text.lower():
        first = next((ln for ln in text.splitlines() if "option" in ln.lower()), "Usage error")
        return first.strip()[:300]
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("messageType") == "ERROR":
                return str(entry.get("messageText", "error"))[:300]
        if any(isinstance(e, dict) and "result" in e for e in data):
            return None
    if returncode not in (0, 10):
        tail = (stderr or stdout).strip().splitlines()
        return (tail[-1] if tail else f"cbmc exited {returncode}")[:300]
    return None


def cbmc_argv(source: str, flags: list[str], unwind: int, function: str | None = None) -> list[str]:
    argv = [
        "cbmc",
        source,
        *CBMC_CHECKS,
        "--unwind",
        str(unwind),
        "--unwinding-assertions",
        "--json-ui",
        *flags,
    ]
    if function:
        argv += ["--function", function]
    return argv


def _render_trace(trace: list[dict]) -> str:
    lines: list[str] = []
    for step in trace:
        st = step.get("stepType")
        if st == "assignment":
            lhs = step.get("lhs", "?")
            val = step.get("value", {})
            data = val.get("data", val) if isinstance(val, dict) else val
            loc = step.get("sourceLocation", {})
            where = f" ({loc.get('file', '')}:{loc.get('line', '')})" if loc else ""
            lines.append(f"{lhs} = {data}{where}")
        elif st == "failure":
            lines.append(f"FAILURE: {step.get('reason', '')}")
    return truncate_lines("\n".join(lines), 40)


def parse_cbmc_json(text: str, repo_root: Path, tu_dir: str | None = None) -> list[dict]:
    """Return raw failure records: dicts with keys kind, file, line, function,
    message, witness, property. Success results are skipped."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # cbmc sometimes prefixes non-JSON noise; find the first '[' or '{'
        start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
        if start < 0:
            return []
        try:
            data = json.loads(text[start:])
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = [data]
    results: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict) or "result" not in entry:
            continue
        for r in entry["result"] or []:
            status = str(r.get("status", "")).upper()
            if status != "FAILURE":
                continue
            desc = r.get("description", "")
            pclass = r.get("property", "")
            loc = r.get("sourceLocation", {}) or {}
            trace = r.get("trace", []) or []
            # location may only be in the trace's failure step
            if not loc and trace:
                for step in trace:
                    if step.get("stepType") == "failure" and step.get("sourceLocation"):
                        loc = step["sourceLocation"]
                        break
            line = loc.get("line")
            try:
                line_i = int(line) if line is not None else None
            except (TypeError, ValueError):
                line_i = None
            results.append(
                {
                    "kind": classify(pclass, desc),
                    "file": normalise_source_path(loc.get("file", ""), repo_root, tu_dir)
                    if loc.get("file")
                    else "",
                    "line": line_i,
                    "function": loc.get("function", ""),
                    "message": desc or pclass,
                    "witness": _render_trace(trace),
                    "property": pclass,
                }
            )
    return results


class CbmcHunter:
    name = "cbmc"

    def run(
        self,
        tus: list[TranslationUnit],
        functions: list[FunctionInfo],
        repo_root: Path,
        workdir: Path,
    ) -> list[Finding]:
        if proc.which("cbmc") is None:
            log.warning("cbmc not found on PATH; skipping (%s)", INSTALL_HINT)
            return []
        workdir.mkdir(parents=True, exist_ok=True)
        locator = FunctionLocator(functions)
        fns_by_tu: dict[str, list[FunctionInfo]] = {}
        for f in functions:
            fns_by_tu.setdefault(f.tu_id, []).append(f)

        findings: list[Finding] = []
        seen: set[tuple] = set()
        for tu in tus:
            tu_fns = fns_by_tu.get(tu.id, [])
            flags = cbmc_flags(tu.arguments)
            has_main = any(f.name == "main" for f in tu_fns)
            entry_points: list[str | None]
            if has_main:
                entry_points = [None]
            else:
                ranked = sorted(tu_fns, key=lambda f: f.attack_score, reverse=True)
                entry_points = [f.name for f in ranked[:MAX_FUNCTIONS_PER_TU]]
            source_abs = str((repo_root / tu.source_path).resolve())
            for fn_name in entry_points:
                argv = cbmc_argv(source_abs, flags, CBMC_UNWIND, fn_name)
                r = proc.run(argv, cwd=Path(tu.directory), timeout=CBMC_TIMEOUT_SECONDS)
                tag = tu.id + ("" if fn_name is None else f"_{fn_name}")
                (workdir / f"{tag}.json").write_text(r.stdout)
                if r.timed_out:
                    log.info(
                        "cbmc timed out on %s%s", tu.source_path, f":{fn_name}" if fn_name else ""
                    )
                    continue
                if r.returncode == 127:
                    log.warning("cbmc vanished mid-run; aborting")
                    return findings
                err = tool_error(r.stdout, r.stderr, r.returncode)
                if err is not None:
                    log.warning(
                        "cbmc could not analyse %s%s: %s",
                        tu.source_path,
                        f":{fn_name}" if fn_name else "",
                        err,
                    )
                    findings.append(
                        Finding(
                            function_id=None,
                            source_path=tu.source_path,
                            line=None,
                            kind="tool_error",
                            tool="cbmc",
                            message=err,
                        )
                    )
                    continue
                for rec in parse_cbmc_json(r.stdout, repo_root, tu.directory):
                    src = rec["file"] or tu.source_path
                    dedupe = (fn_name, src, rec["line"], rec["kind"], rec["message"])
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    fi = locator.locate(src, rec["line"]) or (
                        locator.by_name(rec["function"], src) if rec["function"] else None
                    )
                    findings.append(
                        Finding(
                            function_id=fi.id if fi else None,
                            source_path=src,
                            line=rec["line"],
                            kind=rec["kind"],
                            tool="cbmc",
                            message=rec["message"],
                            witness=rec["witness"],
                            confidence="high" if fn_name is None else "low",
                        )
                    )
        return findings
