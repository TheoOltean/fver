"""MCP server: tool registry maps onto the protocol functions."""

from __future__ import annotations

import asyncio
import json

from fver.mcp import server as srv

EXPECTED = {
    "fver_reference",
    "fver_status",
    "fver_next",
    "fver_task",
    "fver_check",
    "fver_changed",
    "fver_show",
    "fver_scan",
    "fver_hunt",
}


def test_tool_registry():
    s = srv.build_server()
    tools = asyncio.run(s.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED <= names
    for t in tools:
        assert t.description and len(t.description) > 20


def test_tools_call_protocol_functions(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class Ctx:
        def close(self):
            pass

    monkeypatch.setattr(srv, "_ctx", lambda need_backend=True: Ctx())
    for name in (
        "reference",
        "status",
        "next_functions",
        "task",
        "check",
        "changed",
        "show",
        "scan",
        "hunt",
    ):
        monkeypatch.setattr(
            srv.protocol,
            name,
            (lambda n: lambda ctx, **kw: calls.append((n, kw)) or {"ok": n})(name),
        )
    s = srv.build_server()

    async def go():
        out = {}
        out["next"] = await s.call_tool("fver_next", {"limit": 3})
        out["task"] = await s.call_tool("fver_task", {"function": "f"})
        out["check"] = await s.call_tool(
            "fver_check", {"function": "f", "submission": "int f(void){}", "lemmas": "Lemma x."}
        )
        out["bug"] = await s.call_tool(
            "fver_check", {"function": "f", "submission": "BUG: overflow"}
        )
        out["changed"] = await s.call_tool("fver_changed", {})
        return out

    asyncio.run(go())
    kinds = [c[0] for c in calls]
    assert kinds == ["next_functions", "task", "check", "check", "changed"]
    assert calls[0][1] == {"limit": 3, "file": None}
    assert calls[2][1]["files"] == {"function.c": "int f(void){}", "lemmas.v": "Lemma x."}
    assert calls[3][1]["files"] == {"-": "BUG: overflow"}


def test_run_reports_errors_as_json(monkeypatch):
    def boom(need_backend=True):
        raise RuntimeError("no workspace")

    monkeypatch.setattr(srv, "_ctx", boom)
    out = json.loads(srv._run(srv.protocol.status, need_backend=False))
    assert "error" in out and "no workspace" in out["error"]
