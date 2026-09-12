"""Report building and rendering (markdown / JSON) from the ledger."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from fver.core.models import Cost, Status, now_iso
from fver.core.workspace import Workspace
from fver.ledger.api import Ledger, Summary


@dataclass
class FunctionRowData:
    id: str
    name: str
    source_path: str
    start_line: int
    end_line: int
    attack_score: float
    attack_reasons: list[str]
    status: str
    message: str
    assumptions: list[str]
    cost_usd: float
    proof_hash: str | None
    updated_at: str | None


@dataclass
class FileRollup:
    source_path: str
    total: int
    by_status: dict[str, int]


@dataclass
class FindingData:
    function_id: str | None
    source_path: str
    line: int | None
    kind: str
    tool: str
    message: str
    witness: str
    created_at: str


@dataclass
class AssumptionData:
    text: str
    function_count: int


@dataclass
class ReportData:
    generated_at: str
    project: str
    backend: str
    target_key: str
    summary: Summary
    files: list[FileRollup]
    functions: list[FunctionRowData]
    findings: list[FindingData]
    unresolved: list[FunctionRowData]
    assumptions: list[AssumptionData]
    proofs_dir: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def build_report(ledger: Ledger, backend: str, target_key: str, ws: Workspace | None) -> ReportData:
    rows = ledger.list_functions(backend, target_key, order_by_attack_score=True)
    functions: list[FunctionRowData] = []
    files: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    assumption_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        c = row.claim
        fr = FunctionRowData(
            id=row.function.id,
            name=row.function.name,
            source_path=row.function.source_path,
            start_line=row.function.start_line,
            end_line=row.function.end_line,
            attack_score=row.function.attack_score,
            attack_reasons=list(row.function.attack_reasons),
            status=row.status.value,
            message=c.message if c else "",
            assumptions=list(c.assumptions) if c else [],
            cost_usd=c.cost.usd if c else 0.0,
            proof_hash=c.proof_hash if c else None,
            updated_at=c.created_at if c else None,
        )
        functions.append(fr)
        files[fr.source_path][fr.status] += 1
        if row.status is Status.VERIFIED and c is not None:
            for a in set(c.assumptions):
                assumption_counts[a] += 1

    file_rollups = [
        FileRollup(source_path=p, total=sum(v.values()), by_status=dict(v))
        for p, v in sorted(files.items())
    ]
    findings = [
        FindingData(
            function_id=f.function_id,
            source_path=f.source_path,
            line=f.line,
            kind=f.kind,
            tool=f.tool,
            message=f.message,
            witness=f.witness,
            created_at=f.created_at,
        )
        for f in ledger.findings()
    ]
    unresolved = [f for f in functions if f.status == Status.UNRESOLVED.value]
    assumptions = [
        AssumptionData(text=t, function_count=n)
        for t, n in sorted(assumption_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return ReportData(
        generated_at=now_iso(),
        project=ws.project_name if ws is not None else "",
        backend=backend,
        target_key=target_key,
        summary=ledger.summary(backend, target_key),
        files=file_rollups,
        functions=functions,
        findings=findings,
        unresolved=unresolved,
        assumptions=assumptions,
        proofs_dir=str(ws.proofs_dir) if ws is not None else "",
    )


# -- rendering ------------------------------------------------------------------


def _cost_line(c: Cost) -> str:
    return (
        f"${c.usd:.2f} across {c.llm_calls} LLM calls and {c.checker_runs} checker runs "
        f"({c.input_tokens + c.cache_read_tokens + c.cache_write_tokens:,} input tokens, "
        f"{c.output_tokens:,} output tokens)"
    )


def headline(data: ReportData) -> str:
    s = data.summary
    verified = s.by_status.get(Status.VERIFIED.value, 0)
    if data.assumptions:
        assuming = "assuming: " + "; ".join(a.text for a in data.assumptions[:5])
        if len(data.assumptions) > 5:
            assuming += f"; and {len(data.assumptions) - 5} more (see the assumption inventory)"
    else:
        assuming = "with no recorded assumptions"
    return (
        f"{verified} of {s.total_functions} functions are verified free of undefined behaviour "
        f"under backend {data.backend} for target {data.target_key}, {assuming}. "
        f"Weighted by attack surface, coverage is {s.verified_weighted * 100:.1f}%."
    )


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_esc(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def to_markdown(data: ReportData) -> str:
    s = data.summary
    parts: list[str] = []
    title = f"# fver report{' for ' + data.project if data.project else ''}\n"
    parts.append(title)
    parts.append(f"_Generated {data.generated_at}_\n")
    parts.append(headline(data) + "\n")

    parts.append("## Summary\n")
    parts.append(
        _md_table(
            ["Status", "Functions"],
            [[st, str(s.by_status.get(st, 0))] for st in [x.value for x in Status]],
        )
    )
    parts.append(f"- Findings from bug hunters: {s.findings}")
    parts.append(f"- Cost: {_cost_line(s.total_cost)}\n")

    parts.append("## Assumption inventory\n")
    parts.append(
        "Every verified claim is relative to these trusted specifications and axioms. "
        "The count is how many verified functions rely on each.\n"
    )
    parts.append(
        _md_table(
            ["Assumption", "Functions"],
            [[a.text, str(a.function_count)] for a in data.assumptions],
        )
    )

    parts.append("## Per file\n")
    parts.append(
        _md_table(
            [
                "File",
                "Functions",
                "Verified",
                "Bug found",
                "Unresolved",
                "Unsupported",
                "Not attempted",
            ],
            [
                [
                    f.source_path,
                    str(f.total),
                    str(f.by_status.get("verified", 0)),
                    str(f.by_status.get("bug_found", 0)),
                    str(f.by_status.get("unresolved", 0)),
                    str(f.by_status.get("unsupported", 0)),
                    str(f.by_status.get("not_attempted", 0) + f.by_status.get("in_progress", 0)),
                ]
                for f in data.files
            ],
        )
    )

    parts.append("## Findings\n")
    parts.append(
        _md_table(
            ["Location", "Kind", "Tool", "Message"],
            [
                [
                    f"{f.source_path}:{f.line}" if f.line else f.source_path,
                    f.kind,
                    f.tool,
                    f.message,
                ]
                for f in data.findings
            ],
        )
    )

    parts.append("## Unresolved functions\n")
    parts.append(
        "Functions attempted without reaching a proof; the last checker feedback is shown.\n"
    )
    parts.append(
        _md_table(
            ["Function", "Location", "Attack score", "Last feedback"],
            [
                [
                    u.name,
                    f"{u.source_path}:{u.start_line}",
                    f"{u.attack_score:.2f}",
                    u.message[:200],
                ]
                for u in data.unresolved
            ],
        )
    )

    parts.append("## All functions (by attack score)\n")
    parts.append(
        _md_table(
            ["Function", "Location", "Score", "Status", "Assumptions", "Cost", "Note"],
            [
                [
                    fn.name,
                    f"{fn.source_path}:{fn.start_line}",
                    f"{fn.attack_score:.2f}",
                    fn.status,
                    str(len(fn.assumptions)),
                    f"${fn.cost_usd:.2f}",
                    fn.message[:120],
                ]
                for fn in data.functions
            ],
        )
    )
    return "\n".join(parts)


def to_json(data: ReportData) -> str:
    return json.dumps(asdict(data), indent=2, sort_keys=True)
