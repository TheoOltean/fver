"""End-to-end checks against a real RefinedC install.

Opt in with FVER_REFINEDC_BIN=/path/to/refinedc (coqc and dune are expected
next to it). Each check takes a few seconds; the whole file about a minute.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from fver.backends.base import CheckOutcome, Submission
from fver.backends.refinedc.backend import RefinedCBackend
from fver.core.models import FunctionInfo, Target, TranslationUnit

BIN = os.environ.get("FVER_REFINEDC_BIN")
pytestmark = pytest.mark.skipif(not BIN, reason="set FVER_REFINEDC_BIN to run")

SRC = textwrap.dedent(
    """\
    #include <stddef.h>
    #include "helper.h"

    int inc(int x) {
      return x + 1;
    }

    int twice(int y) {
      int a = inc(y);
      return inc(a);
    }

    void set_shared(int *p) {
      *p = 1;
    }
    """
)
HDR = "int inc(int x);\nint twice(int y);\n"

INC_OK = textwrap.dedent(
    """\
    [[rc::parameters("x : Z")]]
    [[rc::args("x @ int<i32>")]]
    [[rc::requires("{x < max_int i32}")]]
    [[rc::returns("{x + 1} @ int<i32>")]]
    int inc(int x) {
      return x + 1;
    }
    """
)
TWICE_OK = textwrap.dedent(
    """\
    [[rc::parameters("y : Z")]]
    [[rc::args("y @ int<i32>")]]
    [[rc::requires("{y < max_int i32 - 1}")]]
    [[rc::returns("{y + 2} @ int<i32>")]]
    int twice(int y) {
      int a = inc(y);
      return inc(a);
    }
    """
)
SET_SHARED_WRONG = textwrap.dedent(
    """\
    [[rc::parameters("p : loc", "x : Z")]]
    [[rc::args("p @ &shr<x @ int<i32>>")]]
    [[rc::returns("void")]]
    void set_shared(int *p) {
      *p = 1;
    }
    """
)
INC_NO_PRECOND = INC_OK.replace('[[rc::requires("{x < max_int i32}")]]\n', "")


def _lines(text: str, name: str) -> tuple[int, int]:
    lines = text.split("\n")
    start = next(
        i + 1 for i, ln in enumerate(lines) if ln.startswith(("int " + name, "void " + name))
    )
    end = next(i + 1 for i in range(start - 1, len(lines)) if lines[i] == "}")
    return start, end


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    r = tmp_path_factory.mktemp("repo")
    (r / "src").mkdir()
    (r / "src" / "m.c").write_text(SRC)
    (r / "src" / "helper.h").write_text(HDR)
    return r


@pytest.fixture(scope="module")
def backend(tmp_path_factory) -> RefinedCBackend:
    ws = tmp_path_factory.mktemp("ws")
    bindir = Path(BIN).resolve().parent
    return RefinedCBackend(
        workspace_dir=ws,
        settings={
            "refinedc_bin": BIN,
            "coqc_bin": str(bindir / "coqc"),
            "dune_bin": str(bindir / "dune"),
        },
        target=Target(),
    )


@pytest.fixture(scope="module")
def tu(repo: Path) -> TranslationUnit:
    return TranslationUnit(
        id="abc123",
        source_path="src/m.c",
        directory=str(repo),
        arguments=["cc", "-Isrc", "-c", "src/m.c"],
    )


def _fn(name: str, tu: TranslationUnit, callees=()) -> FunctionInfo:
    s, e = _lines(SRC, name)
    return FunctionInfo(
        id=f"{tu.id}:{name}",
        name=name,
        tu_id=tu.id,
        source_path=tu.source_path,
        start_line=s,
        end_line=e,
        signature=name,
        body_hash="h" + name,
        callees=list(callees),
    )


def _task(backend, repo, tu, fn, callee_specs=None):
    from fver.backends.base import FunctionTask

    wd = backend.workspace_dir / "checks" / fn.name
    return FunctionTask(
        function=fn,
        tu=tu,
        target=Target(),
        repo_root=repo,
        workdir=wd,
        source_text=SRC,
        function_text="\n".join(SRC.split("\n")[fn.start_line - 1 : fn.end_line]) + "\n",
        callee_specs=callee_specs or {},
        external_specs={},
    )


def test_doctor_and_versions(backend):
    rows = {r.name: r for r in backend.doctor()}
    assert rows["refinedc"].found and rows["coqc"].found and rows["dune"].found
    assert "refinedc" in backend.tool_versions()


def test_prepare_and_translate(backend, repo, tu):
    backend.prepare([tu], repo)
    assert backend.project_file.exists()
    fns = [_fn("inc", tu), _fn("twice", tu, ["inc"]), _fn("set_shared", tu)]
    res = backend.translate(tu, fns, repo)
    assert res.tu_error is None, res.tu_error
    assert all(res.supported.values())


def test_translate_reports_unsupported_construct(backend, repo, tu, tmp_path):
    bad = (
        SRC
        + "\n#include <stdarg.h>\nint va(int n, ...) { va_list ap; va_start(ap, n); va_end(ap); return n; }\n"
    )
    (repo / "src" / "bad.c").write_text(bad)
    tub = TranslationUnit(
        id="bad1", source_path="src/bad.c", directory=str(repo), arguments=["cc", "-c", "src/bad.c"]
    )
    lines = bad.split("\n")
    va_line = next(i + 1 for i, ln in enumerate(lines) if ln.startswith("int va("))
    fns = [
        _fn("inc", tub),
        FunctionInfo(
            id="bad1:va",
            name="va",
            tu_id="bad1",
            source_path="src/bad.c",
            start_line=va_line,
            end_line=va_line,
            signature="va",
            body_hash="hva",
        ),
    ]
    res = backend.translate(tub, fns, repo)
    assert res.supported["va"] is False and "Not implemented" in res.reasons["va"]
    assert res.supported["inc"] is True


def test_check_ok_then_audit(backend, repo, tu):
    fn = _fn("inc", tu)
    task = _task(backend, repo, tu, fn)
    res = backend.check(task, Submission(files={"function.c": INC_OK}), timeout_seconds=600)
    assert res.outcome is CheckOutcome.OK, res.feedback
    assert res.proof_hash and "generated_proof_inc.v" in res.artifacts
    audit = backend.audit(task, res)
    assert audit.passed, audit.violations
    assert any(a.startswith("refinedc:") for a in audit.assumptions)
    assert not any(a.startswith("axiom:") for a in audit.assumptions)


def test_check_goals_remain_quotes_goal_and_source_line(backend, repo, tu):
    fn = _fn("inc", tu)
    task = _task(backend, repo, tu, fn)
    res = backend.check(task, Submission(files={"function.c": INC_NO_PRECOND}), timeout_seconds=600)
    assert res.outcome is CheckOutcome.GOALS_REMAIN, res.feedback
    assert "max_int i32" in res.feedback
    assert "return x + 1" in res.feedback  # mapped back to the source line


def test_check_stuck_on_wrong_ownership(backend, repo, tu):
    fn = _fn("set_shared", tu)
    task = _task(backend, repo, tu, fn)
    res = backend.check(
        task, Submission(files={"function.c": SET_SHARED_WRONG}), timeout_seconds=600
    )
    assert res.outcome is CheckOutcome.AUTOMATION_STUCK, res.feedback
    assert "Shr" in res.feedback or "&shr" in res.feedback


def test_check_rejects_escape_hatch_and_code_change_without_tool(backend, repo, tu):
    fn = _fn("inc", tu)
    task = _task(backend, repo, tu, fn)
    bad = "[[rc::trust_me]]\n" + INC_OK
    res = backend.check(task, Submission(files={"function.c": bad}), timeout_seconds=600)
    assert res.outcome is CheckOutcome.GUARDRAIL and "trust_me" in res.feedback
    changed = INC_OK.replace("x + 1", "x + 2")
    res = backend.check(task, Submission(files={"function.c": changed}), timeout_seconds=600)
    assert res.outcome is CheckOutcome.GUARDRAIL and "code changed" in res.feedback


def test_caller_checked_against_callee_contract(backend, repo, tu):
    inc = _fn("inc", tu)
    contract = backend.extract_spec(Submission(files={"function.c": INC_OK}), inc)
    assert contract.rstrip().endswith("int inc(int x);")
    fn = _fn("twice", tu, ["inc"])
    task = _task(backend, repo, tu, fn, callee_specs={"inc": contract})
    res = backend.check(task, Submission(files={"function.c": TWICE_OK}), timeout_seconds=600)
    assert res.outcome is CheckOutcome.OK, res.feedback
    assert "callee:inc" in res.assumptions
    # The spliced file must carry the prototype and not inc's body.
    spliced = Path(res.artifacts["source"]).read_text()
    assert "int inc(int x);" in spliced and spliced.count("return x + 1") == 0


def test_target_verifies_despite_unsupported_sibling(backend, repo, tu):
    """A varargs function in the same file must not block verifying `inc`."""
    src = (
        SRC
        + "\n#include <stdarg.h>\nint va(int n, ...) { va_list ap; va_start(ap, n); va_end(ap); return n; }\n"
    )
    (repo / "src" / "sib.c").write_text(src)
    tus = TranslationUnit(
        id="sib1", source_path="src/sib.c", directory=str(repo), arguments=["cc", "-c", "src/sib.c"]
    )
    s, e = _lines(src, "inc")
    fn = FunctionInfo(
        id="sib1:inc",
        name="inc",
        tu_id="sib1",
        source_path="src/sib.c",
        start_line=s,
        end_line=e,
        signature="inc",
        body_hash="hinc",
    )
    from fver.backends.base import FunctionTask

    task = FunctionTask(
        function=fn,
        tu=tus,
        target=Target(),
        repo_root=repo,
        workdir=backend.workspace_dir / "checks" / "sib",
        source_text=src,
        function_text="\n".join(src.split("\n")[s - 1 : e]) + "\n",
        callee_specs={},
        external_specs={},
    )
    res = backend.check(task, Submission(files={"function.c": INC_OK}), timeout_seconds=600)
    assert res.outcome is CheckOutcome.OK, res.feedback
    spliced = Path(res.artifacts["source"]).read_text()
    assert "int va(int n, ...);" in spliced and "va_start" not in spliced


def test_posix_includes_parse_with_shims(backend, repo, tu):
    src = "#include <sys/types.h>\n#include <unistd.h>\n#include <sys/wait.h>\n#include <dlfcn.h>\nint g(int x) { return x; }\n"
    (repo / "src" / "px.c").write_text(src)
    tup = TranslationUnit(
        id="px1", source_path="src/px.c", directory=str(repo), arguments=["cc", "-c", "src/px.c"]
    )
    fn = FunctionInfo(
        id="px1:g",
        name="g",
        tu_id="px1",
        source_path="src/px.c",
        start_line=5,
        end_line=5,
        signature="g",
        body_hash="hg",
    )
    res = backend.translate(tup, [fn], repo)
    assert res.tu_error is None and res.supported == {"g": True}, res.tu_error


LUA_LIKE_HDR = textwrap.dedent(
    """\
    #ifndef LOBJ_H
    #define LOBJ_H
    typedef double lua_Number;
    typedef long long lua_Integer;
    typedef union Value { lua_Number n; lua_Integer i; void *p; } Value;
    typedef struct TValue { Value value_; int tt_; } TValue;
    #define ttisint(o) ((o)->tt_ == 3)
    #endif
    """
)
LUA_LIKE_SRC = textwrap.dedent(
    """\
    #include <setjmp.h>
    #include "lobj.h"
    int tv_is_int(TValue *o) { return ttisint(o); }
    lua_Number fadd(lua_Number a, lua_Number b) { return a + b; }
    int fcmp(lua_Number a, lua_Number b) { return a < b; }
    void tv_setflt(TValue *o, lua_Number d) { o->value_.n = d; o->tt_ = 19; }
    lua_Number passthru(lua_Number a) { return a; }
    int guarded(jmp_buf *jb) { if (setjmp(*jb) == 0) return 0; return 1; }
    int plain(int x) { return x; }
    """
)
TV_IS_INT_OK = textwrap.dedent(
    """\
    [[rc::parameters("t : Z")]]
    [[rc::args("&own<struct<struct_TValue, uninit<union_Value>, t @ int<i32>>>")]]
    [[rc::returns("{bool_to_Z (bool_decide (t = 3))} @ int<i32>")]]
    int tv_is_int(TValue *o) { return ttisint(o); }
    """
)


def _lua_like(backend, repo):
    (repo / "src" / "lobj.h").write_text(LUA_LIKE_HDR)
    (repo / "src" / "lua_like.c").write_text(LUA_LIKE_SRC)
    tul = TranslationUnit(
        id="lua1",
        source_path="src/lua_like.c",
        directory=str(repo),
        arguments=["cc", "-Isrc", "-c", "src/lua_like.c"],
    )
    lines = LUA_LIKE_SRC.split("\n")
    fns = []
    for i, ln in enumerate(lines, start=1):
        if ln and not ln.startswith("#"):
            name = ln.split("(")[0].split()[-1].lstrip("*")
            fns.append(
                FunctionInfo(
                    id=f"lua1:{name}",
                    name=name,
                    tu_id="lua1",
                    source_path="src/lua_like.c",
                    start_line=i,
                    end_line=i,
                    signature=name,
                    body_hash="h" + name,
                )
            )
    return tul, fns


def test_opaque_floats_keep_the_file_and_isolate_float_arithmetic(backend, repo, tu):
    """A struct holding a double and a <setjmp.h> include used to reject the
    whole file. Now only functions that compute with floats are unsupported."""
    tul, fns = _lua_like(backend, repo)
    res = backend.translate(tul, fns, repo)
    assert res.tu_error is None, res.tu_error
    assert res.supported == {
        "tv_is_int": True,
        "fadd": False,
        "fcmp": False,
        "tv_setflt": True,
        "passthru": True,
        "guarded": True,
        "plain": True,
    }
    for n in ("fadd", "fcmp"):
        assert "floating-point" in res.reasons[n], res.reasons[n]
    assert (
        "typedef struct fver_f64 lua_Number;"
        in (backend._shadow_root / "src" / "lobj.h").read_text()
    )
    assert "typedef double lua_Number;" in (repo / "src" / "lobj.h").read_text()


def test_tag_read_on_tagged_value_with_opaque_float_verifies(backend, repo, tu):
    from fver.backends.base import FunctionTask

    tul, fns = _lua_like(backend, repo)
    fn = next(f for f in fns if f.name == "tv_is_int")
    task = FunctionTask(
        function=fn,
        tu=tul,
        target=Target(),
        repo_root=repo,
        workdir=backend.workspace_dir / "checks" / "lua_tv",
        source_text=LUA_LIKE_SRC,
        function_text=LUA_LIKE_SRC.split("\n")[fn.start_line - 1] + "\n",
        callee_specs={},
        external_specs={},
    )
    res = backend.check(task, Submission(files={"function.c": TV_IS_INT_OK}), timeout_seconds=600)
    assert res.outcome is CheckOutcome.OK, res.feedback
    spliced = Path(res.artifacts["source"]).read_text()
    assert "double" not in spliced and "setjmp" in spliced  # include kept, floats rewritten
