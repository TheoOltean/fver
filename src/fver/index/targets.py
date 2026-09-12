"""Detect the target platform the compiler builds for, and compare it with
the configured one. Implementation-defined behaviour hangs off these."""

from __future__ import annotations

import re
from dataclasses import replace

from fver.core.models import Target
from fver.util import proc

_DEFINE = re.compile(r"^#define\s+(\w+)\s*(.*)$")


def parse_predefined_macros(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _DEFINE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def target_from_macros(macros: dict[str, str], triple: str, compiler: str) -> Target:
    def bits(name: str, default: int) -> int:
        v = macros.get(name)
        try:
            return int(v) * 8 if v is not None else default
        except ValueError:
            return default

    byte_order = macros.get("__BYTE_ORDER__", "__ORDER_LITTLE_ENDIAN__")
    return Target(
        triple=triple,
        compiler=compiler,
        int_bits=bits("__SIZEOF_INT__", 32),
        long_bits=bits("__SIZEOF_LONG__", 64),
        pointer_bits=bits("__SIZEOF_POINTER__", 64),
        char_signed="__CHAR_UNSIGNED__" not in macros,
        little_endian="LITTLE" in byte_order,
    )


def detect_triple(compiler: str, flags: list[str] | None = None) -> str:
    """clang answers -print-target-triple; gcc (and clang) answer -dumpmachine.
    'unknown' when neither works, so callers keep their configured triple."""
    for probe in ("-print-target-triple", "-dumpmachine"):
        r = proc.run([compiler, *(flags or []), probe], timeout=30)
        text = r.stdout.strip()
        if r.ok and text:
            return text.splitlines()[0].strip()
    return "unknown"


def detect_target(compiler: str = "cc", extra_flags: list[str] | None = None) -> Target | None:
    """Ask the compiler what it targets. None if the compiler is missing."""
    if proc.which(compiler) is None:
        return None
    flags = list(extra_flags or [])
    triple = detect_triple(compiler, flags)
    r = proc.run([compiler, *flags, "-dM", "-E", "-x", "c", "/dev/null"], timeout=30)
    if not r.ok:
        return Target(triple=triple, compiler=compiler)
    return target_from_macros(parse_predefined_macros(r.stdout), triple, compiler)


def compare_target(detected: Target | None, configured: Target) -> list[str]:
    """Human-readable warnings where the compiler disagrees with config.toml."""
    if detected is None:
        return [f"compiler '{configured.compiler}' not found; cannot confirm target"]
    warnings: list[str] = []
    fields = ("int_bits", "long_bits", "pointer_bits", "char_signed", "little_endian")
    for f in fields:
        d, c = getattr(detected, f), getattr(configured, f)
        if d != c:
            warnings.append(f"target.{f}: compiler says {d}, config says {c}")
    if detected.triple != configured.triple and detected.triple != "unknown":
        warnings.append(
            f"target.triple: compiler says {detected.triple}, config says {configured.triple}"
        )
    return warnings


def with_triple(t: Target, triple: str) -> Target:
    return replace(t, triple=triple)
