"""`fver prove`: target resolution, CBMC before the prover, and the run itself
(null backend, scripted LLM, fake hunter)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fver.cli import app
from fver.commands import prove
from fver.core.context import AppContext
from fver.core.models import Finding
from fver.hunters import base as hunters_base

ACCEPT = "```c file=function.c\n/* FVER_ACCEPT */\n{body}\n```"
SRC = "int add(int a, int b) { return a + b; }\nint twice(int x) { return add(x, x); }\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "m.c").write_text(SRC)
    monkeypatch.chdir(root)
    monkeypatch.setenv("FVER_HOME", str(tmp_path / "home"))
    r = CliRunner().invoke(app, ["init", "--backend", "null"])
    assert r.exit_code == 0, r.output
    return root


def _scripted(root: Path, monkeypatch, *bodies: str) -> None:
    fake = root / ".fver" / "fake.json"
    fake.write_text(json.dumps([ACCEPT.format(body=b) for b in bodies]))
    monkeypatch.setenv("FVER_FAKE_LLM", str(fake))


class QuietHunter:
    """Stands in for CBMC: reports `twice` as a real out-of-bounds bug."""

    name = "cbmc"
    calls: list[list[str]] = []

    def doctor(self):
        return []

    def run(self, tus, functions, repo_root, workdir, config):
        QuietHunter.calls.append([f.name for f in functions])
        return [
            Finding(
                f.id, f.source_path, 2, "out_of_bounds", "cbmc", "x too large", confidence="high"
            )
            for f in functions
            if f.name == "twice"
        ]


@pytest.fixture
def fake_cbmc(monkeypatch):
    QuietHunter.calls.clear()
    monkeypatch.setattr(hunters_base, "builtin_hunters", lambda: {"cbmc": QuietHunter})
    return QuietHunter


def test_split_and_select_targets(repo: Path) -> None:
    CliRunner().invoke(app, ["scan", "--no-translate", "--no-preprocess"])
    ctx = AppContext.load(repo, need_backend=True)
    try:
        assert prove.split_targets(ctx, []) == ([], [])
        assert prove.split_targets(ctx, ["add", "src/m.c", "lz*", "other.c"]) == (
            ["add", "lz*"],
            ["src/m.c", "other.c"],
        )
        assert prove.split_targets(ctx, [str(repo / "src" / "m.c")]) == ([], ["src/m.c"])
        everything = [f.name for f in prove.select_functions(ctx, [], None, False)]
        assert everything == ["add", "twice"]  # callee before caller
        assert [f.name for f in prove.select_functions(ctx, ["twice"], None, False)] == [
            "add",
            "twice",
        ]
        assert [f.name for f in prove.select_functions(ctx, ["src/m.c"], 1, False)] == ["add"]
        assert prove.select_functions(ctx, ["nothing.c"], None, False) == []
    finally:
        ctx.close()


def test_prove_indexes_hunts_then_proves(repo: Path, monkeypatch, fake_cbmc) -> None:
    _scripted(repo, monkeypatch, "int add(int a, int b) { return a + b; }")
    r = CliRunner().invoke(app, ["prove"])  # no scan beforehand: prove indexes first
    assert r.exit_code == 0, r.output
    assert "Indexing" in r.output
    assert fake_cbmc.calls == [["add", "twice"]]  # CBMC ran over the selection first
    assert "bug_found" in r.output and "twice" in r.output  # ... and twice was not sent on
    assert "verified" in r.output and "add" in r.output
    ctx = AppContext.load(repo, need_backend=False)
    try:
        rows = {
            r.function.name: r.status.value
            for r in ctx.ledger.list_functions("null", ctx.target.key)
        }
    finally:
        ctx.close()
    assert rows == {"add": "verified", "twice": "bug_found"}
    # Nothing left to do, and no second index pass because nothing changed.
    r = CliRunner().invoke(app, ["prove"])
    assert r.exit_code == 0 and "Nothing to prove" in r.output and "Indexing" not in r.output


def test_prove_targets_and_dry_run(repo: Path, monkeypatch, fake_cbmc) -> None:
    r = CliRunner().invoke(app, ["prove", "--dry-run", "add"])
    assert r.exit_code == 0, r.output
    assert "add" in r.output and fake_cbmc.calls == []  # dry run: no hunting, no proving
    _scripted(repo, monkeypatch, "int add(int a, int b) { return a + b; }")
    r = CliRunner().invoke(app, ["prove", "add"])
    assert r.exit_code == 0, r.output
    assert fake_cbmc.calls == [["add"]]
    r = CliRunner().invoke(app, ["prove", "src/m.c"])
    assert r.exit_code == 0 and fake_cbmc.calls[-1] == ["twice"]  # add is already verified
    r = CliRunner().invoke(app, ["prove", "does_not_exist"])
    assert r.exit_code == 0 and "Nothing to prove" in r.output


def test_prove_reindexes_after_an_edit(repo: Path, monkeypatch, fake_cbmc) -> None:
    _scripted(repo, monkeypatch, "int add(int a, int b) { return a + b; }")
    assert CliRunner().invoke(app, ["prove", "add"]).exit_code == 0
    (repo / "src" / "m.c").write_text(SRC.replace("a + b", "b + a"))
    _scripted(repo, monkeypatch, "int add(int a, int b) { return b + a; }")
    r = CliRunner().invoke(app, ["prove", "add"])
    assert r.exit_code == 0, r.output
    assert "Indexing" in r.output and "verified" in r.output
