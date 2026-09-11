from __future__ import annotations

from pathlib import Path

from fver.backends.base import Backend, CheckOutcome, CheckResult, FunctionTask, Submission
from fver.backends.null import ACCEPT, AXIOM, BUG, CHEAT, GOALS, NullBackend
from fver.core.models import FunctionInfo, Target, TranslationUnit


def _task(tmp_path: Path, name: str = "f") -> FunctionTask:
    fn = FunctionInfo(
        id=f"t:{name}",
        name=name,
        tu_id="t",
        source_path="a.c",
        start_line=1,
        end_line=1,
        signature="int f(void)",
        body_hash="h",
    )
    tu = TranslationUnit(
        id="t", source_path="a.c", directory=str(tmp_path), arguments=["cc", "a.c"]
    )
    return FunctionTask(
        function=fn,
        tu=tu,
        target=Target(),
        repo_root=tmp_path,
        workdir=tmp_path / "wd",
        source_text="int f(void){return 0;}",
        function_text="int f(void){return 0;}",
        callee_specs={},
        external_specs={},
    )


def test_null_backend_outcomes(tmp_path: Path):
    b = NullBackend(tmp_path / "null", {}, Target())
    assert isinstance(b, Backend)
    t = _task(tmp_path)
    cases = {
        f"/* {ACCEPT} */ int f(void){{return 0;}}": CheckOutcome.OK,
        f"/* {GOALS} */ int f(void){{return 0;}}": CheckOutcome.GOALS_REMAIN,
        f"/* {BUG} */ int f(void){{return 0;}}": CheckOutcome.BUG,
        f"/* {CHEAT} */ int f(void){{return 0;}}": CheckOutcome.GUARDRAIL,
        "int f(void){return 0;}": CheckOutcome.AUTOMATION_STUCK,
    }
    for text, expected in cases.items():
        res = b.check(t, Submission(files={"function.c": text}), 10)
        assert res.outcome is expected, text
    ok = b.check(
        t, Submission(files={"function.c": f"/* {ACCEPT} */ int f(void){{return 0;}}"}), 10
    )
    assert ok.proof_hash and ok.assumptions == ["null-backend"]
    assert b.audit(t, ok).passed
    bad = CheckResult(outcome=CheckOutcome.OK, feedback="", artifacts={"x": str(tmp_path / "x")})
    (tmp_path / "x").write_text(AXIOM)
    assert not b.audit(t, bad).passed
    assert [c[0] for c in b.calls][:2] == ["check", "guardrail"]


def test_null_backend_translate_and_spec(tmp_path: Path):
    b = NullBackend(tmp_path / "null", {}, Target())
    t = _task(tmp_path)
    fns = [
        t.function,
        FunctionInfo(
            id="t:g_unsupported",
            name="g_unsupported",
            tu_id="t",
            source_path="a.c",
            start_line=2,
            end_line=2,
            signature="",
            body_hash="",
        ),
    ]
    res = b.translate(t.tu, fns, tmp_path)
    assert res.supported == {"f": True, "g_unsupported": False}
    assert (
        b.extract_spec(Submission(files={"function.c": "int f(void) { return 0; }"}), t.function)
        == "int f(void);"
    )
    assert b.submission_spec().required == ["function.c"]
