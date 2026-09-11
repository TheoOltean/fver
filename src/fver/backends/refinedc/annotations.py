"""Text-level handling of RefinedC annotations in C source.

Everything here is pure string processing: stripping attributes, comparing
code modulo annotations, splicing an annotated function back into its file,
extracting the contract callers depend on, and cheap vacuity checks.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from fver.backends.refinedc import facts
from fver.core.models import FunctionInfo

_STRING_OR_COMMENT = re.compile(
    r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|//[^\n]*|/\*.*?\*/',
    re.DOTALL,
)
_INCLUDE_RE = re.compile(
    r'^[ \t]*#[ \t]*include[ \t]*[<"](?:[^>"]*/)?refinedc\.h[>"][^\n]*\n?', re.MULTILINE
)
_GHOST_STMT_RE = re.compile(
    r"\b" + facts.GHOST_STATEMENT_PREFIX + r"[A-Za-z_]\w*\s*\((?:[^()]|\([^()]*\))*\)\s*;"
)
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


# ---------------------------------------------------------------------------
# Coq identifiers
# ---------------------------------------------------------------------------

_COQ_KEYWORDS = frozenset(
    """as at cofix else end exists exists2 fix for forall fun if IF in let match mod
    Prop return Set then Type using where with Definition Lemma Theorem Proof Qed
    Section End Module Import Export Require From Notation Ltac Ltac2 Axiom
    Parameter Hypothesis Variable Context Class Instance Record Inductive
    CoInductive Fixpoint CoFixpoint""".split()
)
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def coq_ident(name: str, prefix: str = "x") -> str:
    """A valid Coq/dune module-path segment derived from `name`.

    RefinedC turns every directory segment under the project and the C file
    stem into a Coq module path, so each must start with a letter and contain
    only letters, digits and underscores, and must not be a keyword.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
    if not cleaned or not cleaned[0].isalpha() or cleaned in _COQ_KEYWORDS:
        cleaned = f"{prefix}_{cleaned}" if cleaned else prefix
    if not _IDENT_RE.match(cleaned):  # pragma: no cover - defensive
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", prefix + "_" + cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Comment directives  //@rc::<name> <payload>
# ---------------------------------------------------------------------------

_DIRECTIVE_RE = re.compile(r"^[ \t]*//@rc::(?P<name>[A-Za-z_]+)(?P<payload>[^\n]*)$", re.MULTILINE)


