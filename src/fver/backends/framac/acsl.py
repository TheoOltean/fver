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


_CALLS_LINE = re.compile(r"^(?P<indent>[ \t]*)//@\s*calls\b[^\n]*$")
_LOCAL_DEF = re.compile(
    r"^(?P<indent>[ \t]*)(?P<type>(?:[A-Za-z_][\w ]*?\s*\**\s*)+?)\b(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<init>[^;]+;)\s*$"
)


def split_calls_declarations(fn_text: str) -> str:
    """Frama-C cannot attach `//@ calls f;` to a local definition with an
    initialiser (`T x = f();`). Rewrite that pair to `T x;` followed by the
    annotation and `x = f();`, which means the same thing and is accepted.
    Applied to the copy the checker sees, after the code-unchanged check."""
    lines = fn_text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = _CALLS_LINE.match(ln)
        if m and i + 1 < len(lines):
            d = _LOCAL_DEF.match(lines[i + 1])
            if d and not re.search(r"\bconst\b", d.group("type")) and "(" not in d.group("type"):
                ind = d.group("indent")
                ty = d.group("type").rstrip()
                out.append(f"{ind}{ty}{'' if ty.endswith('*') else ' '}{d.group('name')};")
                out.append(ln)
                out.append(f"{ind}{d.group('name')} = {d.group('init')}")
                i += 2
                continue
        out.append(ln)
        i += 1
    return "\n".join(out)


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
    "split_calls_declarations",
]
