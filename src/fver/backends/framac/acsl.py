"""ACSL text helpers: contracts live in `/*@ ... */` and `//@ ...` comments."""

from __future__ import annotations

import re

from fver.backends.refinedc.annotations import definition_prototype, remove_comments

_ACSL_BLOCK = re.compile(r"/\*@.*?\*/", re.DOTALL)
_ACSL_LINE = re.compile(r"//@[^\n]*")


def acsl_blocks(text: str) -> list[str]:
    """Every ACSL comment in `text`, in order."""
    out: list[tuple[int, str]] = []
    for m in _ACSL_BLOCK.finditer(text):
        out.append((m.start(), m.group(0)))
    for m in _ACSL_LINE.finditer(text):
        out.append((m.start(), m.group(0)))
    return [t for _, t in sorted(out)]


def leading_contract(annotated_fn_text: str) -> tuple[str, str]:
    """(contract comments, rest) where the contract is every ACSL comment
    that precedes the function's signature."""
    text = annotated_fn_text.lstrip()
    kept: list[str] = []
    while True:
        m = _ACSL_BLOCK.match(text) or _ACSL_LINE.match(text)
        if m is None:
            break
        kept.append(m.group(0))
        text = text[m.end() :].lstrip()
    return "\n".join(kept), text


def extract_contract(annotated_fn_text: str) -> str:
    """The contract callers depend on: the ACSL block(s) above the function
    plus its prototype, on as few lines as possible."""
    contract, rest = leading_contract(annotated_fn_text)
    proto = definition_prototype(rest)
    return (contract + "\n" if contract else "") + proto


def one_line(contract: str) -> str:
    """A contract with its prototype squeezed onto one line, so it can
    replace a definition without shifting the line numbers below it."""
    return re.sub(r"\s*\n\s*", " ", contract.strip())


def body_annotations(annotated_fn_text: str) -> list[str]:
    """ACSL comments inside the body (loop annotations, assertions)."""
    _, rest = leading_contract(annotated_fn_text)
    return acsl_blocks(rest)


def clauses(contract: str) -> list[str]:
    """`requires ...;` style clauses of an ACSL block, comment markers removed."""
    inner = re.sub(r"^\s*/\*@|\*/\s*$", "", contract.strip(), flags=re.DOTALL)
    inner = re.sub(r"^\s*//@", "", inner, flags=re.MULTILINE)
    return [c.strip() for c in inner.split(";") if c.strip()]


__all__ = [
    "acsl_blocks",
    "body_annotations",
    "clauses",
    "extract_contract",
    "leading_contract",
    "one_line",
    "remove_comments",
]
