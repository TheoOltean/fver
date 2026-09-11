"""fver: LLM-driven formal verification of C codebases.

Proves absence of undefined behaviour (out-of-bounds access, use after free,
null dereference, signed overflow, uninitialised reads, ...) one function at a
time, using an LLM to write the annotations and proofs and a proof checker to
accept or reject them.
"""

__version__ = "0.1.0"
