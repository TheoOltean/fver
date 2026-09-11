"""Rank functions by how likely they are to sit on an attack surface, so
proof effort goes where a bug would matter most. Every point of the score
comes with a reason so the ranking is explainable."""

from __future__ import annotations

import re

from fver.core.models import FunctionInfo
from fver.extract.callgraph import CallGraph
from fver.extract.functions import get_function_text

_NAME_TOKENS = {
    "parse": 0.25,
    "decode": 0.25,
    "deserialize": 0.25,
    "unpack": 0.2,
    "read": 0.15,
    "recv": 0.25,
    "input": 0.2,
    "packet": 0.2,
    "header": 0.15,
    "request": 0.15,
    "msg": 0.1,
    "message": 0.1,
    "len": 0.05,
    "size": 0.05,
    "buf": 0.1,
    "buffer": 0.1,
    "handle": 0.1,
    "process": 0.1,
    "load": 0.1,
    "scan": 0.1,
    "copy": 0.1,
}
_PATH_TOKENS = {
    "net": 0.15,
    "proto": 0.15,
    "parse": 0.15,
    "io": 0.1,
    "input": 0.15,
    "decode": 0.15,
    "pkt": 0.15,
    "http": 0.15,
    "tls": 0.15,
    "ssl": 0.15,
    "codec": 0.1,
    "format": 0.1,
}
_DANGEROUS_CALLS = {
    "memcpy": 0.15,
    "memmove": 0.12,
    "strcpy": 0.25,
    "strncpy": 0.15,
    "sprintf": 0.25,
    "snprintf": 0.1,
    "strcat": 0.25,
    "strncat": 0.15,
    "malloc": 0.1,
    "calloc": 0.08,
    "realloc": 0.15,
    "free": 0.1,
    "alloca": 0.2,
    "gets": 0.4,
    "scanf": 0.25,
    "sscanf": 0.15,
    "read": 0.15,
    "recv": 0.2,
    "recvfrom": 0.2,
    "fread": 0.12,
    "strlen": 0.05,
    "memset": 0.05,
    "strtok": 0.1,
}
_LEN_PARAM = re.compile(
    r"\b(?:size_t|unsigned|int|long|uint\d+_t|ssize_t)\s+\*?\s*(\w*(?:len|size|n|count|cnt)\w*)\b"
)
_PTR_PARAM = re.compile(r"\w+\s*\*\s*(?:const\s+)?\w+")


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", s.lower()))


def score(
    fn: FunctionInfo,
    source_text: str,
    callgraph: CallGraph | None = None,
    externals: list[str] | None = None,
) -> tuple[float, list[str]]:
    """Return (score in [0, 1], reasons)."""
    points = 0.0
    reasons: list[str] = []
    body = get_function_text(source_text, fn)
    body_lines = body.count("\n") + 1

    def add(p: float, why: str) -> None:
        nonlocal points
        points += p
        reasons.append(f"+{p:.2f} {why}")

    name_toks = _tokens(fn.name)
    hits = sorted((t for t in name_toks if t in _NAME_TOKENS), key=lambda t: -_NAME_TOKENS[t])
    for t in hits[:3]:
        add(_NAME_TOKENS[t], f"name mentions '{t}'")

    path_toks = _tokens(fn.source_path)
    phits = sorted((t for t in path_toks if t in _PATH_TOKENS), key=lambda t: -_PATH_TOKENS[t])
    for t in phits[:2]:
        add(_PATH_TOKENS[t], f"file path mentions '{t}'")

    params = fn.signature[fn.signature.find("(") :] if "(" in fn.signature else ""
    n_ptr = len(_PTR_PARAM.findall(params))
    n_len = len(_LEN_PARAM.findall(params))
    if n_ptr and n_len:
        add(0.2, "pointer + length parameter pair")
    elif n_ptr:
        add(0.05 * min(n_ptr, 3), f"{n_ptr} pointer parameter(s)")

    callee_names = externals if externals is not None else fn.callees
    danger = sorted(
        (c for c in set(callee_names) | set(fn.callees) if c in _DANGEROUS_CALLS),
        key=lambda c: -_DANGEROUS_CALLS[c],
    )
    for c in danger[:4]:
        add(_DANGEROUS_CALLS[c], f"calls {c}()")

    if re.search(r"\b(for|while)\b[^{]*\{[^}]*\[[^\]]+\]", body, re.DOTALL) or (
        re.search(r"\b(for|while)\b", body) and "[" in body
    ):
        add(0.15, "array indexing inside a loop")
    if re.search(r"\w+\s*\+\+|\w+\s*\+=\s*\w+|\*\s*\(\s*\w+\s*\+", body) and re.search(
        r"\*\s*\w+\s*(\+\+|\+=)", body
    ):
        add(0.1, "pointer arithmetic")
    if not fn.is_static:
        add(0.1, "externally reachable (non-static)")
    if callgraph is not None and not callgraph.callers_of(fn.id) and not fn.is_static:
        add(0.05, "no internal callers: likely an API entry point")
    if body_lines > 60:
        add(0.1, f"long function ({body_lines} lines)")
    elif body_lines > 25:
        add(0.05, f"medium function ({body_lines} lines)")

    return min(1.0, round(points, 3)), reasons
