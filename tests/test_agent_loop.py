import json

from fver.agent import store
from fver.agent.client import FakeLLMClient, FatalAgentError
from fver.agent.loop import Verifier
from fver.core.models import Status
from tests.test_agent_fakes import FakeBackend, make_ctx, make_repo, sub

ACCEPT = sub("void zero(char *buf, size_t n) { /* [[fake::args(buf, n)]] FVER_ACCEPT */ }")
STUCK = sub("void zero(char *buf, size_t n) { /* wrong */ }")
GOALS = sub("void zero(char *buf, size_t n) { /* FVER_GOALS */ }")
CHEAT = sub("void zero(char *buf, size_t n) { /* FVER_CHEAT */ }")
BUG = sub("void zero(char *buf, size_t n) { /* FVER_BUG */ }")
TRUST = sub("void zero(char *buf, size_t n) { /* [[fake::trust_me]] FVER_ACCEPT */ }")


def _setup(tmp_path, responses, **backend_kw):
    ws, ledger, helper, zero, use = make_repo(tmp_path)
    backend = FakeBackend(**backend_kw)
    ctx = make_ctx(ws, ledger, backend)
    llm = FakeLLMClient(responses=list(responses))
    return ctx, llm, Verifier(ctx, llm, run_id="r1"), (helper, zero, use), backend


def test_accept_on_second_attempt(tmp_path):
    ctx, llm, v, (_helper, zero, _use), _backend = _setup(tmp_path, [STUCK, ACCEPT])
    out = v.verify_function(zero)
    assert out.claim.status is Status.VERIFIED
    assert out.attempts == 2 and len(llm.calls) == 2
    # second call carries the feedback from the first
    msgs = llm.calls[1]["messages"]
    assert msgs[-1]["role"] == "user" and "automation_stuck" in msgs[-1]["content"]
    assert out.claim.cost.llm_calls == 2 and out.claim.cost.checker_runs == 2
    assert out.claim.cost.usd > 0
    assert "libc:memcpy" in out.claim.assumptions
    assert "axiom:functional_extensionality" in out.claim.assumptions  # audit adds
    # stored on disk
    loaded = store.load_accepted(ctx.ws, "src/a.c", "zero")
    assert loaded is not None and "FVER_ACCEPT" in loaded[0].files["function.c"]
    rec = json.loads((ctx.ws.proofs_dir / "src/a.c" / "zero" / "result.json").read_text())
    assert rec["cache_key"] == out.claim.cache_key
    # attempts dir keeps the failed one
    assert (ctx.ws.proofs_dir / "src/a.c" / "zero" / "attempts" / "1" / "function.c").exists()
    # ledger has IN_PROGRESS then VERIFIED
    statuses = [c.status for c in ctx.ledger.claims_for(zero.id)]
    assert statuses == [Status.IN_PROGRESS, Status.VERIFIED]


def test_guardrail_violation_then_accept(tmp_path):
    _ctx, llm, v, (_, zero, _), backend = _setup(tmp_path, [CHEAT, ACCEPT])
    out = v.verify_function(zero)
    assert out.claim.status is Status.VERIFIED
    assert len(backend.checks) == 1  # cheat never reached the checker
    assert "guardrail" in llm.calls[1]["messages"][-1]["content"]


def test_forbidden_pattern_from_prompt_context(tmp_path):
    _ctx, llm, v, (_, zero, _), backend = _setup(tmp_path, [TRUST, ACCEPT])
    out = v.verify_function(zero)
    assert out.claim.status is Status.VERIFIED and len(backend.checks) == 1
    assert "fake::trust_me" in llm.calls[1]["messages"][-1]["content"]


def test_bug_path(tmp_path):
    ctx, _llm, v, (_, zero, _), _ = _setup(tmp_path, [BUG])
    out = v.verify_function(zero)
    assert out.claim.status is Status.BUG_FOUND
    finds = ctx.ledger.findings()
    assert len(finds) == 1 and finds[0].witness == "n = -1" and finds[0].tool == "fake"


def test_llm_bug_report_is_finding_not_proof(tmp_path):
    ctx, _llm, v, (_, zero, _), backend = _setup(
        tmp_path, ["BUG: n can be huge and buf too small."]
    )
    out = v.verify_function(zero)
    assert out.claim.status is Status.UNRESOLVED
    assert out.claim.extra.get("suspected_bug") is True
    assert ctx.ledger.findings()[0].tool == "llm"
    assert backend.checks == []


def test_tool_error_aborts_without_burning_attempts(tmp_path):
    _ctx, llm, v, (_, zero, _), _ = _setup(tmp_path, [ACCEPT, ACCEPT, ACCEPT], tool_error=True)
    out = v.verify_function(zero)
    assert out.claim.status is Status.UNRESOLVED and "checker failed" in out.claim.message
    assert len(llm.calls) == 1


def test_budget_exhaustion(tmp_path):
    _ctx, llm, v, (_, zero, _), _ = _setup(tmp_path, [STUCK, GOALS, STUCK, STUCK, ACCEPT])
    out = v.verify_function(zero)  # max_attempts_per_function = 4
    assert out.claim.status is Status.UNRESOLVED and out.attempts == 4
    assert len(llm.calls) == 4
    assert out.claim.extra["best_submission"]["function.c"].strip().endswith("FVER_GOALS */ }")


def test_usd_budget_per_function(tmp_path):
    ctx, llm, v, (_, zero, _), _ = _setup(tmp_path, [STUCK, STUCK, ACCEPT])
    ctx.config.budget.max_usd_per_function = 0.015
    llm.usd_per_call = 0.01
    out = v.verify_function(zero)
    assert out.claim.status is Status.UNRESOLVED and len(llm.calls) == 2
    assert "budget" in out.claim.message


