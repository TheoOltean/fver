"""The agent protocol: everything an external prover needs, as plain dicts.

An external prover is anything that is not fver's own API loop: a Claude
Code session, a script, a person. fver keeps the parts that must not be
left to the model (task packaging, guardrails, the checker, the audit, the
ledger) and exposes them here. The CLI subcommands (`fver task`, `fver
check`, `fver next`, `fver changed`, ...) and the MCP server are thin
wrappers over these functions, so both behave identically.

Every function takes an AppContext and returns a JSON-serialisable dict.
Errors that the caller can act on are raised as ProtocolError with a
message meant for the model or the user.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fver.agent import prompts, store
from fver.agent.loop import AttemptOutcome, Verifier
from fver.agent.parse import BugReport, ParseError, parse_submission
from fver.backends.base import FunctionTask, Submission
from fver.core.context import AppContext
from fver.core.models import Finding, FunctionInfo, Status
from fver.ledger.memory import derive_status


class ProtocolError(ValueError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def resolve_function(ctx: AppContext, ident: str, file: str | None = None) -> FunctionInfo:
    """Find one function by ledger id or by name (+ optional file)."""
    fn = ctx.ledger.get_function(ident)
    if fn is not None:
        return fn
    matches = ctx.ledger.find_functions(name=ident, source_path=file)
    if not matches and file:
        matches = [f for f in ctx.ledger.find_functions(name=ident) if f.source_path.endswith(file)]
    if not matches:
        raise ProtocolError(
            f"no function named '{ident}'" + (f" in {file}" if file else "") + "; run `fver scan`?"
        )
    if len(matches) > 1:
        cands = ", ".join(f"{m.source_path}:{m.start_line} ({m.id})" for m in matches)
        raise ProtocolError(f"'{ident}' is ambiguous; pass the file or the id: {cands}")
    return matches[0]


def _verifier(ctx: AppContext, run_id: str) -> Verifier:
    if ctx.backend is None:
        raise ProtocolError("this command needs a backend; check [project].backend in config")
    return Verifier(ctx, llm=None, run_id=run_id)


def _fn_dict(fn: FunctionInfo, status: str | None = None, **more: Any) -> dict[str, Any]:
    d = {
        "id": fn.id,
        "name": fn.name,
        "file": fn.source_path,
        "lines": [fn.start_line, fn.end_line],
        "signature": fn.signature,
        "static": fn.is_static,
        "attack_score": round(fn.attack_score, 3),
        "attack_reasons": list(fn.attack_reasons),
        "callees": list(fn.callees),
    }
    if status is not None:
        d["status"] = status
    d.update(more)
    return d


def _status_of(ctx: AppContext, fn: FunctionInfo) -> str:
    # The verifier may have refreshed the function from disk; read it back.
    fn = ctx.ledger.get_function(fn.id) or fn
    claim = ctx.ledger.current_claim(fn.id, ctx.backend_name, ctx.target.key)
    return derive_status(fn, claim).value


def _task_dict(task: FunctionTask) -> dict[str, Any]:
    f = task.function
    return {
        "function": _fn_dict(f),
        "target": asdict(task.target),
        "function_text": task.function_text,
        "file_context": prompts.select_file_context(task.source_text, f),
        "callee_contracts": dict(task.callee_specs),
        "external_specs": dict(task.external_specs),
        "previous_accepted": task.previous.files if task.previous is not None else None,
    }


# ---------------------------------------------------------------------------
# protocol functions
# ---------------------------------------------------------------------------


def reference(ctx: AppContext) -> dict[str, Any]:
    """The backend's annotation language reference, examples and rules."""
    if ctx.backend is None:
        raise ProtocolError("no backend configured")
    pc = ctx.backend.prompt_context()
    spec = ctx.backend.submission_spec()
    return {
        "backend": ctx.backend.name,
        "reference": pc.reference,
        "examples": pc.examples,
        "instructions": "\n\n".join(
            [pc.instructions.strip(), prompts.UNIVERSAL_RULES, prompts.format_instructions(spec)]
        ),
        "forbidden_patterns": list(pc.forbidden_patterns),
        "submission_files": {
            name: {
                "description": desc,
                "required": name in spec.required,
                "language": spec.language_hints.get(name, ""),
            }
            for name, desc in spec.files.items()
        },
    }


