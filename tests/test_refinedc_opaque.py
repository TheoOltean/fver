"""Opaque floating point and the setjmp shim in the RefinedC backend (no tool)."""

from __future__ import annotations

from pathlib import Path

from fver.backends.base import FunctionTask, Submission
from fver.backends.refinedc import backend as mod
from fver.backends.refinedc import facts, opaque
from fver.backends.refinedc.backend import RefinedCBackend
from fver.core.models import FunctionInfo, Target, TranslationUnit

HEADER = (
    "typedef double lua_Number;\n"
    "typedef union Value { lua_Number n; long long i; void *p; } Value;\n"
    "typedef struct TValue { Value value_; int tt_; } TValue;\n"
    "float  scale(float x);  /* a double in a comment */\n"
    'static const char *name = "double float";\n'
    "long double big;\n"
    "int doubled(int x);\n"
)


def test_rewrite_replaces_type_specifiers_only():
    out = opaque.rewrite_floats(HEADER)
    assert "typedef struct fver_f64 lua_Number;" in out
    assert "struct fver_f32  scale(struct fver_f32 x);  /* a double in a comment */" in out
    assert '"double float"' in out  # literals untouched
    assert "struct fver_fld big;" in out
    assert "int doubled(int x);" in out  # identifiers containing the word are not touched
    assert out.count("\n") == HEADER.count("\n")  # line-preserving


def test_rewrite_is_idempotent_and_skips_block_comments():
    src = "/* double\n   float */ double d; // float\n"
    once = opaque.rewrite_floats(src)
    assert once == "/* double\n   float */ struct fver_f64 d; // float\n"
    assert opaque.rewrite_floats(once) == once


def test_mentions_float_ignores_comments_and_strings():
    assert opaque.mentions_float("double f(void);")
    assert not opaque.mentions_float('/* double */ const char *s = "float";')


def test_opaque_header_sizes_long_double_per_target():
    apple = opaque.opaque_header_text(Target(triple="arm64-apple-darwin25.6.0"))
    assert "struct fver_fld { long long fver_bits; };" in apple
    linux = opaque.opaque_header_text(Target(triple="x86_64-linux-gnu"))
    assert "_Alignas(16) unsigned char fver_bits[16];" in linux
    assert "struct fver_f64 { long long fver_bits; };" in linux


def test_explain_reason_adds_hint_once():
    msg = "line 4: Frontend error. invalid operands ('struct fver_f64' and 'struct fver_f64')"
    out = opaque.explain_reason(msg)
    assert out.startswith(msg) and opaque.FLOAT_HINT in out
    assert opaque.explain_reason(out) == out
    assert opaque.explain_reason("line 2: Not implemented: expr va_end") == (
        "line 2: Not implemented: expr va_end"
    )


