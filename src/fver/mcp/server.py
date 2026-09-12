"""`fver mcp`: a stdio MCP server over the agent protocol.

Each tool is a thin wrapper over fver.agent.protocol, so an MCP client and
the CLI see exactly the same behaviour. The server is started inside a C
repository (or with FVER_REPO pointing at one); every call opens the
workspace fresh so the ledger is never stale.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fver.agent import protocol
from fver.core.context import AppContext

RULES = (
    "Rules: the C code may not be changed, only annotated; no escape hatches "
    "(no admitted goals, trusted functions or new axioms); the proof checker is "
    "the only oracle, so a submission is verified only when fver_check says so; "
    "feedback quotes the goal the checker could not close."
)


def _ctx(need_backend: bool = True) -> AppContext:
    start = Path(os.environ["FVER_REPO"]) if os.environ.get("FVER_REPO") else None
    return AppContext.load(start, need_backend=need_backend)


def _run(fn, need_backend: bool = True, **kwargs) -> str:
    """Open the workspace, call a protocol function, close, return JSON text."""
    try:
        ctx = _ctx(need_backend)
    except Exception as e:  # noqa: BLE001 - surface to the model as text
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    try:
        return json.dumps(fn(ctx, **kwargs), indent=2, sort_keys=True, default=str)
    except protocol.ProtocolError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    finally:
        ctx.close()


def build_server() -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="fver",
        instructions=(
            "fver proves C functions free of undefined behaviour. You are the prover: "
            "read fver_reference once, pick a function with fver_next, get its packet "
            "with fver_task, write the annotated function, and submit it with fver_check. "
            "Iterate on the feedback until verified. " + RULES
        ),
    )

    @server.tool(name="fver_reference")
    def fver_reference() -> str:
        """The backend's annotation language reference, worked examples, output format and rules. Read once per session before writing any submission."""
        return _run(protocol.reference)

    @server.tool(name="fver_status")
    def fver_status() -> str:
        """Summary of the ledger: functions by status, attack-weighted coverage, cost, findings."""
        return _run(protocol.status, need_backend=False)

    @server.tool(name="fver_next")
    def fver_next(limit: int = 10, file: str | None = None) -> str:
        """The next functions to prove, ordered by attack surface with unverified callees first so contracts exist before callers. Optional file filter (repo-relative glob)."""
        return _run(protocol.next_functions, limit=limit, file=file)

    @server.tool(name="fver_task")
    def fver_task(function: str, file: str | None = None) -> str:
        """Everything needed to write a submission for one function: its code, file context, callee contracts, external specs, the previous accepted submission if any, the required submission files, and the exact task prompt. Pass the file to disambiguate a name."""
        return _run(protocol.task, ident=function, file=file)

    @server.tool(name="fver_check")
    def fver_check(
        function: str, submission: str, lemmas: str | None = None, file: str | None = None
    ) -> str:
        """Submit an annotated function (the full definition, code unchanged) for checking. `submission` is the content of function.c; `lemmas` optionally the content of lemmas.v. Runs guardrails, the proof checker and the audit, records the outcome in the ledger, and returns kind (verified | feedback | guardrail | audit_failed | bug | tool_error), the checker feedback with the unsolved goal, and the status. Only a verified result counts. Write a line starting with BUG: instead of a submission if the code has a genuine defect no honest precondition can rule out."""
        stripped = submission.lstrip()
        if stripped.startswith("BUG:"):
            files = {"-": submission}
        else:
            files = {"function.c": submission}
            if lemmas:
                files["lemmas.v"] = lemmas
        return _run(protocol.check, ident=function, files=files, file=file)

    @server.tool(name="fver_changed")
    def fver_changed() -> str:
        """After editing C code: re-index quickly and list proofs that went stale plus unverified functions in modified files. Cheap; safe to call after every edit."""
        return _run(protocol.changed, need_backend=False)

    @server.tool(name="fver_show")
    def fver_show(function: str, file: str | None = None) -> str:
        """Ledger details for one function: status, claim history, assumptions, accepted submission, findings, callers."""
        return _run(protocol.show, ident=function, file=file, need_backend=False)

    @server.tool(name="fver_scan")
    def fver_scan(translate: bool = True) -> str:
        """Capture the build, index every function, and (with translate) run the backend front-end to learn what it can represent. Slow on large repos (minutes)."""
        return _run(protocol.scan, translate=translate)

    @server.tool(name="fver_hunt")
    def fver_hunt(file: str | None = None) -> str:
        """Run the bug finders (CBMC, and the sanitizers when hunters.test_command is set) over the indexed code, optionally one file. Concrete bugs become findings; slow on large repos."""
        return _run(protocol.hunt, file=file, need_backend=False)

    return server


def main() -> None:
    server = build_server()
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
