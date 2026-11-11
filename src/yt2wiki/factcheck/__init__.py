"""Independent fact-check overlay (YT2WIKI-FACTCHECK-01, stage A offline).

Advisory only: assesses selected externally checkable claims against
retrieved evidence and presents cited reasons separately. Never rewrites
the canonical article, its citations, or its publication status.
"""

from . import assess, claims, contracts, evidence, panel, run, store

__all__ = ["assess", "claims", "contracts", "evidence", "panel", "run", "store"]
