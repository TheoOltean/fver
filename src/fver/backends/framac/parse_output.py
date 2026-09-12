"""Read `frama-c -wp` output: which goals were proved, which were not, and
what the unproved ones say."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from fver.backends.base import CheckOutcome

# [wp] [Timeout] typed_f_assert_rte_mem_access (Qed 1ms) (Alt-Ergo) (Cached)
_GOAL_STATUS = re.compile(
    r"^\[wp\] \[(Timeout|Unknown|Stepout|Failed|Unproved|Unsuccess)\] (\S+)", re.MULTILINE
)
_PROVED = re.compile(r"^\[wp\] Proved goals:\s+(\d+) / (\d+)", re.MULTILINE)
# The `-wp-print` rendering of one goal:
#   Goal Preservation of Invariant ("t.c", line 12):
#   ...hypotheses...
#   Prove: (i < x) /\ (0 <= i).
#   Prover Alt-Ergo 2.4.3 returns Timeout (Qed:4ms) (10s)
_PRINTED_GOAL = re.compile(
    r"^Goal (?P<title>[^\n]*?)(?: \(\"[^\"]*\", line (?P<line>\d+)\))?(?: in '[^']*')?:\n"
    r"(?P<body>.*?)\nProver (?P<prover>[^\n]*?) returns (?P<status>\w+)",
    re.MULTILINE | re.DOTALL,
)
_KERNEL_ERROR = re.compile(
    r"^\[(?:kernel|wp|rte)[^\]]*\] (?:[^\n]*?: )?(?:Failure|User Error|Error)[^\n]*$", re.MULTILINE
)
_MISSING_SPEC = re.compile(r"Neither code nor specification for function (\w+)")
_NO_ASSIGNS = re.compile(r"Missing assigns clause \(assigns 'everything' instead\)")

# Goal-name suffixes and what they mean to the annotator.
MEANING = [
    (
        "assert_rte_mem_access",
        "memory access may be invalid (RTE): a pointer/array read or write is not covered by \\valid / \\valid_read",
    ),
    ("assert_rte_signed_overflow", "signed integer overflow is not ruled out (RTE)"),
    ("assert_rte_division_by_zero", "division by zero is not ruled out (RTE)"),
    ("assert_rte_shift", "shift amount may be out of range or negative (RTE)"),
    ("assert_rte_index_bound", "array index may be out of bounds (RTE)"),
    ("assert_rte_pointer_value", "pointer arithmetic may leave its object (RTE)"),
    ("assert_rte_initialization", "a value may be read uninitialised (RTE)"),
    ("assert_rte_bool_value", "an invalid _Bool value (RTE)"),
    ("assert_rte_signed_downcast", "a downcast may not fit (RTE)"),
    ("loop_invariant_established", "loop invariant does not hold on entry"),
    ("loop_invariant_preserved", "loop invariant is not preserved by an iteration"),
    ("loop_assigns", "loop assigns clause is too narrow"),
    ("loop_variant", "loop variant does not decrease or may be negative"),
    ("call_requires", "a callee's precondition is not established at the call"),
    ("call_", "a callee's precondition is not established at the call"),
    ("ensures", "the postcondition does not follow"),
    ("assigns", "the assigns clause is too narrow (the function writes elsewhere)"),
    ("assert", "an assertion does not follow"),
]


@dataclass
class Classified:
    outcome: CheckOutcome
    feedback: str
    goals: list[str] = field(default_factory=list)
    proved: int = 0
    total: int = 0
    missing_specs: list[str] = field(default_factory=list)


def _kernel_errors(text: str) -> list[str]:
    """Each kernel error line with its indented continuation lines, minus
    the generic 'treated as fatal error' / 'aborted' noise."""
    lines = text.split("\n")
    out: list[str] = []
    for i, ln in enumerate(lines):
        if not _KERNEL_ERROR.match(ln):
            continue
        if "treated as fatal error" in ln or "aborted" in ln:
            continue
        block = [re.sub(r"^\[[^\]]*\] ", "", ln).strip()]
        j = i + 1
        while j < len(lines) and lines[j].startswith("  "):
            block.append(lines[j].strip())
            j += 1
        out.append(" ".join(block))
    return out


def meaning_of(goal: str) -> str:
    for key, text in MEANING:
        if key in goal:
            return text
    return "unproved"


def classify(
    stdout: str,
    stderr: str,
    returncode: int,
    timed_out: bool,
    function: str,
    source_line: Callable[[int], str | None] | None = None,
) -> Classified:
    """`source_line(n)` returns the text of line n of the checked file."""
    text = stdout + "\n" + stderr
    if timed_out:
        return Classified(
            CheckOutcome.TOOL_ERROR,
            "Frama-C timed out. Simplify the loop annotations, or split the invariant into"
            " smaller facts so each goal is cheap.",
        )
    errors = _kernel_errors(text)
    if errors:
        return Classified(
            CheckOutcome.FRONTEND_ERROR,
            "Frama-C rejected the file:\n"
            + "\n".join(errors[:8])
            + "\nFix the ACSL syntax or the construct named; if the code itself uses"
            " something Frama-C cannot parse, reply UNSUPPORTED:.",
        )
    m = _PROVED.search(text)
    if m is None:
        if returncode != 0:
            tail = "\n".join((stderr or stdout).strip().splitlines()[-12:])
            return Classified(CheckOutcome.TOOL_ERROR, f"frama-c exited {returncode}:\n{tail}")
        return Classified(
            CheckOutcome.TOOL_ERROR, "frama-c produced no WP summary:\n" + text.strip()[-1500:]
        )
    proved, total = int(m.group(1)), int(m.group(2))
    missing = sorted(set(_MISSING_SPEC.findall(text)))
    failed = [(g, st) for st, g in _GOAL_STATUS.findall(text)]
    unproved_printed = [m for m in _PRINTED_GOAL.finditer(text) if m.group("status") != "Valid"]
    if proved == total and not failed and not unproved_printed:
        fb = f"All {total} goal(s) proved."
        return Classified(CheckOutcome.OK, fb, proved=proved, total=total, missing_specs=missing)
    goals: list[str] = []
    lines = [f"WP proved {proved} of {total} goal(s) for `{function}`. Unproved:"]
    for g, st in failed[:12]:
        lines.append(f"- {g} [{st}]: {meaning_of(g)}")
    if len(failed) > 12:
        lines.append(f"... and {len(failed) - 12} more.")
    for m in unproved_printed[:8]:
        title, line, body = m.group("title"), m.group("line"), m.group("body").strip()
        where = f" (line {line})" if line else ""
        src = source_line(int(line)) if (line and source_line) else None
        head = f"Goal {title}{where}" + (f": `{src.strip()}`" if src else "")
        prove = next((ln for ln in reversed(body.splitlines()) if ln.startswith("Prove:")), "")
        lines.append(f"- {head}\n  {prove}")
        goals.append(head + "\n" + "\n".join(body.splitlines()[-45:]))
    if missing:
        lines.append(
            "No contract for callee " + ", ".join(missing) + ": Frama-C assumed it may write"
            " anywhere, so nothing about memory survives the call."
        )
    if _NO_ASSIGNS.search(text) and not missing:
        lines.append("A callee has no assigns clause; WP assumed it writes everywhere.")
    lines.append(
        "Hint: an RTE goal needs a precondition or loop invariant that covers the access"
        " or bounds the value; an `established` goal means the invariant is false before"
        " the loop; a `preserved` goal means one iteration breaks it (often the invariant"
        " must also say what the loop has NOT changed, and `loop assigns` must list every"
        " variable the body writes)."
    )
    return Classified(
        CheckOutcome.GOALS_REMAIN,
        "\n".join(lines),
        goals=goals,
        proved=proved,
        total=total,
        missing_specs=missing,
    )
