"""`fver status [FILE|FUNCTION]`: what is proven, what is not, and why.

Like `git status`, a plain page: one line per file, or every function in a
file, or one function in full. The code is indexed first if it never was or
changed since.
"""

from __future__ import annotations

import fnmatch
from collections import Counter

import typer
from rich.table import Table

from fver.core.context import AppContext
from fver.core.models import FunctionInfo, Status
from fver.ledger.api import FunctionRow
from fver.util.log import console, err_console, setup_logging

STYLE = {
    Status.VERIFIED: "green",
    Status.BUG_FOUND: "red",
    Status.UNRESOLVED: "yellow",
    Status.UNSUPPORTED: "dim",
    Status.STALE: "magenta",
    Status.IN_PROGRESS: "cyan",
    Status.NOT_ATTEMPTED: "white",
}
WORD = {
    Status.VERIFIED: "verified",
    Status.BUG_FOUND: "bug",
    Status.UNRESOLVED: "unresolved",
    Status.UNSUPPORTED: "unsupported",
    Status.STALE: "stale",
    Status.IN_PROGRESS: "in progress",
    Status.NOT_ATTEMPTED: "waiting",
}
WAITING = (Status.NOT_ATTEMPTED, Status.STALE, Status.IN_PROGRESS)


def styled(status: Status) -> str:
    return f"[{STYLE[status]}]{WORD[status]}[/]"


def _rows(ctx: AppContext) -> list[FunctionRow]:
    rows = ctx.ledger.list_functions(ctx.backend_name, ctx.target.key, order_by_attack_score=False)
    return sorted(rows, key=lambda r: (r.function.source_path, r.function.start_line))


def _unreadable(ctx: AppContext) -> dict[str, str]:
    """source path -> preprocessor error, for files that could not be read."""
    index = ctx.ws.read_state("index") or {}
    by_id = {tu["id"]: tu["source_path"] for tu in index.get("tus", [])}
    return {by_id[k]: v for k, v in index.get("preprocess_errors", {}).items() if k in by_id}


def overview(ctx: AppContext) -> None:
    rows = _rows(ctx)
    unreadable = _unreadable(ctx)
    by_file: dict[str, Counter[Status]] = {}
    for r in rows:
        by_file.setdefault(r.function.source_path, Counter())[r.status] += 1
    for path in unreadable:
        by_file.setdefault(path, Counter())
    total: Counter[Status] = Counter()
    for c in by_file.values():
        total.update(c)
    n = sum(total.values())
    provable = n - total[Status.UNSUPPORTED]
    waiting = sum(total[s] for s in WAITING)
    cost = ctx.ledger.summary(ctx.backend_name, ctx.target.key).total_cost
    console.print(
        f"[bold]{ctx.ws.project_name}[/]: {n} functions, {provable} provable; "
        f"[green]{total[Status.VERIFIED]} verified[/], "
        f"[yellow]{total[Status.UNRESOLVED]} unresolved[/], "
        f"[red]{total[Status.BUG_FOUND]} bugs[/], {waiting} waiting; ${cost.usd:.2f} spent."
    )
    if not by_file:
        console.print("No C files found under the configured sources.")
        return
    t = Table(box=None, pad_edge=False, header_style="bold")
    for col, just in (
        ("file", "left"),
        ("functions", "right"),
        ("verified", "right"),
        ("unresolved", "right"),
        ("bugs", "right"),
        ("waiting", "right"),
        ("unsupported", "right"),
        ("", "left"),
    ):
        t.add_column(col, justify=just, no_wrap=col == "file")  # type: ignore[arg-type]
    for path in sorted(by_file):
        c = by_file[path]
        note = "cannot read: " + unreadable[path].split(" | ")[0][:70] if path in unreadable else ""
        fn = sum(c.values())
        t.add_row(
            path,
            str(fn),
            f"[green]{c[Status.VERIFIED]}[/]" if c[Status.VERIFIED] else "0",
            f"[yellow]{c[Status.UNRESOLVED]}[/]" if c[Status.UNRESOLVED] else "0",
            f"[red]{c[Status.BUG_FOUND]}[/]" if c[Status.BUG_FOUND] else "0",
            str(sum(c[s] for s in WAITING)),
            f"[dim]{c[Status.UNSUPPORTED]}[/]" if c[Status.UNSUPPORTED] else "0",
            f"[red]{note}[/]",
        )
    console.print(t)
    console.print("\n`fver status <file>` lists its functions; `fver status <function>` shows one.")


