"""Independent fact-check overlay (YT2WIKI-FACTCHECK-01, stage A offline).

Advisory only: assesses selected externally checkable claims against
retrieved evidence and presents cited reasons separately. Never rewrites
the canonical article, its citations, or its publication status.
"""

from . import assess, claims, contracts, ddgs_provider, evidence, panel, run, store, verifier

__all__ = ["assess", "claims", "contracts", "ddgs_provider", "evidence",
           "panel", "run", "store", "verifier"]