def test_cache_hit_skips_llm(tmp_path):
    ctx, _llm, v, (_, zero, _), backend = _setup(tmp_path, [ACCEPT])
    first = v.verify_function(zero)
    assert first.claim.status is Status.VERIFIED
    llm2 = FakeLLMClient(responses=[])
    v2 = Verifier(ctx, llm2, run_id="r2")
    second = v2.verify_function(zero)
    assert second.cache_hit and second.claim.status is Status.VERIFIED
    assert llm2.calls == [] and len(backend.checks) == 1
    assert second.claim.cache_key == first.claim.cache_key


def test_cache_key_changes_with_body(tmp_path):
    _ctx, _llm, v, (_, zero, _), _ = _setup(tmp_path, [ACCEPT])
    k1 = v.cache_key_for(v.build_task(zero))
    # Edit the function on disk: build_task re-extracts it, so the hash moves.
    src = _ctx.ws.repo_root / zero.source_path
    src.write_text(src.read_text().replace("= 0;", "= 0 + 0;", 1))
    k2 = v.cache_key_for(v.build_task(zero))
    assert k1 != k2


def test_audit_failure_feeds_back(tmp_path):
    _ctx, llm, v, (_, zero, _), _ = _setup(tmp_path, [ACCEPT] * 4, audit_fail=True)
    out = v.verify_function(zero)
    assert out.claim.status is Status.UNRESOLVED and out.attempts == 4
    assert "assumption audit" in llm.calls[1]["messages"][-1]["content"]


def test_callee_specs_flow_into_task(tmp_path):
    ctx, _llm, v, (_helper, zero, use), _ = _setup(tmp_path, [ACCEPT])
    v.verify_function(zero)
    task = v.build_task(use)
    assert task.callee_specs == {
        "zero": "void zero(char *buf, size_t n) { /* [[fake::args(buf, n)]] FVER_ACCEPT */ }"
    }
    assert "helper" not in task.callee_specs
    # verifying a callee changes the caller's cache key
    zero_key_before = v.cache_key_for(task)
    (ctx.ws.proofs_dir / "src/a.c" / "zero" / "function.c").write_text(
        "/* [[fake::args(changed)]] */\n"
    )
    assert v.cache_key_for(v.build_task(use)) != zero_key_before


def test_external_specs(tmp_path):
    ctx, _llm, v, (_helper, _zero, use), _ = _setup(tmp_path, [])
    ctx.ws.external_dir.mkdir(exist_ok=True)
    (ctx.ws.external_dir / "helper.spec").write_text("helper is pure")
    task = v.build_task(use)
    assert task.external_specs == {"helper": "helper is pure"}


def test_refusal_twice_gives_up(tmp_path):
    _ctx, llm, v, (_, zero, _), _ = _setup(tmp_path, [ACCEPT])
    llm.refuse_at = {1, 2}
    out = v.verify_function(zero)
    assert out.claim.status is Status.UNRESOLVED and "refused" in out.claim.message


def test_refusal_once_then_accept(tmp_path):
    _ctx, llm, v, (_, zero, _), _ = _setup(tmp_path, [ACCEPT])
    llm.refuse_at = {1}
    out = v.verify_function(zero)
    assert out.claim.status is Status.VERIFIED and len(llm.calls) == 2


def test_run_orders_by_attack_score_and_accumulates_cost(tmp_path):
    _ctx, _llm, v, (helper, zero, use), _ = _setup(tmp_path, [ACCEPT, ACCEPT, ACCEPT])
    done = []
    outs = v.run(
        [helper, use, zero],
        max_usd_run=10.0,
        parallelism=1,
        on_done=lambda f, o: done.append(f.name),
    )
    assert done == ["zero", "use", "helper"]
    assert all(o.claim.status is Status.VERIFIED for o in outs)
    assert v.run_cost.llm_calls == 3 and v.run_cost.usd > 0


def test_run_stops_at_budget(tmp_path):
    _ctx, llm, v, (helper, zero, use), _ = _setup(tmp_path, [ACCEPT, ACCEPT, ACCEPT])
    llm.usd_per_call = 1.0
    outs = v.run([helper, use, zero], max_usd_run=1.5, parallelism=1)
    assert len(outs) == 2


def test_run_parallel(tmp_path):
    _ctx, _llm, v, (helper, zero, use), _ = _setup(tmp_path, [ACCEPT, ACCEPT, ACCEPT])
    outs = v.run([helper, use, zero], max_usd_run=10.0, parallelism=3)
    assert len(outs) == 3


def test_run_propagates_fatal(tmp_path):
    _ctx, _llm, v, (_helper, zero, _use), _ = _setup(tmp_path, [])
    try:
        v.run([zero], max_usd_run=10.0)
    except FatalAgentError:
        pass
    else:
        raise AssertionError("expected FatalAgentError")


def test_recheck_pass_and_fail(tmp_path):
    ctx, _llm, v, (_, zero, _), _backend = _setup(tmp_path, [ACCEPT])
    assert v.verify_function(zero).claim.status is Status.VERIFIED
    assert v.recheck_function(zero).claim.status is Status.VERIFIED
    (ctx.ws.proofs_dir / "src/a.c" / "zero" / "function.c").write_text("/* nothing */\n")
    out = v.recheck_function(zero)
    assert out.claim.status is Status.UNRESOLVED
    assert not store.cache_path(ctx.ws, out.claim.cache_key).exists()
