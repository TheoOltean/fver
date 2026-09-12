"""`fver check FUNCTION [FILE]`: let something other than the built-in loop
write the proof (a Claude Code session, a script, a person).

  fver check FUNCTION        print the task: the annotation rules, the
                             function, its file context, the contracts of
                             its callees. No API key needed.
  fver check FUNCTION FILE   judge FILE (the annotated function) with the
                             checker and record the result exactly as
                             `fver prove` would, at no LLM cost. Prints the
                             checker's feedback; exit 0 means verified.
"""

from __future__ import annotations

from pathlib import Path

import typer

from fver.backends.base import Submission
from fver.core.context import AppContext
from fver.core.models import FunctionInfo
from fver.util.log import console, err_console, setup_logging


class _NoLLM:
    def complete(self, *a, **k):  # pragma: no cover - never called
        raise RuntimeError("fver check does not call the model")


def _function(ctx: AppContext, name: str) -> FunctionInfo:
    matches = ctx.ledger.find_functions(name=name)
    if not matches:
        err_console.print(f"[red]No function named '{name}'.[/] `fver status <file>` lists them.")
        raise typer.Exit(code=1)
    if len(matches) > 1:
        err_console.print(f"[red]'{name}' is defined in several files:[/]")
        for m in matches:
            err_console.print(f"  {m.source_path}:{m.start_line}")
        raise typer.Exit(code=1)
    return matches[0]


def print_task(ctx: AppContext, fn: FunctionInfo) -> None:
    from fver.prove import prompts
    from fver.prove.loop import Verifier

    assert ctx.backend is not None
    v = Verifier(ctx, _NoLLM(), run_id="check")
    task = v.build_task(fn)
    pc = ctx.backend.prompt_context()
    body = prompts.build_task_message(task, [], prompts.select_file_context(task.source_text, fn))
    instructions = pc.instructions.strip()
    if "Output format" in instructions and "Rules:" in instructions:
        # The fenced-block format is for the API loop; here the answer is a file.
        head, _, tail = instructions.partition("Output format")
        instructions = head.rstrip() + "\n\nRules:" + tail.split("Rules:", 1)[1]
    out = [
        f"# fver check: prove `{fn.name}` ({fn.source_path}:{fn.start_line}) free of undefined behaviour",
        "",
        "Write the complete function with annotations added and nothing else changed into a",
        f"file, then run `fver check {fn.name} <file>`. Repeat until it says verified.",
        "",
        instructions,
        "",
        pc.reference.strip(),
        "",
        body.strip(),
    ]
    typer.echo("\n".join(out))


def judge(ctx: AppContext, fn: FunctionInfo, path: Path) -> int:
    from fver.prove.loop import Verifier

    assert ctx.backend is not None
    text = path.read_text(encoding="utf-8", errors="replace")
    run_id = ctx.ledger.start_run("check", ctx.backend.name, ctx.target.key, {"function": fn.name})
    v = Verifier(ctx, _NoLLM(), run_id=run_id)
    try:
        out = v.submit(fn, Submission(files={"function.c": text}))
        ctx.ledger.end_run(run_id, True)
    except Exception as e:  # noqa: BLE001
        ctx.ledger.end_run(run_id, False, str(e))
        raise
    if out.kind == "verified":
        console.print(f"[green]verified[/]  {fn.name}  {fn.source_path}")
        console.print(
            f"  proof: {ctx.ws.relpath(ctx.ws.proofs_dir / fn.source_path / fn.name / 'function.c')}"
        )
        return 0
    console.print(f"[yellow]{out.kind}[/]  {fn.name}  {fn.source_path}")
    typer.echo(out.feedback.strip())
    return 1


def register(app: typer.Typer) -> None:
    @app.command("check")
    def check(
        function: str = typer.Argument(..., help="The function to prove."),
        file: Path | None = typer.Argument(
            None,
            help="The annotated function. Without it, the task is printed.",
            show_default=False,
        ),  # noqa: B008
    ) -> None:
        """Prove a function with your own annotations: print the task, or judge an annotated file with the checker and record the result."""
        from fver.commands.prove import refresh_index

        ctx = AppContext.load(need_backend=True)
        setup_logging(ctx.ws.logs_dir, run_name="check", console=False)
        try:
            refresh_index(ctx)
            fn = _function(ctx, function)
            if file is None:
                print_task(ctx, fn)
                code = 0
            else:
                if not file.exists():
                    err_console.print(f"[red]{file} does not exist.[/]")
                    raise typer.Exit(code=1)
                code = judge(ctx, fn, file)
        finally:
            ctx.close()
        raise typer.Exit(code)
