"""Opaque floating point for the RefinedC front-end.

RefinedC's C semantics has no floating-point values, and its front-end
rejects a whole translation unit as soon as a struct or union *declaration*
contains a `float` or `double` (the layout cannot be computed). That would
put every file that defines a tagged value type out of reach.

fver therefore checks a copy of the code in which every floating-point type
specifier is replaced by a struct of the same size and alignment that carries
no arithmetic:

    float        -> struct fver_f32
    double       -> struct fver_f64
    long double  -> struct fver_fld

The rewrite is a pure text substitution over the translation unit copy and
over shadow copies of the project's headers; the user's files are untouched
and line numbers are preserved. It is sound for proofs of undefined-behaviour
freedom because a struct can only be stored, copied, passed and returned:
any function that adds, compares, converts or dereferences a float value as a
number hits a type error in the strict ISO front-end and is reported as
unsupported, while a function that merely moves the bytes is checked against
a memory layout identical to the real one. Floating-point arithmetic itself
has no undefined behaviour under IEEE semantics, so nothing provable is lost
for the functions that survive; what is lost is coverage of the functions
that compute with floats, which the ledger reports honestly.
"""

from __future__ import annotations

import re
from pathlib import Path

from fver.core.models import Target

F32 = "fver_f32"
F64 = "fver_f64"
FLD = "fver_fld"
OPAQUE_HEADER = "fver_opaque.h"

# Comments and string/char literals are passed through untouched; only code
# tokens are rewritten. Newlines inside a block comment are kept as-is.
_SEGMENT = re.compile(
    r"(?P<lit>//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*')|(?P<code>[^\"'/]+|/)",
    re.DOTALL,
)
_LONG_DOUBLE = re.compile(r"\blong[ \t]+double\b")
_DOUBLE = re.compile(r"\bdouble\b")
_FLOAT = re.compile(r"\bfloat\b")
_MENTIONS = re.compile(r"\b(?:float|double)\b")
_INCLUDE_LINE = re.compile(r"^[ \t]*#[ \t]*include\b.*$", re.MULTILINE)
# `(void)expr` discards a value; the front-end does not implement the cast, so
# it is dropped. A `(void)` preceded by an identifier or `)` is a parameter
# list (`int f(void)`, `void (*fp)(void)`) and is left alone; after a keyword
# such as `return` it is a cast.
_VOID_CAST = re.compile(r"\(\s*void\s*\)")
_KEYWORDS_BEFORE_CAST = ("return", "case", "else", "do")


def _drop_void_casts(code: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _VOID_CAST.finditer(code):
        before = code[: m.start()].rstrip()
        prev = before[-1] if before else ""
        word = re.search(r"([A-Za-z_][A-Za-z0-9_]*)$", before)
        is_param_list = (prev.isalnum() or prev in "_)") and not (
            word and word.group(1) in _KEYWORDS_BEFORE_CAST
        )
        after = code[m.end() :].lstrip()
        if is_param_list or after.startswith("*"):
            continue
        out.append(code[pos : m.start()])
        pos = m.end()
    out.append(code[pos:])
    return "".join(out)


def _rewrite_code(code: str) -> str:
    code = _LONG_DOUBLE.sub(f"struct {FLD}", code)
    code = _DOUBLE.sub(f"struct {F64}", code)
    code = _FLOAT.sub(f"struct {F32}", code)
    return _drop_void_casts(code)


def rewrite_floats(text: str) -> str:
    """Replace floating-point type specifiers (and drop `(void)` casts) outside
    comments, literals and #include lines. Line-preserving: no newline is
    added or removed."""
    out: list[str] = []
    for m in _SEGMENT.finditer(text):
        seg = m.group(0)
        if m.group("lit") is not None:
            out.append(seg)
            continue
        # #include lines name files (<float.h>): leave them exactly as they are.
        pos = 0
        for inc in _INCLUDE_LINE.finditer(seg):
            out.append(_rewrite_code(seg[pos : inc.start()]))
            out.append(inc.group(0))
            pos = inc.end()
        out.append(_rewrite_code(seg[pos:]))
    return "".join(out)


def mentions_float(text: str) -> bool:
    """True if the code (comments and literals excluded) names a float type."""
    return any(
        _MENTIONS.search(m.group(0)) for m in _SEGMENT.finditer(text) if m.group("lit") is None
    )


_OPAQUE_NAME = re.compile(r"struct (?:fver_f32|fver_f64|fver_fld)\b")
FLOAT_HINT = (
    "floating-point arithmetic, comparison or conversion; floats are opaque to the "
    "checker, only storing, copying and passing them is supported"
)


def explain_reason(reason: str) -> str:
    """Front-end messages mention the stand-in structs; say what they mean."""
    if _OPAQUE_NAME.search(reason) and FLOAT_HINT not in reason:
        return f"{reason} [{FLOAT_HINT}]"
    return reason


def _long_double_layout(target: Target) -> tuple[int, int]:
    """(size, alignment) of `long double` for the target."""
    t = target.triple.lower()
    if "apple" in t and ("arm64" in t or "aarch64" in t):
        return 8, 8  # long double is binary64 on Apple arm64
    if t.startswith(("x86_64", "amd64", "aarch64", "arm64")):
        return 16, 16
    if t.startswith(("i386", "i686")):
        return 12, 4
    return 8, 8  # 32-bit ARM, RISC-V without Q, and the safe default


def opaque_header_text(target: Target) -> str:
    """The struct definitions, sized for the target. Written next to the
    backend project and force-included before every checked file."""
    ld_size, ld_align = _long_double_layout(target)
    if ld_size == 8:
        fld = "long long fver_bits;"
    else:
        fld = f"_Alignas({ld_align}) unsigned char fver_bits[{ld_size}];"
    return (
        "/* fver: opaque stand-ins for floating-point types (see opaque.py).\n"
        " * Same size and alignment as the real types; no arithmetic possible. */\n"
        "#ifndef _FVER_OPAQUE_H\n"
        "#define _FVER_OPAQUE_H\n"
        f"struct {F32} {{ int fver_bits; }};\n"
        f"struct {F64} {{ long long fver_bits; }};\n"
        f"struct {FLD} {{ {fld} }};\n"
        "#endif\n"
    )


def write_opaque_header(workspace_dir: Path, target: Target) -> Path:
    p = workspace_dir / OPAQUE_HEADER
    text = opaque_header_text(target)
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")
    return p


HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".inc")


def sync_shadow_headers(repo_root: Path, shadow_root: Path) -> int:
    """Mirror every header under `repo_root` into `shadow_root` with the
    rewrite applied, so `#include "x.h"` from a checked copy resolves to the
    rewritten header when the shadow directory precedes the real one on the
    include path. Copies are refreshed when the source is newer. Returns the
    number of files written."""
    written = 0
    for src in _iter_headers(repo_root):
        rel = src.relative_to(repo_root)
        dst = shadow_root / rel
        try:
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(rewrite_floats(text), encoding="utf-8")
        written += 1
    return written


def _iter_headers(repo_root: Path):
    for p in repo_root.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        if p.suffix.lower() not in HEADER_SUFFIXES:
            continue
        rel = p.relative_to(repo_root)
        if any(part.startswith(".") for part in rel.parts):
            continue  # .git, .fver, editor state
        yield p


def shadow_dir_for(include_dir: Path, repo_root: Path, shadow_root: Path) -> Path | None:
    """The shadow twin of a project include directory, or None if the
    directory is outside the repository."""
    try:
        rel = include_dir.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    return shadow_root / rel
