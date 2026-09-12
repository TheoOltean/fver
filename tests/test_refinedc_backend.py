"""Tests for the RefinedC backend. No external tools, no network.

Fixture outputs under tests/fixtures/refinedc/ were captured from a real
RefinedC run (see facts.py for the versions)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fver.backends.base import CheckOutcome, FunctionTask, Submission
from fver.backends.refinedc import annotations as ann
from fver.backends.refinedc import facts
from fver.backends.refinedc.backend import RefinedCBackend
from fver.backends.refinedc.parse_output import (
    classify,
    classify_full,
    extract_failures,
    extract_frontend_errors,
    extract_goals,
    strip_ansi,
)
from fver.backends.registry import make_backend
from fver.core.models import FunctionInfo, Target, TranslationUnit, sha256_text

FIX = Path(__file__).parent / "fixtures" / "refinedc"

ORIGINAL = """void zero(int *p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] = 0;
}
"""

ANNOTATED = """[[rc::parameters("p : loc", "n : nat")]]
[[rc::args("p @ &own<array<i32, {replicate n (uninit (it_layout i32))}>>", "n @ int<size_t>")]]
[[rc::returns("void")]]
[[rc::ensures("own p : array<i32, {replicate n (0 @ int i32)}>")]]
[[rc::tactics("all: try (apply zero_step; lia).")]]
void zero(int *p, size_t n) {
  [[rc::exists("i : nat")]]
  [[rc::inv_vars("i : i @ int<size_t>",
                 "p : p @ &own<array<i32, {replicate i (0 @ int i32) ++ replicate (n - i) (uninit (it_layout i32))}>>")]]
  [[rc::constraints("{i ≤ n}")]]
  for (size_t i = 0; i < n; i++) p[i] = 0;
}
"""

SOURCE = """#include <stddef.h>

static int helper(int x) {
  return x + 1;
}

void zero(int *p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] = 0;
}

