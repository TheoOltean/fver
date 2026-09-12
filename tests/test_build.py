"""Tests for fver.build: compile_commands, preprocess, targets."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from fver.build import targets
from fver.build.compile_commands import (
    capture_build,
    is_included,
    parse_compile_commands,
    synthesise,
)
from fver.build.preprocess import preprocess_argv, preprocess_tu
from fver.core.config import BuildConfig, FverConfig
from fver.core.models import Target, TranslationUnit
from fver.core.workspace import Workspace
from fver.util.proc import ProcResult

FIXTURE = Path(__file__).parent / "fixtures" / "miniproj"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Copy the fixture project and rewrite compile_commands directories to absolute."""
    dst = tmp_path / "miniproj"
    shutil.copytree(FIXTURE, dst)
    cc = dst / "compile_commands.json"
    entries = json.loads(cc.read_text())
    for e in entries:
        e["directory"] = str(dst)
    cc.write_text(json.dumps(entries))
    return dst


def test_include_exclude_globs() -> None:
    b = BuildConfig()
    assert is_included("src/a.c", b)
    assert is_included("a.c", b)
    assert not is_included("tests/x.c", b)
    assert not is_included("deep/tests/x.c", b)
    assert not is_included("third_party/lib/x.c", b)
    b2 = BuildConfig(include=["src/net/*.c"])
    assert is_included("src/net/p.c", b2)
    assert not is_included("src/util.c", b2)


def test_parse_both_forms_filters_and_dedupes(repo: Path) -> None:
    tus, warnings = parse_compile_commands(repo / "compile_commands.json", repo, BuildConfig())
    paths = [t.source_path for t in tus]
    assert paths == ["src/util.c", "src/net/parser.c"]  # tests/ excluded, duplicate dropped
    util = tus[0]
    assert util.arguments[0] == "clang" and "-Isrc" in util.arguments
    parser = tus[1]
    assert parser.arguments == [
        "clang",
        "-std=c11",
        "-Isrc",
        "-c",
        "src/net/parser.c",
        "-o",
        "parser.o",
    ]
    assert util.id == TranslationUnit.make_id("src/util.c", util.arguments)
    assert not warnings


def test_capture_falls_back_to_synthesis(repo: Path) -> None:
    (repo / "compile_commands.json").unlink()
    cap = capture_build(repo, BuildConfig(fallback_flags=["-std=c99"]), compiler="cc")
    assert cap.source == "synthesised"
    assert any("no build system found" in w for w in cap.warnings)
    assert sorted(t.source_path for t in cap.tus) == ["src/net/parser.c", "src/util.c"]
    assert cap.tus[0].arguments[:2] == ["cc", "-std=c99"]


def test_capture_uses_configured_path(repo: Path) -> None:
    moved = repo / "out" / "cc.json"
    moved.parent.mkdir()
    (repo / "compile_commands.json").rename(moved)
    cap = capture_build(repo, BuildConfig(compile_commands="out/cc.json"))
    assert cap.source == "config"
    assert len(cap.tus) == 2


def test_capture_command_runs(repo: Path) -> None:
    (repo / "compile_commands.json").rename(repo / "saved.json")
    cap = capture_build(repo, BuildConfig(capture_command="cp saved.json compile_commands.json"))
    assert cap.source.startswith("captured:")
    assert len(cap.tus) == 2


def test_synthesise_skips_hidden_dirs(repo: Path) -> None:
    (repo / ".hidden").mkdir()
    (repo / ".hidden" / "x.c").write_text("int x;")
    tus = synthesise(repo, BuildConfig(), "clang")
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
    tus, _ = parse_compile_commands(repo / "compile_commands.json", repo, BuildConfig())
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
    """The scan pipeline with an in-memory ledger and no backend: build capture,
    extraction, callgraph, scoring, ledger upserts and the index file."""
    from types import SimpleNamespace

    from fver.commands.scan import run_scan
    from fver.ledger.memory import InMemoryLedger

    monkeypatch.chdir(repo)
    cfg = FverConfig()
    ws = Workspace.create(repo, cfg)
    ledger = InMemoryLedger()
    ctx = SimpleNamespace(
        ws=ws,
        config=cfg,
        ledger=ledger,
        target=cfg.target.to_target(),
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
    assert all(
        p.is_relative_to(FIXTURE) or p.name in {"compile_commands.json"} or p.suffix in {".c", ".h"}
        for p in written
    )


def test_capture_redirects_bear_output_into_work_dir(tmp_path):
    from fver.build.compile_commands import _bear_with_output

    out = tmp_path / "cc.json"
    assert _bear_with_output("bear -- make", out) == f"bear --output {out} -- make"
    assert _bear_with_output("bear --output x.json -- make", out) == "bear --output x.json -- make"
    assert _bear_with_output("cmake -B build", out) == "cmake -B build"


def test_capture_empty_result_falls_back_to_synthesis(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void){return 0;}\n")
    work = repo / ".fver" / "work"
    cap = capture_build(
        repo,
        BuildConfig(capture_command="echo '[]' > .fver/work/compile_commands.new.json"),
        compiler="cc",
        work_dir=work,
    )
    assert cap.source == "synthesised"
    assert any("empty" in w for w in cap.warnings)
    assert not (repo / "compile_commands.json").exists()


def test_capture_keeps_previous_good_capture_when_build_is_up_to_date(tmp_path):
    import json as _json

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void){return 0;}\n")
    work = repo / ".fver" / "work"
    work.mkdir(parents=True)
    good = [{"directory": str(repo), "arguments": ["cc", "-DX", "-c", "a.c"], "file": "a.c"}]
    (work / "compile_commands.json").write_text(_json.dumps(good))
    cap = capture_build(
        repo,
        BuildConfig(capture_command="echo '[]' > .fver/work/compile_commands.new.json"),
        compiler="cc",
        work_dir=work,
    )
    assert cap.source.startswith("captured(previous)")
    assert [t.arguments for t in cap.tus] == [["cc", "-DX", "-c", "a.c"]]
    assert not (work / "compile_commands.new.json").exists()
