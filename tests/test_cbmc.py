from __future__ import annotations

from pathlib import Path

import pytest

from fver.core.models import FunctionInfo, TranslationUnit
from fver.prove import cbmc
from fver.util import proc

FIX = Path(__file__).parent / "fixtures" / "hunters"


def _functions(tu_id: str = "tu1") -> list[FunctionInfo]:
    return [
        FunctionInfo(
            f"{tu_id}:copy",
            "copy",
            tu_id,
            "src/buf.c",
            8,
            15,
            "void copy(char*,int)",
            "h1",
            attack_score=0.9,
        ),
        FunctionInfo(
            f"{tu_id}:add",
            "add",
            tu_id,
            "src/buf.c",
            18,
            22,
            "int add(int,int)",
            "h2",
            attack_score=0.1,
        ),
    ]


def _tu(repo: Path, with_main: bool = False, tu_id: str = "tu1") -> TranslationUnit:
    return TranslationUnit(
        tu_id,
        "src/buf.c",
        str(repo),
        [
            "clang",
            "-c",
            "-O2",
            "-Wall",
            "-Isrc",
            "-DFOO=1",
            "-std=c11",
            "-include",
            "cfg.h",
            "-o",
            "x.o",
            "src/buf.c",
        ],
    )


# --- base ---------------------------------------------------------------


def test_preprocessor_flags_keep_only_pp_flags(tmp_path):
    tu = _tu(tmp_path)
    assert cbmc.preprocessor_flags(tu.arguments) == [
        "-Isrc",
        "-DFOO=1",
        "-std=c11",
        "-include",
        "cfg.h",
    ]


def test_locator_maps_lines_to_functions():
    loc = cbmc.FunctionLocator(_functions())
    assert loc.locate("src/buf.c", 12).name == "copy"
    assert loc.locate("src/buf.c", 20).name == "add"
    assert loc.locate("src/buf.c", 16) is None
    assert loc.locate("other.c", 12) is None
    assert loc.by_name("add").id == "tu1:add"


# --- cbmc ---------------------------------------------------------------


