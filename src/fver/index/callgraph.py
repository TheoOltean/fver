"""Resolve callee names to function ids across translation units."""

from __future__ import annotations

from dataclasses import dataclass, field

from fver.core.models import FunctionInfo


@dataclass
class CallGraph:
    edges: dict[str, list[str]] = field(default_factory=dict)  # function_id -> callee ids
    externals: dict[str, list[str]] = field(default_factory=dict)  # function_id -> undefined names
    reverse: dict[str, list[str]] = field(default_factory=dict)  # function_id -> caller ids

    def callers_of(self, function_id: str) -> list[str]:
        return list(self.reverse.get(function_id, []))

    def all_externals(self) -> set[str]:
        return {n for names in self.externals.values() for n in names}


def build_callgraph(functions: list[FunctionInfo]) -> CallGraph:
    """Static functions resolve within their own TU first; non-static ones
    resolve by name across TUs. Names defined nowhere are externals."""
    by_tu_static: dict[tuple[str, str], str] = {}
    by_name_global: dict[str, str] = {}
    by_name_any: dict[str, list[str]] = {}
    for f in functions:
        by_name_any.setdefault(f.name, []).append(f.id)
        if f.is_static:
            by_tu_static[(f.tu_id, f.name)] = f.id
        else:
            by_name_global.setdefault(f.name, f.id)

    cg = CallGraph()
    for f in functions:
        callees: list[str] = []
        externals: list[str] = []
        for name in f.callees:
            target = by_tu_static.get((f.tu_id, name)) or by_name_global.get(name)
            if target is None:
                # only static definitions elsewhere: not reachable from here
                target = None
            if target is None:
                externals.append(name)
            else:
                callees.append(target)
        cg.edges[f.id] = callees
        cg.externals[f.id] = externals
        for c in callees:
            cg.reverse.setdefault(c, []).append(f.id)
    for f in functions:
        cg.reverse.setdefault(f.id, [])
    return cg