def file_page(ctx: AppContext, pattern: str) -> int:
    rows = [
        r
        for r in _rows(ctx)
        if fnmatch.fnmatch(r.function.source_path, pattern)
        or r.function.source_path.endswith(pattern)
    ]
    unreadable = _unreadable(ctx)
    hit = {
        p: e for p, e in unreadable.items() if fnmatch.fnmatch(p, pattern) or p.endswith(pattern)
    }
    if not rows and not hit:
        err_console.print(f"[red]No indexed file matches '{pattern}'.[/]")
        return 1
    for path, err in hit.items():
        console.print(f"[red]{path}: cannot read.[/] {err}")
    t = Table(box=None, pad_edge=False, header_style="bold")
    t.add_column("function")
    t.add_column("line", justify="right")
    t.add_column("status")
    t.add_column("note")
    for r in rows:
        note = r.claim.message if r.claim else ""
        t.add_row(r.function.name, str(r.function.start_line), styled(r.status), note[:100])
    console.print(t)
    return 0


def function_page(ctx: AppContext, name: str) -> int:
    matches = ctx.ledger.find_functions(name=name)
    if not matches:
        err_console.print(f"[red]No function named '{name}'.[/] (`fver status <file>` lists them.)")
        return 1
    if len(matches) > 1:
        console.print(f"'{name}' is defined in several files:")
        for m in matches:
            console.print(f"  {m.source_path}:{m.start_line}")
        console.print("Pass the file to `fver status` to see them.")
        return 0
    _detail(ctx, matches[0])
    return 0


def _detail(ctx: AppContext, fn: FunctionInfo) -> None:
    from fver.prove import store

    backend, tk = ctx.backend_name, ctx.target.key
    claim = ctx.ledger.current_claim(fn.id, backend, tk)
    status = claim.status if claim else Status.NOT_ATTEMPTED
    console.print(f"[bold]{fn.name}[/]  {fn.source_path}:{fn.start_line}-{fn.end_line}")
    console.print(f"  {fn.signature}")
    console.print(
        f"  status: {styled(status)}" + (f"  {claim.message}" if claim and claim.message else "")
    )
    console.print(
        f"  attack score: {fn.attack_score:.2f}"
        + (f"  ({', '.join(fn.attack_reasons)})" if fn.attack_reasons else "")
    )
    if fn.callees:
        console.print(f"  calls: {', '.join(fn.callees)}")
    deps = ctx.ledger.dependents(fn.name)
    if deps:
        console.print("  called by: " + ", ".join(f"{d.name} ({d.source_path})" for d in deps))

    loaded = store.load_accepted(ctx.ws, fn.source_path, fn.name)
    if loaded is not None:
        sub, _record = loaded
        console.print("\n[bold]Accepted contract[/] (what callers rely on):")
        contract = ctx.backend.extract_spec(sub, fn) if ctx.backend is not None else ""
        if not contract.strip():
            contract = "\n".join(
                ln.strip() for t in sub.files.values() for ln in t.splitlines() if "rc::" in ln
            )
        for line in contract.splitlines():
            console.print("  " + line.rstrip(), markup=False, highlight=False)
    if claim and claim.assumptions:
        console.print("\n[bold]Trusted[/] (specs this proof relies on):")
        for a in claim.assumptions:
            console.print(f"  - {a}")

    history = ctx.ledger.claims_for(fn.id)
    if history:
        console.print("\n[bold]History[/]")
        for c in history:
            console.print(
                f"  {c.created_at[:19]}  {styled(c.status)}  ${c.cost.usd:.2f}  {c.message[:90]}"
            )
    findings = ctx.ledger.findings(function_id=fn.id)
    if findings:
        console.print("\n[bold]CBMC findings[/]")
        for f in findings:
            console.print(f"  line {f.line or '?'}: {f.kind} ({f.confidence}) {f.message[:90]}")
    pdir = ctx.ws.proofs_dir / fn.source_path / fn.name
    if pdir.exists() and any(pdir.iterdir()):
        console.print(f"\nProof files: {pdir}")


def register(app: typer.Typer) -> None:
    @app.command("status")
    def status(
        target: str | None = typer.Argument(
            None,
            help="A file (or glob) for its functions, a function name for its details.",
            show_default=False,
        ),
    ) -> None:
        """What is proven, what is not, and why. Indexes the code first if needed."""
        from fver.commands.prove import refresh_index

        ctx = AppContext.load(need_backend=True)
        setup_logging(ctx.ws.logs_dir, run_name="status")
        try:
            refresh_index(ctx)
            if target is None:
                overview(ctx)
                code = 0
            elif "/" in target or target.endswith((".c", ".h")) or "*" in target:
                code = file_page(ctx, target)
            else:
                code = function_page(ctx, target)
        finally:
            ctx.close()
        raise typer.Exit(code)
