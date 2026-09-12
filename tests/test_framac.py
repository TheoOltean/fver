"""The Frama-C/WP backend: output parsing, ACSL helpers, guardrail, splicing."""

from __future__ import annotations

from pathlib import Path

from fver.backends.base import CheckOutcome, Submission
from fver.backends.framac import acsl
from fver.backends.framac.backend import FramaCBackend
from fver.backends.framac.parse_output import classify
from fver.core.models import Target

WP_OK = """[kernel] Parsing t.c (with preprocessing)
[wp] 5 goals scheduled
[wp] Proved goals:    5 / 5
  Qed:             3
  Alt-Ergo 2.4.3:  2
"""
WP_FAIL = """[kernel] Parsing t.c (with preprocessing)
[wp] [Timeout] typed_zero_item_loop_invariant_preserved (Qed 4ms) (Alt-Ergo) (Cached)
[wp] [Timeout] typed_zero_item_assert_rte_mem_access_2 (Qed 2ms) (Alt-Ergo) (Cached)
[wp] Proved goals:   22 / 24
  Qed:               7
  Timeout:           2
Goal Establishment of Invariant ("t.c", line 12):
Prove: true.
Prover Qed returns Valid
------------------------------------------------------------
Goal Preservation of Invariant ("t.c", line 12):
Let a = shiftfield_F1_item_vals(it_0).
Assume {
  (* Pre-condition *)
  Have: valid_rw(Malloc_0, it_0, 2).
}
Prove: (i < x_2) /\\ ((-1) <= i).
Prover Alt-Ergo 2.4.3 returns Timeout (Qed:4ms) (10s)
------------------------------------------------------------
"""
WP_MISSING = """[kernel:annot:missing-spec] m.c:1: Warning:
  Neither code nor specification for function helper, generating default exits,
  assigns and terminates. See -generated-spec-* options for more info.
[wp] [Timeout] typed_call_assert_rte_signed_overflow (Alt-Ergo) (Cached)
[wp] Proved goals:    2 / 3
"""
WP_RANGE = """[kernel] Parsing t.c (with preprocessing)
[wp] Running WP plugin...
[wp] t.c:196: User Error:
  Invalid infinite range destination_0+(0..)
[kernel] Plug-in wp aborted: invalid user input.
"""
WP_ERR = """[kernel] Parsing t.c (with preprocessing)
[kernel:annot-error] t.c:20: Failure:
  unbound logic predicate \\valid_read_string. Ignoring logic specification of function first_is_a
[kernel] User Error: warning annot-error treated as fatal error.
[kernel] Frama-C aborted: invalid user input.
"""


def test_classify_outcomes() -> None:
    assert classify(WP_OK, "", 0, False, "f").outcome is CheckOutcome.OK
    c = classify(WP_FAIL, "", 0, False, "zero_item", lambda n: f"src line {n}")
    assert c.outcome is CheckOutcome.GOALS_REMAIN and c.proved == 22 and c.total == 24
    assert "loop invariant is not preserved" in c.feedback
    assert "Preservation of Invariant (line 12): `src line 12`" in c.feedback
    assert "Prove: (i < x_2)" in c.feedback and len(c.goals) == 1
    m = classify(WP_MISSING, "", 0, False, "call")
    assert m.missing_specs == ["helper"] and "No contract for callee helper" in m.feedback
    e = classify(WP_ERR, "", 1, False, "first_is_a")
    assert e.outcome is CheckOutcome.FRONTEND_ERROR and "valid_read_string" in e.feedback
    assert classify("", "", 0, True, "f").outcome is CheckOutcome.TOOL_ERROR
    partial = classify(WP_FAIL.split("Proved goals")[0], "", -1, True, "zero_item")
    assert partial.outcome is CheckOutcome.GOALS_REMAIN and "ran out of time" in partial.feedback
    assert "loop_invariant_preserved" in partial.feedback
    r = classify(WP_RANGE, "", 1, False, "f")
    assert r.outcome is CheckOutcome.FRONTEND_ERROR and "Invalid infinite range" in r.feedback
    assert "aborted" not in r.feedback


