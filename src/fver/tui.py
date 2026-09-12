"""The terminal view of the ledger: a tree of directories, files and functions
coloured by status, a detail pane for whatever is selected, and a status bar
with coverage and cost. `fver status` opens it read-only; `fver prove` runs
the pipeline in a worker thread behind it and refreshes as claims land.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Static, Tree

from fver.core.context import AppContext
from fver.core.models import Claim, FunctionInfo, Status
from fver.ledger.api import FunctionRow, Summary

STATUS_STYLE = {
    Status.VERIFIED.value: "green",
    Status.BUG_FOUND.value: "red",
    Status.UNRESOLVED.value: "yellow",
    Status.STALE.value: "magenta",
    Status.IN_PROGRESS.value: "cyan",
    Status.UNSUPPORTED.value: "dim",
    Status.NOT_ATTEMPTED.value: "white",
}
GLYPH = {
    Status.VERIFIED.value: "✓",
    Status.BUG_FOUND.value: "✗",
    Status.UNRESOLVED.value: "?",
    Status.STALE.value: "~",
    Status.IN_PROGRESS.value: "…",
    Status.UNSUPPORTED.value: "-",
    Status.NOT_ATTEMPTED.value: "·",
}
ORDER = [
    Status.VERIFIED.value,
    Status.BUG_FOUND.value,
    Status.UNRESOLVED.value,
    Status.STALE.value,
    Status.IN_PROGRESS.value,
    Status.NOT_ATTEMPTED.value,
    Status.UNSUPPORTED.value,
]


@dataclass
class Snapshot:
    """Everything the view needs, read from the ledger in one go."""

    project: str
    target: str
    backend: str
    summary: Summary
    rows: list[FunctionRow]
    by_id: dict[str, FunctionRow] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_id = {r.function.id: r for r in self.rows}


def read_snapshot(repo_root: Path) -> Snapshot:
    ctx = AppContext.load(repo_root, need_backend=False)
    try:
        backend, tk = ctx.backend_name, ctx.target.key
        return Snapshot(
            project=ctx.ws.project_name,
            target=ctx.target.triple,
            backend=backend,
            summary=ctx.ledger.summary(backend, tk),
            rows=ctx.ledger.list_functions(backend, tk, order_by_attack_score=True),
        )
    finally:
        ctx.close()


def counts_label(name: str, rows: list[FunctionRow], bold: bool = False) -> Text:
    """`src/  ✓ 12  ✗ 1  · 30` for a directory or file."""
    t = Text(name, style="bold" if bold else "")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    for st in ORDER:
        if counts.get(st):
            t.append("  ")
            t.append(f"{GLYPH[st]} {counts[st]}", style=STATUS_STYLE[st])
    return t


def function_label(row: FunctionRow) -> Text:
    st = row.status.value
    t = Text()
    t.append(f"{GLYPH[st]} ", style=STATUS_STYLE[st])
    t.append(row.function.name, style=STATUS_STYLE[st] if st != "not_attempted" else "")
    t.append(f"  {row.function.attack_score:.2f}", style="dim")
    return t


def plain(renderable) -> str:
    """A renderable as plain text (what the widget shows; used by tests)."""
    console = Console(width=200, force_terminal=False, color_system=None)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def coverage_bar(summary: Summary, width: int = 24) -> Text:
    frac = max(0.0, min(1.0, summary.verified_weighted))
    filled = round(frac * width)
    t = Text()
    t.append("█" * filled, style="green")
    t.append("░" * (width - filled), style="dim")
    t.append(f" {frac * 100:.1f}% attack-weighted coverage")
    return t


def function_detail(repo_root: Path, row: FunctionRow) -> Group:
    """The right-hand pane for one function."""
    fn = row.function
    st = row.status.value
    parts: list = []
    head = Text()
    head.append(fn.name, style="bold")
    head.append(f"  {fn.source_path}:{fn.start_line}-{fn.end_line}")
    parts.append(head)
    line = Text("status: ")
    line.append(st, style=STATUS_STYLE[st])
    if row.claim and row.claim.message:
        line.append(f"  {row.claim.message}")
    parts.append(line)
    parts.append(Text(f"signature: {fn.signature}"))
    score = Text(f"attack score: {fn.attack_score:.2f}")
    if fn.attack_reasons:
        score.append("  " + "; ".join(fn.attack_reasons), style="dim")
    parts.append(score)
    parts.append(Text(f"callees: {', '.join(fn.callees) if fn.callees else '-'}"))

    ctx = AppContext.load(repo_root, need_backend=False)
    try:
        history: list[Claim] = ctx.ledger.claims_for(fn.id)
        findings = ctx.ledger.findings(function_id=fn.id)
        callers = ctx.ledger.dependents(fn.name)
        proof = ctx.ws.proofs_dir / fn.source_path / fn.name / "function.c"
    finally:
        ctx.close()

    if row.claim and row.claim.assumptions:
        parts.append(Text(""))
        parts.append(Text("assumptions this proof rests on:", style="bold"))
        for a in row.claim.assumptions:
            parts.append(Text(f"  - {a}"))
    if proof.exists():
        parts.append(Text(""))
        parts.append(Text("accepted annotations:", style="bold"))
        body = proof.read_text(encoding="utf-8", errors="replace")
        parts.append(Text(body[:2000] + ("\n..." if len(body) > 2000 else ""), style="cyan"))
    if history:
        parts.append(Text(""))
        t = Table(title="attempts", expand=False, show_edge=False)
        t.add_column("when")
        t.add_column("status")
        t.add_column("cost", justify="right")
        t.add_column("message", overflow="fold", max_width=60)
        for c in history[-8:]:
            t.add_row(
                c.created_at[:19],
                Text(c.status.value, style=STATUS_STYLE[c.status.value]),
                f"${c.cost.usd:.2f}",
                c.message[:300],
            )
        parts.append(t)
    if findings:
        parts.append(Text(""))
        t = Table(title="findings", expand=False, show_edge=False)
        t.add_column("line")
        t.add_column("kind")
        t.add_column("tool")
        t.add_column("message", overflow="fold", max_width=60)
        for f in findings:
            t.add_row(str(f.line or ""), f.kind, f.tool, f.message)
        parts.append(t)
    if callers:
        parts.append(Text(""))
        parts.append(Text("called by (their proofs depend on this contract):", style="bold"))
        for d in callers:
            parts.append(Text(f"  {d.name}  {d.source_path}:{d.start_line}"))
    return Group(*parts)


def group_detail(name: str, rows: list[FunctionRow]) -> Group:
    parts: list = [Text(name, style="bold"), Text("")]
    counts: dict[str, int] = {}
    weight = 0.0
    done = 0.0
    for r in rows:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
        w = r.function.attack_score or 0.01
        weight += w
        if r.status is Status.VERIFIED:
            done += w
    for st in ORDER:
        if counts.get(st):
            t = Text(f"  {GLYPH[st]} {st:14}", style=STATUS_STYLE[st])
            t.append(str(counts[st]))
            parts.append(t)
    parts.append(Text(""))
    parts.append(
        Text(f"{len(rows)} functions, {done / weight * 100 if weight else 0:.1f}% attack-weighted")
    )
    return Group(*parts)


class FverApp(App):
    """`fver status` (read-only) and the live view behind `fver prove`."""

    CSS = """
    #tree { width: 45%; border-right: solid $primary; }
    #detail { width: 55%; padding: 0 1; }
    #bar { dock: bottom; height: 3; padding: 0 1; background: $surface; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(
        self,
        repo_root: Path,
        worker: Callable[[Callable[[FunctionInfo, object], None]], int] | None = None,
        select: str | None = None,
    ) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.worker_fn = worker
        self.select_name = select
        self.snapshot: Snapshot | None = None
        self.last_event = ""
        self.running = worker is not None
        self.exit_code: int | None = None
        self._expanded: set[str] = set()
        self._selected_key: str | None = None
        self._lock = threading.Lock()
        self.detail_text = ""  # plain copies of what the panes show
        self.bar_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree("", id="tree")
            with VerticalScroll(id="detail"):
                yield Static("", id="detail_body")
        yield Static("", id="bar")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        if self.worker_fn is not None:
            self.set_interval(1.0, self.refresh_data)
            self.run_pipeline()

    # ------------------------------------------------------------- pipeline

    @work(thread=True, exclusive=True)
    def run_pipeline(self) -> None:
        assert self.worker_fn is not None

        def on_done(fn: FunctionInfo, out: object) -> None:
            status = getattr(getattr(out, "claim", None), "status", None)
            text = f"{fn.name}: {status.value if status else '?'}"
            self.call_from_thread(self._note, text)

        try:
            code = self.worker_fn(on_done)
        except Exception as e:  # noqa: BLE001 - shown in the bar, then the app keeps running
            code = 2
            self.call_from_thread(self._note, f"error: {e}")
        self.call_from_thread(self._finished, code)

    def _note(self, text: str) -> None:
        self.last_event = text
        self.refresh_data()

    def _finished(self, code: int) -> None:
        self.running = False
        self.exit_code = code
        self.refresh_data()

    # ----------------------------------------------------------------- data

    def action_refresh(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        try:
            snap = read_snapshot(self.repo_root)
        except Exception as e:  # noqa: BLE001
            self.query_one("#bar", Static).update(Text(f"cannot read ledger: {e}", style="red"))
            return
        self.snapshot = snap
        self.title = f"fver  {snap.project}"
        self.sub_title = f"{snap.backend} · {snap.target}"
        shown = self._rebuild_tree(snap)
        self._update_bar(snap)
        self._update_detail(shown)

    def _rebuild_tree(self, snap: Snapshot):
        """Rebuild and return the node the detail pane should show, if any."""
        tree = self.query_one("#tree", Tree)
        # Remember what was open and selected, then rebuild.
        for node in tree.root.children:
            self._remember(node)
        tree.clear()
        tree.root.data = ("group", "", snap.rows)
        tree.root.label = counts_label(snap.project, snap.rows, bold=True)
        tree.root.expand()
        by_dir: dict[str, dict[str, list[FunctionRow]]] = {}
        for r in snap.rows:
            p = Path(r.function.source_path)
            by_dir.setdefault(str(p.parent), {}).setdefault(p.name, []).append(r)
        to_select = None
        for d in sorted(by_dir):
            files = by_dir[d]
            drows = [r for rows in files.values() for r in rows]
            if d in (".", ""):
                dnode = tree.root
            else:
                key = f"dir:{d}"
                dnode = tree.root.add(counts_label(d + "/", drows), data=("group", key, drows))
                if key in self._expanded or not self._expanded:
                    dnode.expand()
            for fname in sorted(files):
                frows = sorted(files[fname], key=lambda r: -r.function.attack_score)
                key = f"file:{d}/{fname}"
                fnode = dnode.add(counts_label(fname, frows), data=("group", key, frows))
                if key in self._expanded or (not self._expanded and len(snap.rows) <= 60):
                    fnode.expand()
                for r in frows:
                    leaf = fnode.add_leaf(function_label(r), data=("fn", r.function.id, r))
                    if self.select_name and r.function.name == self.select_name:
                        to_select = leaf
                        self.select_name = None
                    elif self._selected_key == r.function.id:
                        to_select = leaf
        if to_select is not None:
            # Node lines exist only after the tree has laid itself out.
            self.call_after_refresh(self._show_node, to_select)
        return to_select

    def _show_node(self, node) -> None:
        tree = self.query_one("#tree", Tree)
        tree.move_cursor(node)
        tree.scroll_to_node(node)
        self._update_detail(node)

    def _remember(self, node) -> None:
        data = node.data
        if data and data[0] == "group" and data[1]:
            if node.is_expanded:
                self._expanded.add(data[1])
            else:
                self._expanded.discard(data[1])
        for child in node.children:
            self._remember(child)

    def _update_bar(self, snap: Snapshot) -> None:
        s = snap.summary
        line1 = coverage_bar(s)
        line2 = Text()
        for st in ORDER:
            n = s.by_status.get(st, 0)
            if n:
                line2.append(f"{GLYPH[st]} {st} {n}   ", style=STATUS_STYLE[st])
        c = s.total_cost
        line2.append(f"${c.usd:.2f} spent, {c.llm_calls} LLM calls, {c.checker_runs} checks")
        line3 = Text()
        if self.running:
            line3.append("proving… ", style="cyan bold")
            line3.append(self.last_event)
        elif self.exit_code is not None:
            line3.append("done. ", style="green bold" if self.exit_code == 0 else "red bold")
            line3.append(self.last_event + "   press q to quit")
        else:
            line3.append("q quit · r refresh · ↑↓ move · enter open", style="dim")
        group = Group(line1, line2, line3)
        self.bar_text = plain(group)
        self.query_one("#bar", Static).update(group)

    def _update_detail(self, node=None) -> None:
        tree = self.query_one("#tree", Tree)
        if node is None:
            node = tree.cursor_node
        body = self.query_one("#detail_body", Static)
        if node is None or node.data is None:
            content: Group | Text = Text("select a function", style="dim")
        else:
            kind, key, payload = node.data
            if kind == "fn":
                self._selected_key = key
                snap = self.snapshot
                row = snap.by_id.get(key, payload) if snap else payload
                content = function_detail(self.repo_root, row)
            else:
                content = group_detail(str(node.label), payload)
        self.detail_text = plain(content)
        body.update(content)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        self._update_detail()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._update_detail()


def run_status(repo_root: Path, select: str | None = None) -> None:
    FverApp(repo_root, select=select).run()


def run_prove_with_tui(
    repo_root: Path, worker: Callable[[Callable[[FunctionInfo, object], None]], int]
) -> int:
    app = FverApp(repo_root, worker=worker)
    app.run()
    if app.exit_code is None:
        return 130  # quit before the pipeline finished
    return app.exit_code
