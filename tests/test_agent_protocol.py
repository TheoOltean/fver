"""The agent protocol (task / check / next / changed / show / status) over the
null backend, both as functions and as CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fver.cli import app
from fver.core.context import AppContext
from fver.prove import protocol

ACCEPT_FN = "/* FVER_ACCEPT */\nint add(int a, int b) { return a + b; }\n"
PLAIN_FN = "int add(int a, int b) { return a + b; }\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "m.c").write_text(
        "int add(int a, int b) { return a + b; }\nint twice(int x) { return add(x, x); }\n"
    )
    monkeypatch.chdir(root)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--backend", "null"]).exit_code == 0
    r = runner.invoke(app, ["prove", "--dry-run"])
    assert r.exit_code == 0, r.output
    return root


def _ctx(root: Path) -> AppContext:
    return AppContext.load(root, need_backend=True)


def test_next_orders_callees_first_and_counts_unverified(repo: Path) -> None:
    ctx = _ctx(repo)
    try:
        d = protocol.next_functions(ctx, 10)
    finally:
        ctx.close()
    names = [f["name"] for f in d["functions"]]
    assert names == ["add", "twice"]
    by = {f["name"]: f for f in d["functions"]}
    assert by["twice"]["unverified_internal_callees"] == 1
    assert by["add"]["status"] == "not_attempted"


def test_task_packet_shape(repo: Path) -> None:
    ctx = _ctx(repo)
    try:
        d = protocol.task(ctx, "add")
        text = protocol.render_task_text(ctx, d)
        ref = protocol.reference(ctx)
    finally:
        ctx.close()
    assert d["function"]["name"] == "add"
    assert "int add(int a, int b)" in d["function_text"]
    assert d["attempts_so_far"] == 0
    assert d["previous_accepted"] is None
    assert "function.c" in d["submission_files"]
    assert d["cache_key"]
    assert "# Task: prove `add`" in d["prompt"]
    assert ref["backend"] == "null" and "Universal rules" in ref["instructions"]
    assert text.startswith("# Reference")
    with pytest.raises(protocol.ProtocolError):
        protocol.task(ctx, "nope")


def test_check_records_attempt_then_verified(repo: Path) -> None:
    ctx = _ctx(repo)
    try:
        d1 = protocol.check(ctx, "add", {"function.c": PLAIN_FN})
        assert d1["kind"] == "feedback" and d1["verified"] is False and d1["exit_code"] == 1
        assert d1["status"] == "in_progress"
        assert (repo / ".fver/proofs/src/m.c/add/attempts/1/function.c").exists()

        d2 = protocol.check(ctx, "add", {"function.c": ACCEPT_FN})
        assert d2["kind"] == "verified" and d2["exit_code"] == 0 and d2["status"] == "verified"
        assert (repo / ".fver/proofs/src/m.c/add/function.c").exists()

        s = protocol.show(ctx, "add")
        assert s["function"]["status"] == "verified"
        assert s["current_claim"]["extra"]["prover"] == "external"
        assert s["current_claim"]["cost"]["usd"] == 0
        assert s["accepted_submission"]["function.c"] == ACCEPT_FN

        # fenced-block reply on "stdin"
        reply = "note\n```c file=function.c\n/* FVER_ACCEPT */\nint twice(int x) { return add(x, x); }\n```\n"
        d3 = protocol.check(ctx, "twice", {"-": reply})
        assert d3["kind"] == "verified"
        st = protocol.status(ctx)
        assert st["by_status"]["verified"] == 2
    finally:
        ctx.close()


def test_check_guardrail_bug_report_and_parse_error(repo: Path) -> None:
    ctx = _ctx(repo)
    try:
        g = protocol.check(
            ctx,
            "add",
            {
                "function.c": "/* FVER_CHEAT FVER_ACCEPT */\nint add(int a, int b) { return a + b; }\n"
            },
        )
        assert g["kind"] == "guardrail" and g["violations"]
        b = protocol.check(ctx, "add", {"-": "BUG: a + b overflows for INT_MAX inputs"})
        assert b["kind"] == "bug_report"
        assert any(f.kind == "suspected_ub" for f in ctx.ledger.findings())
        p = protocol.check(ctx, "add", {"wrong.c": PLAIN_FN})
        assert p["kind"] == "parse_error"
    finally:
        ctx.close()


def test_changed_reports_stale_after_edit(repo: Path) -> None:
    ctx = _ctx(repo)
    try:
        assert protocol.check(ctx, "add", {"function.c": ACCEPT_FN})["kind"] == "verified"
    finally:
        ctx.close()
    (repo / "src" / "m.c").write_text(
        "int add(int a, int b) { return a - b; }\nint twice(int x) { return add(x, x); }\n"
    )
    ctx = AppContext.load(repo, need_backend=False)
    try:
        d = protocol.changed(ctx)
    finally:
        ctx.close()
    assert "src/m.c" in d["modified_files"]
    assert [f["name"] for f in d["stale"]] == ["add"]
    assert d["count"] >= 1


def test_cli_commands_and_exit_codes(repo: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["agent", "next", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["count"] == 2
    r = runner.invoke(app, ["agent", "task", "add", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["function"]["name"] == "add"
    sub = repo / ".fver" / "sub.c"
    sub.write_text(PLAIN_FN)
    r = runner.invoke(app, ["agent", "check", "add", "--submission", str(sub)])
    assert r.exit_code == 1
    sub.write_text(ACCEPT_FN)
    r = runner.invoke(app, ["agent", "check", "add", "--submission", str(sub), "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["verified"] is True
    r = runner.invoke(
        app,
        ["agent", "check", "twice", "--submission", "-"],
        input="```c file=function.c\n/* FVER_ACCEPT */\nint twice(int x) { return add(x, x); }\n```\n",
    )
    assert r.exit_code == 0
    r = runner.invoke(app, ["status", "-f", "add", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["function"]["status"] == "verified"
    r = runner.invoke(app, ["agent", "changed", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["count"] == 0
    r = runner.invoke(app, ["agent", "check", "missing", "--submission", str(sub)])
    assert r.exit_code == 1


def test_budget_limits_a_run_and_flags_override(repo: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["agent", "next", "--json", "--limit", "1"])
    assert r.exit_code == 0 and json.loads(r.stdout)["count"] == 1
    assert runner.invoke(app, ["config", "set", "budget.max_functions_per_run", "1"]).exit_code == 0
    r = runner.invoke(app, ["prove", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "add" in r.output and "twice" not in r.output
    r = runner.invoke(app, ["prove", "--dry-run", "--limit", "2"])
    assert r.exit_code == 0 and "twice" in r.output
    r = runner.invoke(app, ["agent", "task", "add", "--no-reference"])
    assert r.exit_code == 0 and "# Task: prove `add`" in r.output and "rc::" not in r.output