def find_directives(text: str) -> list[tuple[str, str]]:
    """All `//@rc::<name> ...` comment directives as (name, payload)."""
    return [(m.group("name"), m.group("payload").strip()) for m in _DIRECTIVE_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Attribute scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attribute:
    name: str  # e.g. "rc::args"
    args: tuple[str, ...]  # the string literal arguments, unquoted
    start: int  # offset of the opening "[["
    end: int  # offset just past the closing "]]"
    text: str


def _scan_attribute_end(text: str, start: int) -> int:
    """Given `start` at a "[[", return the offset just past the matching "]]".

    Tracks string literals so brackets inside quotes do not count. Returns -1
    if unterminated.
    """
    depth = 0
    i = start
    in_str = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _unquote_args(body: str) -> tuple[str, ...]:
    return tuple(
        m.group(1).encode().decode("unicode_escape") if "\\" in m.group(1) else m.group(1)
        for m in re.finditer(r'"((?:\\.|[^"\\])*)"', body)
    )


def find_attributes(text: str) -> list[Attribute]:
    """All `[[rc::...]]` attributes in order of appearance."""
    out: list[Attribute] = []
    i = 0
    while True:
        i = text.find("[[", i)
        if i < 0:
            break
        j = i + 2
        while j < len(text) and text[j].isspace():
            j += 1
        if not text.startswith(facts.ATTR_PREFIX, j):
            i += 2
            continue
        end = _scan_attribute_end(text, i)
        if end < 0:
            break
        inner = text[i + 2 : end - 2]
        m = re.match(r"\s*(rc::[A-Za-z_]\w*)\s*(?:\((?P<body>.*)\))?\s*$", inner, re.DOTALL)
        if m:
            body = m.group("body") or ""
            out.append(Attribute(m.group(1), _unquote_args(body), i, end, text[i:end]))
        i = end
    return out


def strip_annotations(c_text: str) -> str:
    """Remove all rc:: attributes, ghost statements and the refinedc.h include."""
    attrs = find_attributes(c_text)
    pieces: list[str] = []
    pos = 0
    for a in attrs:
        pieces.append(c_text[pos : a.start])
        pos = a.end
    pieces.append(c_text[pos:])
    text = "".join(pieces)
    text = _INCLUDE_RE.sub("", text)
    text = _GHOST_STMT_RE.sub("", text)
    return text


def remove_comments(c_text: str) -> str:
    return _STRING_OR_COMMENT.sub(lambda m: m.group(1) if m.group(1) else " ", c_text)


def normalise(c_text: str) -> str:
    """Comments removed, whitespace collapsed to what is syntactically needed."""
    text = remove_comments(c_text)
    tokens = _TOKEN_RE.findall(text)
    out: list[str] = []
    for tok in tokens:
        if (
            out
            and (out[-1][-1].isalnum() or out[-1][-1] == "_")
            and (tok[0].isalnum() or tok[0] == "_")
        ):
            out.append(" ")
        out.append(tok)
    return "".join(out)


def code_unchanged(original_fn_text: str, annotated_fn_text: str) -> tuple[bool, str]:
    """True iff the annotated function equals the original once annotations,
    comments and whitespace are ignored. The explanation names the first
    difference in token terms."""
    a = _TOKEN_RE.findall(remove_comments(strip_annotations(original_fn_text)))
    b = _TOKEN_RE.findall(remove_comments(strip_annotations(annotated_fn_text)))
    if a == b:
        return True, ""
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ctx = " ".join(a[max(0, i1 - 6) : i1])
        was = " ".join(a[i1:i2]) or "(nothing)"
        now = " ".join(b[j1:j2]) or "(nothing)"
        return False, (
            f"code changed near `{ctx}`: original had `{was}`, submission has `{now}`. "
            "Only rc:: attributes, ghost statements and comments may be added."
        )
    return False, "code differs"


# ---------------------------------------------------------------------------
# Splicing
# ---------------------------------------------------------------------------


def find_definition_range(source_text: str, name: str) -> tuple[int, int] | None:
    """Locate the definition of `name` in a file: 1-based (start, end) line
    numbers, inclusive, covering any rc:: attributes immediately above it.
    Returns None when not found. Heuristic, meant for verified callees whose
    exact FunctionInfo is not at hand."""
    clean = remove_comments(source_text)
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", clean):
        # Must be followed by a ')' then '{' (a definition, not a call/prototype).
        depth = 0
        i = m.end() - 1
        while i < len(clean):
            if clean[i] == "(":
                depth += 1
            elif clean[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        j = i + 1
        while j < len(clean) and clean[j].isspace():
            j += 1
        if j >= len(clean) or clean[j] != "{":
            continue
        # Preceded by a type name on the same logical declaration (not `;`, `=` or `(`).
        k = m.start() - 1
        while k >= 0 and clean[k].isspace():
            k -= 1
        if k < 0 or not (clean[k].isalnum() or clean[k] in "_*)]"):
            continue
        # Find the end of the body.
        depth = 0
        e = j
        while e < len(clean):
            if clean[e] == "{":
                depth += 1
            elif clean[e] == "}":
                depth -= 1
                if depth == 0:
                    break
            e += 1
        # Start of the definition: beginning of the line holding the return type.
        line_start = clean.rfind("\n", 0, m.start()) + 1
        # Walk further up over any attribute lines.
        start_line = clean.count("\n", 0, line_start) + 1
        lines = source_text.split("\n")
        while start_line > 1 and lines[start_line - 2].lstrip().startswith("[["):
            start_line -= 1
        end_line = clean.count("\n", 0, e) + 1
        return start_line, end_line
    return None


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def splice(
    source_text: str,
    function: FunctionInfo,
    annotated_fn_text: str,
    callee_replacements: dict[str, tuple[FunctionInfo, str]] | None = None,
) -> str:
    """Return a copy of the source with the target function's lines replaced
    by `annotated_fn_text` and each callee (same file only) replaced by its
    accepted text. Replacement is bottom-up so line numbers stay valid.
    `#include <refinedc.h>` is added at the top if absent."""
    lines = source_text.split("\n")
    repl: list[tuple[int, int, str]] = [(function.start_line, function.end_line, annotated_fn_text)]
    for info, text in (callee_replacements or {}).values():
        if info.source_path != function.source_path or info.id == function.id:
            continue
        repl.append((info.start_line, info.end_line, text))
    # Reject overlaps rather than silently corrupt.
    repl.sort(key=lambda r: r[0], reverse=True)
    prev_start = None
    for start, end, _ in repl:
        if prev_start is not None and end >= prev_start:
            raise ValueError(f"overlapping replacement ranges around line {start}")
        prev_start = start
    for start, end, text in repl:
        if start < 1 or end > len(lines) or start > end:
            raise ValueError(f"line range {start}-{end} out of bounds for {function.source_path}")
        new_lines = _ensure_trailing_newline(text).split("\n")[:-1]
        lines[start - 1 : end] = new_lines
    out = "\n".join(lines)
    if not has_refinedc_include(out):
        out = facts.HEADER_INCLUDE + "\n" + out
    return out


def definition_prototype(definition_text: str) -> str:
    """`static int f(int x)` from a definition's text: everything up to the
    first top-level `{`, whitespace-collapsed, plus `;`."""
    clean = remove_comments(definition_text)
    depth = 0
    for i, ch in enumerate(clean):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "{" and depth == 0:
            clean = clean[:i]
            break
    sig = re.sub(r"\s+", " ", clean).strip().rstrip(";")
    return sig + ";"


def stub_definition(source_text: str, start_line: int, end_line: int) -> str:
    """Replace the definition on lines start..end (1-based, inclusive) by its
    prototype, padded with blank lines so every other line keeps its number.
    Attributes above the definition are left in place (they then attach to
    the prototype, which is what RefinedC wants for a spec-only callee)."""
    lines = source_text.split("\n")
    if start_line < 1 or end_line > len(lines) or start_line > end_line:
        raise ValueError(f"line range {start_line}-{end_line} out of bounds")
    body = lines[start_line - 1 : end_line]
    proto = definition_prototype("\n".join(body))
    # Preprocessor directives inside the body must stay, or #if/#else/#endif
    # nesting breaks for the rest of the file. Everything else goes blank.
    kept = [ln if ln.lstrip().startswith("#") else "" for ln in body]
    kept[0] = proto if not kept[0] else proto + " " + kept[0]
    lines[start_line - 1 : end_line] = kept
    return "\n".join(lines)


def enclosing_definition(source_text: str, line: int) -> tuple[int, int, str] | None:
    """The top-level `{...}` block (a function body) containing `line`
    (1-based), as (start_line, end_line, name), with start_line at the line
    that opens the declarator (walking up to the previous blank line or `;`
    or `}`). Used when the extractor did not recognise a definition (macro
    in the declarator, K&R style). None if `line` is not inside a body."""
    clean = remove_comments(source_text)
    lines = clean.split("\n")
    depth = 0
    open_line = 0
    in_str = None
    in_directive = False  # a #define may span lines and contain unbalanced braces
    for i, ln in enumerate(lines, start=1):
        if in_directive or ln.lstrip().startswith("#"):
            in_directive = ln.rstrip().endswith("\\")
            continue
        for ch in ln:
            if in_str:
                if ch == in_str:
                    in_str = None
                continue
            if ch in "\"'":
                in_str = ch
            elif ch == "{":
                if depth == 0:
                    open_line = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and open_line and open_line <= line <= i:
                    start = open_line
                    while start > 1:
                        prev = lines[start - 2].strip()
                        if not prev or prev.endswith((";", "}", "{", ">")) or prev.startswith("#"):
                            break
                        start -= 1
                    head = " ".join(lines[start - 1 : open_line])
                    head = head[: head.index("{")] if "{" in head else head
                    m = re.search(r"([A-Za-z_]\w*)\s*\([^;{]*$", head)
                    name = m.group(1) if m else ""
                    return start, i, name
                if depth == 0:
                    open_line = 0
        in_str = None  # strings do not span lines in C source
    return None


def blank_lines(source_text: str, start_line: int, end_line: int) -> str:
    """Replace lines start..end (1-based, inclusive) by empty lines."""
    lines = source_text.split("\n")
    if start_line < 1 or end_line > len(lines) or start_line > end_line:
        raise ValueError(f"line range {start_line}-{end_line} out of bounds")
    lines[start_line - 1 : end_line] = [
        ln if ln.lstrip().startswith("#") else "" for ln in lines[start_line - 1 : end_line]
    ]
    return "\n".join(lines)


def has_refinedc_include(text: str) -> bool:
    return _INCLUDE_RE.search(text) is not None


def prepend_prototypes(source_text: str, prototypes: list[str]) -> str:
    """Insert spec-carrying prototypes after the last #include at the top."""
    if not prototypes:
        return source_text
    block = "\n".join(p.rstrip() for p in prototypes) + "\n"
    lines = source_text.split("\n")
    last_include = -1
    for i, ln in enumerate(lines):
        if re.match(r"\s*#\s*include\b", ln):
            last_include = i
        elif (
            ln.strip() and not ln.lstrip().startswith(("#", "//", "/*", "*")) and last_include >= 0
        ):
            break
    insert_at = last_include + 1
    lines[insert_at:insert_at] = block.split("\n")[:-1]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def extract_contract(annotated_fn_text: str) -> str:
    """The attribute block plus the signature, as a prototype ending in ';'.

    This is what callers depend on. Attributes are kept verbatim (except
    rc::tactics / rc::lemmas, which belong to the proof, not the contract)."""
    text = annotated_fn_text.lstrip()
    attrs = find_attributes(text)
    pos = 0
    kept: list[str] = []
    for a in attrs:
        # Only the leading block: stop at the first attribute that appears
        # after non-whitespace code (a loop annotation inside the body).
        if text[pos : a.start].strip():
            break
        if a.name not in (facts.ATTR_TACTICS, facts.ATTR_LEMMAS):
            kept.append(a.text)
        pos = a.end
    rest = text[pos:]
    brace = rest.find("{")
    if brace < 0:
        sig = rest.strip().rstrip(";")
    else:
        sig = rest[:brace].strip()
    sig = remove_comments(sig)
    sig = re.sub(r"\s+", " ", sig).strip()
    return "\n".join(kept + [sig + ";"])


def contract_prototype(contract: str) -> str:
    """A contract is already a prototype; kept for symmetry/readability."""
    return contract if contract.rstrip().endswith(";") else contract.rstrip() + ";"


# ---------------------------------------------------------------------------
# Vacuity
# ---------------------------------------------------------------------------

_FALSE_PROPS = (
    r"^\s*\{?\s*False\s*\}?\s*$",
    r"^\s*\{?\s*0\s*=\s*1\s*\}?\s*$",
    r"^\s*\{?\s*1\s*=\s*0\s*\}?\s*$",
    r"^\s*\{?\s*(\w+)\s*≠\s*\1\s*\}?\s*$",
    r"^\s*\{?\s*(\w+)\s*<>\s*\1\s*\}?\s*$",
    r"^\s*\{?\s*(\w+)\s*<\s*\1\s*\}?\s*$",
    r"^\s*\{?\s*(\w+)\s*>\s*\1\s*\}?\s*$",
    r"^\s*\{?\s*\(\s*\w+\s*<\s*0\s*\)\s*%nat\s*\}?\s*$",
)


def vacuity_check(annotated_fn_text: str) -> list[str]:
    """Cheap syntactic checks for preconditions that make the proof trivial."""
    problems: list[str] = []
    attrs = find_attributes(annotated_fn_text)
    nat_params: set[str] = set()
    for a in attrs:
        if a.name == facts.ATTR_PARAMETERS:
            for p in a.args:
                m = re.match(r"\s*(\w+)\s*:\s*(nat|N)\s*$", p)
                if m:
                    nat_params.add(m.group(1))
    for a in attrs:
        if a.name in (facts.ATTR_REQUIRES, facts.ATTR_CONSTRAINTS):
            for prop in a.args:
                for pat in _FALSE_PROPS:
                    if re.match(pat, prop):
                        problems.append(
                            f"{a.name} contains an unsatisfiable precondition: {prop!r}"
                        )
                        break
                m = re.match(r"^\s*\{?\s*(\w+)\s*<\s*0\s*\}?\s*$", prop)
                if m and m.group(1) in nat_params:
                    problems.append(
                        f"{a.name}: {prop!r} is unsatisfiable because {m.group(1)} is a nat"
                    )
        if (
            a.name == facts.ATTR_ARGS
            and a.args
            and all(re.fullmatch(r"\s*null\s*", x) for x in a.args)
        ):
            problems.append(
                "rc::args gives every argument the type `null`; the function is never callable with real data"
            )
    return problems
