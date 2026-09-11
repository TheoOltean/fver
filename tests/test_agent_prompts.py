from pathlib import Path

from fver.agent import prompts
from fver.agent.client import Completion
from fver.agent.loop import HISTORY_KEEP, Conversation
from fver.backends.base import CheckOutcome, CheckResult, FunctionTask, Submission
from fver.core.models import Cost
from tests.test_agent_fakes import FakeBackend, make_repo


def _task(tmp_path):
    ws, ledger, _helper, _zero, use = make_repo(tmp_path)
    tu = ledger.get_tu("tu1")
    src = (ws.repo_root / "src/a.c").read_text()
    lines = src.split("\n")
    return FunctionTask(
        function=use,
        tu=tu,
        target=ws.config.target.to_target(),
        repo_root=ws.repo_root,
        workdir=tmp_path / "wd",
        source_text=src,
        function_text="\n".join(lines[use.start_line - 1 : use.end_line]),
        callee_specs={"zero": "[[fake::args(buf, n)]]"},
        external_specs={"memcpy": "spec of memcpy"},
        previous=Submission({"function.c": "OLD SUBMISSION"}),
    )


def test_system_blocks_order_and_content():
    b = FakeBackend()
    blocks = prompts.build_system(b.prompt_context(), b.submission_spec(), _target())
    assert blocks[0] == "FAKE REFERENCE"
    assert blocks[1] == "FAKE EXAMPLES"
    instr = blocks[2]
    assert "fake::trust_me" in instr and "Admitted" in instr
    assert "BUG:" in instr and "Never change the C code" in instr
    assert "file=function.c" in instr


def _target():
    from fver.core.models import Target

    return Target()


def test_task_message_includes_context(tmp_path):
    task = _task(tmp_path)
    ctx = prompts.select_file_context(task.source_text, task.function)
    msg = prompts.build_task_message(task, [], ctx)
    assert "prove `use`" in msg
    assert "[[fake::args(buf, n)]]" in msg  # callee contract
    assert "spec of memcpy" in msg  # external spec
    assert "`helper`" in msg  # callee without a contract is listed
    assert "OLD SUBMISSION" in msg  # previous accepted submission
    assert "int is 32 bits" in msg
    assert "1| #include" in ctx  # whole file when small


def test_file_context_large_file_uses_head_and_function():
    from fver.core.models import FunctionInfo

    lines = [f"// line {i}" for i in range(1, 1001)]
    lines[800] = "void f(void) {"
    lines[801] = "}"
    src = "\n".join(lines)
    fn = FunctionInfo(
        id="t:f",
        name="f",
        tu_id="t",
        source_path="x.c",
        start_line=801,
        end_line=802,
        signature="void f(void)",
        body_hash="h",
    )
    ctx = prompts.select_file_context(src, fn)
    assert "// line 1" in ctx and "void f(void) {" in ctx
    assert "// line 500" not in ctx
    assert "elided" in ctx


def test_feedback_message():
    r = CheckResult(CheckOutcome.GOALS_REMAIN, "goal output", goals=["0 <= i"])
    msg = prompts.build_feedback_message(r, 2, 3)
    assert (
        "Attempt 2" in msg
        and "goals_remain" in msg
        and "0 <= i" in msg
        and "3 attempt(s) remain" in msg
    )
    last = prompts.build_feedback_message(r, 5, 0)
    assert "last attempt" in last


def test_conversation_caps_history():
    convo = Conversation("TASK")
    for i in range(1, HISTORY_KEEP + 3):
        c = Completion(
            text=f"sub {i}", content_blocks=[{"type": "text", "text": f"sub {i}"}], cost=Cost()
        )
        convo.add(c, f"feedback {i}", f"attempt {i}: stuck")
    msgs = convo.render()
    assert msgs[0] == {"role": "user", "content": "TASK"}
    roles = [m["role"] for m in msgs]
    assert roles == ["user"] + ["assistant", "user"] * HISTORY_KEEP
    assert msgs[1]["content"][0]["text"] == f"sub {3}"  # oldest kept is attempt 3
    assert "attempt 1: stuck" in msgs[-1]["content"] and "attempt 2: stuck" in msgs[-1]["content"]
    assert msgs[-1]["role"] == "user"


def test_conversation_short_history_unchanged():
    convo = Conversation("TASK")
    c = Completion(text="s", content_blocks=[{"type": "text", "text": "s"}], cost=Cost())
    convo.add(c, "fb", "attempt 1")
    msgs = convo.render()
    assert len(msgs) == 3 and "elided" not in msgs[-1]["content"]


def test_path_unused():
    assert Path
