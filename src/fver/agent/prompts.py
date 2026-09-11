"""Prompt assembly. Stable, backend-supplied material goes in the system
blocks (cached); everything about the specific function goes in messages.
"""

from __future__ import annotations

from fver.backends.base import (
    CheckOutcome,
    CheckResult,
    FunctionTask,
    PromptContext,
    Submission,
    SubmissionSpec,
)
from fver.core.models import FunctionInfo, Target

WHOLE_FILE_LINE_LIMIT = 400
HEAD_CONTEXT_LINES = 120

UNIVERSAL_RULES = """\
Universal rules (these override anything else):
1. The only goal is to prove the function is free of undefined behaviour for every
   input its callers may give it: no out-of-bounds access, no use after free, no null
   or dangling dereference, no signed overflow, no division by zero, no invalid
   shifts, no uninitialised reads, no invalid pointer arithmetic. Do not attempt
   functional correctness beyond what the safety proof needs.
2. Never change the C code. Add annotations and proof text only. Reformatting,
   renaming, reordering, deleting or "fixing" code is forbidden and is detected.
3. No escape hatches: no admitted goals, no trusted/skipped functions, no new axioms,
   no assumptions that make the precondition unsatisfiable. A proof that goes through
   by demanding `false` of the caller is worthless and is detected.
4. Preconditions must be the weakest ones that make the function safe, so that real
   callers can satisfy them. Do not narrow the precondition just to make the proof
   easier.
5. If you believe the code has a genuine defect that no honest precondition can rule
   out, do not force a proof. Instead reply with a single paragraph beginning with
   `BUG:` describing the input that triggers the undefined behaviour and where.
6. Write the whole submission every time. Partial diffs are not accepted.
"""


def format_instructions(spec: SubmissionSpec) -> str:
    lines = ["Output format: reply with one fenced code block per file, exactly:", ""]
    for name, desc in spec.files.items():
        lang = spec.language_hints.get(name, "")
        req = "required" if name in spec.required else "optional"
        lines.append(f"```{lang} file={name}")
        lines.append(f"... {desc} ({req}) ...")
        lines.append("```")
        lines.append("")
    lines.append("You may add a short explanation before the blocks. Nothing after the last block.")
    return "\n".join(lines)


def build_system(backend: PromptContext, spec: SubmissionSpec, target: Target) -> list[str]:
    """Stable system blocks in a fixed order (reference, examples, instructions)."""
    forbidden = ""
    if backend.forbidden_patterns:
        forbidden = "Forbidden constructs (regexes that reject a submission):\n" + "\n".join(
            f"  - {p}" for p in backend.forbidden_patterns
        )
    instructions = "\n\n".join(
        part
        for part in (
            (
                "You are a verification engineer. You produce annotations and proofs that a "
                "proof checker accepts; you do not persuade, you satisfy the checker."
            ),
            backend.instructions.strip(),
            UNIVERSAL_RULES,
            forbidden,
            format_instructions(spec),
        )
        if part
    )
    return [backend.reference.strip(), backend.examples.strip(), instructions]


def describe_target(target: Target) -> str:
    return (
        f"Target: {target.triple} ({target.compiler}); int is {target.int_bits} bits, long is "
        f"{target.long_bits} bits, pointers are {target.pointer_bits} bits, char is "
        f"{'signed' if target.char_signed else 'unsigned'}, "
        f"{'little' if target.little_endian else 'big'}-endian."
    )


def select_file_context(source_text: str, function: FunctionInfo) -> str:
    """The whole file if small; otherwise the file head (includes, types,
    macros, globals) plus the function itself, with line numbers."""
    lines = source_text.split("\n")
    if len(lines) <= WHOLE_FILE_LINE_LIMIT:
        return _numbered(lines, 1)
    head = lines[:HEAD_CONTEXT_LINES]
    start, end = function.start_line, function.end_line
    body = lines[start - 1 : end]
    parts = [_numbered(head, 1)]
    if start > HEAD_CONTEXT_LINES + 1:
        parts.append(f"... ({start - HEAD_CONTEXT_LINES - 1} lines elided) ...")
        parts.append(_numbered(body, start))
    return "\n".join(parts)


def _numbered(lines: list[str], first: int) -> str:
    width = len(str(first + len(lines)))
    return "\n".join(f"{i:{width}d}| {ln}" for i, ln in enumerate(lines, first))


