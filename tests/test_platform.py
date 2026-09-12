"""Portability helpers: install hints per package manager, compiler/triple fallbacks."""

from __future__ import annotations

from fver.index import targets
from fver.util import platform as plat
from fver.util.proc import ProcResult


def _reset():
    plat.package_manager.cache_clear()


def _fake_run(responses):
    def run(argv, **kw):
        for key, (code, out) in responses.items():
            if key in argv:
                return ProcResult(argv, code, out, "", 0.0)
        return ProcResult(argv, 1, "", "unknown option", 0.0)

    return run


def test_detect_triple_prefers_clang_then_gcc(monkeypatch):
    monkeypatch.setattr(
        targets.proc, "run", _fake_run({"-print-target-triple": (0, "arm64-apple-darwin\n")})
    )
    assert targets.detect_triple("clang") == "arm64-apple-darwin"
    monkeypatch.setattr(targets.proc, "run", _fake_run({"-dumpmachine": (0, "x86_64-linux-gnu\n")}))
    assert targets.detect_triple("gcc") == "x86_64-linux-gnu"
    monkeypatch.setattr(targets.proc, "run", _fake_run({}))
    assert targets.detect_triple("tcc") == "unknown"


def test_detect_target_survives_all_probes_failing(monkeypatch):
    monkeypatch.setattr(targets.proc, "which", lambda n: "/usr/bin/cc")
    monkeypatch.setattr(targets.proc, "run", _fake_run({}))
    t = targets.detect_target("cc")
    assert t is not None and t.triple == "unknown" and t.compiler == "cc"
    assert targets.compare_target(t, t) == []
