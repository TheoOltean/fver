"""Turn `refinedc check` output into a CheckOutcome and LLM-facing feedback.

The formats handled here were captured from a real RefinedC (see
tests/fixtures/refinedc/ and facts.py). Outcomes:

  OK                the file was checked (`successfully checked.`)
  FRONTEND_ERROR    the C front-end / annotation parser rejected the file,
                    or Rocq rejected the *spec* (bad Coq inside `{...}`)
  AUTOMATION_STUCK  `Type system got stuck`: ownership / typing annotations
                    do not describe the program
  GOALS_REMAIN      `Cannot solve side condition`: pure facts left over, or
                    the user's lemmas.v does not compile
  TOOL_ERROR        timeout, missing binary, or output we cannot read

Coq-level locations are in preprocessed coordinates; the backend passes a
`line_of` callback that maps them to (source line, text) when it can.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from fver.backends.base import CheckOutcome
from fver.backends.refinedc import facts

MAX_GOAL_LINES = 40
MAX_FEEDBACK_LINES = 70
MAX_GOALS_SHOWN = 3

LineMapper = Callable[[int], tuple[int, str] | None]


def strip_ansi(text: str) -> str:
    return re.sub(facts.ANSI_REGEX, "", text)


def _tail(text: str, n: int = 25) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _truncate(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n... ({len(lines) - n} more lines)"


@dataclass
class Failure:
    """One `Cannot solve side condition` / `Type system got stuck` report."""

    kind: str  # "stuck" | "sidecond"
    function: str
    block: str = ""
    location: tuple[int, int, int, int] | None = None  # l1, c1, l2, c2 (preprocessed)
    case: str = ""  # "Case distinction (...) -> true"
    goal: str = ""  # the Goal: block, hypotheses + conclusion
    conclusion: str = ""

    def as_text(self) -> str:
        return self.goal


def extract_failures(text: str) -> list[Failure]:
    """Parse every stuck / side-condition report with its goal block."""
    lines = text.splitlines()
    out: list[Failure] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        kind = None
        if facts.STUCK_MARKER in ln:
            kind = "stuck"
        elif facts.SIDECOND_MARKER in ln:
            kind = "sidecond"
        if kind is None:
            i += 1
            continue
        m = re.search(r'in function "(?P<fn>[^"]+)"(?: in block "(?P<blk>[^"]+)")?', ln)
        f = Failure(
            kind=kind,
            function=m.group("fn") if m else "",
            block=(m.group("blk") or "") if m else "",
        )
        j = i + 1
        # Header lines up to "Goal:".
        while j < len(lines) and facts.GOAL_HEADER not in lines[j]:
            hl = lines[j]
            lm = re.search(facts.LOCATION_REGEX, hl) or re.search(facts.AT_LOCATION_REGEX, hl)
            if lm and f.location is None:
                f.location = tuple(int(lm.group(k)) for k in ("l1", "c1", "l2", "c2"))  # type: ignore[assignment]
            if facts.CASE_MARKER in hl:
                f.case = hl.strip()
            if facts.STUCK_MARKER in hl or facts.SIDECOND_MARKER in hl:
                break
            j += 1
        if j < len(lines) and facts.GOAL_HEADER in lines[j]:
            k = j + 1
            block: list[str] = []
            while k < len(lines) and lines[k].strip() and not lines[k].startswith("File "):
                block.append(lines[k])
                k += 1
            f.goal = "\n".join(block).strip()
            sep = max(f.goal.rfind(facts.GOAL_SEPARATOR_IRIS), f.goal.rfind(facts.GOAL_SEPARATOR))
            f.conclusion = f.goal[sep:].split("\n", 1)[1].strip() if sep >= 0 else f.goal
            j = k
        out.append(f)
        i = j
    return out


def extract_goals(text: str) -> list[str]:
    """Goal blocks only (backwards-compatible helper)."""
    seen: set[str] = set()
    goals: list[str] = []
    for f in extract_failures(strip_ansi(text)):
        if f.goal and f.goal not in seen:
            seen.add(f.goal)
            goals.append(f.goal)
    return goals


@dataclass
class FrontendError:
    file: str
    line: int | None
    message: str


def extract_frontend_errors(text: str) -> list[FrontendError]:
    """`[file:line:col...] Frontend error.` (+ detail on the next line),
    `[file:line:col] Not implemented: ...`, cpp `file:line:col: error: ...`."""
    lines = text.splitlines()
    out: list[FrontendError] = []
    for i, ln in enumerate(lines):
        if any(w in ln for w in facts.WARNING_MARKERS):
            continue
        m = re.search(facts.FRONTEND_LOCATION_REGEX, ln)
        # Any `[file:line:col...] message` line from refinedc that is not one
        # of the known warnings is an error (parser, "Not implemented",
        # "Forbidden", Ail typing errors such as "Invalid use of ...").
        if m and ln[: m.start()].strip() == "" and ln[m.end() :].strip():
            msg = ln[m.end() :].strip()
            if msg.rstrip(".").endswith("Frontend error") and i + 1 < len(lines):
                msg = msg.rstrip() + " " + lines[i + 1].strip()
            out.append(FrontendError(m.group("file"), int(m.group("line")), msg))
            continue
        m2 = re.search(facts.CPP_LOCATION_REGEX, ln)
        if m2:
            out.append(
                FrontendError(m2.group("file"), int(m2.group("line")), ln[m2.end() :].strip())
            )
    return out


@dataclass
class CoqError:
    file: str
    line: int
    message: str


def extract_coq_errors(text: str) -> list[CoqError]:
    lines = text.splitlines()
    out: list[CoqError] = []
    for i, ln in enumerate(lines):
        m = re.search(facts.COQ_FILE_ERROR_REGEX, ln)
        if not m:
            continue
        msg_lines: list[str] = []
        j = i + 1
        while (
            j < len(lines)
            and lines[j].strip()
            and not lines[j].startswith(("File ", "Leaving", "Entering"))
        ):
            msg_lines.append(lines[j].rstrip())
            j += 1
        out.append(CoqError(m.group("file"), int(m.group("line")), "\n".join(msg_lines).strip()))
    return out


@dataclass
class Classification:
    outcome: CheckOutcome
    feedback: str
    goals: list[str] = field(default_factory=list)
    witness: str = ""
    failures: list[Failure] = field(default_factory=list)


def _describe_location(loc: tuple[int, int, int, int] | None, line_of: LineMapper | None) -> str:
    if loc is None:
        return ""
    l1, c1, _l2, c2 = loc
    if line_of is not None:
        mapped = line_of(l1)
        if mapped is not None:
            sl, text = mapped
            snippet = text.strip()
            frag = text[c1 - 1 : c2 - 1].strip() if 0 < c1 <= len(text) else ""
            where = f" at source line {sl}: `{snippet}`"
            if frag and frag != snippet:
                where += f" (columns {c1}-{c2}: `{frag}`)"
            return where
    return f" (checker location line {l1}, columns {c1}-{c2}; line numbers are in preprocessed coordinates)"


def classify_full(
    stdout: str,
    stderr: str,
    returncode: int,
    timed_out: bool,
    function_name: str,
    line_of: LineMapper | None = None,
) -> Classification:
    raw = (stdout or "") + ("\n" if stdout and stderr else "") + (stderr or "")
    combined = strip_ansi(raw)

    if timed_out:
        return Classification(
            CheckOutcome.TOOL_ERROR,
            "The checker timed out. Either the automation is looping on an annotation "
            "(simplify the loop invariant, avoid deeply nested existentials) or "
            "[budget].checker_timeout_seconds is too low.\n" + _tail(combined, 10),
        )
    if returncode == 127 or "command not found" in combined:
        return Classification(
            CheckOutcome.TOOL_ERROR,
            f"`{facts.REFINEDC_BIN}` is not installed or not on PATH. Run `fver doctor`.",
        )
    if returncode == 0 or facts.SUCCESS_MARKER in combined:
        return Classification(CheckOutcome.OK, "Accepted by refinedc.")

    # Bug witness (rare: Cerberus naming UB statically).
    ub = [ln for ln in combined.splitlines() if any(m in ln.lower() for m in facts.UB_MARKERS)]
    if ub:
        witness = "\n".join(ub[:10])
        return Classification(
            CheckOutcome.BUG,
            "The front-end reports undefined behaviour in the code itself:\n" + witness,
            witness=witness,
        )

    # Internal error: in practice a malformed attribute string crashes the
    # annotation parser (exit 125).
    if returncode == facts.EXIT_INTERNAL_ERROR or facts.INTERNAL_ERROR_MARKER in combined:
        detail = _tail(combined, 6)
        where = (
            "the annotation parser"
            if "Rc_annot" in combined or "annot" in combined
            else "the front-end"
        )
        fb = (
            f"FRONTEND_ERROR: refinedc crashed in {where} (internal error). This is almost "
            "always a malformed attribute string: check every [[rc::...]] argument is a "
            "complete string literal, angle brackets and braces are balanced, and `@`, `&own<>`, "
            "`{...}` are used as in the reference.\n" + detail
        )
        return Classification(CheckOutcome.FRONTEND_ERROR, _truncate(fb, MAX_FEEDBACK_LINES))

    fes = extract_frontend_errors(combined)
    if fes:
        detail = "\n".join(
            f"{e.file}:{e.line}: {e.message}" if e.line else f"{e.file}: {e.message}"
            for e in fes[:8]
        )
        unsupported = any(
            k in e.message
            for e in fes
            for k in ("Not implemented", "not yet supported", "not currently supported")
        )
        if unsupported:
            fb = (
                "FRONTEND_ERROR: the C front-end does not support a construct this file uses "
                "(line numbers are real source lines):\n" + detail + "\n"
                "This is a limitation of the verifier, not of the annotations. If the construct "
                "is in a header the function does not depend on, nothing can be done here; report "
                "it in a Note: line starting with `UNSUPPORTED:`."
            )
        else:
            fb = (
                "FRONTEND_ERROR: the C front-end or the annotation parser rejected the file "
                "(line numbers are real source lines):\n" + detail + "\n"
                "Hint: check attribute syntax (one string literal per clause, balanced `<>` and "
                "`{}`), that every name used in `{Coq}` braces is declared in rc::parameters or "
                "rc::exists, and that struct annotations sit right after the `struct` keyword."
            )
        return Classification(CheckOutcome.FRONTEND_ERROR, _truncate(fb, MAX_FEEDBACK_LINES))

    failures = extract_failures(combined)
    coq_errors = extract_coq_errors(combined)
    goals = []
    seen: set[str] = set()
    for f in failures:
        if f.goal and f.goal not in seen:
            seen.add(f.goal)
            goals.append(f.goal)

    if failures:
        stuck = [f for f in failures if f.kind == "stuck"]
        shown = stuck if stuck else failures
        parts: list[str] = []
        for f in shown[:MAX_GOALS_SHOWN]:
            where = _describe_location(f.location, line_of)
            head = (
                f"[{f.kind}] in `{f.function}`" + (f" block {f.block}" if f.block else "") + where
            )
            if f.case:
                head += f"\n{f.case}"
            parts.append(head + "\nGoal:\n" + _truncate(f.goal, MAX_GOAL_LINES))
        more = len(shown) - min(len(shown), MAX_GOALS_SHOWN)
        body = "\n\n".join(parts) + (f"\n\n... and {more} more goal(s)." if more > 0 else "")
        if stuck:
            hint = (
                "\nHint: the type system got stuck on a program step, so an ownership or typing "
                "annotation does not describe what the code does. Typical causes: an rc::args "
                "pointer typed &shr where the code writes through it (needs &own); an array or "
                "struct type that does not cover the memory the code touches; a loop without a loop invariant "
                "(rc::exists/rc::inv_vars/rc::constraints; the goal mentions `Goto`/`typed_if` at "
                "the loop head), or an inv_vars entry whose type does not match the variable "
                "at the loop head. Repair the annotations; tactics will not help here."
            )
            return Classification(
                CheckOutcome.AUTOMATION_STUCK,
                _truncate(
                    f"AUTOMATION_STUCK while checking `{function_name}`.\n" + body + hint,
                    MAX_FEEDBACK_LINES,
                ),
                goals,
                failures=failures,
            )
        hint = (
            "\nHint: the program type-checks; pure facts remain that the automation could not "
            "discharge (`Case distinction` says which branch). Options, in order: strengthen "
            "rc::constraints / rc::requires so the fact follows by linear arithmetic; make the "
            "loop invariant say exactly what has been done so far (e.g. `replicate i ... ++ "
            'replicate (n - i) ...`); add `[[rc::tactics("all: try (...).")]]` with lia / nia / '
            "naive_solver / an explicit `exists`; or prove a helper lemma in lemmas.v and apply it "
            "from rc::tactics. Goals about `replicate`, `take`, `drop`, `<[i:=v]>` usually need a "
            "helper lemma."
        )
        return Classification(
            CheckOutcome.GOALS_REMAIN,
            _truncate(f"GOALS_REMAIN in `{function_name}`.\n" + body + hint, MAX_FEEDBACK_LINES),
            goals,
            failures=failures,
        )

    if coq_errors:
        e = coq_errors[0]
        if e.file.endswith(facts.LEMMAS_FILE) or f"/{facts.LEMMAS_FILE}" in e.file:
            fb = (
                f"GOALS_REMAIN: your lemmas.v does not compile (line {e.line}):\n{e.message}\n"
                "Fix the lemma statement or proof. Lemmas that mention RefinedC types must sit in "
                "`Section s. Context `{!typeG Σ}. ... End s.`; `lia` fails on `max_int i32` unless "
                "you first `have -> : max_int i32 = 2147483647 by vm_compute.`"
            )
            return Classification(CheckOutcome.GOALS_REMAIN, _truncate(fb, MAX_FEEDBACK_LINES))
        if facts.GENERATED_SPEC in e.file or "generated_spec" in e.file:
            fb = (
                f"FRONTEND_ERROR: Rocq rejected the generated specification (line {e.line} of "
                f"{facts.GENERATED_SPEC}):\n{e.message}\n"
                "Something inside a `{...}` escape or a type expression is not valid Coq. Inside "
                "braces write Coq, not annotation syntax: `int i32` not `int<i32>`, `x @ int i32`, "
                "`l `at_type` int i32`, `&own ty`, `uninit (it_layout i32)`."
            )
            return Classification(CheckOutcome.FRONTEND_ERROR, _truncate(fb, MAX_FEEDBACK_LINES))
        if facts.INCOMPLETE_PROOF_MARKER in e.message:
            fb = (
                "GOALS_REMAIN: the generated proof is incomplete but refinedc printed no goal "
                "(a tactic in rc::tactics may have raised an error instead of failing quietly). "
                f"Rocq said at {e.file}:{e.line}:\n{e.message}\nWrap tactics in `try (...)`."
            )
            return Classification(CheckOutcome.GOALS_REMAIN, _truncate(fb, MAX_FEEDBACK_LINES))
        fb = f"The Rocq build failed at {e.file}:{e.line}:\n{e.message}"
        return Classification(CheckOutcome.GOALS_REMAIN, _truncate(fb, MAX_FEEDBACK_LINES))

    return Classification(
        CheckOutcome.TOOL_ERROR,
        f"refinedc exited with status {returncode} and no recognisable diagnostic:\n"
        + _tail(combined, 20),
    )


def classify(
    stdout: str,
    stderr: str,
    returncode: int,
    timed_out: bool,
    function_name: str,
    line_of: LineMapper | None = None,
) -> tuple[CheckOutcome, str, list[str], str]:
    """Return (outcome, feedback, goals, witness)."""
    c = classify_full(stdout, stderr, returncode, timed_out, function_name, line_of)
    return c.outcome, c.feedback, c.goals, c.witness
