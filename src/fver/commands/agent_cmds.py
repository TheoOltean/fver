"""External-prover commands: task, check, next, changed.

These let something other than fver's own API loop do the proving: a Claude
Code session, a script, a person. JSON in/out is stable for scripting; the
plain output is meant to be read by a model or a human.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from fver.agent import protocol
from fver.core.context import AppContext
from fver.util.log import console, err_console


def _dump(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _fail(msg: str, code: int = 1) -> None:
    err_console.print(f"[red]{msg}[/]")
    raise typer.Exit(code=code)


agent_app = typer.Typer(
    help="Session-mode primitives (what `fver mcp` exposes): task, check, next, changed.",
    no_args_is_help=True,
)


def register(app: typer.Typer) -> None:
    app.add_typer(agent_app, name="agent", hidden=True)
    _register(agent_app)


def _register(app: typer.Typer) -> None:
    @app.command("task")
    def task(
        function: str = typer.Argument(..., help="Function name or ledger id."),
        file: str | None = typer.Option(None, "--file", help="Source path, to disambiguate."),
        as_json: bool = typer.Option(False, "--json", help="Emit one JSON object."),
        reference: bool | None = typer.Option(
            None,
            "--reference/--no-reference",
            help="Include the language reference in plain output (config: verify.task_reference).",
        ),
    ) -> None:
        """Print the proving packet for one function: reference, code, context, contracts, prompt."""
        ctx = AppContext.load(need_backend=True)
        if reference is None:
            reference = ctx.config.verify.task_reference
        try:
            d = protocol.task(ctx, function, file)
            if as_json:
                _dump(d)
            else:
                typer.echo(protocol.render_task_text(ctx, d, with_reference=reference))
        except protocol.ProtocolError as e:
            _fail(str(e))
        finally:
            ctx.close()

    @app.command("check")
    def check(
        function: str = typer.Argument(..., help="Function name or ledger id."),
        submission: str = typer.Option(
            ..., "--submission", "-s", help="Path to the annotated function.c, or - for stdin."
        ),
        lemmas: str | None = typer.Option(
            None, "--lemmas", help="Optional helper-lemma file (lemmas.v)."
        ),
        file: str | None = typer.Option(None, "--file", help="Source path, to disambiguate."),
        as_json: bool = typer.Option(False, "--json", help="Emit one JSON object."),
    ) -> None:
        """Check a submission against the proof checker and record the outcome.

        Exit code 0 = verified, 1 = not verified, 2 = tool error. With
        `--submission -` the text on stdin may be the fenced-block reply format.
        """
        ctx = AppContext.load(need_backend=True)
        try:
            if submission == "-":
                files = {"-": sys.stdin.read()}
            else:
                p = Path(submission)
                if not p.exists():
                    _fail(f"no such file: {submission}")
                files = {"function.c": p.read_text(encoding="utf-8", errors="replace")}
                if lemmas:
                    files["lemmas.v"] = Path(lemmas).read_text(encoding="utf-8", errors="replace")
            d = protocol.check(ctx, function, files, file)
        except protocol.ProtocolError as e:
            _fail(str(e))
        finally:
            ctx.close()
        if as_json:
            _dump(d)
        else:
            kind = d["kind"]
            colour = {"verified": "green", "bug": "red", "tool_error": "red"}.get(kind, "yellow")
            console.print(f"[{colour}]{kind}[/]  {d['function']['name']}  status={d.get('status')}")
            if d.get("feedback"):
                typer.echo(d["feedback"])
            for v in d.get("violations", []):
                typer.echo(f"- {v}")
            if kind == "verified":
                typer.echo(f"proof saved under {d.get('proof_dir')}")
        raise typer.Exit(code=int(d.get("exit_code", 1)))

    @app.command("next")
    def next_(
        limit: int | None = typer.Option(
            None, "--limit", "-n", help="How many (config: verify.next_limit)."
        ),
        file: str | None = typer.Option(None, "--file", help="Only this source file (glob)."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """The next functions to prove, callees before callers, highest attack surface first."""
        ctx = AppContext.load(need_backend=True)
        if limit is None:
            limit = ctx.config.verify.next_limit
        try:
            d = protocol.next_functions(ctx, limit, file)
        finally:
            ctx.close()
        if as_json:
            _dump(d)
            return
        if not d["functions"]:
            console.print("Nothing to do. Run `fver scan` first, or everything is verified.")
            return
        for f in d["functions"]:
            typer.echo(
                f"{f['attack_score']:.2f}  {f['name']:32} {f['file']}:{f['lines'][0]}  "
                f"{f['status']}  unverified callees: {f['unverified_internal_callees']}"
            )

    @app.command("changed")
    def changed(
        as_json: bool = typer.Option(False, "--json"),
        no_scan: bool = typer.Option(False, "--no-scan", help="Do not re-index first."),
    ) -> None:
        """After editing C code: re-index and list stale proofs and unverified functions in modified files."""
        ctx = AppContext.load(need_backend=False)
        try:
            d = protocol.changed(ctx, quick_scan=not no_scan)
        finally:
            ctx.close()
        if as_json:
            _dump(d)
            return
        if not d["count"]:
            console.print("[green]Nothing changed that affects proofs.[/]")
            return
        if d["stale"]:
            typer.echo("Stale proofs (code or a callee contract changed):")
            for f in d["stale"]:
                typer.echo(f"  {f['name']}  {f['file']}:{f['lines'][0]}  {f.get('reason', '')}")
        if d["unverified_in_modified_files"]:
            typer.echo("Unverified functions in modified files:")
            for f in d["unverified_in_modified_files"]:
                typer.echo(f"  {f['name']}  {f['file']}:{f['lines'][0]}  {f['status']}")
