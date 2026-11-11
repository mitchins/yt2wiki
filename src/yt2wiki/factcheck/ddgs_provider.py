"""Keyless evidence provider: DDGS search + bounded direct page fetch.

No credentials, no paid APIs, no additional model endpoints. Search runs
only when the operator explicitly invokes a fact-check run — never during
generation, viewing, or export. All retrieval is bounded (result counts,
bytes, redirects, time) and cached; failures are typed `SearchFailure`s,
never verdicts.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from .evidence import FetchedPage, SearchFailure, SearchHit, safe_url

# Public-suffix exceptions where the registrable domain needs three labels.
_TWO_LEVEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "co.kr", "com.au",
    "net.au", "org.au", "co.nz", "co.in", "com.br", "co.za",
})

_USER_AGENT = "yt2wiki-factcheck/1 (operator-invoked research; local use only)"
_SEARCH_PAUSE_S = 1.5


def registrable_domain(host: str) -> str:
    """Grouping key for independence: eTLD+1 with a compact suffix table."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) < 2:
        return host.lower()
    if ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def publisher_name(host: str) -> str:
    name = host.lower()
    if name.startswith("www."):
        name = name[4:]
    return name


def _classify_ddgs_error(exc: Exception) -> SearchFailure:
    msg = str(exc)[:300]
    low = msg.lower()
    if "429" in low or "rate" in low or "ratelimit" in low:
        return SearchFailure("rate_limited", f"DDGS rate-limited: {msg}")
    if "403" in low or "captcha" in low or "blocked" in low:
        return SearchFailure("blocked", f"DDGS blocked: {msg}")
    if "timeout" in low or "timed out" in low:
        return SearchFailure("timeout", f"DDGS timeout: {msg}")
    return SearchFailure("network", f"DDGS search failed: {msg}")


class _TextExtractor(HTMLParser):
    """stdlib HTML→text: drops script/style, keeps visible text + key meta."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.meta: dict[str, str] = {}
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip = True
            return
        if tag == "meta":
            d = {k.lower(): (v or "") for k, v in attrs}
            key = d.get("property") or d.get("name") or ""
            if key.lower() in ("article:published_time", "datepublished",
                               "publish_date", "date", "og:title"):
                self.meta[key.lower()] = d.get("content", "")
        if tag in ("p", "br", "div", "article", "section", "h1", "h2", "h3",
                   "h4", "li", "tr", "blockquote"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


@dataclass
class FetchPolicy:
    timeout_s: float = 15.0
    max_bytes: int = 200_000
    max_redirects: int = 3


def _public_ip_ok(ip_str: str) -> bool:
    """True for a public unicast literal (no private/loopback/link-local)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return bool(ip.is_global and not ip.is_multicast)


