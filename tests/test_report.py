from __future__ import annotations

import json
from pathlib import Path

from fver.core.config import FverConfig
from fver.core.models import Claim, Cost, Finding, FunctionInfo, PropertyClass, Status
from fver.core.workspace import Workspace
from fver.ledger.memory import InMemoryLedger
from fver.ledger.report import build_report, headline, to_json, to_markdown

BACKEND = "null"
TARGET = "t"


def _populate(led: InMemoryLedger) -> None:
    fns = [
        FunctionInfo(
            id="tu:parse",
            name="parse",
            tu_id="tu",
            source_path="src/p.c",
            start_line=1,
            end_line=5,
            signature="int parse(char*)",
            body_hash="a",
            attack_score=0.9,
            attack_reasons=["input"],
        ),
        FunctionInfo(
            id="tu:helper",
            name="helper",
            tu_id="tu",
            source_path="src/p.c",
            start_line=6,
            end_line=9,
            signature="int helper(void)",
            body_hash="b",
            attack_score=0.2,
        ),
        FunctionInfo(
            id="tu2:main",
            name="main",
            tu_id="tu2",
            source_path="src/m.c",
            start_line=1,
            end_line=3,
            signature="int main(void)",
            body_hash="c",
        ),
    ]
    led.upsert_functions(fns)
    led.record_claim(
        Claim(
            "tu:parse",
            PropertyClass.UB_FREE,
            BACKEND,
            TARGET,
            Status.VERIFIED,
            "a",
            "k",
            proof_hash="ph",
            assumptions=["libc:strlen", "libc:memcpy"],
            cost=Cost(usd=1.25, llm_calls=2),
        )
    )
    led.record_claim(
        Claim(
            "tu:helper",
            PropertyClass.UB_FREE,
            BACKEND,
            TARGET,
            Status.VERIFIED,
            "b",
            "k",
            assumptions=["libc:strlen"],
        )
    )
    led.record_claim(
        Claim(
            "tu2:main",
            PropertyClass.UB_FREE,
            BACKEND,
            TARGET,
            Status.UNRESOLVED,
            "c",
            "k",
            message="Lithium stuck at loop invariant",
        )
    )
    led.record_finding(
        Finding(
            function_id="tu2:main",
            source_path="src/m.c",
            line=2,
            kind="out_of_bounds",
            tool="cbmc",
            message="array index out of bounds",
            witness="i=3",
        )
    )


def _ws(tmp_path: Path) -> Workspace:
    cfg = FverConfig()
    cfg.project.name = "demo"
    return Workspace.create(tmp_path, cfg)


def test_markdown_report(tmp_path):
    led = InMemoryLedger()
    _populate(led)
    data = build_report(led, BACKEND, TARGET, _ws(tmp_path))
    md = to_markdown(data)
    assert (
        "2 of 3 functions are verified free of undefined behaviour under backend null for target t"
        in md
    )
    assert headline(data).startswith("2 of 3 functions")
    assert "## Assumption inventory" in md
    assert "| libc:strlen | 2 |" in md
    assert "| libc:memcpy | 1 |" in md
    assert "Lithium stuck at loop invariant" in md
    assert "array index out of bounds" in md
    assert "src/m.c:2" in md
    # functions sorted by attack score
    tail = md[md.index("## All functions") :]
    assert tail.index("| parse |") < tail.index("| helper |") < tail.index("| main |")
    assert data.files[0].source_path == "src/m.c" and data.files[1].by_status["verified"] == 2
    assert [a.text for a in data.assumptions] == ["libc:strlen", "libc:memcpy"]


def test_json_report_round_trips(tmp_path):
    led = InMemoryLedger()
    _populate(led)
    data = build_report(led, BACKEND, TARGET, _ws(tmp_path))
    obj = json.loads(to_json(data))
    assert obj["summary"]["total_functions"] == 3
    assert obj["summary"]["by_status"]["verified"] == 2
    assert len(obj["functions"]) == 3 and obj["functions"][0]["name"] == "parse"
    assert obj["findings"][0]["witness"] == "i=3"
    assert obj["unresolved"][0]["name"] == "main"
    assert obj["assumptions"][0] == {"text": "libc:strlen", "function_count": 2}
    assert obj["project"] == "demo"


def test_empty_report():
    data = build_report(InMemoryLedger(), BACKEND, TARGET, None)
    md = to_markdown(data)
    assert "0 of 0 functions" in md and "with no recorded assumptions" in md
    json.loads(to_json(data))
