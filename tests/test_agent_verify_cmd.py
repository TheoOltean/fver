from fver.agent.client import FakeLLMClient
from fver.agent.loop import Verifier
from fver.commands.verify import _select
from fver.core.models import Status
from tests.test_agent_fakes import FakeBackend, make_ctx, make_repo, sub

ACCEPT = sub("void zero(char *buf, size_t n) { /* FVER_ACCEPT */ }")
STUCK = sub("void zero(char *buf, size_t n) { /* wrong */ }")


def test_select_statuses_filters_and_limit(tmp_path):
    ws, ledger, _helper, zero, use = make_repo(tmp_path)
    ctx = make_ctx(ws, ledger, FakeBackend())
    names = [f.name for f in _select(ctx, [], None, None, False, False)]
    assert names == [
        "zero",
        "helper",
        "use",
    ]  # helper is a callee of use: deps first  # attack-score order
    assert [f.name for f in _select(ctx, ["z*"], None, None, False, False)] == ["zero"]
    assert [f.name for f in _select(ctx, [], "src/a.c", 2, False, False)] == [
        "zero",
        "helper",
    ]  # deps first, then limit
    assert _select(ctx, [], "other.c", None, False, False) == []

    v = Verifier(ctx, FakeLLMClient(responses=[ACCEPT, STUCK] + [STUCK] * 3), run_id="r")
    assert v.verify_function(zero).claim.status is Status.VERIFIED
    assert v.verify_function(use).claim.status is Status.UNRESOLVED
    assert [f.name for f in _select(ctx, [], None, None, False, False)] == ["helper"]
    assert [f.name for f in _select(ctx, [], None, None, True, False)] == ["helper", "use"]
    assert [f.name for f in _select(ctx, [], None, None, False, True)] == ["zero"]