def _resolve_public_ips(host: str, port: int) -> list[str]:
    """Resolve and verify: every address must be public, else refuse."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SearchFailure("blocked",
                            f"DNS resolution failed for {host}: {exc}") from exc
    ips = [info[4][0] for info in infos]
    if not ips or not all(_public_ip_ok(ip) for ip in ips):
        raise SearchFailure("blocked",
                            f"non-public DNS target for {host}")
    return ips


@contextlib.contextmanager
def _pinned_dns(host: str, port: int, ips: list[str]):
    """Freeze DNS for one request: close the check/connect rebinding gap.

    The hostname is resolved and verified first; for the duration of the
    single request, this process resolves that exact (host, port) to the
    verified addresses only. A hostile resolver flipping answers between
    the check and the connect (DNS rebinding) cannot steer the connection
    elsewhere. Scope is one hostname, one request; always restored.
    """
    real_getaddrinfo = socket.getaddrinfo

    def _shim(h, p, *args, **kwargs):
        if h == host and int(p) == int(port):
            pinned = []
            for ip in ips:
                family = socket.AF_INET6 if ":" in ip else socket.AF_INET
                sockaddr = ((ip, int(port), 0, 0) if family == socket.AF_INET6
                            else (ip, int(port)))
                pinned.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
            return pinned
        return real_getaddrinfo(h, p, *args, **kwargs)

    socket.getaddrinfo = _shim  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[method-assign]


@dataclass
class FactcheckCache:
    """File cache of fetched pages (operator-local, TTL-bounded)."""

    directory: Path | None = None
    ttl_s: float = 7 * 24 * 3600

    def _path(self, url: str) -> Path | None:
        if self.directory is None:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, url: str) -> FetchedPage | None:
        path = self._path(url)
        if path is None or not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - float(record.get("retrieved_epoch", 0)) > self.ttl_s:
            return None
        page = FetchedPage(url=url, title=record.get("title", ""),
                           text=record.get("text", ""),
                           published_at=record.get("published_at"),
                           content_hash=record.get("content_hash", ""))
        return page if page.text else None

    def put(self, url: str, page: FetchedPage) -> None:
        path = self._path(url)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "url": url, "title": page.title, "text": page.text[:500_000],
                "published_at": page.published_at,
                "content_hash": page.content_hash,
                "retrieved_epoch": time.time(),
            }), encoding="utf-8")
        except OSError:
            pass  # cache is best-effort; retrieval already succeeded


@dataclass
class DDGSProvider:
    """`EvidenceProvider` over keyless DDGS search + bounded httpx fetch."""

    name: str = "ddgs"
    fetch_policy: FetchPolicy = field(default_factory=FetchPolicy)
    cache: FactcheckCache = field(default_factory=FactcheckCache)
    primary_domains: tuple[str, ...] = ()
    searches: list[str] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)
    cache_hits: int = 0
    _last_search_at: float = 0.0

    # ------------------------------------------------------------ search --
    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        from ddgs import DDGS
        wait = _SEARCH_PAUSE_S - (time.monotonic() - self._last_search_at)
        if wait > 0:
            time.sleep(wait)
        self.searches.append(query)
        try:
            results = DDGS().text(query, max_results=max(1, min(limit, 10)))
        except Exception as exc:  # noqa: BLE001 — classified below
            raise _classify_ddgs_error(exc) from exc
        finally:
            self._last_search_at = time.monotonic()
        hits = []
        for item in results or []:
            url = (item.get("href") or "").strip()
            if safe_url(url) is None:
                continue
            host = urlparse(url).hostname or ""
            hits.append(SearchHit(url=url, title=(item.get("title") or "")[:300],
                                  publisher=publisher_name(host),
                                  snippet=(item.get("body") or "")[:600]))
        return hits[:limit]

    # ------------------------------------------------------------- fetch --
    def fetch(self, url: str) -> FetchedPage:
        cached = self.cache.get(url)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.fetches.append(url)
        page = self._fetch_bounded(url)
        self.cache.put(url, page)
        return page

    def _fetch_bounded(self, url: str) -> FetchedPage:
        import httpx
        policy = self.fetch_policy
        current = url
        for _ in range(policy.max_redirects + 1):
            if safe_url(current) is None:
                raise SearchFailure("unsupported_url",
                                    f"redirect target rejected: {current[:120]}")
            parts = urlparse(current)
            host = parts.hostname or ""
            port = parts.port or (443 if parts.scheme == "https" else 80)
            # Resolve + verify, then pin for the single request: a resolver
            # that flips answers between check and connect cannot rebind.
            ips = _resolve_public_ips(host, port)
            try:
                # Nested (not parenthesised): the pin must wrap the client.
                with _pinned_dns(host, port, ips):  # noqa: SIM117
                    with httpx.Client(
                            follow_redirects=False, timeout=policy.timeout_s,
                            headers={"User-Agent": _USER_AGENT}) as client, \
                            client.stream("GET", current) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            from urllib.parse import urljoin
                            location = resp.headers.get("location", "")
                            current = (location if location.startswith("http")
                                       else urljoin(current, location))
                            continue
                        if resp.status_code != 200:
                            raise SearchFailure(
                                "network",
                                f"HTTP {resp.status_code} for {current[:120]}")
                        ctype = resp.headers.get("content-type", "")
                        if ("html" not in ctype and "text" not in ctype
                                and ctype.split(";")[0].strip()):
                            raise SearchFailure(
                                "unsupported_url",
                                f"non-text content ({ctype[:60]}) for {current[:120]}")
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in resp.iter_bytes(65536):
                            need = policy.max_bytes - total
                            if need <= 0:
                                break
                            chunks.append(chunk[:need])
                            total += len(chunk)
                        raw = b"".join(chunks)
            except SearchFailure:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SearchFailure("network",
                                    f"fetch failed for {current[:120]}: {str(exc)[:200]}") from exc
            text = raw.decode("utf-8", "replace")
            extractor = _TextExtractor()
            try:
                extractor.feed(text)
            except Exception as exc:  # noqa: BLE001
                raise SearchFailure("parse",
                                    f"HTML parse failed for {current[:120]}") from exc
            body = " ".join("".join(extractor.parts).split())
            if not body:
                raise SearchFailure("parse",
                                    f"no extractable text for {current[:120]}")
            title = (extractor.meta.get("og:title") or "").strip()
            published = (extractor.meta.get("article:published_time")
                         or extractor.meta.get("datepublished")
                         or extractor.meta.get("publish_date")
                         or extractor.meta.get("date") or None)
            return FetchedPage(url=current, title=title, text=body,
                               published_at=published)
        raise SearchFailure("blocked",
                            f"too many redirects for {url[:120]}")

    # -------------------------------------------------------------- group --
    def independence_group(self, url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        domain = registrable_domain(host)
        if domain in {d.lower() for d in self.primary_domains}:
            return f"primary:{domain}"
        return domain
