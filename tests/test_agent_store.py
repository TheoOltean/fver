from fver.backends.base import CheckOutcome, CheckResult, FunctionTask, Submission
from fver.core.models import Status, Target
from fver.prove import store
from tests.test_agent_fakes import make_repo


def _task(ws, ledger, fn):
    return FunctionTask(
        function=fn,
        tu=ledger.get_tu(fn.tu_id),
        target=Target(),
        repo_root=ws.repo_root,
        workdir=ws.work_dir,
        source_text="",
        function_text="",
        callee_specs={},
        external_specs={},
    )


def test_save_and_load_round_trip(tmp_path):
    ws, ledger, _helper, zero, _use = make_repo(tmp_path)
    task = _task(ws, ledger, zero)
    sub = Submission({"function.c": "A\n", "extra.v": "B\n"}, note="why")
    res = CheckResult(
        CheckOutcome.OK, "", proof_hash="p1", assumptions=["x"], tool_versions={"t": "1"}
    )
    d = store.save_accepted(ws, task, sub, res, "key1", "fake")
    assert (d / "function.c").read_text() == "A\n"
    loaded = store.load_accepted(ws, "src/a.c", "zero")
    assert loaded is not None
    got, rec = loaded
    assert got.files == sub.files and got.note == "why"
    assert rec["proof_hash"] == "p1" and rec["cache_key"] == "key1" and rec["backend"] == "fake"
    # saving again removes stale files
    store.save_accepted(ws, task, Submission({"function.c": "C\n"}), res, "key2", "fake")
    assert not (d / "extra.v").exists()
    assert store.load_accepted(ws, "src/a.c", "zero")[0].files == {"function.c": "C\n"}


def test_cache_round_trip(tmp_path):
    ws, _ledger, _helper, _zero, _use = make_repo(tmp_path)
    assert store.cache_lookup(ws, "nokey") is None
    res = CheckResult(CheckOutcome.OK, "", proof_hash="p", assumptions=["a"])
    store.cache_insert(ws, "k", Status.VERIFIED, Submission({"function.c": "Z\n"}), res)
    rec = store.cache_lookup(ws, "k")
    assert rec["status"] == "verified" and rec["submission"] == {"function.c": "Z\n"}
    assert rec["proof_hash"] == "p" and rec["assumptions"] == ["a"]


def test_hash_specs_stable():
    a = store.hash_specs({"f": "x", "g": "y"})
    assert a == store.hash_specs({"g": "y", "f": "x"})
    assert a != store.hash_specs({"f": "x2", "g": "y"})


def test_missing_accepted_returns_none(tmp_path):
    ws, *_ = make_repo(tmp_path)
    assert store.load_accepted(ws, "src/a.c", "nope") is None