def build_task_message(
    task: FunctionTask,
    examples_from_repo: list[tuple[FunctionInfo, Submission]],
    file_context: str,
) -> str:
    f = task.function
    parts: list[str] = []
    parts.append(f"# Task: prove `{f.name}` free of undefined behaviour")
    parts.append(describe_target(task.target))
    parts.append(
        f"File: `{f.source_path}` lines {f.start_line}-{f.end_line}. Signature: `{f.signature}`."
    )
    parts.append("## The function\n```c\n" + task.function_text.rstrip("\n") + "\n```")
    parts.append(
        "## File context (read-only; for types, macros and globals)\n```c\n"
        + file_context.rstrip("\n")
        + "\n```"
    )
    if task.callee_specs:
        parts.append(
            "## Contracts of callees (already verified; rely on these, do not restate them)\n"
            + "\n".join(f"### `{n}`\n```\n{s.rstrip()}\n```" for n, s in task.callee_specs.items())
        )
    if task.external_specs:
        parts.append(
            "## Trusted specifications of external functions\n"
            + "\n".join(
                f"### `{n}`\n```\n{s.rstrip()}\n```" for n, s in task.external_specs.items()
            )
        )
    unresolved = [
        c for c in f.callees if c not in task.callee_specs and c not in task.external_specs
    ]
    if unresolved:
        parts.append(
            "## Callees without a contract yet: "
            + ", ".join(f"`{c}`" for c in unresolved)
            + "\nIf the function calls these, you must state the contract you rely on for them "
            "in the submission; it will be checked against the callee when that one is verified."
        )
    if task.previous is not None:
        prev = "\n".join(
            f"```{name} \n{content.rstrip()}\n```" for name, content in task.previous.files.items()
        )
        parts.append(
            "## Previously accepted submission (the code has changed since; adapt it)\n" + prev
        )
    if examples_from_repo:
        ex = []
        for info, sub in examples_from_repo:
            ex.append(f"### `{info.name}` ({info.source_path})")
            for name, content in sub.files.items():
                ex.append(f"```file={name}\n{content.rstrip()}\n```")
        parts.append(
            "## Accepted submissions from this repository (style reference)\n" + "\n".join(ex)
        )
    parts.append("Produce the submission now.")
    return "\n\n".join(parts)


_OUTCOME_HEADLINE = {
    CheckOutcome.AUTOMATION_STUCK: "The checker's automation got stuck. Usually the ownership "
    "description or a loop invariant is wrong or missing.",
    CheckOutcome.GOALS_REMAIN: "The automation finished but proof goals remain. Close them with "
    "proof text (or tighten the annotations so they do not arise).",
    CheckOutcome.FRONTEND_ERROR: "The front-end rejected the submission (syntax or unsupported "
    "construct). Fix the annotation syntax; do not change the code.",
    CheckOutcome.GUARDRAIL: "The submission used a forbidden escape hatch or altered the code.",
    CheckOutcome.TOOL_ERROR: "The checker itself failed.",
    CheckOutcome.BUG: "The checker found a concrete undefined-behaviour witness.",
    CheckOutcome.OK: "Accepted.",
}


def build_feedback_message(result: CheckResult, attempt_no: int, remaining: int) -> str:
    parts = [f"# Attempt {attempt_no} result: {result.outcome.value}"]
    parts.append(_OUTCOME_HEADLINE.get(result.outcome, ""))
    if result.feedback.strip():
        parts.append("## Checker feedback\n```\n" + result.feedback.strip()[:12000] + "\n```")
    if result.goals:
        goals = "\n\n".join(g.strip() for g in result.goals[:10])
        parts.append("## Remaining goals\n```\n" + goals[:12000] + "\n```")
    if remaining > 0:
        parts.append(
            f"{remaining} attempt(s) remain. Revise and resend the complete submission "
            "(every file), following the same format."
        )
    else:
        parts.append("This was the last attempt.")
    return "\n\n".join(p for p in parts if p)


def build_guardrail_message(violations: list[str], attempt_no: int, remaining: int) -> str:
    body = "\n".join(f"- {v}" for v in violations)
    return build_feedback_message(
        CheckResult(outcome=CheckOutcome.GUARDRAIL, feedback=body), attempt_no, remaining
    )


def build_parse_error_message(message: str, attempt_no: int, remaining: int) -> str:
    parts = [f"# Attempt {attempt_no}: could not read the submission", message]
    if remaining > 0:
        parts.append(f"{remaining} attempt(s) remain. Resend in the required format.")
    return "\n\n".join(parts)


def build_audit_failure_message(violations: list[str], attempt_no: int, remaining: int) -> str:
    body = "The proof was accepted by the checker but failed the assumption audit:\n" + "\n".join(
        f"- {v}" for v in violations
    )
    return build_feedback_message(
        CheckResult(outcome=CheckOutcome.GUARDRAIL, feedback=body), attempt_no, remaining
    )
