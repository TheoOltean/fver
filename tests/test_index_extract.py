"""Tests for fver.index: functions, callgraph, attack surface."""

from __future__ import annotations

from pathlib import Path

from fver.core.models import TranslationUnit
from fver.index.attack_surface import score
from fver.index.callgraph import build_callgraph
from fver.index.functions import extract_from_source, extract_from_tu, get_function_text

FIXTURE = Path(__file__).parent / "fixtures" / "miniproj"


def _fixture_functions():
    util_tu = TranslationUnit("tu_util", "src/util.c", str(FIXTURE), ["clang", "-c", "src/util.c"])
    parser_tu = TranslationUnit(
        "tu_parser", "src/net/parser.c", str(FIXTURE), ["clang", "-c", "src/net/parser.c"]
    )
    return extract_from_tu(FIXTURE, util_tu) + extract_from_tu(FIXTURE, parser_tu)


def test_extract_names_lines_static_callees() -> None:
    fns = {f.id: f for f in _fixture_functions()}
    assert set(fns) == {
        "tu_util:helper",
        "tu_util:add",
        "tu_util:get_value",
        "tu_parser:helper",
        "tu_parser:parse_header",
    }
    helper = fns["tu_util:helper"]
    assert helper.is_static and helper.start_line == 4 and helper.end_line == 6
    add = fns["tu_util:add"]
    assert not add.is_static and add.callees == ["helper"]
    assert add.signature == "int add(int a, int b)"
    gv = fns["tu_util:get_value"]
    assert gv.start_line == gv.end_line == 13
    ph = fns["tu_parser:parse_header"]
    assert ph.callees == ["memcpy", "helper", "add"]
    assert ph.signature.startswith("int parse_header(const unsigned char *buf")


def test_skips_prototypes_and_handles_function_pointer_returns() -> None:
    src = "int g(int x);\nint (*h(void))(int) { return 0; }\nstatic inline int k(void) { return 1; }\n"
    fns = extract_from_source(src, "x.c", "t")
    assert [f.name for f in fns] == ["h", "k"]
    assert fns[1].is_static


def test_body_hash_stable_under_whitespace_and_comments() -> None:
    a = "int f(int x) {\n  return x + 1; // note\n}\n"
    b = "int f(int x) { return x + 1; }\n"
    c = "int f(int x) { return x + 2; }\n"
    ha = extract_from_source(a, "x.c", "t")[0].body_hash
    hb = extract_from_source(b, "x.c", "t")[0].body_hash
    hc = extract_from_source(c, "x.c", "t")[0].body_hash
    assert ha == hb
    assert ha != hc


def test_get_function_text() -> None:
    src = "int a;\nint f(int x)\n{\n  return x;\n}\nint b;\n"
    fn = extract_from_source(src, "x.c", "t")[0]
    assert get_function_text(src, fn) == "int f(int x)\n{\n  return x;\n}\n"


def test_callgraph_static_shadowing_and_externals() -> None:
    fns = _fixture_functions()
    cg = build_callgraph(fns)
    assert cg.edges["tu_util:add"] == ["tu_util:helper"]
    assert cg.edges["tu_parser:parse_header"] == ["tu_parser:helper", "tu_util:add"]
    assert cg.externals["tu_parser:parse_header"] == ["memcpy"]
    assert cg.callers_of("tu_util:add") == ["tu_parser:parse_header"]
    assert cg.callers_of("tu_util:helper") == ["tu_util:add"]
    assert cg.all_externals() == {"memcpy"}


def test_static_only_elsewhere_is_external() -> None:
    a = extract_from_source("static int s(void){return 0;}", "a.c", "ta")
    b = extract_from_source("int t(void){return s();}", "b.c", "tb")
    cg = build_callgraph(a + b)
    assert cg.edges["tb:t"] == [] and cg.externals["tb:t"] == ["s"]


def test_attack_score_parser_beats_getter() -> None:
    fns = _fixture_functions()
    cg = build_callgraph(fns)
    by_id = {f.id: f for f in fns}
    parser_src = (FIXTURE / "src/net/parser.c").read_text()
    util_src = (FIXTURE / "src/util.c").read_text()
    ps, preasons = score(
        by_id["tu_parser:parse_header"], parser_src, cg, cg.externals["tu_parser:parse_header"]
    )
    gs, greasons = score(by_id["tu_util:get_value"], util_src, cg, [])
    hs, _ = score(by_id["tu_util:helper"], util_src, cg, [])
    assert 0.0 <= gs < ps <= 1.0
    assert hs < gs  # static helper is less exposed than a public getter
    assert any("memcpy" in r for r in preasons)
    assert any("pointer + length" in r for r in preasons)
    assert any("loop" in r for r in preasons)
    assert all(r.startswith("+") for r in preasons + greasons)
