"""Turn `refinedc check` output into a CheckOutcome and LLM-facing feedback.

All the substrings this relies on live in facts.py; the heuristics here are
deliberately conservative: when unsure between "annotations wrong" and
"pure goal left", prefer AUTOMATION_STUCK, which sends the LLM back to the
annotations rather than into proof text.
"""

from __future__ import annotations

import re

from fver.backends.base import CheckOutcome
from fver.backends.refinedc import facts

MAX_GOAL_LINES = 40
MAX_FEEDBACK_LINES = 60


def _tail(text: str, n: int = 25) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-n:])


def _find_location(text: str) -> tuple[str, int] | None:
    m = re.search(facts.LOCATION_REGEX, text)
    if not m:
        return None
    return m.group("file"), int(m.group("line"))


def extract_goals(text: str) -> list[str]:
    """Goal blocks: hypotheses, the ==== separator, the conclusion, up to a
    blank line or the next Error/ marker. Also catches single-line
    'Cannot solve side condition: ...' style messages."""
    goals: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if facts.COQ_GOAL_SEPARATOR in lines[i]:
            # Walk up to the start of the hypothesis block.
            start = i
            while (
                start > 0
                and lines[start - 1].strip()
                and not lines[start - 1].startswith(facts.COQ_ERROR_MARKER)
            ):
                start -= 1
            end = i + 1
            while (
                end < len(lines)
                and lines[end].strip()
                and not lines[end].startswith(facts.COQ_ERROR_MARKER)
            ):
                end += 1
            block = "\n".join(lines[start:end]).strip()
            goals.append(block)
            i = end
            continue
        if "Cannot solve" in lines[i] or "solve_goal failed" in lines[i]:
            block = [lines[i]]
            j = i + 1
            while (
                j < len(lines)
                and lines[j].strip()
                and not lines[j].startswith(facts.COQ_ERROR_MARKER)
            ):
                block.append(lines[j])
                j += 1
            goals.append("\n".join(block).strip())
            i = j
            continue
        i += 1
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out = []
    for g in goals:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _truncate(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n... ({len(lines) - n} more lines)"


def _is_stuck(goal: str) -> bool:
    return any(m in goal for m in facts.STUCK_MARKERS)


def _errors(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        if (
            facts.COQ_ERROR_MARKER in ln
            or re.search(facts.LOCATION_REGEX, ln)
            and "error" in ln.lower()
        ):
            out.append(ln.strip())
    return out


def classify(
    stdout: str,
    stderr: str,
    returncode: int,
    timed_out: bool,
    function_name: str,
) -> tuple[CheckOutcome, str, list[str], str]:
    """Return (outcome, feedback, goals, witness)."""
    combined = (stdout or "") + ("\n" if stdout and stderr else "") + (stderr or "")

    if timed_out:
        return (
            CheckOutcome.TOOL_ERROR,
            "The checker timed out. Either the automation is looping on an annotation "
            "(simplify the loop invariant, avoid deeply nested existentials) or the "
            "timeout in [budget].checker_timeout_seconds is too low.\n" + _tail(combined, 10),
            [],
            "",
        )
    if (
        returncode == 127
        or "command not found" in combined
        or "No such file or directory" in combined
        and not _errors(combined)
    ):
        return (
            CheckOutcome.TOOL_ERROR,
            f"`{facts.REFINEDC_BIN}` is not installed or not on PATH. Run `fver doctor` for install hints.",
            [],
            "",
        )
    if returncode == 0:
        return CheckOutcome.OK, "Accepted by refinedc.", [], ""

    loc = _find_location(combined)
    loc_text = f" at {loc[0]}:{loc[1]}" if loc else ""

    # Bug witness from the front-end (Cerberus statically detected UB).
    if any(m in combined for m in facts.UB_MARKERS):
        witness_lines = [
            ln for ln in combined.splitlines() if any(m in ln for m in facts.UB_MARKERS)
        ]
        witness = "\n".join(witness_lines[:10])
        return (
            CheckOutcome.BUG,
            f"The front-end reports undefined behaviour in the code{loc_text}. This is a bug in "
            "the program, not in the annotations:\n" + witness,
            [],
            witness,
        )

    goals = extract_goals(combined)
    errs = _errors(combined)

    # Front-end failures take precedence when there is no Coq goal to report.
    if not goals and any(m in combined for m in facts.FRONTEND_ERROR_MARKERS):
        detail = "\n".join(errs[:8]) or _tail(combined, 15)
        fb = (
            f"FRONTEND_ERROR{loc_text}: the C front-end or the annotation parser rejected the "
            "file.\n" + detail + "\n"
            "Hint: check attribute syntax (each rc:: attribute takes string literals, one per "
            "clause), balanced angle brackets in types, and that every name in {Coq} braces is "
            "declared in rc::parameters or rc::exists. If the message names a C construct as "
            "unsupported, the function cannot be verified with this backend."
        )
        return CheckOutcome.FRONTEND_ERROR, _truncate(fb, MAX_FEEDBACK_LINES), [], ""

    if goals:
        first = goals[0]
        stuck = _is_stuck(first) or _is_stuck("\n".join(errs))
        shown = _truncate(first, MAX_GOAL_LINES)
        if stuck:
            hint = (
                "Hint: the automation could not type-check a program step. This almost always "
                "means an ownership description is missing or wrong (an rc::args pointer type "
                "that does not cover the memory the code touches, an rc::inv_vars entry that "
                "does not describe a variable's type at the loop head, or a missing "
                "rc::constraints fact such as `{i ≤ n}`). Repair the annotations; do not "
                "add proof text."
            )
            if "loop" in combined.lower() or "inv_vars" in combined:
                hint += " The failure is inside a loop: revisit rc::exists/rc::inv_vars/rc::constraints on that loop."
            fb = f"AUTOMATION_STUCK{loc_text} while checking `{function_name}`.\nRemaining goal:\n{shown}\n{hint}"
            return CheckOutcome.AUTOMATION_STUCK, _truncate(fb, MAX_FEEDBACK_LINES), goals, ""
        hint = (
            "Hint: the program type-checks but a pure side condition was not discharged "
            "automatically. Either strengthen rc::constraints / rc::requires so the fact "
            'follows by linear arithmetic, add `[[rc::tactics("all: try lia.")]]` '
            "(or nia / set_solver), or prove a helper lemma in lemmas.v and cite it via "
            "rc::lemmas."
        )
        fb = f"GOALS_REMAIN{loc_text} in `{function_name}`.\nUnsolved side condition:\n{shown}\n{hint}"
        return CheckOutcome.GOALS_REMAIN, _truncate(fb, MAX_FEEDBACK_LINES), goals, ""

    if errs:
        fb = f"The checker failed{loc_text} without a recognisable goal:\n" + "\n".join(errs[:10])
        return CheckOutcome.AUTOMATION_STUCK, _truncate(fb, MAX_FEEDBACK_LINES), [], ""

    return (
        CheckOutcome.TOOL_ERROR,
        f"refinedc exited with status {returncode} and no recognisable diagnostic:\n"
        + _tail(combined, 20),
        [],
        "",
    )
