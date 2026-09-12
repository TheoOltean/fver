from __future__ import annotations

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from fver.cli import app
from fver.core.config import FverConfig, load_config
from fver.core.models import Finding, FunctionInfo, Status, Target, TranslationUnit
from fver.core.workspace import Workspace
from fver.prove import hunt

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "a.c").write_text("int f(void){return 1;}")
    (d / "Makefile").write_text("all:\n\tcc src/a.c\n")
    monkeypatch.chdir(d)
    return d


# --- init ---------------------------------------------------------------


def test_init_creates_layout_and_config(repo):
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    ws = Workspace.open(repo)
    for d in ("proofs", "external", "work", "backend", "cache", "logs"):
        assert (ws.root / d).is_dir()
    assert "config.toml" in (ws.root / ".gitignore").read_text()
    cfg = load_config(repo)
    assert cfg.project.backend == "framac" and ws.project_name == "proj"
    # The file shows the few knobs a user touches, at their defaults, with every
    # other key and its default in the header comment.
    text = (ws.root / "config.toml").read_text()
    for key in ('api_key = ""', "effort", "max_usd_per_run", "max_usd_per_function"):
        assert key in text, key
    assert text.startswith("# fver configuration for this project")
    assert "#   max_attempts_per_function = 8" in text and "\n[project]" not in text
    assert cfg.model.api_key is None  # "" counts as unset
    # user tree untouched apart from .fver
    assert sorted(p.name for p in repo.iterdir()) == [".fver", "Makefile", "src"]
    # A second init leaves the config alone.
    (ws.root / "config.toml").write_text(text.replace('api_key = ""', 'api_key = "sk-x"'))
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert load_config(repo).model.api_key == "sk-x"


def test_init_with_null_backend_and_bad_config_values(repo):
    assert runner.invoke(app, ["init", "--backend", "null"]).exit_code == 0
    assert load_config(repo).project.backend == "null"
    cfg_path = repo / ".fver" / "config.toml"
    cfg_path.write_text(
        cfg_path.read_text().replace("max_usd_per_run = 200.0", 'max_usd_per_run = "many"')
    )
    with pytest.raises(ValidationError):
        load_config(repo)


class FakeLedger:
    def __init__(self, tus, functions):
        self.tus = {t.id: t for t in tus}
        self.functions = functions
        self.findings: list[Finding] = []
        self.claims = []
        self.runs = []

    def list_functions(
        self, backend, target_key, status=None, order_by_attack_score=True, limit=None
    ):
        from fver.ledger.api import FunctionRow

        return [FunctionRow(f, Status.NOT_ATTEMPTED, None) for f in self.functions]

    def get_tu(self, tu_id):
        return self.tus.get(tu_id)

    def start_run(self, command, backend, target_key, meta):
        self.runs.append(("start", command))
        return "run1"

    def end_run(self, run_id, ok, message=""):
        self.runs.append(("end", ok, message))

    def delete_findings(self, source_paths, tool=None):
        before = len(self.findings)
        self.findings = [
            f
            for f in self.findings
            if not (f.source_path in source_paths and (tool is None or f.tool == tool))
        ]
        return before - len(self.findings)

    def record_finding(self, f):
        self.findings.append(f)

    def record_claim(self, c):
        self.claims.append(c)

    def close(self):
        pass


class FakeCtx:
    def __init__(self, ws, ledger):
        self.ws = ws
        self.ledger = ledger
        self.config = FverConfig()
        self.target = Target()
        self.backend_name = "null"


class FakeHunter:
    name = "cbmc"

    def run(self, tus, functions, repo_root, workdir):
        return [
            Finding(functions[0].id, functions[0].source_path, 12, "out_of_bounds", "cbmc", "oob"),
            Finding(
                functions[0].id, functions[0].source_path, 11, "bound_reached", "cbmc", "unwind"
            ),
            Finding(None, "src/other.c", None, "signed_overflow", "cbmc", "no fn"),
        ]


def test_hunt_records_findings_and_bug_claims(repo, monkeypatch):
    runner.invoke(app, ["init", "--backend", "null"])
    ws = Workspace.open(repo)
    tu = TranslationUnit("tu1", "src/a.c", str(repo), ["clang", "src/a.c"])
    fns = [FunctionInfo("tu1:f", "f", "tu1", "src/a.c", 1, 1, "int f(void)", "bh")]
    ledger = FakeLedger([tu], fns)
    monkeypatch.setattr(hunt, "CbmcHunter", FakeHunter)
    findings = hunt.run_hunt(FakeCtx(ws, ledger), None, None, None)
    assert len(findings) == 3 and len(ledger.findings) == 3
    assert all(f.run_id == "run1" for f in ledger.findings)
    assert len(ledger.claims) == 1
    c = ledger.claims[0]
    assert c.status is Status.BUG_FOUND and c.function_id == "tu1:f" and c.body_hash == "bh"
    assert (
        c.message == "cbmc: out_of_bounds at line 12"
        and c.extra["finding"]["kind"] == "out_of_bounds"
    )
    assert ledger.runs == [("start", "hunt"), ("end", True, "3 finding(s)")]


def test_hunt_without_scan_tells_user(repo):
    runner.invoke(app, ["init", "--backend", "null"])
    ws = Workspace.open(repo)
    assert hunt.run_hunt(FakeCtx(ws, FakeLedger([], [])), None, None, None) == []


def test_hunt_filters_by_function_and_file(repo):
    runner.invoke(app, ["init", "--backend", "null"])
    ws = Workspace.open(repo)
    tu = TranslationUnit("tu1", "src/a.c", str(repo), ["clang", "src/a.c"])
    fns = [
        FunctionInfo("tu1:f", "f", "tu1", "src/a.c", 1, 1, "int f(void)", "bh"),
        FunctionInfo("tu1:g", "g", "tu1", "src/a.c", 2, 2, "int g(void)", "bh2"),
    ]
    ctx = FakeCtx(ws, FakeLedger([tu], fns))
    tus, functions = hunt._load_index(ctx, "g", None)
    assert [f.name for f in functions] == ["g"] and [t.id for t in tus] == ["tu1"]
    tus, functions = hunt._load_index(ctx, None, "src/none.c")
    assert functions == [] and tus == []


def test_log_records_from_child_loggers_carry_the_command_tag(tmp_path):
    import logging

    from fver.util.log import LOG_FILE, setup_logging

    setup_logging(tmp_path, run_name="prove", console=False)
    logging.getLogger("fver.some.child").info("hello from a child")
    for h in logging.getLogger("fver").handlers:
        h.flush()
    text = (tmp_path / LOG_FILE).read_text()
    assert "[prove] fver.some.child: hello from a child" in text
