"""The propose -> check -> repair loop, per function, and the run driver.

Everything backend-specific goes through `Backend`. Everything LLM-specific
goes through `LLMClient`. This module owns budgets, the conversation shape,
cache hits, guardrails, and what gets recorded in the ledger.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fver.backends.base import (
    Backend,
    CheckOutcome,
    CheckResult,
    FunctionTask,
    Submission,
)
from fver.core.context import AppContext
from fver.core.models import Claim, Cost, Finding, FunctionInfo, PropertyClass, Status
from fver.ledger.cache import cache_key as make_cache_key
from fver.prove import invalidate, prompts, store
from fver.prove.client import Completion, FatalAgentError, RetryableAgentError
from fver.prove.parse import BugReport, ParseError, parse_submission
from fver.prove.retrieval import pick_examples

log = logging.getLogger("fver.prove.loop")

HISTORY_KEEP = 3  # exchanges kept verbatim; older ones are summarised


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


@dataclass
class Exchange:
    assistant_blocks: list[Any]
    feedback: str
    summary: str  # one line, for when this exchange is elided


@dataclass
class Conversation:
    """Task message first, then (assistant submission, user feedback) pairs.

    Rendering keeps the task and the last HISTORY_KEEP exchanges verbatim;
    older ones are condensed into a note appended to the newest feedback.
    Earlier turns are never edited, so replayed thinking blocks stay valid.
    """

    task_message: str
    exchanges: list[Exchange] = field(default_factory=list)

    def add(self, completion: Completion, feedback: str, summary: str) -> None:
        self.exchanges.append(Exchange(list(completion.content_blocks), feedback, summary))

    def render(self) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": self.task_message}]
        dropped = self.exchanges[:-HISTORY_KEEP] if len(self.exchanges) > HISTORY_KEEP else []
        kept = self.exchanges[-HISTORY_KEEP:] if self.exchanges else []
        for i, ex in enumerate(kept):
            msgs.append({"role": "assistant", "content": ex.assistant_blocks})
            feedback = ex.feedback
            if dropped and i == len(kept) - 1:
                note = "\n".join(f"- {d.summary}" for d in dropped)
                feedback = feedback + "\n\nEarlier attempts (elided):\n" + note
            msgs.append({"role": "user", "content": feedback})
        return msgs


# ---------------------------------------------------------------------------
# Outcome of one function
# ---------------------------------------------------------------------------


@dataclass
class FunctionOutcome:
    claim: Claim
    cache_hit: bool = False
    attempts: int = 0


class RunBudgetExceeded(RuntimeError):
    pass


@dataclass
class AttemptOutcome:
    """What happened to one submission. `kind` is one of:
    verified, bug, tool_error, guardrail, audit_failed, feedback (checker
    rejected; `feedback` says why). `claim` is set for the terminal kinds
    (verified, bug, tool_error) and is NOT yet recorded in the ledger."""

    kind: str
    feedback: str = ""
    result: CheckResult | None = None
    violations: list[str] = field(default_factory=list)
    claim: Claim | None = None

    @property
    def terminal(self) -> bool:
        return self.kind in ("verified", "bug", "tool_error")


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class Verifier:
    def __init__(self, ctx: AppContext, llm: Any, run_id: str):
        if ctx.backend is None:
            raise ValueError("Verifier needs a backend")
        self.ctx = ctx
        self.ws = ctx.ws
        self.ledger = ctx.ledger
        self.backend: Backend = ctx.backend
        self.llm = llm
        self.run_id = run_id
        self.cfg = ctx.config
        self.target = ctx.target
        self._prompt_ctx = self.backend.prompt_context()
        self._spec = self.backend.submission_spec()
        self._system = prompts.build_system(self._prompt_ctx, self._spec, self.target)
        self._forbidden = [re.compile(p, re.MULTILINE) for p in self._prompt_ctx.forbidden_patterns]
        self._tool_versions = self.backend.tool_versions()
        self._cost_lock = threading.Lock()
        self.run_cost = Cost()

    # -- task construction --------------------------------------------------------

    def build_task(self, function: FunctionInfo) -> FunctionTask:
        tu = self.ledger.get_tu(function.tu_id)
        if tu is None:
            raise ValueError(f"translation unit {function.tu_id} not in ledger; run `fver status`")
        src_path = self.ws.repo_root / function.source_path
        source_text = src_path.read_text(encoding="utf-8", errors="replace")
        function = self._refresh_from_source(function, source_text)
        lines = source_text.split("\n")
        function_text = "\n".join(lines[function.start_line - 1 : function.end_line]) + "\n"
        workdir = self.ws.backend_dir(self.backend.name) / "work" / function.id.replace(":", "_")
        workdir.mkdir(parents=True, exist_ok=True)
        callees = store.callee_specs(self.ws, self.ledger, self.backend, function, self.target.key)
        externals = store.external_specs(self.ws, function)
        previous = None
        loaded = store.load_accepted(self.ws, function.source_path, function.name)
        if loaded is not None:
            previous = loaded[0]
        return FunctionTask(
            function=function,
            tu=tu,
            target=self.target,
            repo_root=self.ws.repo_root,
            workdir=workdir,
            source_text=source_text,
            function_text=function_text,
            callee_specs=callees,
            external_specs=externals,
            previous=previous,
        )

    def _refresh_from_source(self, function: FunctionInfo, source_text: str) -> FunctionInfo:
        """The file may have changed since the last scan (an edit, a revert).
        Re-extract this function so lines, callees and above all the body
        hash reflect the code on disk; update the ledger if anything moved."""
        from dataclasses import replace

        from fver.index.functions import extract_from_source

        try:
            fresh = [
                f
                for f in extract_from_source(source_text, function.source_path, function.tu_id)
                if f.name == function.name
            ]
        except Exception:  # noqa: BLE001 - extraction trouble: keep the indexed view
            return function
        if not fresh:
            return function
        f = fresh[0]
        if (f.start_line, f.end_line, f.body_hash, f.callees) == (
            function.start_line,
            function.end_line,
            function.body_hash,
            function.callees,
        ):
            return function
        updated = replace(
            function,
            start_line=f.start_line,
            end_line=f.end_line,
            body_hash=f.body_hash,
            signature=f.signature,
            callees=list(f.callees),
        )
        self.ledger.upsert_functions([updated])
        return updated

    def cache_key_for(self, task: FunctionTask) -> str:
        return make_cache_key(
            body_hash=task.function.body_hash,
            callee_spec_hashes=store.hash_specs(task.callee_specs),
            external_spec_hashes=store.hash_specs(task.external_specs),
            backend=self.backend.name,
            tool_versions=self._tool_versions,
            target_key=self.target.key,
            property_class=PropertyClass.UB_FREE.value,
        )

    def _claim(
        self,
        task: FunctionTask,
        status: Status,
        key: str,
        cost: Cost,
        message: str,
        result: CheckResult | None = None,
        **extra: Any,
    ) -> Claim:
        return Claim(
            function_id=task.function.id,
            property_class=PropertyClass.UB_FREE,
            backend=self.backend.name,
            target_key=self.target.key,
            status=status,
            body_hash=task.function.body_hash,
            cache_key=key,
            proof_hash=result.proof_hash if result else None,
            tool_versions=result.tool_versions
            if result and result.tool_versions
            else dict(self._tool_versions),
            assumptions=list(result.assumptions) if result else [],
            message=message[:2000],
            cost=cost,
            run_id=self.run_id,
            extra=extra,
        )

    def _add_cost(self, cost: Cost) -> None:
        with self._cost_lock:
            self.run_cost = self.run_cost.add(cost)

    # -- guardrails --------------------------------------------------------------

    def _guardrail(self, submission: Submission) -> list[str]:
        violations = list(self.backend.guardrail(submission))
        for name, content in submission.files.items():
            for rx in self._forbidden:
                m = rx.search(content)
                if m:
                    violations.append(
                        f"{name}: forbidden pattern `{rx.pattern}` at `{m.group(0)[:60]}`"
                    )
        return violations

    # -- one attempt: guardrail -> check -> audit -> save --------------------------------

    def attempt_submission(
        self,
        task: FunctionTask,
        key: str,
        submission: Submission,
        attempt: int,
        remaining: int,
        cost: Cost,
        **claim_extra: Any,
    ) -> AttemptOutcome:
        """Judge one submission for `task`. Saves accepted proofs and failed
        attempts to disk; returns claims without recording them so the caller
        controls cost accounting and ordering."""
        f = task.function
        violations = self._guardrail(submission)
        if violations:
            fb = prompts.build_guardrail_message(violations, attempt, remaining)
            store.save_attempt(self.ws, task, attempt, submission, fb)
            return AttemptOutcome("guardrail", fb, violations=violations)

        result = self.backend.check(task, submission, self.cfg.budget.checker_timeout_seconds)
        cost.checker_runs += 1

        if result.outcome is CheckOutcome.TOOL_ERROR:
            msg = "checker failed: " + (result.feedback or result.stderr)[:500]
            claim = self._claim(task, Status.UNRESOLVED, key, cost, msg, result, **claim_extra)
            return AttemptOutcome("tool_error", result.feedback, result, claim=claim)

        if result.outcome is CheckOutcome.BUG:
            self.ledger.record_finding(
                Finding(
                    function_id=f.id,
                    source_path=f.source_path,
                    line=f.start_line,
                    kind="undefined_behaviour",
                    tool=self.backend.name,
                    message=result.feedback[:2000],
                    witness=result.witness,
                    run_id=self.run_id,
                )
            )
            store.save_attempt(self.ws, task, attempt, submission, result.feedback)
            claim = self._claim(
                task,
                Status.BUG_FOUND,
                key,
                cost,
                "bug found: " + result.feedback[:300],
                result,
                **claim_extra,
            )
            return AttemptOutcome("bug", result.feedback, result, claim=claim)

        if result.outcome is CheckOutcome.OK:
            audit = self.backend.audit(task, result)
            if not audit.passed:
                fb = prompts.build_audit_failure_message(audit.violations, attempt, remaining)
                store.save_attempt(self.ws, task, attempt, submission, fb)
                return AttemptOutcome("audit_failed", fb, result, violations=audit.violations)
            extra_assumptions = set(audit.assumptions) | set(
                store.unverified_callee_assumptions(
                    self.ledger, self.backend, task.function, self.target.key
                )
            )
            if extra_assumptions:
                result.assumptions = sorted(set(result.assumptions) | extra_assumptions)
            if not result.tool_versions:
                result.tool_versions = dict(self._tool_versions)
            if result.proof_hash is None:
                result.proof_hash = submission.content_hash()
            old_contract = (
                self.backend.extract_spec(task.previous, task.function)
                if task.previous is not None
                else None
            )
            store.save_accepted(self.ws, task, submission, result, key, self.backend.name)
            store.cache_insert(self.ws, key, Status.VERIFIED, submission, result)
            claim = self._claim(
                task,
                Status.VERIFIED,
                key,
                cost,
                f"verified in {attempt} attempt(s)",
                result,
                **claim_extra,
            )
            invalidate.invalidate_dependents(
                self.ctx,
                task.function,
                old_contract,
                self.backend.extract_spec(submission, task.function),
                self.run_id,
            )
            return AttemptOutcome("verified", "", result, claim=claim)

        # AUTOMATION_STUCK / GOALS_REMAIN / FRONTEND_ERROR / GUARDRAIL: feed back.
        fb = prompts.build_feedback_message(result, attempt, remaining)
        store.save_attempt(self.ws, task, attempt, submission, fb)
        return AttemptOutcome("feedback", fb, result)

    # -- external prover: one submission judged and recorded -------------------------

    def submit(self, function: FunctionInfo, submission: Submission) -> AttemptOutcome:
        """Judge a submission written by an external prover (a Claude Code
        session, a script, a human) and record the outcome in the ledger the
        same way the internal loop would. LLM cost is zero; the claim carries
        extra["prover"] = "external"."""
        task = self.build_task(function)
        key = self.cache_key_for(task)
        cached = store.cache_lookup(self.ws, key)
        if (
            cached is not None
            and cached.get("status") == Status.VERIFIED.value
            and cached.get("submission") == submission.files
        ):
            # Same code, same contracts, same annotations: the stored proof
            # stands (e.g. an edit was reverted). No checker run needed.
            result = CheckResult(
                outcome=CheckOutcome.OK,
                feedback="",
                proof_hash=cached.get("proof_hash"),
                assumptions=list(cached.get("assumptions", [])),
                tool_versions=dict(cached.get("tool_versions", {})),
            )
            store.save_accepted(self.ws, task, submission, result, key, self.backend.name)
            claim = self._claim(
                task,
                Status.VERIFIED,
                key,
                Cost(),
                "cache hit",
                result,
                cache_hit=True,
                prover="external",
            )
            self.ledger.record_claim(claim)
            return AttemptOutcome(kind="verified", feedback="", result=result, claim=claim)
        attempt = self._next_attempt_number(task)
        cost = Cost()
        cur = self.ledger.current_claim(function.id, self.backend.name, self.target.key)
        if cur is None or cur.status is not Status.IN_PROGRESS:
            self.ledger.record_claim(
                self._claim(task, Status.IN_PROGRESS, key, Cost(), "external prover started")
            )
        out = self.attempt_submission(task, key, submission, attempt, 0, cost, prover="external")
        if out.claim is not None:
            self.ledger.record_claim(out.claim)
        else:
            first = out.feedback.split("\n", 1)[0][:200]
            self.ledger.record_claim(
                self._claim(
                    task,
                    Status.IN_PROGRESS,
                    key,
                    cost,
                    f"attempt {attempt} ({out.kind}): {first}",
                    out.result,
                    prover="external",
                )
            )
        return out

    def _next_attempt_number(self, task: FunctionTask) -> int:
        d = self.ws.proofs_dir / task.function.source_path / task.function.name / "attempts"
        if not d.exists():
            return 1
        nums = [int(p.name) for p in d.iterdir() if p.name.isdigit()]
        return (max(nums) + 1) if nums else 1

    # -- the loop -------------------------------------------------------------------

    def verify_function(self, function: FunctionInfo) -> FunctionOutcome:
        t0 = time.monotonic()
        task = self.build_task(function)
        key = self.cache_key_for(task)
        log.info("[%s] cache key %s", function.name, key[:12])

        cached = store.cache_lookup(self.ws, key)
        if cached is not None and cached.get("status") == Status.VERIFIED.value:
            sub = Submission(files=cached["submission"], note=cached.get("note", ""))
            result = CheckResult(
                outcome=CheckOutcome.OK,
                feedback="",
                proof_hash=cached.get("proof_hash"),
                assumptions=list(cached.get("assumptions", [])),
                tool_versions=dict(cached.get("tool_versions", {})),
            )
            store.save_accepted(self.ws, task, sub, result, key, self.backend.name)
            claim = self._claim(
                task, Status.VERIFIED, key, Cost(), "cache hit", result, cache_hit=True
            )
            self.ledger.record_claim(claim)
            log.info("[%s] verified (cache hit)", function.name)
            return FunctionOutcome(claim=claim, cache_hit=True)

        self.ledger.record_claim(self._claim(task, Status.IN_PROGRESS, key, Cost(), "started"))
        outcome = self._attempt_loop(task, key)
        outcome.claim.cost.wall_seconds = time.monotonic() - t0
        self.ledger.record_claim(outcome.claim)
        self._add_cost(outcome.claim.cost)
        log.info(
            "[%s] %s after %d attempt(s), $%.3f",
            function.name,
            outcome.claim.status.value,
            outcome.attempts,
            outcome.claim.cost.usd,
        )
        return outcome

    def _attempt_loop(self, task: FunctionTask, key: str) -> FunctionOutcome:
        f = task.function
        budget = self.cfg.budget
        max_attempts = budget.max_attempts_per_function
        cost = Cost()
        examples = pick_examples(self.ledger, self.ws, self.backend, task)
        file_context = prompts.select_file_context(task.source_text, f)
        convo = Conversation(prompts.build_task_message(task, examples, file_context))
        last_feedback = ""
        best: tuple[int, Submission] | None = None  # (rank, submission) higher rank = closer
        refusals = 0
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            remaining = max_attempts - attempt
            if cost.usd >= budget.max_usd_per_function:
                last_feedback = f"per-function budget of ${budget.max_usd_per_function:.2f} spent"
                attempt -= 1
                break
            try:
                completion = self.llm.complete(
                    self._system, convo.render(), log_name=f"{f.name}-{attempt}"
                )
            except RetryableAgentError as e:
                claim = self._claim(task, Status.UNRESOLVED, key, cost, f"LLM unavailable: {e}")
                return FunctionOutcome(claim, attempts=attempt - 1)
            cost = cost.add(completion.cost)

            if completion.refused:
                refusals += 1
                log.warning("[%s] model refused (%s)", f.name, completion.refusal_reason)
                if refusals >= 2:
                    msg = f"model refused twice: {completion.refusal_reason or 'no reason given'}"
                    claim = self._claim(task, Status.UNRESOLVED, key, cost, msg)
                    return FunctionOutcome(claim, attempts=attempt)
                continue  # nothing to append; simply ask again

            parsed = parse_submission(completion.text, self._spec)

            if isinstance(parsed, BugReport):
                self.ledger.record_finding(
                    Finding(
                        function_id=f.id,
                        source_path=f.source_path,
                        line=f.start_line,
                        kind="suspected_ub",
                        tool="llm",
                        message=parsed.explanation[:2000],
                        run_id=self.run_id,
                    )
                )
                msg = "model reports a suspected bug (unconfirmed): " + parsed.explanation[:300]
                claim = self._claim(task, Status.UNRESOLVED, key, cost, msg, suspected_bug=True)
                return FunctionOutcome(claim, attempts=attempt)

            if isinstance(parsed, ParseError):
                last_feedback = prompts.build_parse_error_message(
                    parsed.message, attempt, remaining
                )
                convo.add(completion, last_feedback, f"attempt {attempt}: unreadable submission")
                continue

            submission = parsed
            out = self.attempt_submission(task, key, submission, attempt, remaining, cost)
            if out.kind == "guardrail":
                last_feedback = out.feedback
                convo.add(completion, last_feedback, f"attempt {attempt}: guardrail violation")
                continue
            if out.kind == "tool_error":
                assert out.claim is not None
                return FunctionOutcome(out.claim, attempts=attempt)
            if out.kind == "bug":
                assert out.claim is not None
                return FunctionOutcome(out.claim, attempts=attempt)
            if out.kind == "verified":
                assert out.claim is not None
                return FunctionOutcome(out.claim, attempts=attempt)
            if out.kind == "audit_failed":
                last_feedback = out.feedback
                convo.add(completion, last_feedback, f"attempt {attempt}: audit failed")
                continue
            # feedback: checker rejected the submission
            result = out.result
            assert result is not None
            rank = {CheckOutcome.GOALS_REMAIN: 3, CheckOutcome.AUTOMATION_STUCK: 2}.get(
                result.outcome, 1
            )
            if best is None or rank >= best[0]:
                best = (rank, submission)
            last_feedback = out.feedback
            convo.add(completion, last_feedback, f"attempt {attempt}: {result.outcome.value}")

        msg = f"unresolved after {attempt} attempt(s)"
        if last_feedback:
            msg += ": " + last_feedback.split("\n", 1)[0][:200]
        claim = self._claim(
            task,
            Status.UNRESOLVED,
            key,
            cost,
            msg,
            best_submission=(best[1].files if best else None),
        )
        return FunctionOutcome(claim, attempts=attempt)

    # -- recheck: re-run the checker on the stored submission without the LLM --------

    def recheck_function(self, function: FunctionInfo) -> FunctionOutcome:
        task = self.build_task(function)
        key = self.cache_key_for(task)
        loaded = store.load_accepted(self.ws, function.source_path, function.name)
        if loaded is None:
            claim = self._claim(
                task, Status.UNRESOLVED, key, Cost(), "no stored submission to recheck"
            )
            self.ledger.record_claim(claim)
            return FunctionOutcome(claim)
        submission, _ = loaded
        result = self.backend.check(task, submission, self.cfg.budget.checker_timeout_seconds)
        cost = Cost(checker_runs=1)
        if result.outcome is CheckOutcome.OK and self.backend.audit(task, result).passed:
            if result.proof_hash is None:
                result.proof_hash = submission.content_hash()
            if not result.tool_versions:
                result.tool_versions = dict(self._tool_versions)
            store.save_accepted(self.ws, task, submission, result, key, self.backend.name)
            store.cache_insert(self.ws, key, Status.VERIFIED, submission, result)
            claim = self._claim(task, Status.VERIFIED, key, cost, "recheck passed", result)
        else:
            cp = store.cache_path(self.ws, key)
            if cp.exists():
                cp.unlink()
            status = Status.BUG_FOUND if result.outcome is CheckOutcome.BUG else Status.UNRESOLVED
            claim = self._claim(
                task, status, key, cost, "recheck failed: " + result.feedback[:300], result
            )
        self.ledger.record_claim(claim)
        return FunctionOutcome(claim, attempts=1)

    # -- run driver -------------------------------------------------------------------

    def run(
        self,
        functions: list[FunctionInfo],
        max_usd_run: float,
        parallelism: int = 1,
        recheck: bool = False,
        on_done: Callable[[FunctionInfo, FunctionOutcome], None] | None = None,
    ) -> list[FunctionOutcome]:
        """Verify functions in attack-score order, stopping when the run cap is hit."""
        ordered = sorted(functions, key=lambda fn: (-fn.attack_score, fn.source_path, fn.name))
        work = self.recheck_function if recheck else self.verify_function
        outcomes: list[FunctionOutcome] = []
        parallelism = max(1, parallelism)
        fatal: FatalAgentError | None = None
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            pending: dict[Future, FunctionInfo] = {}
            queue = list(ordered)
            while queue or pending:
                while queue and len(pending) < parallelism and fatal is None:
                    if self.run_cost.usd >= max_usd_run:
                        log.warning("run budget of $%.2f reached; not scheduling more", max_usd_run)
                        queue.clear()
                        break
                    fn = queue.pop(0)
                    pending[pool.submit(work, fn)] = fn
                if not pending:
                    break
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in done:
                    fn = pending.pop(fut)
                    try:
                        outcome = fut.result()
                    except FatalAgentError as e:
                        fatal = e
                        queue.clear()
                        log.error("fatal: %s", e)
                        continue
                    except Exception as e:  # keep the run going; record the failure
                        log.exception("[%s] crashed", fn.name)
                        task = self.build_task(fn)
                        key = self.cache_key_for(task)
                        claim = self._claim(
                            task, Status.UNRESOLVED, key, Cost(), f"internal error: {e}"
                        )
                        self.ledger.record_claim(claim)
                        outcome = FunctionOutcome(claim)
                    outcomes.append(outcome)
                    if on_done:
                        on_done(fn, outcome)
        if fatal is not None:
            raise fatal
        return outcomes


def workdir_for(ws_backend_dir: Path, function_id: str) -> Path:
    return ws_backend_dir / "work" / function_id.replace(":", "_")