def test_cbmc_json_parsing(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    text = (FIX / "cbmc_output.json").read_text().replace("/repo/", str(repo) + "/")
    recs = cbmc.parse_cbmc_json(text, repo, str(repo))
    kinds = [r["kind"] for r in recs]
    assert kinds == ["out_of_bounds", "signed_overflow", "bound_reached"]
    assert recs[0]["file"] == "src/buf.c" and recs[0]["line"] == 12
    assert (
        recs[1]["file"] == "src/buf.c" and recs[1]["line"] == 20
    )  # relative path resolved via tu dir
    assert "i = 4" in recs[0]["witness"] and "FAILURE" in recs[0]["witness"]


def test_cbmc_tolerates_garbage():
    assert cbmc.parse_cbmc_json("not json", Path("/x"), None) == []
    assert cbmc.parse_cbmc_json('noise\n[{"result": []}]', Path("/x"), None) == []


def test_cbmc_run_per_function_when_no_main(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "buf.c").write_text("int x;")
    calls: list[list[str]] = []
    text = (FIX / "cbmc_output.json").read_text().replace("/repo/", str(repo) + "/")

    def fake_run(argv, cwd=None, timeout=None, env=None, input_text=None):
        calls.append(argv)
        return proc.ProcResult(argv, 10, text, "", 0.1)

    monkeypatch.setattr(proc, "which", lambda n: "/usr/bin/cbmc")
    monkeypatch.setattr(proc, "run", fake_run)
    tu = _tu(repo)
    findings = cbmc.CbmcHunter().run([tu], _functions(), repo, tmp_path / "work")
    # two functions, no main -> two invocations, highest attack score first
    assert len(calls) == 2
    assert calls[0][-2:] == ["--function", "copy"]
    assert calls[1][-2:] == ["--function", "add"]
    assert "--unwind" in calls[0] and calls[0][calls[0].index("--unwind") + 1] == str(
        cbmc.CBMC_UNWIND
    )
    assert "-Isrc" in calls[0] and "-O2" not in calls[0]
    assert len(findings) == 6  # 3 failures x 2 runs
    assert findings[0].function_id == "tu1:copy" and findings[0].kind == "out_of_bounds"
    assert findings[1].function_id == "tu1:add" and findings[1].kind == "signed_overflow"
    assert (tmp_path / "work" / "tu1_copy.json").exists()


def test_cbmc_uses_main_when_present(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(proc, "which", lambda n: "/usr/bin/cbmc")
    monkeypatch.setattr(
        proc,
        "run",
        lambda argv, **kw: (calls.append(argv), proc.ProcResult(argv, 0, "[]", "", 0.0))[1],
    )
    fns = _functions() + [
        FunctionInfo("tu1:main", "main", "tu1", "src/buf.c", 30, 40, "int main(void)", "h3")
    ]
    cbmc.CbmcHunter().run([_tu(repo)], fns, repo, tmp_path / "work")
    assert len(calls) == 1 and "--function" not in calls[0]


def test_cbmc_absent_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "which", lambda n: None)
    assert cbmc.CbmcHunter().run([_tu(tmp_path)], _functions(), tmp_path, tmp_path / "w") == []
    monkeypatch.setattr(proc, "which", lambda n: "/usr/bin/cbmc")
    monkeypatch.setattr(
        proc, "run", lambda argv, **kw: proc.ProcResult(argv, -1, "", "", 1.0, timed_out=True)
    )
    assert cbmc.CbmcHunter().run([_tu(tmp_path)], _functions(), tmp_path, tmp_path / "w") == []


@pytest.mark.parametrize(
    "pclass,desc,kind",
    [
        ("x.array_bounds.1", "array 'a' lower bound", "out_of_bounds"),
        ("x.pointer_dereference.2", "dereference failure: pointer NULL", "null_or_invalid_deref"),
        ("x.division-by-zero.1", "division by zero in a / b", "division_by_zero"),
        ("x.undefined-shift.1", "shift distance too large", "undefined_shift"),
        ("x.unwind.0", "unwinding assertion loop 0", "bound_reached"),
    ],
)
def test_cbmc_classify(pclass, desc, kind):
    assert cbmc.classify(pclass, desc) == kind


def test_cbmc_flags_translate_and_drop():
    from fver.prove.cbmc import cbmc_flags

    argv = [
        "gcc",
        "-Wall",
        "-O2",
        "-std=c99",
        "-DLUA_USE_LINUX",
        "-fno-common",
        "-I",
        "inc",
        "-isystem",
        "sys",
        "-include",
        "pre.h",
        "-m64",
        "-c",
        "-o",
        "x.o",
        "x.c",
    ]
    assert cbmc_flags(argv) == [
        "--c99",
        "-DLUA_USE_LINUX",
        "-I",
        "inc",
        "-I",
        "sys",
        "--include",
        "pre.h",
        "--64",
    ]
    assert cbmc_flags(["gcc", "-std=gnu11", "-std=weird"]) == ["--c11"]


def test_cbmc_tool_error_detection():
    from fver.prove.cbmc import tool_error

    assert tool_error("Usage error!\n* * CBMC 6 * *\nUsage:\n cbmc [-?] --unknown option", "", 6)
    err_json = '[{"messageText": "PARSING ERROR", "messageType": "ERROR"}]'
    assert tool_error(err_json, "", 6) == "PARSING ERROR"
    ok_json = '[{"program": "CBMC"}, {"result": [], "messageType": "x"}]'
    assert tool_error(ok_json, "", 10) is None
    assert tool_error(ok_json, "", 0) is None


def test_missing_body_is_informational():
    from fver.prove.cbmc import UB_KINDS, classify

    kind = classify("pointer dereference", "no body for callee lua_pushlstring")
    assert kind == "missing_body" and kind not in UB_KINDS
