"""FACTCHECK-01 stage C: live DDGS search/fetch smoke (deployment environment).

Opt-in only: runs solely when `YT2WIKI_LIVE_SMOKE=1` is set, and is marked
`network` so CI stays deterministic (CI runs the full suite without live
access). Uses public-knowledge queries only — never private article text.

Records: successful searches, blocked/rate-limited searches, passage
verification failures, request counts, latency. No fabricated citations and
no article mutations are hard gates; a DDGS outage must surface as
`unavailable`/`partial`, never as verdicts.
"""

from __future__ import annotations

import os
import time

import pytest

from yt2wiki.factcheck import ddgs_provider as dd
from yt2wiki.factcheck import evidence as ev
from yt2wiki.factcheck import run as runmod

LIVE = os.environ.get("YT2WIKI_LIVE_SMOKE") == "1"
pytestmark = [pytest.mark.network, pytest.mark.skipif(
    not LIVE, reason="live smoke runs only with YT2WIKI_LIVE_SMOKE=1")]

QUERIES = [
    "DART mission Dimorphos Didymos September 2022",
    "Schwab index fund expense ratio 2024",
    "tomato plant pruning suckers cooperative extension",
]


def test_live_search_returns_mapped_hits():
    provider = dd.DDGSProvider()
    started = time.monotonic()
    hits = provider.search(QUERIES[0], limit=5)
    elapsed = time.monotonic() - started
    assert isinstance(hits, list)
    assert elapsed < 60
    for hit in hits:
        assert hit.url.startswith(("http://", "https://"))
        assert dd.safe_url(hit.url) is not None
    print(f"\nsearch ok: {len(hits)} hits in {elapsed:.1f}s")


def test_live_search_failure_modes_are_typed():
    provider = dd.DDGSProvider()
    outcomes: dict[str, int] = {}
    for query in QUERIES:
        started = time.monotonic()
        try:
            hits = provider.search(query, limit=4)
            outcomes["ok"] = outcomes.get("ok", 0) + 1
            print(f"\nquery {query[:40]!r}: {len(hits)} hits in "
                  f"{time.monotonic() - started:.1f}s")
        except ev.SearchFailure as exc:
            outcomes[exc.kind] = outcomes.get(exc.kind, 0) + 1
            print(f"\nquery {query[:40]!r}: {exc.kind} after "
                  f"{time.monotonic() - started:.1f}s")
    # Reliability is reported, not asserted: DDGS may rate-limit CI-like
    # environments. Verdicts must never be invented either way (see below).
    print(f"\nsearch outcomes: {outcomes}")
    assert sum(outcomes.values()) == len(QUERIES)


def test_live_end_to_end_never_invents_verdicts(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe DART mission struck Dimorphos, the small moon "
          "orbiting Didymos, in September 2022 after launch.\n")
    db.execute("INSERT INTO sources(source_id, kind, created_at) VALUES('src-live', 'u', 1)")
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, status, "
        "version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("art9-fc-live", "src-live", "T", md, "published", 1, 1.0, 1.0))
    before = db.query_one("SELECT markdown, version, status FROM articles "
                          "WHERE article_id='art9-fc-live'")
    provider = dd.DDGSProvider(
        cache=dd.FactcheckCache(directory=tmp_path / "fc-cache"))
    started = time.monotonic()
    report = runmod.run_factcheck(db, "art9-fc-live", provider=provider,
                                  max_claims=4)
    elapsed = time.monotonic() - started
    overlay = report.overlay
    assert overlay.validate() == []
    by_status = overlay.run_status
    print(f"\nlive run: status={by_status} searches={report.searches} "
          f"fetches={report.fetches} bytes={report.fetch_bytes} "
          f"failures={len(report.failures)} time={elapsed:.1f}s")
    for claim in overlay.claims:
        print(f"  [{claim.assessment}] {claim.quote[:80]} "
              f"({len(claim.evidence)} evidence)")
        for e in claim.evidence:
            print(f"    - {e.relationship} {e.independence_group} "
                  f"{e.url[:80]} loc={e.locator[:24]}")
    for failure in report.failures[:10]:
        print(f"  failure: {failure}")
    # Hard gates hold live too: every assessed excerpt re-verifies against a
    # genuinely fetched page, and the article row is byte-identical.
    for claim in overlay.claims:
        if claim.assessment in ("corroborated", "contradicted"):
            assert claim.evidence, f"categorical verdict without evidence: {claim.claim_id}"
    after = db.query_one("SELECT markdown, version, status FROM articles "
                         "WHERE article_id='art9-fc-live'")
    assert dict(after) == dict(before)
    if by_status == "unavailable":
        assert report.fetches == 0
        assert all(c.assessment in ("not_checked", "insufficient_evidence")
                   or c.claim_kind in ("attributed_opinion", "normative_judgment",
                                       "personal_preference", "prediction", "satire")
                   for c in overlay.claims)