def test_shadow_headers_mirror_rewritten_and_refresh(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src" / "sub").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "src" / "a.h").write_text("double x;\n")
    (repo / "src" / "sub" / "b.inc").write_text("float y;\n")
    (repo / "src" / "c.c").write_text("double z;\n")  # not a header
    (repo / ".git" / "d.h").write_text("double hidden;\n")
    shadow = tmp_path / "shadow"
    assert opaque.sync_shadow_headers(repo, shadow) == 2
    assert (shadow / "src" / "a.h").read_text() == "struct fver_f64 x;\n"
    assert (shadow / "src" / "sub" / "b.inc").read_text() == "struct fver_f32 y;\n"
    assert not (shadow / "src" / "c.c").exists() and not (shadow / ".git").exists()
    assert opaque.sync_shadow_headers(repo, shadow) == 0  # up to date
    import os
    import time

    (repo / "src" / "a.h").write_text("double x; double w;\n")
    future = time.time() + 5
    os.utime(repo / "src" / "a.h", (future, future))
    assert opaque.sync_shadow_headers(repo, shadow) == 1
    assert "struct fver_f64 w" in (shadow / "src" / "a.h").read_text()
    assert opaque.shadow_dir_for(repo / "src", repo, shadow) == shadow / "src"
    assert opaque.shadow_dir_for(Path("/usr/include"), repo, shadow) is None


def _backend(tmp_path: Path, **settings) -> RefinedCBackend:
    return RefinedCBackend(
        workspace_dir=tmp_path / "rc",
        settings={"refinedc_bin": "definitely-not-a-binary-xyz", **settings},
        target=Target(triple="x86_64-linux-gnu"),
    )


def _tu(repo: Path) -> TranslationUnit:
    return TranslationUnit(
        id="t1",
        source_path="src/l.c",
        directory=str(repo),
        arguments=["cc", "-Iinclude", "-I/usr/local/include", "-c", "src/l.c"],
    )


def test_cpp_flags_put_shadow_dirs_before_project_dirs(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    be = _backend(tmp_path)
    flags = be._cpp_flags(_tu(repo), repo)
    shadow = be._shadow_root
    real_src, real_inc = f"-I{repo / 'src'}", f"-I{repo / 'include'}"
    assert flags.index(f"-I{shadow / 'src'}") < flags.index(real_src)
    assert flags.index(f"-I{shadow / 'include'}") < flags.index(real_inc)
    assert flags.index(f"-I{shadow / 'include'}") < flags.index(real_src)  # all shadows first
    assert "-I/usr/local/include" in flags and not any("shadow/usr" in f for f in flags)
    be.opaque_floats = False
    assert not any(str(shadow) in f for f in be._cpp_flags(_tu(repo), repo))


def test_check_argv_force_includes_setjmp_shim_and_opaque_header(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    be = _backend(tmp_path)
    argv = be._check_argv(Path("x.c"), _tu(repo), repo, no_build=False)
    assert f"--include={mod._SHIMS_DIR / 'setjmp.h'}" in argv
    assert f"--include={be.workspace_dir / opaque.OPAQUE_HEADER}" in argv
    assert "setjmp.h" in facts.SHIMMED_HEADERS and "setjmp.h" in facts.FORCE_INCLUDED_SHIMS
    assert "_SETJMP_H_" in (mod._SHIMS_DIR / "setjmp.h").read_text()  # Cerberus's own guard
    be.opaque_floats = False
    argv = be._check_argv(Path("x.c"), _tu(repo), repo, no_build=False)
    assert not any(opaque.OPAQUE_HEADER in a for a in argv)


SRC = '#include "lobj.h"\nint tv_is_int(TValue *o) { return o->tt_ == 3; }\ndouble half(double x) { return x / 2; }\n'


def _task(be: RefinedCBackend, repo: Path) -> FunctionTask:
    fn = FunctionInfo(
        id="t1:tv_is_int",
        name="tv_is_int",
        tu_id="t1",
        source_path="src/l.c",
        start_line=2,
        end_line=2,
        signature="tv_is_int",
        body_hash="h",
    )
    return FunctionTask(
        function=fn,
        tu=_tu(repo),
        target=be.target,
        repo_root=repo,
        workdir=be.workspace_dir / "checks" / "tv",
        source_text=SRC,
        function_text=SRC.split("\n")[1] + "\n",
        callee_specs={},
        external_specs={},
    )


def test_translate_and_check_write_rewritten_copies(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lobj.h").write_text(HEADER)
    (repo / "src" / "l.c").write_text(SRC)
    be = _backend(tmp_path)
    res = be.translate(_tu(repo), [_task(be, repo).function], repo)
    copy = Path(res.artifacts["copy"]).read_text()
    assert "struct fver_f64 half(struct fver_f64 x)" in copy and "double" not in copy
    assert (be.workspace_dir / opaque.OPAQUE_HEADER).exists()
    assert "typedef struct fver_f64 lua_Number;" in (be._shadow_root / "src" / "lobj.h").read_text()
    # The user's files are untouched.
    assert "typedef double lua_Number;" in (repo / "src" / "lobj.h").read_text()
    task = _task(be, repo)
    sub = Submission(files={"function.c": '[[rc::returns("int<i32>")]]\n' + task.function_text})
    out = be.check(task, sub, timeout_seconds=5)  # tool missing: builds the source, then stops
    spliced = Path(out.artifacts["source"]).read_text()
    assert "struct fver_f64 half(struct fver_f64 x);" in spliced  # stubbed sibling, rewritten
    assert "double" not in spliced
    # Submissions are compared against the *original* code, so the guardrail still holds.
    bad = Submission(files={"function.c": "int tv_is_int(TValue *o) { return o->tt_ == 4; }\n"})
    assert "code changed" in be.check(task, bad, timeout_seconds=5).feedback
