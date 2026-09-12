"""The terminal view: built from the ledger, updated by a worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Tree
from typer.testing import CliRunner

from fver import tui
from fver.cli import app
from fver.core.context import AppContext

SRC = "int add(int a, int b) { return a + b; }\nint twice(int x) { return add(x, x); }\n"
ACCEPT = "```c file=function.c\n/* FVER_ACCEPT */\n{body}\n```"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "m.c").write_text(SRC)
    (root / "other.c").write_text("int top(void) { return 1; }\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("FVER_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--backend", "null"]).exit_code == 0
    assert runner.invoke(app, ["scan", "--no-translate", "--no-preprocess"]).exit_code == 0
    fake = root / ".fver" / "fake.json"
    fake.write_text(json.dumps([ACCEPT.format(body="int add(int a, int b) { return a + b; }")]))
    monkeypatch.setenv("FVER_FAKE_LLM", str(fake))
    assert runner.invoke(app, ["verify", "--function", "add", "--parallel", "1"]).exit_code == 0
    return root


def _labels(node) -> list[str]:
    out = [str(node.label)]
    for c in node.children:
        out += _labels(c)
    return out


def test_snapshot_and_labels(repo: Path) -> None:
    snap = tui.read_snapshot(repo)
    assert snap.project == "repo" and {r.function.name for r in snap.rows} == {
        "add",
        "twice",
        "top",
    }
    verified = next(r for r in snap.rows if r.function.name == "add")
    assert str(tui.function_label(verified)).startswith("✓ add")
    assert "✓ 1" in str(tui.counts_label("src/", snap.rows)) and "· 2" in str(
        tui.counts_label("src/", snap.rows)
    )
    assert "%" in str(tui.coverage_bar(snap.summary))


def test_app_tree_detail_and_bar(repo: Path) -> None:
    asyncio.run(_check_tree_detail_and_bar(repo))


async def _check_tree_detail_and_bar(repo: Path) -> None:
    app_ = tui.FverApp(repo, select="add")
    async with app_.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tree = app_.query_one("#tree", Tree)
        labels = _labels(tree.root)
        assert any(lab.startswith("src/") for lab in labels) and any("m.c" in lab for lab in labels)
        assert any(lab.startswith("✓ add") for lab in labels) and any(
            "other.c" in lab for lab in labels
        )
        # `select="add"` opened on that function: the detail pane shows it.
        assert "add" in app_.detail_text and "verified" in app_.detail_text
        assert "src/m.c" in app_.detail_text and "attack score" in app_.detail_text
        assert "coverage" in app_.bar_text and "verified 1" in app_.bar_text
        assert "q quit" in app_.bar_text


def test_app_runs_worker_and_refreshes(repo: Path, monkeypatch) -> None:
    asyncio.run(_check_worker(repo, monkeypatch))


async def _check_worker(repo: Path, monkeypatch) -> None:
    fake = repo / ".fver" / "fake.json"
    fake.write_text(json.dumps([ACCEPT.format(body="int twice(int x) { return add(x, x); }")]))
    monkeypatch.setenv("FVER_FAKE_LLM", str(fake))
    from fver.commands.prove import run_prove
    from fver.hunters import base as hunters_base

    class NoHunter:
        name = "cbmc"

        def doctor(self):
            return []

        def run(self, *a, **k):
            return []

    monkeypatch.setattr(hunters_base, "builtin_hunters", lambda: {"cbmc": NoHunter})

    def worker(on_done) -> int:
        ctx = AppContext.load(repo, need_backend=True)
        try:
            return run_prove(ctx, ["twice"], on_done=on_done)
        finally:
            ctx.close()

    app_ = tui.FverApp(repo, worker=worker)
    async with app_.run_test(size=(120, 40)) as pilot:
        for _ in range(50):
            await pilot.pause(0.1)
            if not app_.running:
                break
        assert app_.exit_code == 0 and "twice: verified" in app_.last_event
        labels = _labels(app_.query_one("#tree", Tree).root)
        assert any(lab.startswith("✓ twice") for lab in labels)
