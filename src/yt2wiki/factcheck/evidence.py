"""Evidence provider interface, mock provider, citation gate, URL safety.

The language model may propose queries and assess retrieved material, but
must never invent a source URL, citation, title, quotation, or retrieval
result. Every displayed supporting excerpt must occur in material genuinely
fetched from that source. A search snippet alone is not corroboration.

Retrieved documents are untrusted evidence, never executable instructions.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

_WS_RE = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    """Collapse whitespace for robust excerpt matching."""
    return _WS_RE.sub(" ", text).strip()


class SearchFailure(Exception):
    """Typed evidence-acquisition failure (never a verdict)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # network|blocked|rate_limited|parse|unsupported_url|timeout


@dataclass
class SearchHit:
    url: str
    title: str = ""
    publisher: str = ""
    published_at: str | None = None
    snippet: str = ""  # display only: snippets alone never corroborate


@dataclass
class FetchedPage:
    url: str
    title: str = ""
    text: str = ""
    published_at: str | None = None
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash and self.text:
            self.content_hash = hashlib.sha256(
                self.text.encode("utf-8", "replace")).hexdigest()


class EvidenceProvider(Protocol):
    """Injectable search-and-fetch interface (stage A: mock; stage B: DDGS)."""

    name: str

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        ...

    def fetch(self, url: str) -> FetchedPage:
        ...


@dataclass
class MockProvider:
    """Deterministic fixture provider for offline qualification."""

    name: str = "mock"
    hits: dict[str, list[SearchHit]] = field(default_factory=dict)
    pages: dict[str, FetchedPage] = field(default_factory=dict)
    failures: dict[str, SearchFailure] = field(default_factory=dict)
    searches: list[str] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        self.searches.append(query)
        if query in self.failures:
            raise self.failures[query]
        return list(self.hits.get(query, []))[:limit]

    def fetch(self, url: str) -> FetchedPage:
        self.fetches.append(url)
        if url in self.failures:
            raise self.failures[url]
        try:
            return self.pages[url]
        except KeyError:
            raise SearchFailure("network", f"mock has no page for {url}") from None


_UNSAFE_HOSTS = (".localhost", ".local", ".invalid", ".internal", ".onion")


def safe_url(url: str) -> str | None:
    """Return the URL when it is fetchable evidence, else None.

    http/https only, no credentials, no literal private/loopback/link-local
    IPs, no metadata-style or LAN hostnames. Hostname resolution happens at
    fetch time with timeouts (stage B); this is the static first screen.
    """
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if any(host == h.lstrip(".") or host.endswith(h) for h in _UNSAFE_HOSTS):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                           or ip.is_multicast or ip.is_reserved):
        return None
    return url.strip()


def verify_excerpt(page_text: str, excerpt: str) -> str | None:
    """Check the excerpt genuinely occurs in the fetched page text.

    Returns a verifiable locator (`char {start}-{end}` into the
    whitespace-normalised page text) or None when the excerpt is absent.
    Matching is exact on normalised text: a near-quote is not a quote.
    """
    norm_page = normalise_text(page_text)
    norm_excerpt = normalise_text(excerpt)
    if not norm_excerpt or len(norm_excerpt) < 12:
        return None  # too short to be a verifiable passage
    start = norm_page.find(norm_excerpt)
    if start < 0:
        return None
    return f"char {start}-{start + len(norm_excerpt)}"
