"""Behavioural tests run against both ledger implementations."""

from __future__ import annotations

import threading
import time

import pytest

from fver.core.models import (
    Claim,
    Cost,
    Finding,
    FunctionInfo,
    PropertyClass,
    Status,
    TranslationUnit,
)
from fver.ledger.memory import InMemoryLedger
from fver.ledger.sqlite import SqliteLedger

BACKEND = "null"
TARGET = "x86_64-linux-gnu|clang|int32|long64|ptr64|schar|le"


@pytest.fixture(params=["sqlite", "memory"])
def ledger(request, tmp_path):
    if request.param == "sqlite":
        led = SqliteLedger(tmp_path / "ledger.sqlite")
    else:
        led = InMemoryLedger()
    yield led
    led.close()


def fn(
    name: str,
    tu: str = "tu1",
    score: float = 0.0,
    callees: list[str] | None = None,
    body: str = "h",
) -> FunctionInfo:
    return FunctionInfo(
        id=FunctionInfo.make_id(tu, name),
        name=name,
        tu_id=tu,
        source_path=f"src/{tu}.c",
        start_line=1,
        end_line=10,
        signature=f"int {name}(void)",
        body_hash=body,
        callees=callees or [],
        attack_score=score,
        attack_reasons=["parser"] if score else [],
    )


def claim(
    f: FunctionInfo, status: Status, usd: float = 0.0, created_at: str | None = None, **kw
) -> Claim:
    c = Claim(
        function_id=f.id,
        property_class=PropertyClass.UB_FREE,
        backend=BACKEND,
        target_key=TARGET,
        status=status,
        body_hash=f.body_hash,
        cache_key="k",
        cost=Cost(usd=usd, llm_calls=1),
        **kw,
    )
    if created_at:
        c.created_at = created_at
    return c


def test_tu_upsert_idempotent(ledger):
    tu = TranslationUnit(id="tu1", source_path="src/tu1.c", directory="/r", arguments=["cc", "-c"])
    ledger.upsert_tus([tu])
    ledger.upsert_tus([tu])
    got = ledger.get_tu("tu1")
    assert got is not None and got.arguments == ["cc", "-c"]
    tu2 = TranslationUnit(
        id="tu1",
        source_path="src/tu1.c",
        directory="/r",
        arguments=["cc", "-O2"],
        preprocessed_path="w/p.i",
    )
    ledger.upsert_tus([tu2])
    got = ledger.get_tu("tu1")
    assert got.arguments == ["cc", "-O2"] and got.preprocessed_path == "w/p.i"
    assert ledger.get_tu("nope") is None


def test_function_upsert_updates_body_hash_and_callees(ledger):
    f = fn("a", callees=["b"])
    ledger.upsert_functions([f])
    ledger.upsert_functions([f])
    assert len(ledger.find_functions(name="a")) == 1
    f2 = fn("a", callees=["c"], body="h2")
    ledger.upsert_functions([f2])
    got = ledger.get_function(f.id)
    assert got.body_hash == "h2" and got.callees == ["c"]
    assert ledger.get_function("missing") is None


def test_find_functions_filters(ledger):
    ledger.upsert_functions([fn("a", tu="tu1"), fn("a", tu="tu2"), fn("b", tu="tu1")])
    assert len(ledger.find_functions(name="a")) == 2
    assert len(ledger.find_functions(name="a", source_path="src/tu2.c")) == 1
    assert len(ledger.find_functions(source_path="src/tu1.c")) == 2
    assert len(ledger.find_functions()) == 3


def test_status_progression(ledger):
    f = fn("a")
    ledger.upsert_functions([f])
    assert ledger.current_claim(f.id, BACKEND, TARGET) is None
    assert ledger.list_functions(BACKEND, TARGET)[0].status is Status.NOT_ATTEMPTED

    ledger.record_claim(claim(f, Status.IN_PROGRESS, created_at="2026-01-01T00:00:00+00:00"))
    assert ledger.current_claim(f.id, BACKEND, TARGET).status is Status.IN_PROGRESS

    ledger.record_claim(
        claim(
            f,
            Status.VERIFIED,
            usd=1.5,
            created_at="2026-01-01T00:00:01+00:00",
            proof_hash="p",
            assumptions=["libc:memcpy"],
        )
    )
    cur = ledger.current_claim(f.id, BACKEND, TARGET)
    assert (
        cur.status is Status.VERIFIED
        and cur.proof_hash == "p"
        and cur.assumptions == ["libc:memcpy"]
    )
    assert cur.cost.usd == 1.5
    assert [c.status for c in ledger.claims_for(f.id)] == [Status.IN_PROGRESS, Status.VERIFIED]