ANNOTATED = """/*@ requires \\valid(p);
    assigns *p;
    ensures *p == 0;
*/
void clear(int *p) {
  /*@ loop invariant 0 <= i <= 1;
      loop assigns i;
  */
  for (int i = 0; i < 1; i++) *p = 0;
}
"""


def test_acsl_contract_helpers() -> None:
    contract = acsl.extract_contract(ANNOTATED)
    assert contract.startswith("/*@ requires \\valid(p);") and contract.endswith(
        "void clear(int *p);"
    )
    assert "loop invariant" not in contract
    assert acsl.one_line(contract) == (
        "/*@ requires \\valid(p); assigns *p; ensures *p == 0; */ void clear(int *p);"
    )
    assert acsl.body_annotations(ANNOTATED)[0].startswith("/*@ loop invariant")
    assert acsl.clauses(contract.split("*/")[0] + "*/") == [
        "requires \\valid(p)",
        "assigns *p",
        "ensures *p == 0",
    ]


def test_guardrail_and_splice(tmp_path: Path) -> None:
    b = FramaCBackend(tmp_path / "ws", {}, Target())
    bad = Submission(files={"function.c": "/*@ requires \\false; */\nvoid f(void) {}"})
    assert any("requires" in p for p in b.guardrail(bad))
    assert b.guardrail(Submission(files={"function.c": "//@ admit x;\nvoid f(void){}"}))
    assert b.guardrail(Submission(files={"function.c": ANNOTATED})) == []
    from fver.backends.base import FunctionTask
    from fver.core.models import FunctionInfo, TranslationUnit

    src = "int helper(int x) {\n  return x + 1;\n}\n\nvoid clear(int *p) {\n  for (int i = 0; i < 1; i++) *p = 0;\n}\n\nint other(void) {\n  return helper(1);\n}\n"
    tu = TranslationUnit("t", "a.c", str(tmp_path), ["cc", "-Iinc", "-DX=1", "-c", "a.c"])
    fn = FunctionInfo("t:clear", "clear", "t", "a.c", 5, 7, "void clear(int *p)", "h")
    task = FunctionTask(
        function=fn,
        tu=tu,
        target=Target(),
        repo_root=tmp_path,
        workdir=tmp_path / "wd",
        source_text=src,
        function_text="void clear(int *p) {\n  for (int i = 0; i < 1; i++) *p = 0;\n}\n",
        callee_specs={
            "helper": "/*@ requires x < 10;\n    assigns \\nothing; */\nint helper(int x);"
        },
        external_specs={},
    )
    out = b._build_source(task, ANNOTATED)
    lines = out.split("\n")
    assert lines[0].startswith("/*@ requires x < 10; assigns \\nothing; */ int helper(int x);")
    assert "return x + 1" not in out and "return helper(1)" not in out  # both stubbed
    assert "loop invariant 0 <= i <= 1" in out and "int other(void);" in out
    argv = b._check_argv(tmp_path / "a.c", tu, "clear")
    assert "-wp-rte" in argv and argv[argv.index("-wp-fct") + 1] == "clear"
    extra = next(a for a in argv if a.startswith("-cpp-extra-args="))
    assert f"-I{tmp_path}/inc -DX=1" in extra  # include paths absolute: frama-c runs from .fver


def test_calls_before_a_local_definition_is_split() -> None:
    src = "int f(struct h *hooks) {\n  //@ calls malloc;\n  void *p = hooks->allocate(16);\n  return p != 0;\n}\n"
    out = acsl.split_calls_declarations(src)
    assert out == (
        "int f(struct h *hooks) {\n  void *p;\n  //@ calls malloc;\n  p = hooks->allocate(16);\n"
        "  return p != 0;\n}\n"
    )
    plain = "int g(void) {\n  int x = 1;\n  return x;\n}\n"
    assert acsl.split_calls_declarations(plain) == plain
    const = "int g(struct h *hooks) {\n  //@ calls malloc;\n  void *const p = hooks->allocate(1);\n  return 0;\n}\n"
    assert acsl.split_calls_declarations(const) == const  # const locals are left alone