def task(ctx: AppContext, ident: str, file: str | None = None) -> dict[str, Any]:
    """Everything needed to write a submission for one function."""
    fn = resolve_function(ctx, ident, file)
    v = _verifier(ctx, "task")
    t = v.build_task(fn)
    d = _task_dict(t)
    d["status"] = _status_of(ctx, fn)
    d["cache_key"] = v.cache_key_for(t)
    d["submission_files"] = reference(ctx)["submission_files"]
    d["attempts_so_far"] = v._next_attempt_number(t) - 1
    d["prompt"] = prompts.build_task_message(
        t, [], d["file_context"]
    )  # the same message the API loop would send
    return d


def render_task_text(ctx: AppContext, d: dict[str, Any], with_reference: bool = True) -> str:
    """Human/model readable packet: reference first, then the task prompt."""
    parts: list[str] = []
    if with_reference:
        ref = reference(ctx)
        parts += [
            "# Reference\n",
            ref["reference"].strip(),
            "\n# Examples\n",
            ref["examples"].strip(),
            "\n# Instructions\n",
            ref["instructions"].strip(),
        ]
    parts += ["\n", d["prompt"]]
    if d.get("attempts_so_far"):
        parts.append(f"\n(Attempts so far for this function: {d['attempts_so_far']}.)")
    return "\n".join(parts)


def submission_from_files(
    ctx: AppContext, files: dict[str, str]
) -> Submission | ParseError | BugReport:
    """Accept either named files or a single text in the fenced-block format."""
    if ctx.backend is None:
        raise ProtocolError("no backend configured")
    spec = ctx.backend.submission_spec()
    if len(files) == 1 and next(iter(files)) in ("-", "text", "reply"):
        return parse_submission(next(iter(files.values())), spec)
    clean = {Path(k).name: (v if v.endswith("\n") else v + "\n") for k, v in files.items()}
    missing = [f for f in spec.required if f not in clean]
    if missing:
        return ParseError("missing required file(s): " + ", ".join(missing))
    unknown = [f for f in clean if f not in spec.files]
    if unknown:
        return ParseError(
            "unknown file(s): " + ", ".join(unknown) + "; expected " + ", ".join(spec.files)
        )
    return Submission(files=clean)


def check(
    ctx: AppContext, ident: str, files: dict[str, str], file: str | None = None
) -> dict[str, Any]:
    """Judge a submission and record the outcome. Never raises for checker
    verdicts; the dict says what happened. `files` is {filename: content}, or
    {"-": whole reply in fenced-block format}."""
    fn = resolve_function(ctx, ident, file)
    run_id = ctx.ledger.start_run("check", ctx.backend_name, ctx.target.key, {"function": fn.id})
    ok = False
    try:
        parsed = submission_from_files(ctx, files)
        if isinstance(parsed, ParseError):
            return {
                "function": _fn_dict(fn),
                "kind": "parse_error",
                "verified": False,
                "feedback": parsed.message,
                "exit_code": 1,
            }
        if isinstance(parsed, BugReport):
            ctx.ledger.record_finding(
                Finding(
                    function_id=fn.id,
                    source_path=fn.source_path,
                    line=fn.start_line,
                    kind="suspected_ub",
                    tool="external-prover",
                    message=parsed.explanation[:2000],
                    run_id=run_id,
                )
            )
            ok = True
            return {
                "function": _fn_dict(fn),
                "kind": "bug_report",
                "verified": False,
                "feedback": "recorded as a suspected (unconfirmed) bug; a human should look",
                "exit_code": 1,
            }
        v = _verifier(ctx, run_id)
        t0 = time.monotonic()
        out: AttemptOutcome = v.submit(fn, parsed)
        ok = True
        return _attempt_dict(ctx, fn, out, time.monotonic() - t0)
    finally:
        ctx.ledger.end_run(run_id, ok)


def _attempt_dict(ctx: AppContext, fn: FunctionInfo, out: AttemptOutcome, secs: float) -> dict:
    res = out.result
    d: dict[str, Any] = {
        "function": _fn_dict(fn),
        "kind": out.kind,
        "verified": out.kind == "verified",
        "outcome": res.outcome.value if res else None,
        "feedback": out.feedback,
        "goals": list(res.goals) if res else [],
        "violations": list(out.violations),
        "assumptions": list(res.assumptions) if res else [],
        "proof_hash": res.proof_hash if res else None,
        "status": _status_of(ctx, fn),
        "seconds": round(secs, 2),
        "exit_code": 0 if out.kind == "verified" else (2 if out.kind == "tool_error" else 1),
    }
    if out.kind == "verified":
        d["proof_dir"] = str(ctx.ws.proof_dir(fn.source_path, fn.name))
    return d


