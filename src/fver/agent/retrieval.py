"""Pick previously verified functions from the same repository to show the
model as style references. Cheap lexical similarity; no embeddings."""

from __future__ import annotations

import re

from fver.agent.store import load_accepted
from fver.backends.base import Backend, FunctionTask, Submission
from fver.core.models import FunctionInfo, Status
from fver.core.workspace import Workspace
from fver.ledger.api import Ledger

_TOKEN_RE = re.compile(r"[a-z]+|\d+")


def _tokens(name: str) -> set[str]:
    return set(_TOKEN_RE.findall(re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()))


def similarity(a: FunctionInfo, b: FunctionInfo) -> float:
    score = 0.0
    if a.source_path == b.source_path:
        score += 2.0
    shared = set(a.callees) & set(b.callees)
    score += min(len(shared), 3) * 0.75
    ta, tb = _tokens(a.name), _tokens(b.name)
    if ta and tb:
        score += 1.5 * len(ta & tb) / len(ta | tb)
    la, lb = a.end_line - a.start_line + 1, b.end_line - b.start_line + 1
    score += 1.0 - min(abs(la - lb) / max(la, lb, 1), 1.0)
    return score


def pick_examples(
    ledger: Ledger,
    ws: Workspace,
    backend: Backend,
    task: FunctionTask,
    k: int = 3,
) -> list[tuple[FunctionInfo, Submission]]:
    rows = ledger.list_functions(
        backend.name, task.target.key, status=Status.VERIFIED, order_by_attack_score=False
    )
    scored = []
    for row in rows:
        if row.function.id == task.function.id:
            continue
        scored.append((similarity(task.function, row.function), row.function))
    scored.sort(key=lambda t: (-t[0], t[1].name))
    out: list[tuple[FunctionInfo, Submission]] = []
    for _, info in scored:
        loaded = load_accepted(ws, info.source_path, info.name)
        if loaded is None:
            continue
        out.append((info, loaded[0]))
        if len(out) >= k:
            break
    return out