def test_current_claim_picks_newest_and_is_per_backend_target(ledger):
    f = fn("a")
    ledger.upsert_functions([f])
    ledger.record_claim(claim(f, Status.VERIFIED, created_at="2026-01-02T00:00:00+00:00"))
    ledger.record_claim(claim(f, Status.UNRESOLVED, created_at="2026-01-01T00:00:00+00:00"))
    assert ledger.current_claim(f.id, BACKEND, TARGET).status is Status.VERIFIED
    other = claim(f, Status.BUG_FOUND)
    other.backend = "refinedc"
    ledger.record_claim(other)
    assert ledger.current_claim(f.id, BACKEND, TARGET).status is Status.VERIFIED
    assert ledger.current_claim(f.id, "refinedc", TARGET).status is Status.BUG_FOUND
    assert ledger.current_claim(f.id, BACKEND, "other-target") is None


def test_same_timestamp_newest_insert_wins(ledger):
    f = fn("a")
    ledger.upsert_functions([f])
    ts = "2026-01-01T00:00:00+00:00"
    ledger.record_claim(claim(f, Status.UNRESOLVED, created_at=ts))
    ledger.record_claim(claim(f, Status.VERIFIED, created_at=ts))
    assert ledger.current_claim(f.id, BACKEND, TARGET).status is Status.VERIFIED


def test_list_functions_ordering_filter_limit(ledger):
    a, b, c = fn("a", score=0.1), fn("b", score=0.9), fn("c", score=0.5)
    ledger.upsert_functions([a, b, c])
    ledger.record_claim(claim(b, Status.VERIFIED))
    rows = ledger.list_functions(BACKEND, TARGET)
    assert [r.function.name for r in rows] == ["b", "c", "a"]
    assert rows[0].status is Status.VERIFIED and rows[0].claim is not None
    assert rows[1].status is Status.NOT_ATTEMPTED and rows[1].claim is None
    assert [
        r.function.name for r in ledger.list_functions(BACKEND, TARGET, status=Status.NOT_ATTEMPTED)
    ] == ["c", "a"]
    assert [r.function.name for r in ledger.list_functions(BACKEND, TARGET, limit=2)] == ["b", "c"]
    assert [
        r.function.name for r in ledger.list_functions(BACKEND, TARGET, order_by_attack_score=False)
    ] == ["a", "b", "c"]
    assert [
        r.function.name
        for r in ledger.list_functions(BACKEND, TARGET, status=Status.NOT_ATTEMPTED, limit=1)
    ] == ["c"]


def test_mark_unsupported(ledger):
    f = fn("a")
    ledger.upsert_functions([f])
    ledger.mark_unsupported(f.id, BACKEND, TARGET, "inline asm", run_id="r1")
    cur = ledger.current_claim(f.id, BACKEND, TARGET)
    assert cur.status is Status.UNSUPPORTED and cur.message == "inline asm" and cur.run_id == "r1"
    with pytest.raises(KeyError):
        ledger.mark_unsupported("nope", BACKEND, TARGET, "x", run_id="r1")


def test_dependents(ledger):
    ledger.upsert_functions([fn("a", callees=["b", "c"]), fn("d", callees=["b"]), fn("b")])
    assert sorted(f.name for f in ledger.dependents("b")) == ["a", "d"]
    assert [f.name for f in ledger.dependents("c")] == ["a"]
    assert ledger.dependents("zzz") == []
    # re-upsert with changed callees replaces the edges
    ledger.upsert_functions([fn("a", callees=["c"])])
    assert [f.name for f in ledger.dependents("b")] == ["d"]


def test_findings(ledger):
    f = fn("a")
    ledger.upsert_functions([f])
    ledger.record_finding(
        Finding(
            function_id=f.id,
            source_path=f.source_path,
            line=4,
            kind="out_of_bounds",
            tool="cbmc",
            message="idx",
            witness="i=5",
        )
    )
    ledger.record_finding(
        Finding(
            function_id=None,
            source_path="src/other.c",
            line=None,
            kind="signed_overflow",
            tool="cerberus",
            message="ov",
        )
    )
    assert len(ledger.findings()) == 2
    assert len(ledger.findings(function_id=f.id)) == 1
    assert ledger.findings(source_path="src/other.c")[0].kind == "signed_overflow"
    assert ledger.findings(function_id=f.id)[0].witness == "i=5"


