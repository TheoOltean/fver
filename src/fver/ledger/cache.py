"""Content-addressed cache keys.

A function's proof depends on exactly: its own body, the contracts of the
functions it calls, the external specs it uses, the backend and its tool
versions, and the target. Hash those and nothing else, so unchanged
functions are never re-verified.
"""

from __future__ import annotations

from fver.core.models import sha256_text


def cache_key(
    body_hash: str,
    callee_spec_hashes: dict[str, str],
    external_spec_hashes: dict[str, str],
    backend: str,
    tool_versions: dict[str, str],
    target_key: str,
    property_class: str,
) -> str:
    parts = [
        "body=" + body_hash,
        "callees=" + ",".join(f"{k}:{v}" for k, v in sorted(callee_spec_hashes.items())),
        "externals=" + ",".join(f"{k}:{v}" for k, v in sorted(external_spec_hashes.items())),
        "backend=" + backend,
        "tools=" + ",".join(f"{k}:{v}" for k, v in sorted(tool_versions.items())),
        "target=" + target_key,
        "property=" + property_class,
    ]
    return sha256_text("\n".join(parts))
