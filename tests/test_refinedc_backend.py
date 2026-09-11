"""Tests for the RefinedC backend. No external tools, no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from fver.backends.base import CheckOutcome, FunctionTask, Submission
from fver.backends.refinedc import annotations as ann
from fver.backends.refinedc import facts
from fver.backends.refinedc.backend import RefinedCBackend
from fver.backends.refinedc.parse_output import classify, extract_goals
from fver.backends.registry import make_backend
from fver.core.models import FunctionInfo, Target, TranslationUnit, sha256_text

FIX = Path(__file__).parent / "fixtures" / "refinedc"

ORIGINAL = """void zero(int *p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] = 0;
}
"""

ANNOTATED = """[[rc::parameters("p : loc", "n : nat")]]
[[rc::args("p @ &own<array<int<i32>, {replicate n (uninit (it_layout i32))}>>", "n @ int<size_t>")]]
[[rc::returns("void")]]
[[rc::ensures("own p : array<int<i32>, {replicate n (0 @ int<i32>)}>")]]
void zero(int *p, size_t n) {
  [[rc::exists("i : nat")]]
  [[rc::inv_vars("i : i @ int<size_t>",
                 "p : p @ &own<array<int<i32>, {replicate i (0 @ int<i32>) ++ replicate (n - i) (uninit (it_layout i32))}>>")]]
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
    text = "#include <refinedc.h>\n" + ANNOTATED.replace("  for", "  rc_unfold(n);\n  for")
    stripped = ann.strip_annotations(text)
    assert "rc::" not in stripped
    assert "refinedc.h" not in stripped
    assert "rc_unfold" not in stripped
    assert ann.normalise(stripped) == ann.normalise(ORIGINAL)


def test_strip_handles_brackets_inside_strings():
    text = '[[rc::args("&own<array<int<i32>, {xs !! 0 = Some [1]}>>")]]\nint f(void) { return 0; }'
    assert ann.strip_annotations(text).strip() == "int f(void) { return 0; }"


def test_find_attributes_parses_args():
    attrs = ann.find_attributes(ANNOTATED)
    names = [a.name for a in attrs]
    assert names[:4] == ["rc::parameters", "rc::args", "rc::returns", "rc::ensures"]
    assert attrs[0].args == ("p : loc", "n : nat")


def test_code_unchanged_positive_cases():
    ok, _ = ann.code_unchanged(ORIGINAL, ANNOTATED)
    assert ok
    ws = ORIGINAL.replace("  for", "\n\n    for").replace("p[i] = 0;", "p[ i ]=0 ;")
    assert ann.code_unchanged(ORIGINAL, ws)[0]
    commented = ORIGINAL.replace("p[i] = 0;", "p[i] = 0; // clear /* it */")
    assert ann.code_unchanged(ORIGINAL, commented)[0]


def test_code_unchanged_catches_semantic_edits():
    ok, why = ann.code_unchanged(ORIGINAL, ANNOTATED.replace("i < n", "i <= n"))
    assert not ok and "<" in why
    ok, why = ann.code_unchanged(ORIGINAL, ANNOTATED.replace("p[i] = 0;", "if (p) p[i] = 0;"))
    assert not ok and "if" in why
    ok, _ = ann.code_unchanged(ORIGINAL, ANNOTATED.replace("void zero", "void zero2"))
    assert not ok


def test_normalise_keeps_identifier_boundaries():
    assert ann.normalise("unsigned   int  x ;") == "unsigned int x;"
    assert ann.normalise("a<b") == ann.normalise("a < b")
    assert ann.normalise("int x") != ann.normalise("intx")


def test_splice_replaces_target_and_callee_bottom_up_and_adds_include_once():
    target = fn_info("zero", 7, 9)
    helper = fn_info("helper", 3, 5)
    helper_text = '[[rc::args("x @ int<i32>")]]\nstatic int helper(int x);\n'
    out = ann.splice(SOURCE, target, ANNOTATED, {"helper": (helper, helper_text)})
    assert out.startswith(facts.HEADER_INCLUDE + "\n")
    assert out.count("refinedc.h") == 1
    assert "static int helper(int x);" in out
    assert "return x + 1;" not in out
    assert 'rc::constraints("{i ≤ n}")' in out
    assert "int after(void) {\n  return helper(1);\n}" in out
    # Splicing twice does not duplicate the include.
    again = ann.splice(out, target, ANNOTATED, None)
    assert again.count("refinedc.h") == 1


def test_splice_ignores_callees_from_other_files_and_rejects_overlap():
    target = fn_info("zero", 7, 9)
    other = fn_info("helper", 3, 5, path="src/other.c")
    out = ann.splice(SOURCE, target, ANNOTATED, {"helper": (other, "nope")})
    assert "return x + 1;" in out
    overlap = fn_info("helper", 8, 12)
    with pytest.raises(ValueError):
        ann.splice(SOURCE, target, ANNOTATED, {"helper": (overlap, "x")})


def test_find_definition_range_skips_calls_and_prototypes():
    src = "int helper(int x);\nint use(void) { return helper(1); }\n\nint helper(int x)\n{\n  return x;\n}\n"
    assert ann.find_definition_range(src, "helper") == (4, 7)
    assert ann.find_definition_range(src, "missing") is None


def test_extract_contract_drops_proof_only_attrs_and_body():
    text = '[[rc::tactics("all: lia.")]]\n' + ANNOTATED
    c = ann.extract_contract(text)
    assert c.startswith('[[rc::parameters("p : loc", "n : nat")]]')
    assert "rc::tactics" not in c
    assert "rc::inv_vars" not in c  # loop attribute, not part of the contract
    assert c.rstrip().endswith("void zero(int *p, size_t n);")


def test_vacuity_check():
    bad = '[[rc::parameters("n : nat")]]\n[[rc::requires("{n < 0}")]]\nvoid f(void) {}'
    assert ann.vacuity_check(bad)
    assert ann.vacuity_check('[[rc::requires("{False}")]]\nvoid f(void) {}')
    assert ann.vacuity_check('[[rc::requires("{0 = 1}")]]\nvoid f(void) {}')
    assert ann.vacuity_check('[[rc::args("null", "null")]]\nvoid f(int*a,int*b) {}')
    assert not ann.vacuity_check(ANNOTATED)
    assert not ann.vacuity_check('[[rc::args("null", "n @ int<i32>")]]\nvoid f(int*a,int n) {}')


# ------------------------------------------------------------- parse_output


def canned(name: str) -> str:
    return (FIX / name).read_text()


def test_classify_ok():
    out, _fb, goals, _w = classify("all good", "", 0, False, "zero")
    assert out is CheckOutcome.OK and not goals


def test_classify_frontend_error():
    out, fb, _goals, _ = classify(canned("frontend_error.txt"), "", 1, False, "zero")
    assert out is CheckOutcome.FRONTEND_ERROR
    assert "zero.c:7" in fb and "rc::argz" in fb


def test_classify_stuck_extracts_goal():
    out, fb, goals, _ = classify("", canned("stuck.txt"), 1, False, "zero")
    assert out is CheckOutcome.AUTOMATION_STUCK
    assert goals and "typed_place" in goals[0]
    assert "zero.c:9" in fb and "loop" in fb.lower()


def test_classify_goals_remain():
    out, fb, goals, _ = classify("", canned("goals.txt"), 1, False, "sum_to")
    assert out is CheckOutcome.GOALS_REMAIN
    assert goals and "s + i ≤ max_int i32" in goals[0]
    assert "rc::tactics" in fb


def test_classify_bug_and_tool_errors():
    out, fb, _, witness = classify(canned("bug.txt"), "", 1, False, "find")
    assert out is CheckOutcome.BUG and "UB045" in witness
    out, fb, _, _ = classify("", "", 0, True, "f")
    assert out is CheckOutcome.TOOL_ERROR and "timed out" in fb
    out, fb, _, _ = classify("", "refinedc: command not found", 127, False, "f")
    assert out is CheckOutcome.TOOL_ERROR and "doctor" in fb
    out, fb, _, _ = classify("", canned("tool_error.txt"), 2, False, "f")
    assert out is CheckOutcome.TOOL_ERROR


def test_extract_goals_dedups():
    text = canned("goals.txt") + "\n" + canned("goals.txt")
    assert len(extract_goals(text)) == 1


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
        id="tu1", source_path="src/a.c", directory=str(repo), arguments=["clang", "-c", "src/a.c"]
    )
    return FunctionTask(
        function=fn_info("zero", 7, 9),
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
            "memset": '[[rc::args("&own<any<{ly}>>", "int<i32>", "int<size_t>")]]\nvoid *memset(void *s, int c, size_t n);'
        },
    )