def next_functions(ctx: AppContext, limit: int | None = 10, file: str | None = None) -> dict:
    """The next functions to attempt, in dependency-aware attack-score order."""
    from fver.commands.verify import _select

    selected = _select(ctx, [], file, limit, False, False, with_deps=True)
    items = []
    for fn in selected:
        unverified = 0
        for callee in fn.callees:
            if callee == fn.name:
                continue
            cands = ctx.ledger.find_functions(name=callee)
            if cands and _status_of(ctx, cands[0]) != Status.VERIFIED.value:
                unverified += 1
        items.append(
            _fn_dict(fn, status=_status_of(ctx, fn), unverified_internal_callees=unverified)
        )
    return {"functions": items, "count": len(items)}


def status(ctx: AppContext) -> dict[str, Any]:
    s = ctx.ledger.summary(ctx.backend_name, ctx.target.key)
    d = asdict(s)
    d["project"] = ctx.ws.project_name
    d["backend"] = ctx.backend_name
    d["target"] = ctx.target.key
    return d


def show(ctx: AppContext, ident: str, file: str | None = None) -> dict[str, Any]:
    fn = resolve_function(ctx, ident, file)
    claim = ctx.ledger.current_claim(fn.id, ctx.backend_name, ctx.target.key)
    history = ctx.ledger.claims_for(fn.id)
    loaded = store.load_accepted(ctx.ws, fn.source_path, fn.name)
    return {
        "function": _fn_dict(fn, status=_status_of(ctx, fn)),
        "current_claim": _claim_dict(claim) if claim else None,
        "history": [_claim_dict(c) for c in history],
        "accepted_submission": loaded[0].files if loaded else None,
        "proof_dir": str(ctx.ws.proofs_dir / fn.source_path / fn.name),
        "findings": [asdict(f) for f in ctx.ledger.findings(function_id=fn.id)],
        "callers": [_fn_dict(d, status=_status_of(ctx, d)) for d in ctx.ledger.dependents(fn.name)],
    }


def _claim_dict(c) -> dict[str, Any]:
    d = asdict(c)
    d["status"] = c.status.value
    d["property_class"] = c.property_class.value
    return d


def changed(ctx: AppContext, quick_scan: bool = True) -> dict[str, Any]:
    """Functions whose proofs went stale or that are unverified in files
    modified since the last scan. Re-runs a quick scan (no backend) first."""
    modified = _modified_files(ctx)
    if quick_scan:
        from fver.commands.scan import run_scan

        run_scan(ctx, preprocess=False, translate=False, quiet=True)
    rows = ctx.ledger.list_functions(ctx.backend_name, ctx.target.key, order_by_attack_score=True)
    stale = [
        _fn_dict(r.function, status=r.status.value, reason=(r.claim.message if r.claim else ""))
        for r in rows
        if r.status is Status.STALE
    ]
    in_modified = [
        _fn_dict(r.function, status=r.status.value)
        for r in rows
        if r.function.source_path in modified and r.status is not Status.VERIFIED
    ]
    return {
        "modified_files": sorted(modified),
        "stale": stale,
        "unverified_in_modified_files": in_modified,
        "count": len(stale) + len(in_modified),
    }


def _modified_files(ctx: AppContext) -> set[str]:
    root = ctx.ws.repo_root
    out: set[str] = set()
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                path = line[3:].strip().split(" -> ")[-1]
                if path.endswith((".c", ".h")):
                    out.add(path)
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    # Not a git repo: files newer than the last scan.
    last = ctx.ws.state_path("index")
    since = last.stat().st_mtime if last.exists() else 0.0
    for p in root.rglob("*.[ch]"):
        if ctx.ws.is_inside_workspace(p):
            continue
        try:
            if p.stat().st_mtime > since:
                out.add(ctx.ws.relpath(p))
        except OSError:
            continue
    return out


def scan(ctx: AppContext, translate: bool = True) -> dict[str, Any]:
    from fver.commands.scan import run_scan

    index = run_scan(ctx, preprocess=True, translate=translate and ctx.backend is not None)
    return {
        "translation_units": len(index.get("tus", [])),
        "functions": len(index.get("functions", [])),
        "unsupported": len(index.get("unsupported", {})),
        "stale": len(index.get("stale", [])),
        "build_source": index.get("build_source"),
    }


def hunt(ctx: AppContext, file: str | None = None, only: list[str] | None = None) -> dict:
    from fver.commands.hunt import run_hunt

    findings = run_hunt(ctx, only, None, file)
    return {"findings": [asdict(f) for f in findings], "count": len(findings)}
