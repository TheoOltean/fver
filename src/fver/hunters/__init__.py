"""Bug finders: tools that turn up concrete undefined behaviour cheaply.

Hunters never prove anything. They run before proof effort is spent, and a
definite hit becomes a Finding (and a BUG_FOUND claim) in the ledger.
"""
