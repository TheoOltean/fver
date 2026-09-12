"""Stale-proof tracking: body edits, callee contract changes, and reconcile()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fver.backends.null import NullBackend
from fver.core.config import FverConfig
from fver.core.context import AppContext
from fver.core.models import FunctionInfo, Status, Target, TranslationUnit
from fver.core.workspace import Workspace
from fver.ledger.memory import InMemoryLedger
from fver.prove.client import FakeLLMClient
from fver.prove.loop import Verifier

ACCEPT = "```c file=function.c\n/* FVER_ACCEPT */\nint f(void) { return 0; }\n```\n"


def _ctx(tmp_path: Path) -> AppContext:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text(
        "int callee(int x) {\n  return x + 1;\n}\n\nint caller(int y) {\n  return callee(y);\n}\n"
    )
    cfg = FverConfig()
    cfg.project.backend = "null"
    ws = Workspace.create(repo, cfg)
    target = Target()
    backend = NullBackend(workspace_dir=ws.backend_dir("null"), settings={}, target=target)
    return AppContext(ws=ws, config=cfg, ledger=InMemoryLedger(), target=target, backend=backend)


def _index(ctx: AppContext) -> tuple[FunctionInfo, FunctionInfo]:
    tu = TranslationUnit(
        id="tu1", source_path="src/a.c", directory=".", arguments=["cc", "src/a.c"]
    )
    callee = FunctionInfo(
        id="tu1:callee",
        name="callee",
        tu_id="tu1",
        source_path="src/a.c",
        start_line=1,
        end_line=3,
        signature="int callee(int x)",
        body_hash="h-callee-1",
        attack_score=0.5,
    )
    caller = FunctionInfo(
        id="tu1:caller",
        name="caller",
        tu_id="tu1",
        source_path="src/a.c",
        start_line=5,
        end_line=7,
        signature="int caller(int y)",
        body_hash="h-caller-1",
        callees=["callee"],
        attack_score=0.9,
    )
    ctx.ledger.upsert_tus([tu])
    ctx.ledger.upsert_functions([callee, caller])
    return callee, caller


def _statuses(ctx: AppContext) -> dict[str, str]:
    rows = ctx.ledger.list_functions(ctx.backend_name, ctx.target.key)
    return {r.function.name: r.status.value for r in rows}


def test_body_change_makes_proof_stale_and_reverify_clears_it(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    callee, caller = _index(ctx)
    v = Verifier(ctx, FakeLLMClient(responses=[ACCEPT, ACCEPT]), "run1")
    v.verify_function(callee)
    v.verify_function(caller)
    assert _statuses(ctx) == {"callee": "verified", "caller": "verified"}

    # The user edits callee: scan re-indexes it with a new body hash.
    callee.body_hash = "h-callee-2"
    ctx.ledger.upsert_functions([callee])
    assert _statuses(ctx)["callee"] == "stale"
    assert _statuses(ctx)["caller"] == "verified"

    from fver.prove.invalidate import reconcile

    stale = reconcile(ctx, "run2")
    assert [s.function.name for s in stale] == ["callee"]
    cur = ctx.ledger.current_claim(callee.id, ctx.backend_name, ctx.target.key)
    assert cur is not None and cur.status is Status.STALE and "body changed" in cur.message

    v2 = Verifier(ctx, FakeLLMClient(responses=[ACCEPT]), "run3")
    v2.verify_function(callee)
    assert _statuses(ctx) == {"callee": "verified", "caller": "verified"}


def test_callee_contract_change_invalidates_caller(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    callee, caller = _index(ctx)
    v = Verifier(ctx, FakeLLMClient(responses=[ACCEPT, ACCEPT]), "run1")
    v.verify_function(callee)
    v.verify_function(caller)

    # Re-verify callee with a different contract (the null backend's
    # extract_spec returns the submission text, so any change is a contract change).
    new = "```c file=function.c\n/* FVER_ACCEPT v2 */\nint f(void) { return 0; }\n```\n"
    src = ctx.ws.repo_root / "src" / "a.c"
    src.write_text(src.read_text().replace("return x + 1;", "return x + 2;"))
    Verifier(ctx, FakeLLMClient(responses=[new]), "run2").verify_function(callee)
    st = _statuses(ctx)
    assert st["callee"] == "verified"
    assert st["caller"] == "stale"
    latest = ctx.ledger.current_claim(caller.id, ctx.backend_name, ctx.target.key)
    assert latest is not None and "callee" in latest.message


def test_reconcile_detects_cache_key_drift(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    callee, caller = _index(ctx)
    Verifier(ctx, FakeLLMClient(responses=[ACCEPT]), "run1").verify_function(caller)
    # Simulate a tool version change by editing the stored cache key's inputs:
    # a new external spec file for a callee changes the key.
    (ctx.ws.external_dir / "callee.spec").write_text("[[trusted]] int callee(int);")
    from fver.prove.invalidate import reconcile

    stale = reconcile(ctx, "run2")
    assert [s.function.name for s in stale] == ["caller"]
    assert _statuses(ctx)["caller"] == "stale"


@pytest.mark.parametrize("status", [Status.UNRESOLVED, Status.BUG_FOUND])
def test_non_verified_results_also_go_stale_on_edit(tmp_path: Path, status: Status) -> None:
    ctx = _ctx(tmp_path)
    callee, _ = _index(ctx)
    from fver.core.models import Claim, PropertyClass

    ctx.ledger.record_claim(
        Claim(
            function_id=callee.id,
            property_class=PropertyClass.UB_FREE,
            backend="null",
            target_key=ctx.target.key,
            status=status,
            body_hash="h-callee-1",
            cache_key="k",
        )
    )
    assert _statuses(ctx)["callee"] == status.value
    callee.body_hash = "h-callee-2"
    ctx.ledger.upsert_functions([callee])
    assert _statuses(ctx)["callee"] == "stale"
    json.dumps(_statuses(ctx))  # serialisable
