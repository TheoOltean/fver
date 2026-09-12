"""Extract function definitions from C source with tree-sitter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import tree_sitter_c
from tree_sitter import Language, Node, Parser

from fver.core.models import FunctionInfo, TranslationUnit, sha256_text


@lru_cache(maxsize=1)
def _language() -> Language:
    return Language(tree_sitter_c.language())


def _parser() -> Parser:
    return Parser(_language())


def parse(source: bytes) -> Node:
    return _parser().parse(source).root_node


@dataclass
class RawFunction:
    """A function definition as found in one file, before it gets an id."""

    name: str
    signature: str
    start_line: int
    end_line: int
    is_static: bool
    body_hash: str
    callees: list[str]
    start_byte: int
    end_byte: int


def _declarator_name(node: Node) -> str | None:
    """Descend a (possibly nested pointer/parenthesised) declarator to its identifier."""
    cur: Node | None = node
    while cur is not None:
        if cur.type == "identifier":
            return cur.text.decode() if cur.text else None
        if cur.type == "function_declarator":
            cur = cur.child_by_field_name("declarator")
            continue
        if cur.type in ("pointer_declarator", "parenthesized_declarator", "attributed_declarator"):
            inner = cur.child_by_field_name("declarator")
            if inner is None:
                inner = next((c for c in cur.children if c.is_named), None)
            cur = inner
            continue
        return None
    return None


def _has_function_declarator(node: Node) -> bool:
    cur: Node | None = node
    while cur is not None:
        if cur.type == "function_declarator":
            return True
        if cur.type in ("pointer_declarator", "parenthesized_declarator", "attributed_declarator"):
            cur = cur.child_by_field_name("declarator") or next(
                (c for c in cur.children if c.is_named), None
            )
            continue
        return False
    return False


def _strip_comments_and_ws(node: Node, source: bytes) -> str:
    """Definition text with comments removed and whitespace runs collapsed."""
    pieces: list[bytes] = []
    last = node.start_byte

    def visit(n: Node) -> None:
        nonlocal last
        if n.type == "comment":
            pieces.append(source[last : n.start_byte])
            last = n.end_byte
            return
        for c in n.children:
            visit(c)

    visit(node)
    pieces.append(source[last : node.end_byte])
    text = b"".join(pieces).decode("utf-8", errors="replace")
    return re.sub(r"\s+", " ", text).strip()


def _collect_callees(body: Node) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" and fn.text:
                name = fn.text.decode()
                if name not in seen:
                    seen.add(name)
                    out.append(name)
        # push in reverse so traversal is source order
        stack.extend(reversed(n.children))
    return out


def extract_raw(source: bytes) -> list[RawFunction]:
    root = parse(source)
    out: list[RawFunction] = []
    for node in root.children:
        if node.type != "function_definition":
            continue
        declarator = node.child_by_field_name("declarator")
        body = node.child_by_field_name("body")
        if declarator is None or body is None or not _has_function_declarator(declarator):
            continue
        name = _declarator_name(declarator)
        if not name:
            continue
        is_static = any(
            c.type == "storage_class_specifier" and c.text == b"static" for c in node.children
        )
        signature = source[node.start_byte : body.start_byte].decode("utf-8", errors="replace")
        signature = re.sub(r"\s+", " ", signature).strip()
        out.append(
            RawFunction(
                name=name,
                signature=signature,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                is_static=is_static,
                body_hash=sha256_text(_strip_comments_and_ws(node, source)),
                callees=_collect_callees(body),
                start_byte=node.start_byte,
                end_byte=node.end_byte,
            )
        )
    return out


def extract_from_source(source_text: str, source_path: str, tu_id: str) -> list[FunctionInfo]:
    raws = extract_raw(source_text.encode("utf-8"))
    return [
        FunctionInfo(
            id=FunctionInfo.make_id(tu_id, r.name),
            name=r.name,
            tu_id=tu_id,
            source_path=source_path,
            start_line=r.start_line,
            end_line=r.end_line,
            signature=r.signature,
            body_hash=r.body_hash,
            is_static=r.is_static,
            callees=list(r.callees),
        )
        for r in raws
    ]


def extract_from_tu(repo_root: Path, tu: TranslationUnit) -> list[FunctionInfo]:
    path = repo_root / tu.source_path
    text = path.read_text(encoding="utf-8", errors="replace")
    return extract_from_source(text, tu.source_path, tu.id)


def get_function_text(source_text: str, fn: FunctionInfo) -> str:
    """The definition text of `fn` as it appears in the file (by line range)."""
    lines = source_text.splitlines(keepends=True)
    return "".join(lines[fn.start_line - 1 : fn.end_line])
