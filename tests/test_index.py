"""Tests for fver.index: sources, preprocess, targets."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fver.core.config import FverConfig, SourcesConfig
from fver.core.models import Target, TranslationUnit
from fver.core.workspace import Workspace
from fver.index import targets
from fver.index.preprocess import preprocess_argv, preprocess_tu
from fver.index.sources import header_dirs, is_included, list_sources
from fver.util.proc import ProcResult

FIXTURE = Path(__file__).parent / "fixtures" / "miniproj"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    dst = tmp_path / "miniproj"
    shutil.copytree(FIXTURE, dst)
    (dst / "compile_commands.json").unlink(missing_ok=True)
    return dst


def test_include_exclude_globs() -> None:
    b = SourcesConfig()
    assert is_included("src/a.c", b)
    assert is_included("a.c", b)
    assert not is_included("tests/x.c", b)
    assert not is_included("deep/tests/x.c", b)
    assert not is_included("third_party/lib/x.c", b)
    b2 = SourcesConfig(include=["src/net/*.c"])
    assert is_included("src/net/p.c", b2)
    assert not is_included("src/util.c", b2)


def test_synthesise_skips_hidden_dirs(repo: Path) -> None:
    (repo / ".hidden").mkdir()
    (repo / ".hidden" / "x.c").write_text("int x;")
    tus = list_sources(repo, SourcesConfig(), "clang")
    assert all(".hidden" not in t.source_path for t in tus)


def test_preprocess_argv_strips_output_flags() -> None:
    tu = TranslationUnit(
        "id", "a.c", "/x", ["gcc", "-O2", "-c", "a.c", "-o", "a.o", "-MMD", "-MF", "a.d", "-DFOO=1"]
    )
    argv = preprocess_argv(tu)
    assert argv == ["gcc", "-O2", "a.c", "-DFOO=1", "-E", "-C", "-x", "c"]


@pytest.mark.skipif(shutil.which("clang") is None, reason="clang not on PATH")
def test_preprocess_with_real_compiler(repo: Path) -> None:
    ws = Workspace.create(repo, FverConfig())
    tus = list_sources(repo, SourcesConfig(), "clang")
    tu = next(t for t in tus if t.source_path == "src/net/parser.c")
    err = preprocess_tu(ws, tu)
    assert err is None, err
    assert tu.preprocessed_path is not None
    out = (ws.root / tu.preprocessed_path).read_text()
    assert "parse_header" in out and "memcpy" in out
    assert (ws.root / tu.preprocessed_path).is_relative_to(ws.root)


def test_preprocess_reports_missing_compiler(repo: Path) -> None:
    ws = Workspace.create(repo, FverConfig())
    tu = TranslationUnit(
        "id", "src/util.c", str(repo), ["definitely-not-a-compiler", "-c", "src/util.c"]
    )
    err = preprocess_tu(ws, tu)
    assert err is not None and "command not found" in err


CANNED_DM = """
#define __SIZEOF_INT__ 4
#define __SIZEOF_LONG__ 4
#define __SIZEOF_POINTER__ 4
#define __CHAR_UNSIGNED__ 1
#define __BYTE_ORDER__ __ORDER_BIG_ENDIAN__
#define __GNUC__ 4
"""


def test_detect_target_parses_macros(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, cwd=None, timeout=None, env=None, input_text=None):
        if "-print-target-triple" in argv:
            return ProcResult(argv, 0, "armv7-none-eabi\n", "", 0.0)
        return ProcResult(argv, 0, CANNED_DM, "", 0.0)

    monkeypatch.setattr(targets.proc, "run", fake_run)
    monkeypatch.setattr(targets.proc, "which", lambda name: "/usr/bin/" + name)
    t = targets.detect_target("arm-gcc")
    assert t == Target(
        triple="armv7-none-eabi",
        compiler="arm-gcc",
        int_bits=32,
        long_bits=32,
        pointer_bits=32,
        char_signed=False,
        little_endian=False,
    )
    warns = targets.compare_target(t, Target())
    assert any("long_bits" in w for w in warns)
    assert any("char_signed" in w for w in warns)
    assert any("triple" in w for w in warns)
    assert targets.compare_target(None, Target()) == [
        "compiler 'cc' not found; cannot confirm target"
    ]


def test_detect_target_missing_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(targets.proc, "which", lambda name: None)
    assert targets.detect_target("nope") is None


def test_run_scan_end_to_end_without_backend(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan pipeline with an in-memory ledger and no backend: source listing,
    extraction, callgraph, scoring, ledger upserts and the index file."""
    from types import SimpleNamespace

    from fver.index.scan import run_scan
    from fver.ledger.memory import InMemoryLedger

    monkeypatch.chdir(repo)
    cfg = FverConfig()
    ws = Workspace.create(repo, cfg)
    ledger = InMemoryLedger()
    ctx = SimpleNamespace(
        ws=ws,
        config=cfg,
        ledger=ledger,
        target=Target(),
        backend=None,
        backend_name="null",
    )
    index = run_scan(ctx, preprocess=False, translate=False)
    names = sorted(f.name for f in index["functions"])
    assert names == ["add", "get_value", "helper", "helper", "parse_header"]
    assert ledger.find_functions(name="parse_header")
    ph = ledger.find_functions(name="parse_header")[0]
    assert ph.attack_score > ledger.find_functions(name="get_value")[0].attack_score
    assert (ws.work_dir / "index.json").exists()
    assert "callgraph" in index and "externals" in index
    # nothing written outside .fver/
    written = {p for p in repo.rglob("*") if p.is_file() and not p.is_relative_to(ws.root)}
    assert all(p.is_relative_to(FIXTURE) or p.suffix in {".c", ".h"} for p in written)


def test_source_direct_include_path(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "src" / "net").mkdir(parents=True)
    (root / "include").mkdir()
    (root / ".git").mkdir()
    (root / "src" / "a.c").write_text("int a;")
    (root / "src" / "net" / "b.c").write_text("int b;")
    (root / "src" / "net" / "b.h").write_text("")
    (root / "include" / "api.h").write_text("")
    (root / ".git" / "x.h").write_text("")
    assert header_dirs(root) == ["include", "src/net"]
    tus = {t.source_path: t.arguments for t in list_sources(root, SourcesConfig(), "cc")}
    assert (
        tus["src/net/b.c"][-3:] == ["-Iinclude", "-c", "src/net/b.c"]
        or "-Isrc/net" in tus["src/net/b.c"]
    )
    # a file's own directory comes first, then the others, shallowest first
    b = tus["src/net/b.c"]
    assert b.index("-Isrc/net") < b.index("-Iinclude")
    a = tus["src/a.c"]
    assert a.index("-Isrc") < a.index("-Iinclude") < a.index("-Isrc/net")