int after(void) {
  return helper(1);
}
"""


def canned(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fn_info(name: str, start: int, end: int, path: str = "src/a.c") -> FunctionInfo:
    return FunctionInfo(
        id=f"tu1:{name}",
        name=name,
        tu_id="tu1",
        source_path=path,
        start_line=start,
        end_line=end,
        signature=f"{name}()",
        body_hash=sha256_text(name),
    )


# ------------------------------------------------------------- annotations


def test_strip_removes_attributes_include_and_ghosts():
    text = "#include <refinedc.h>\n" + ANNOTATED.replace("  for", "  rc_unfold_int(n);\n  for")
    out = ann.strip_annotations(text)
    assert "rc::" not in out and "refinedc.h" not in out and "rc_unfold_int" not in out
    assert ann.normalise(out) == ann.normalise(ORIGINAL)


def test_strip_handles_brackets_inside_strings():
    text = '[[rc::args("p @ &own<array<i32, {xs `at_type` int i32}>>")]]\nvoid f(int *p);'
    assert ann.strip_annotations(text).strip() == "void f(int *p);"


def test_find_attributes_parses_args():
    attrs = ann.find_attributes(ANNOTATED)
    names = [a.name for a in attrs]
    assert names[:4] == ["rc::parameters", "rc::args", "rc::returns", "rc::ensures"]
    assert attrs[0].args == ("p : loc", "n : nat")


def test_find_directives():
    text = "#include <refinedc.h>\n//@rc::import lemmas from refinedc.project.x.f\n//@rc::inlined Axiom a : False.\nint f(void);"
    assert ann.find_directives(text) == [
        ("import", "lemmas from refinedc.project.x.f"),
        ("inlined", "Axiom a : False."),
    ]


def test_code_unchanged_positive_cases():
    assert ann.code_unchanged(ORIGINAL, ANNOTATED)[0]
    reformatted = ANNOTATED.replace("p[i] = 0;", "p[i]=0 ; /* zero */")
    assert ann.code_unchanged(ORIGINAL, reformatted)[0]
    with_ghost = ANNOTATED.replace("  [[rc::exists", "  rc_unfold_int(n);\n  [[rc::exists")
    assert ann.code_unchanged(ORIGINAL, with_ghost)[0]


def test_code_unchanged_catches_semantic_edits():
    ok, why = ann.code_unchanged(ORIGINAL, ANNOTATED.replace("i < n", "i <= n"))
    assert not ok and "code changed" in why and "=" in why
    ok, why = ann.code_unchanged(ORIGINAL, ANNOTATED.replace("p[i] = 0;", "p[i] = 0; n = 0;"))
    assert not ok and "n = 0" in why


def test_coq_ident_sanitises_module_path_segments():
    assert ann.coq_ident("0c39be4cc5fb9ec6") == "x_0c39be4cc5fb9ec6"
    assert ann.coq_ident("tu_0c39be4cc5fb9ec6", "tu") == "tu_0c39be4cc5fb9ec6"
    assert ann.coq_ident("lua-x", "f") == "lua_x"
    assert ann.coq_ident("end") == "x_end"
    assert ann.coq_ident("_leading") == "leading"
    assert ann.coq_ident("___") == "x"
    assert ann.coq_ident("", "f") == "f"
    assert ann.coq_ident("tu1:parse_header", "f") == "tu1_parse_header"


def test_splice_replaces_target_and_callee_bottom_up_and_adds_include_once():
    helper = fn_info("helper", 3, 5)
    proto = '[[rc::args("x @ int<i32>")]]\nstatic int helper(int x);'
    out = ann.splice(SOURCE, fn_info("zero", 7, 9), ANNOTATED, {"helper": (helper, proto)})
    assert out.startswith(facts.HEADER_INCLUDE)
    assert "return x + 1;" not in out and proto in out
    assert "rc::inv_vars" in out and "int after(void)" in out
    assert out.count(facts.HEADER_INCLUDE) == 1
    assert (
        ann.splice(
            out,
            fn_info("after", out.count("\n") - 2, out.count("\n")),
            "int after(void) { return 0; }",
        ).count(facts.HEADER_INCLUDE)
        == 1
    )


def test_find_definition_range_skips_calls_and_prototypes():
    src = "int helper(int x);\nint use(void) { return helper(1); }\n\nint helper(int x)\n{\n  return x;\n}\n"
    assert ann.find_definition_range(src, "helper") == (4, 7)
    assert ann.find_definition_range(src, "missing") is None


def test_extract_contract_drops_proof_only_attrs_and_body():
    c = ann.extract_contract(ANNOTATED)
    assert "rc::tactics" not in c and "rc::inv_vars" not in c
    assert c.endswith("void zero(int *p, size_t n);") and "rc::ensures" in c


def test_vacuity_check():
    bad = ANNOTATED.replace(
        '[[rc::returns("void")]]', '[[rc::requires("{False}")]]\n[[rc::returns("void")]]'
    )
    assert ann.vacuity_check(bad)
    assert ann.vacuity_check(ANNOTATED.replace('"{i ≤ n}"', '"{n < 0}"'))
    assert ann.vacuity_check(ANNOTATED) == []


# ------------------------------------------------------------ parse_output


def test_classify_ok():
    out, _fb, goals, _w = classify(canned("ok.txt"), "", 0, False, "find")
    assert out is CheckOutcome.OK and not goals


def test_classify_internal_error_from_bad_attribute():
    out, fb, _g, _ = classify(canned("internal_error_bad_attribute.txt"), "", 125, False, "id")
    assert out is CheckOutcome.FRONTEND_ERROR
    assert "annotation parser" in fb and "malformed" in fb


def test_classify_frontend_not_implemented_and_unsupported():
    out, fb, _g, _ = classify(canned("frontend_not_implemented_varargs.txt"), "", 1, False, "sum")
    assert out is CheckOutcome.FRONTEND_ERROR
    assert "fe2.c:11" in fb and "va_end" in fb and "UNSUPPORTED" in fb
    out, fb, _g, _ = classify(canned("frontend_float_unsupported.txt"), "", 1, False, "luaZ_read")
    assert out is CheckOutcome.FRONTEND_ERROR and "lobject.h:54" in fb and "float" in fb
    out, fb, _g, _ = classify(canned("frontend_error_const_expr.txt"), "", 1, False, "x")
    assert (
        out is CheckOutcome.FRONTEND_ERROR
        and "lobject.c:496" in fb
        and "constant expressions" in fb
    )
    out, fb, _g, _ = classify(canned("internal_error_tags.txt"), "", 125, False, "x")
    assert out is CheckOutcome.FRONTEND_ERROR


def test_extract_frontend_errors_ignores_warnings():
    text = strip_ansi(canned("frontend_float_unsupported.txt"))
    errs = extract_frontend_errors(text)
    assert len(errs) == 1 and errs[0].file.endswith("lobject.h") and errs[0].line == 54
    warn = "[x.c:46:1-2] a function call potentially introduces non-determinism\n"
    assert extract_frontend_errors(warn) == []
    cpp = "/tmp/x/lapi.c:1:10: fatal error: 'refinedc.h' file not found\n    1 | #include <refinedc.h>\n"
    errs = extract_frontend_errors(cpp)
    assert len(errs) == 1 and errs[0].line == 1 and "file not found" in errs[0].message


def test_classify_stuck_extracts_goal_and_location():
    out, fb, goals, _ = classify(canned("stuck_shared_write.txt"), "", 1, False, "set1")
    assert out is CheckOutcome.AUTOMATION_STUCK
    assert goals and "typed_write_end" in goals[0] and "Shr" in goals[0]
    assert "block #0" in fb and "&shr" in fb
    f = extract_failures(strip_ansi(canned("stuck_shared_write.txt")))[0]
    assert f.kind == "stuck" and f.function == "set1" and f.location == (129, 2, 129, 9)


def test_classify_stuck_missing_invariant_mentions_loops():
    out, fb, _goals, _ = classify(canned("stuck_missing_invariant.txt"), "", 1, False, "zero_noinv")
    assert out is CheckOutcome.AUTOMATION_STUCK and "invariant" in fb


def test_classify_goals_remain_with_line_mapping():
    def line_of(reported: int):
        return (13, "  for (size_t i = 0; i < n; i++) p[i] = 0;") if reported == 134 else None

    c = classify_full(canned("goals_remain.txt"), "", 1, False, "zero", line_of)
    assert c.outcome is CheckOutcome.GOALS_REMAIN
    assert len(c.failures) == 2 and c.failures[0].case.startswith("Case distinction")
    assert "source line 13" in c.feedback and "i < n" in c.feedback
    assert "list_subequiv" in c.goals[0] and "rc::tactics" in c.feedback


def test_classify_lemma_and_spec_errors():
    out, fb, _g, _ = classify(canned("lemma_error.txt"), "", 1, False, "sum_to")
    assert out is CheckOutcome.GOALS_REMAIN and "lemmas.v" in fb and "line 5" in fb
    out, fb, _g, _ = classify(canned("spec_syntax_error.txt"), "", 1, False, "zero")
    assert out is CheckOutcome.FRONTEND_ERROR and "generated_spec" in fb and "int i32" in fb


def test_classify_tool_errors():
    out, fb, _, _ = classify("", "", 0, True, "f")
    assert out is CheckOutcome.TOOL_ERROR and "timed out" in fb
    out, fb, _, _ = classify("", "refinedc: command not found", 127, False, "f")
    assert out is CheckOutcome.TOOL_ERROR and "doctor" in fb
    out, fb, _, _ = classify("", "something odd", 3, False, "f")
    assert out is CheckOutcome.TOOL_ERROR


def test_extract_goals_dedups():
    text = canned("goals_remain.txt") + "\n" + canned("goals_remain.txt")
    assert len(extract_goals(text)) == 2


# ------------------------------------------------------------------ backend


@pytest.fixture
def backend(tmp_path: Path) -> RefinedCBackend:
    return RefinedCBackend(
        workspace_dir=tmp_path / "rc",
        settings={"refinedc_bin": "definitely-not-a-binary-xyz"},
        target=Target(),
    )


@pytest.fixture
def task(tmp_path: Path, backend: RefinedCBackend) -> FunctionTask:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text(SOURCE)
    tu = TranslationUnit(
        id="0c39be4cc5fb9ec6",
        source_path="src/a.c",
        directory=str(repo),
        arguments=["clang", "-Iinclude", "-DFOO=1", "-c", "src/a.c"],
    )
    return FunctionTask(
        function=FunctionInfo(
            id=f"{tu.id}:zero",
            name="zero",
            tu_id=tu.id,
            source_path="src/a.c",
            start_line=7,
            end_line=9,
            signature="zero()",
            body_hash="hz",
        ),
        tu=tu,
        target=Target(),
        repo_root=repo,
        workdir=backend.workspace_dir / "checks" / "zero",
        source_text=SOURCE,
        function_text=ORIGINAL,
        callee_specs={
            "helper": '[[rc::args("x @ int<i32>")]]\n[[rc::returns("int<i32>")]]\nstatic int helper(int x);'
        },
        external_specs={
            "memset": '[[rc::args("&own<uninit<{ly}>>", "int<i32>", "int<size_t>")]]\nvoid *memset(void *s, int c, size_t n);'
        },
    )


def test_registry_loads_both_backends(tmp_path: Path):
    rc = make_backend("refinedc", tmp_path / "a", {}, Target())
    nb = make_backend("null", tmp_path / "b", {}, Target())
    assert rc.name == "refinedc" and nb.name == "null"


def test_doctor_rows_are_labelled(backend: RefinedCBackend):
    statuses = {s.name: s for s in backend.doctor()}
    assert set(statuses) >= {"refinedc", "coqc", "dune", "opam"}
    assert not statuses["refinedc"].found and "opam" in statuses["refinedc"].hint


def test_paths_are_coq_identifiers(backend: RefinedCBackend, task: FunctionTask):
    d = backend._tu_dir(task.tu)
    assert d.name == "tu_0c39be4cc5fb9ec6"
    c = backend._check_dir(task)
    assert c.name == "f_0c39be4cc5fb9ec6_zero"
    c_file = c / "a.c"
    assert backend._module_path(c_file) == "checks.f_0c39be4cc5fb9ec6_zero.a"
    assert backend._proofs_dir(c_file) == c / "proofs" / "a"
    assert backend._stem_for("lua-x.c") == "lua_x"


def test_cpp_flags_forward_source_dir_includes_and_defines(
    backend: RefinedCBackend, task: FunctionTask
):
    flags = backend._cpp_flags(task.tu, task.repo_root)
    # The source's own directory leads the project dirs; with opaque floats on,
    # its rewritten shadow twin comes first of all.
    assert flags[0] == f"-I{backend._shadow_root / 'src'}"
    assert flags[2] == f"-I{task.repo_root / 'src'}"
    assert f"-I{Path(task.tu.directory) / 'include'}" in flags and "-DFOO=1" in flags
    backend.opaque_floats = False
    assert backend._cpp_flags(task.tu, task.repo_root)[0] == f"-I{task.repo_root / 'src'}"
    argv = backend._check_argv(Path("x.c"), task.tu, task.repo_root, no_build=True)
    assert argv[:4] == [backend.refinedc_bin, "check", "--no-extra-analysis", "--no-build"]
    assert argv[-1] == "x.c"


def test_guardrail_catches_every_escape_hatch(backend: RefinedCBackend):
    for attr in facts.LLM_FORBIDDEN_ATTRIBUTES:
        sub = Submission(files={"function.c": f"[[{attr}]]\n" + ANNOTATED})
        assert any(attr in p for p in backend.guardrail(sub)), attr
    for d in facts.LLM_FORBIDDEN_DIRECTIVES:
        sub = Submission(files={"function.c": f"//@rc::{d} x from y\n" + ANNOTATED})
        assert any(d in p for p in backend.guardrail(sub)), d
    for bad in (
        "Admitted.",
        "admit.",
        "Axiom foo : False.",
        "Parameter x : nat.",
        "Hypothesis h : 0 = 1.",
        "Unset Guard Checking.",
    ):
        sub = Submission(files={"function.c": ANNOTATED, "lemmas.v": f"Lemma l : True.\n{bad}\n"})
        assert any("lemmas.v" in p for p in backend.guardrail(sub)), bad
    sub = Submission(files={"function.c": "#include <stdlib.h>\n" + ANNOTATED})
    assert any("include" in p for p in backend.guardrail(sub))
    sub = Submission(files={"function.c": ANNOTATED, "evil.sh": "rm -rf /"})
    assert any("unexpected file" in p for p in backend.guardrail(sub))
    assert backend.guardrail(Submission(files={"function.c": ANNOTATED})) == []


def test_check_rejects_code_changes_before_running_tool(
    backend: RefinedCBackend, task: FunctionTask
):
    sub = Submission(files={"function.c": ANNOTATED.replace("i < n", "i <= n")})
    res = backend.check(task, sub, timeout_seconds=5)
    assert res.outcome is CheckOutcome.GUARDRAIL and "code changed" in res.feedback


def test_check_without_tool_builds_source_and_returns_tool_error(
    backend: RefinedCBackend, task: FunctionTask
):
    sub = Submission(
        files={"function.c": ANNOTATED, "lemmas.v": "Lemma t : True. Proof. exact I. Qed."}
    )
    res = backend.check(task, sub, timeout_seconds=5)
    assert res.outcome is CheckOutcome.TOOL_ERROR and "opam" in res.feedback
    src = Path(res.artifacts["source"])
    assert (
        src.is_relative_to(backend.workspace_dir) and src.parent.name == "f_0c39be4cc5fb9ec6_zero"
    )
    text = src.read_text()
    assert text.startswith(facts.HEADER_INCLUDE)
    assert facts.LINE_MARKER_TEXT in text
    assert (
        "//@rc::import lemmas from refinedc.project.fver.checks.f_0c39be4cc5fb9ec6_zero.a" in text
    )
    assert "static int helper(int x);" in text and "return x + 1;" not in text
    assert "void *memset(void *s, int c, size_t n);" in text
    assert Path(res.artifacts["lemmas"]).parent == backend._proofs_dir(src)
    assert (task.repo_root / "src" / "a.c").read_text() == SOURCE


def test_marker_offset_from_generated_code():
    code = canned("generated_code_marker.v")
    marker_line = next(
        i + 1 for i, ln in enumerate(canned("marker_source.c").split("\n")) if "__fver_marker" in ln
    )
    # The fixture used the older marker name; adapt the regex target.
    code = code.replace("impl___fver_marker", facts.IMPL_DEF_FMT.format(fn=facts.LINE_MARKER_FN))
    off = RefinedCBackend._marker_offset(code, marker_line)
    assert off == 125 - marker_line
    # `*p = 1;` sits on source line 14 and was reported at 135.
    assert 14 + off == 135
    assert RefinedCBackend._marker_offset("nothing here", 4) is None


def test_translate_without_tool_marks_supported(backend: RefinedCBackend, task: FunctionTask):
    res = backend.translate(task.tu, [task.function], task.repo_root)
    assert res.supported == {"zero": True}
    copy = Path(res.artifacts["copy"])
    assert copy.parent.name == "tu_0c39be4cc5fb9ec6" and copy.name == "a.c"
    assert copy.read_text().startswith(facts.HEADER_INCLUDE)
    assert (task.repo_root / "src" / "a.c").read_text() == SOURCE


def test_translate_maps_frontend_errors_to_functions(
    backend: RefinedCBackend, task: FunctionTask, monkeypatch
):
    from fver.util.proc import ProcResult

    out = strip_ansi(canned("frontend_not_implemented_varargs.txt")).replace("fe2.c:11", "a.c:9")
    monkeypatch.setattr(backend, "_have_refinedc", lambda: True)
    calls: list[str] = []

    def fake_run(argv, cwd, timeout):
        # First round: the error inside `zero`; once it is stubbed the file passes.
        if "check" not in argv:  # `refinedc init`
            return ProcResult(argv, 0, "", "", 0.1)
        calls.append(Path(argv[-1]).read_text())
        first = len(calls) == 1
        return ProcResult(argv, 1 if first else 0, out if first else "", "", 0.1)

    monkeypatch.setattr(backend, "_run", fake_run)
    fns = [task.function, fn_info("after", 11, 13)]
    res = backend.translate(task.tu, fns, task.repo_root)
    # a.c:9 in the copy is source line 8 (one include line prepended): inside zero.
    assert res.supported == {"zero": False, "after": True} and res.tu_error is None
    assert "va_end" in res.reasons["zero"]
    assert len(calls) == 2
    assert "void zero(int *p, size_t n);" in calls[1] and "p[i] = 0" not in calls[1]
    assert calls[1].count("\n") == calls[0].count("\n")  # line-preserving stub
    # The same error again at the stubbed prototype: it is blanked, then the file passes.
    calls.clear()

    def fake_run2(argv, cwd, timeout):
        if "check" not in argv:
            return ProcResult(argv, 0, "", "", 0.1)
        calls.append(Path(argv[-1]).read_text())
        return ProcResult(
            argv, 1 if len(calls) <= 2 else 0, out if len(calls) <= 2 else "", "", 0.1
        )

    monkeypatch.setattr(backend, "_run", fake_run2)
    res = backend.translate(task.tu, fns, task.repo_root)
    assert res.supported == {"zero": False, "after": True} and res.tu_error is None
    assert "prototype also rejected" in res.reasons["zero"]
    assert "void zero(int *p, size_t n);" not in calls[2]
    # A header-level error rejects the whole file.
    out2 = strip_ansi(canned("frontend_float_unsupported.txt"))
    monkeypatch.setattr(
        backend, "_run", lambda argv, cwd, timeout: ProcResult(argv, 1, out2, "", 0.1)
    )
    res = backend.translate(task.tu, fns, task.repo_root)
    assert res.supported == {"zero": False, "after": False} and "float" in (res.tu_error or "")
    # An Ail-level error in preprocessed coordinates is mapped through the cpp map.
    ail = "[src/tu_x/a.c:2756:17-63] Invalid use of binary operation [+]\n"
    calls.clear()

    def fake_run3(argv, cwd, timeout):
        if "check" not in argv:
            return ProcResult(argv, 0, "", "", 0.1)
        calls.append(Path(argv[-1]).read_text())
        first = len(calls) == 1
        return ProcResult(argv, 1 if first else 0, ail if first else "", "", 0.1)

    monkeypatch.setattr(backend, "_run", fake_run3)
    monkeypatch.setattr(backend, "_cpp_map_for", lambda c_file, tu, repo_root: {2756: 13})
    res = backend.translate(task.tu, fns, task.repo_root)
    assert res.supported == {"zero": True, "after": False} and res.tu_error is None
    assert "binary operation" in res.reasons["after"]


def test_prepare_writes_project_file_when_tool_missing(
    backend: RefinedCBackend, task: FunctionTask
):
    backend.prepare([task.tu], task.repo_root)
    assert backend.project_file.exists()
    assert facts.DEFAULT_COQ_ROOT in backend.project_file.read_text()


def test_audit_without_generated_proof_or_coqc_fails_closed(
    backend: RefinedCBackend, task: FunctionTask, tmp_path: Path
):
    from fver.backends.base import CheckResult

    res = backend.audit(task, CheckResult(outcome=CheckOutcome.OK, feedback=""))
    assert not res.passed and "not found" in res.violations[0]
    proof = tmp_path / facts.GENERATED_PROOF_FMT.format(fn="zero")
    proof.write_text("Lemma type_zero : True. Proof. exact I. Qed.")
    backend.coqc_bin = "definitely-not-coqc-xyz"
    res = backend.audit(
        task, CheckResult(outcome=CheckOutcome.OK, feedback="", artifacts={proof.name: str(proof)})
    )
    assert not res.passed and any("audit tool unavailable" in v for v in res.violations)
    proof.write_text("Lemma type_zero : True. Admitted.")
    res = backend.audit(
        task, CheckResult(outcome=CheckOutcome.OK, feedback="", artifacts={proof.name: str(proof)})
    )
    assert any("Qed" in v for v in res.violations)


def test_prompt_context_and_submission_spec(backend: RefinedCBackend):
    ctx = backend.prompt_context()
    assert (
        "rc::args" in ctx.reference
        and "file=function.c" in ctx.examples
        and "Note:" in ctx.instructions
    )
    assert (
        "array<i32" in ctx.examples and "int<i32>, {" not in ctx.examples
    )  # old wrong syntax gone
    assert any("trust_me" in p for p in ctx.forbidden_patterns)
    assert any("rc::import" in p for p in ctx.forbidden_patterns)
    spec = backend.submission_spec()
    assert spec.required == ["function.c"] and "lemmas.v" in spec.files


def test_extract_spec(backend: RefinedCBackend):
    c = backend.extract_spec(Submission(files={"function.c": ANNOTATED}), fn_info("zero", 1, 1))
    assert "rc::ensures" in c and c.endswith("void zero(int *p, size_t n);")


def test_tool_env_sets_opam_prefix_from_binary_location(tmp_path: Path, monkeypatch):
    from fver.backends.refinedc.backend import _tool_env

    prefix = tmp_path / "switch"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib" / "refinedc").mkdir(parents=True)
    (prefix / "lib" / "cerberus-lib" / "runtime").mkdir(parents=True)
    rc = prefix / "bin" / "refinedc"
    rc.write_text("#!/bin/sh\n")
    monkeypatch.delenv("OPAM_SWITCH_PREFIX", raising=False)
    monkeypatch.delenv("CERB_RUNTIME", raising=False)
    env = _tool_env(str(rc))
    assert env["PATH"].split(os.pathsep)[0] == str(prefix / "bin")
    assert env["OPAM_SWITCH_PREFIX"] == str(prefix)
    assert env["CERB_RUNTIME"] == str(prefix / "lib" / "cerberus-lib" / "runtime")
    # Another switch activated in the shell must not win over the binary's own.
    monkeypatch.setenv("OPAM_SWITCH_PREFIX", "/elsewhere")
    assert _tool_env(str(rc))["OPAM_SWITCH_PREFIX"] == str(prefix)
    # A bare name that is not found leaves the environment alone.
    monkeypatch.setenv("OPAM_SWITCH_PREFIX", "/elsewhere")
    assert _tool_env("no-such-binary-xyz")["OPAM_SWITCH_PREFIX"] == "/elsewhere"


def test_definition_prototype_and_stub():
    src = "int a(void);\nstatic int\nhelper(int x, int (*f)(int)) /* c */\n{\n  return f(x);\n}\nint b(void) { return 1; }\n"
    assert (
        ann.definition_prototype("static inline int f(int a,\n int b) { return a; }")
        == "static inline int f(int a, int b);"
    )
    out = ann.stub_definition(src, 2, 6)
    assert out.split("\n")[1] == "static int helper(int x, int (*f)(int));"
    assert out.count("\n") == src.count("\n") and "return f(x)" not in out
    assert "int b(void) { return 1; }" in out
    assert ann.blank_lines(src, 2, 6).split("\n")[1:6] == [""] * 5


def test_check_stubs_every_other_definition(backend: RefinedCBackend, task: FunctionTask):
    task.callee_specs = {}
    res = backend.check(task, Submission(files={"function.c": ANNOTATED}), timeout_seconds=5)
    text = Path(res.artifacts["source"]).read_text()
    assert "static int helper(int x);" in text and "return x + 1;" not in text
    assert "int after(void);" in text and "return helper(1);" not in text
    assert "p[i] = 0;" in text  # the target keeps its body


def test_shim_dirs_come_after_project_flags(
    backend: RefinedCBackend, task: FunctionTask, monkeypatch, tmp_path: Path
):
    from fver.backends.refinedc import backend as mod

    runtime = tmp_path / "rt"
    (runtime / "libc" / "include" / "posix").mkdir(parents=True)
    monkeypatch.setattr(backend, "_env", lambda: {"CERB_RUNTIME": str(runtime)})
    flags = backend._cpp_flags(task.tu, task.repo_root)
    posix = f"-I{runtime / 'libc' / 'include' / 'posix'}"
    assert flags.index(posix) < flags.index(f"-I{mod._SHIMS_DIR}")
    assert flags.index(f"-I{Path(task.tu.directory) / 'include'}") < flags.index(posix)
    backend.posix_shims = False
    assert not any(str(mod._SHIMS_DIR) in f for f in backend._cpp_flags(task.tu, task.repo_root))
    for h in facts.SHIMMED_HEADERS:
        assert (mod._SHIMS_DIR / h).exists(), h


def test_cpp_line_map_follows_line_markers():
    from fver.backends.refinedc.backend import cpp_line_map

    out = (
        '# 1 "a.c"\n'  # phys 1
        '# 1 "<built-in>"\n'  # 2
        '# 1 "a.c" 2\n'  # 3
        '# 1 "/inc/stddef.h" 1\n'  # 4
        "typedef long ptrdiff_t;\n"  # 5 (stddef.h:1)
        "\n"  # 6
        '# 2 "a.c" 2\n'  # 7
        "int f(void) {\n"  # 8 -> a.c:2
        "  return 0;\n"  # 9 -> a.c:3
        "}\n"  # 10 -> a.c:4
    )
    assert cpp_line_map(out, "/x/a.c") == {8: 2, 9: 3, 10: 4}


def test_check_argv_force_includes_prelude(backend: RefinedCBackend, task: FunctionTask):
    from fver.backends.refinedc import backend as mod

    argv = backend._check_argv(Path("x.c"), task.tu, task.repo_root, no_build=False)
    assert f"--include={mod._SHIMS_DIR / facts.PRELUDE_HEADER}" in argv
    backend.posix_shims = False
    backend.opaque_floats = False  # its generated header is force-included too
    argv = backend._check_argv(Path("x.c"), task.tu, task.repo_root, no_build=False)
    assert not any(a.startswith("--include=") for a in argv)


def test_enclosing_definition_by_brace_scan():
    src = (
        "#include <x.h>\n"
        "int a;\n"
        "int ZEXPORTVA gzprintf(gzFile file, const char *format, ...)\n"
        "{\n"
        '  char s[] = "{";\n'
        "  if (x) { y(); }\n"
        "  return 0;\n"
        "}\n"
        "struct s { int q; };\n"
        "static int k(void) { return 1; }\n"
    )
    assert ann.enclosing_definition(src, 6) == (3, 8, "gzprintf")
    assert ann.enclosing_definition(src, 10) == (10, 10, "k")
    assert ann.enclosing_definition(src, 2) is None
    assert ann.enclosing_definition(src, 9) == (9, 9, "")  # a struct body, no name


def test_translate_isolates_body_the_extractor_missed(
    backend: RefinedCBackend, task: FunctionTask, monkeypatch
):
    from fver.util.proc import ProcResult

    # `helper` is not in the function list handed to translate(), yet the
    # error inside it must not reject the file: the body is stubbed by brace
    # matching and the known functions stay supported.
    err = "[a.c:5:3-10] Forbidden: nested assignment\n"  # copy line 5 = source line 4 (in helper)
    calls: list[str] = []

    def fake_run(argv, cwd, timeout):
        if "check" not in argv:
            return ProcResult(argv, 0, "", "", 0.1)
        calls.append(Path(argv[-1]).read_text())
        first = len(calls) == 1
        return ProcResult(argv, 1 if first else 0, err if first else "", "", 0.1)

    monkeypatch.setattr(backend, "_have_refinedc", lambda: True)
    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(backend, "_cpp_map_for", lambda c_file, tu, repo_root: {})
    fns = [task.function, fn_info("after", 11, 13)]
    res = backend.translate(task.tu, fns, task.repo_root)
    assert res.tu_error is None and res.supported == {"zero": True, "after": True}
    assert "static int helper(int x);" in calls[1] and "return x + 1;" not in calls[1]


def test_enclosing_definition_ignores_macros_with_braces():
    src = (
        "#define send_bits(s, value, length) \\\n"
        "{ int len = length; \\\n"
        "  s->bi_buf |= (value) << s->bi_valid; \\\n"
        "}\n"
        "local void compress_block(deflate_state *s, const ct_data *ltree)\n"
        "{\n"
        "#ifdef ZLIB_DEBUG\n"
        "    int x = 0;\n"
        "#endif\n"
        "    send_bits(s, 1, 2);\n"
        "}\n"
    )
    assert ann.enclosing_definition(src, 10) == (5, 11, "compress_block")
    out = ann.stub_definition(src, 5, 11)
    assert (
        out.split("\n")[4] == "local void compress_block(deflate_state *s, const ct_data *ltree);"
    )
    assert out.split("\n")[6] == "#ifdef ZLIB_DEBUG" and out.split("\n")[8] == "#endif"
    assert out.count("\n") == src.count("\n")


def test_definition_ranges_from_preprocessed_text(
    backend: RefinedCBackend, task: FunctionTask, monkeypatch
):
    # The raw copy has `helper` decorated by a macro tree-sitter cannot parse;
    # the preprocessed text (with line markers) reveals it.
    cpp = (
        '# 1 "a.c"\n'
        '# 1 "<built-in>"\n'
        '# 1 "a.c" 2\n'
        "\n"  # 4 -> a.c:1
        '# 3 "a.c"\n'
        "static int helper(int x) {\n"  # 6 -> a.c:3
        "  return x + 1;\n"  # 7 -> a.c:4
        "}\n"  # 8 -> a.c:5
    )
    monkeypatch.setattr(
        backend,
        "_preprocess",
        lambda c, t, r: (
            __import__("fver.backends.refinedc.backend", fromlist=["cpp_line_map"]).cpp_line_map(
                cpp, "a.c"
            ),
            cpp,
        ),
    )
    assert backend._definition_ranges(Path("a.c"), task.tu, task.repo_root) == {"helper": (3, 5)}
    # translate() stubs it by that range even though it is not in `functions`.
    from fver.util.proc import ProcResult

    err = "[a.c:5:3-10] Forbidden: nested assignment\n"  # copy line 5 = helper's body
    calls: list[str] = []

    def fake_run(argv, cwd, timeout):
        if "check" not in argv:
            return ProcResult(argv, 0, "", "", 0.1)
        calls.append(Path(argv[-1]).read_text())
        first = len(calls) == 1
        return ProcResult(argv, 1 if first else 0, err if first else "", "", 0.1)

    monkeypatch.setattr(backend, "_have_refinedc", lambda: True)
    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(backend, "_definition_ranges", lambda c, t, r: {"helper": (4, 6)})
    fns = [task.function, fn_info("after", 11, 13)]
    res = backend.translate(task.tu, fns, task.repo_root)
    assert res.tu_error is None and res.supported == {"zero": True, "after": True}
    assert "static int helper(int x);" in calls[1] and "return x + 1;" not in calls[1]


def test_translate_bisects_a_front_end_crash(
    backend: RefinedCBackend, task: FunctionTask, monkeypatch
):
    from fver.util.proc import ProcResult

    crash = "refinedc: internal error, uncaught exception:\n  Assertion failed\n"
    seen: list[str] = []

    def fake_run(argv, cwd, timeout):
        if "check" not in argv:
            return ProcResult(argv, 0, "", "", 0.1)
        text = Path(argv[-1]).read_text()
        seen.append(text)
        # The crash is caused by `after`'s body; anything else keeps crashing.
        if "return helper(1);" in text:
            return ProcResult(argv, 125, crash, "", 0.1)
        return ProcResult(argv, 0, "", "", 0.1)

    monkeypatch.setattr(backend, "_have_refinedc", lambda: True)
    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(backend, "_definition_ranges", lambda c, t, r: {})
    fns = [task.function, fn_info("after", 11, 13)]
    res = backend.translate(task.tu, fns, task.repo_root)
    assert res.tu_error is None
    assert res.supported == {"zero": True, "after": False}
    assert "crash" in res.reasons["after"] and "Assertion" in res.reasons["after"]
    assert "int after(void);" in seen[-1] and "p[i] = 0" in seen[-1]