def test_registry_loads_both_backends(tmp_path: Path):
    rc = make_backend("refinedc", tmp_path / "a", {}, Target())
    nb = make_backend("null", tmp_path / "b", {}, Target())
    assert rc.name == "refinedc" and nb.name == "null"


def test_doctor_reports_missing_tools_with_hints(backend: RefinedCBackend):
    statuses = {s.name: s for s in backend.doctor()}
    assert not statuses["definitely-not-a-binary-xyz"].found
    assert "opam" in statuses["definitely-not-a-binary-xyz"].hint


def test_guardrail_catches_every_escape_hatch(backend: RefinedCBackend):
    for attr in facts.LLM_FORBIDDEN_ATTRIBUTES:
        sub = Submission(files={"function.c": f"[[{attr}]]\n" + ANNOTATED})
        assert any(attr in p for p in backend.guardrail(sub)), attr
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
    assert res.outcome is CheckOutcome.GUARDRAIL
    assert "code changed" in res.feedback


def test_check_without_tool_returns_tool_error_and_never_raises(
    backend: RefinedCBackend, task: FunctionTask
):
    sub = Submission(
        files={"function.c": ANNOTATED, "lemmas.v": "Lemma t : True. Proof. exact I. Qed."}
    )
    res = backend.check(task, sub, timeout_seconds=5)
    assert res.outcome is CheckOutcome.TOOL_ERROR
    assert "opam" in res.feedback
    # The spliced source was still built, inside the backend workspace only.
    src = Path(res.artifacts["source"])
    assert src.is_relative_to(backend.workspace_dir)
    text = src.read_text()
    assert text.startswith(facts.HEADER_INCLUDE)
    assert (
        "static int helper(int x);" in text and "return x + 1;" not in text
    )  # callee -> prototype
    assert "void *memset(void *s, int c, size_t n);" in text  # external -> prototype prepended
    assert 'rc::import("lemmas"' in text  # backend wired lemmas.v in
    assert Path(res.artifacts["lemmas"]).read_text().startswith("Lemma t")
    # User file untouched.
    assert (task.repo_root / "src" / "a.c").read_text() == SOURCE


