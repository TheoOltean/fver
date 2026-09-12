"""Dependency-aware ordering for `fver verify`."""

from __future__ import annotations

from fver.core.config import FverConfig
from fver.core.context import AppContext
from fver.core.models import Claim, FunctionInfo, PropertyClass, Status, Target
from fver.core.workspace import Workspace
from fver.ledger.memory import InMemoryLedger
from fver.prove.select import order_with_dependencies


def _fn(name, callees=(), score=0.0, tu="t1", static=False):
    return FunctionInfo(
        id=f"{tu}:{name}",
        name=name,
        tu_id=tu,
        source_path="a.c",
        start_line=1,
        end_line=2,
        signature=name,
        body_hash="h" + name,
        is_static=static,
        callees=list(callees),
        attack_score=score,
    )


def _ctx(tmp_path, fns):
    cfg = FverConfig()
    ws = Workspace.create(tmp_path / "repo", cfg)
    ledger = InMemoryLedger()
    ledger.upsert_functions(fns)
    return AppContext(ws=ws, config=cfg, ledger=ledger, target=Target(), backend=None)


def test_callees_come_before_callers_and_priority_is_kept(tmp_path):
    parse = _fn("parse", ["read_hdr", "checksum"], score=0.9)
    read_hdr = _fn("read_hdr", ["checksum"], score=0.3)
    checksum = _fn("checksum", score=0.1)
    other = _fn("other", score=0.5)
    ctx = _ctx(tmp_path, [parse, read_hdr, checksum, other])
    order = order_with_dependencies(ctx, [parse, other])
    assert [f.name for f in order] == ["checksum", "read_hdr", "parse", "other"]


def test_verified_callees_and_cycles_are_skipped(tmp_path):
    a = _fn("a", ["b", "c"], score=0.9)
    b = _fn("b", ["a"], score=0.2)  # cycle
    c = _fn("c", score=0.2)
    ctx = _ctx(tmp_path, [a, b, c])
    ctx.ledger.record_claim(
        Claim(
            function_id=c.id,
            property_class=PropertyClass.UB_FREE,
            backend="refinedc",
            target_key=ctx.target.key,
            status=Status.VERIFIED,
            body_hash="hc",
            cache_key="k",
        )
    )
    order = order_with_dependencies(ctx, [a])
    assert [f.name for f in order] == ["b", "a"]


def test_unsupported_callees_are_not_pulled_in(tmp_path):
    a = _fn("a", ["u"], score=0.9)
    u = _fn("u", score=0.2)
    ctx = _ctx(tmp_path, [a, u])
    ctx.ledger.mark_unsupported(u.id, "refinedc", ctx.target.key, "union", "r1")
    assert [f.name for f in order_with_dependencies(ctx, [a])] == ["a"]