def test_summary_math(ledger):
    a, b, c = fn("a", score=1.0), fn("b", score=0.0), fn("c", score=0.0)
    ledger.upsert_functions([a, b, c])
    ledger.record_claim(claim(a, Status.VERIFIED, usd=2.0))
    ledger.record_claim(
        claim(b, Status.UNRESOLVED, usd=1.0, created_at="2026-01-01T00:00:00+00:00")
    )
    ledger.record_claim(
        claim(b, Status.UNRESOLVED, usd=3.0, created_at="2026-01-01T00:00:01+00:00")
    )
    ledger.record_finding(
        Finding(function_id=None, source_path="x.c", line=1, kind="k", tool="t", message="m")
    )
    s = ledger.summary(BACKEND, TARGET)
    assert s.total_functions == 3
    assert (
        s.by_status["verified"] == 1
        and s.by_status["unresolved"] == 1
        and s.by_status["not_attempted"] == 1
    )
    # weights: a=1.0, b=c=0.05 floor -> verified 1.0 / 1.1
    assert s.verified_weighted == pytest.approx(1.0 / 1.1)
    # cost sums the latest claim per function only
    assert s.total_cost.usd == pytest.approx(5.0)
    assert s.total_cost.llm_calls == 2
    assert s.findings == 1


def test_summary_empty(ledger):
    s = ledger.summary(BACKEND, TARGET)
    assert s.total_functions == 0 and s.verified_weighted == 0.0


def test_runs(ledger):
    rid = ledger.start_run("verify", BACKEND, TARGET, {"n": 1})
    assert isinstance(rid, str) and len(rid) >= 8
    ledger.end_run(rid, ok=True, message="done")
    rid2 = ledger.start_run("scan", BACKEND, TARGET, {})
    assert rid2 != rid


def test_thread_safety(ledger):
    fns = [fn(f"f{i}") for i in range(10)]
    ledger.upsert_functions(fns)
    errors: list[BaseException] = []

    def worker(k: int) -> None:
        try:
            for i in range(50):
                f = fns[(k * 7 + i) % len(fns)]
                ledger.record_claim(
                    claim(
                        f,
                        Status.VERIFIED if i % 2 else Status.UNRESOLVED,
                        created_at=f"2026-01-01T00:00:{i:02d}+00:00",
                    )
                )
                ledger.current_claim(f.id, BACKEND, TARGET)
                time.sleep(0.0005)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    total = sum(len(ledger.claims_for(f.id)) for f in fns)
    assert total == 200


def test_sqlite_persists_across_instances(tmp_path):
    p = tmp_path / "l.sqlite"
    led = SqliteLedger(p)
    f = fn("a")
    led.upsert_functions([f])
    led.record_claim(claim(f, Status.VERIFIED))
    led.close()
    led2 = SqliteLedger(p)
    assert led2.current_claim(f.id, BACKEND, TARGET).status is Status.VERIFIED
    led2.close()


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_delete_findings_replaces_previous_run(tmp_path, kind):
    from fver.core.models import Finding
    from fver.ledger.memory import InMemoryLedger
    from fver.ledger.sqlite import SqliteLedger

    ledger = SqliteLedger(tmp_path / "l.sqlite") if kind == "sqlite" else InMemoryLedger()
    ledger.record_finding(Finding(None, "a.c", 1, "out_of_bounds", "cbmc", "x"))
    ledger.record_finding(Finding(None, "a.c", 2, "out_of_bounds", "cerberus", "y"))
    ledger.record_finding(Finding(None, "b.c", 3, "out_of_bounds", "cbmc", "z"))
    assert ledger.delete_findings({"a.c"}, tool="cbmc") == 1
    assert {(f.source_path, f.tool) for f in ledger.findings()} == {
        ("a.c", "cerberus"),
        ("b.c", "cbmc"),
    }
    assert ledger.delete_findings({"a.c", "b.c"}) == 2
    assert ledger.findings() == []
    ledger.close()