def test_translate_without_tool_marks_supported(backend: RefinedCBackend, task: FunctionTask):
    res = backend.translate(task.tu, [task.function], task.repo_root)
    assert res.supported == {"zero": True}
    assert res.reasons["zero"] == "refinedc not installed; front-end check skipped"
    copy = Path(res.artifacts["copy"])
    assert copy.is_relative_to(backend.workspace_dir) and copy.read_text().startswith(
        facts.HEADER_INCLUDE
    )
    assert (task.repo_root / "src" / "a.c").read_text() == SOURCE


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
    assert any("Admitted" in v for v in res.violations)


def test_prompt_context_and_submission_spec(backend: RefinedCBackend):
    ctx = backend.prompt_context()
    assert (
        "rc::args" in ctx.reference
        and "file=function.c" in ctx.examples
        and "Note:" in ctx.instructions
    )
    assert any("trust_me" in p for p in ctx.forbidden_patterns)
    spec = backend.submission_spec()
    assert spec.required == ["function.c"] and "lemmas.v" in spec.files


def test_extract_spec(backend: RefinedCBackend):
    c = backend.extract_spec(Submission(files={"function.c": ANNOTATED}), fn_info("zero", 1, 1))
    assert "rc::ensures" in c and c.endswith("void zero(int *p, size_t n);")
