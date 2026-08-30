"""A GitHub API client built for sustained crawling.

Three things keep this alive where a naive crawler dies:

1. **Conditional requests.** Every GET carries the ETag we saw last time. A 304
   response costs no rate-limit quota at all, so re-crawling a repo that has not
   changed is free. This matters more than any other optimisation here.
2. **A token pool.** Each PAT gets 5,000 core requests/hour. We track every
   token's remaining quota per resource bucket (core, search, code_search,
   graphql) from the response headers and always route to the token with the
   most headroom, sleeping only when every token is spent.
3. **Honest backoff.** Primary limits are visible in the headers. Secondary
   limits are not: they arrive as a 403 or 429 with a `retry-after` header and
   no warning. We obey the header exactly rather than guessing.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import USER_AGENT

log = logging.getLogger("skill_engine.github")

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

# GitHub reports which bucket a request drew from via `x-ratelimit-resource`.
# These are the starting assumptions before we have seen a real header.
DEFAULT_LIMITS = {"core": 5000, "search": 30, "code_search": 10, "graphql": 5000}


class RateLimited(Exception):
    """Every token in the pool is exhausted for the requested resource."""


class NotFound(Exception):
    """404, 451 (DMCA), or 409 (empty repository) — do not retry."""


@dataclass
class Bucket:
    limit: int
    remaining: int
    reset_at: float

    @property
    def available(self) -> bool:
        return self.remaining > 0 or time.time() >= self.reset_at


@dataclass
class Token:
    value: str
    label: str
    buckets: dict[str, Bucket] = field(default_factory=dict)
    # Set when a secondary rate limit tells this specific token to back off.
    blocked_until: float = 0.0

    def bucket(self, resource: str) -> Bucket:
        if resource not in self.buckets:
            self.buckets[resource] = Bucket(
                limit=DEFAULT_LIMITS.get(resource, 5000),
                remaining=DEFAULT_LIMITS.get(resource, 5000),
                reset_at=time.time() + 3600,
            )
        b = self.buckets[resource]
        if time.time() >= b.reset_at:
            b.remaining = b.limit
            b.reset_at = time.time() + 3600
        return b

    def headroom(self, resource: str) -> int:
        if time.time() < self.blocked_until:
            return -1
        return self.bucket(resource).remaining

    def ready_at(self, resource: str) -> float:
        """When this token can next be used for `resource`.

        A token parked by a secondary rate limit still has quota left, so it
        frees up at `blocked_until` — waiting for the hourly reset instead would
        idle the crawler for up to an hour over a 60-second cool-off.
        """
        bucket = self.bucket(resource)
        if bucket.remaining > 0:
            return self.blocked_until
        return max(self.blocked_until, bucket.reset_at)


class GitHubClient:
    def __init__(
        self,
        tokens: list[str],
        etag_store: Any | None = None,
        concurrency: int = 6,
        raw_concurrency: int = 12,
        max_retries: int = 4,
    ) -> None:
        self.tokens = [
            Token(value=t, label=f"tok{i}:…{t[-4:]}") for i, t in enumerate(tokens)
        ]
        if not self.tokens:
            # Anonymous still works, at 60 req/hour. Useful for a smoke test only.
            self.tokens = [Token(value="", label="anonymous")]
            for tk in self.tokens:
                tk.buckets["core"] = Bucket(60, 60, time.time() + 3600)
        self.etags = etag_store
        self.max_retries = max_retries
        self._sem = asyncio.Semaphore(concurrency)
        self._raw_sem = asyncio.Semaphore(raw_concurrency)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        self.stats = {"requests": 0, "not_modified": 0, "retries": 0, "waited": 0.0}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- tokens

    async def _pick(self, resource: str) -> Token:
        """Return the token with the most headroom, waiting if all are spent."""
        while True:
            best = max(self.tokens, key=lambda t: t.headroom(resource))
            if best.headroom(resource) > 0:
                return best
            wait = min(t.ready_at(resource) for t in self.tokens) - time.time()
            wait = max(1.0, min(wait, 900.0))
            log.warning(
                "all tokens exhausted for %s; sleeping %.0fs", resource, wait
            )
            self.stats["waited"] += wait
            await asyncio.sleep(wait)

    def _absorb(self, token: Token, resp: httpx.Response) -> None:
        """Update our view of the token's quota from the response headers."""
        resource = resp.headers.get("x-ratelimit-resource", "core")
        try:
            limit = int(resp.headers["x-ratelimit-limit"])
            remaining = int(resp.headers["x-ratelimit-remaining"])
            reset_at = float(resp.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            return
        token.buckets[resource] = Bucket(limit, remaining, reset_at)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        """Seconds to sleep, taken from the response rather than guessed."""
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return float(ra)
            except ValueError:
                pass
        reset = resp.headers.get("x-ratelimit-reset")
        if reset:
            try:
                return max(1.0, float(reset) - time.time())
            except ValueError:
                pass
        return 60.0

    # --------------------------------------------------------------- request

    async def request(
        self,
        method: str,
        path: str,
        *,
        resource: str = "core",
        etag_key: str | None = None,
        accept: str = "application/vnd.github+json",
        **kwargs: Any,
    ) -> httpx.Response | None:
        """Perform one API call. Returns None when the server says 304."""
        url = path if path.startswith("http") else f"{API}{path}"
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            **kwargs.pop("headers", {}),
        }

        cached = None
        if etag_key and self.etags is not None:
            cached = self.etags.get_etag(etag_key)
            if cached:
                headers["If-None-Match"] = cached

        # Transport and 5xx failures count as retries; rate-limit backoffs do
        # not, because waiting out a quota window is normal operation rather
        # than a failure. Both are bounded so a request can never loop forever.
        attempt = 0
        backoffs = 0
        while attempt <= self.max_retries and backoffs <= 8:
            token = await self._pick(resource)
            if token.value:
                headers["Authorization"] = f"Bearer {token.value}"

            try:
                async with self._sem:
                    resp = await self._client.request(
                        method, url, headers=headers, **kwargs
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self.max_retries:
                    raise
                delay = 2**attempt + random.random()
                log.debug("transport error %s; retry in %.1fs", exc, delay)
                self.stats["retries"] += 1
                attempt += 1
                await asyncio.sleep(delay)
                continue

            self.stats["requests"] += 1
            self._absorb(token, resp)

            if resp.status_code == 304:
                # Free: a 304 is not charged against the rate limit.
                self.stats["not_modified"] += 1
                return None

            if resp.status_code < 300:
                if etag_key and self.etags is not None:
                    tag = resp.headers.get("etag")
                    if tag:
                        self.etags.put_etag(etag_key, tag)
                return resp

            if resp.status_code in (404, 451, 409):
                raise NotFound(f"{resp.status_code} {url}")

            if resp.status_code in (403, 429):
                body = resp.text[:200].lower()
                secondary = "secondary rate" in body or "abuse" in body
                primary = resp.headers.get("x-ratelimit-remaining") == "0"
                if secondary or primary or resp.status_code == 429:
                    delay = self._retry_after(resp)
                    if secondary:
                        # Park this token only; siblings may still be healthy,
                        # and _pick will route the retry to one of them.
                        token.blocked_until = time.time() + delay
                        log.warning(
                            "secondary rate limit on %s; parked for %.0fs",
                            token.label,
                            delay,
                        )
                    else:
                        token.bucket(resource).remaining = 0
                        token.bucket(resource).reset_at = time.time() + delay
                    # No sleep here: _pick either hands us a healthy token
                    # immediately or does the waiting itself.
                    backoffs += 1
                    continue
                # A real permission problem — retrying will not fix it.
                raise NotFound(f"403 {url}: {resp.text[:200]}")

            if resp.status_code >= 500:
                if attempt >= self.max_retries:
                    resp.raise_for_status()
                delay = 2**attempt + random.random()
                self.stats["retries"] += 1
                attempt += 1
                await asyncio.sleep(delay)
                continue

            resp.raise_for_status()

        raise RateLimited(f"gave up on {url} after {attempt} retries, {backoffs} backoffs")

    async def get_json(self, path: str, **kw: Any) -> Any | None:
        resp = await self.request("GET", path, **kw)
        return None if resp is None else resp.json()

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        resource: str = "core",
        max_pages: int = 10,
        items_key: str | None = None,
    ) -> AsyncIterator[dict]:
        """Walk Link-header pagination, stopping at max_pages."""
        params: dict[str, Any] | None = dict(params or {})
        params.setdefault("per_page", 100)
        url: str | None = path
        pages = 0
        while url and pages < max_pages:
            resp = await self.request("GET", url, resource=resource, params=params)
            if resp is None:
                return
            payload = resp.json()
            items = payload.get(items_key, []) if items_key else payload
            if isinstance(items, dict):
                items = items.get("items", [])
            for item in items:
                yield item
            pages += 1
            url = _next_link(resp.headers.get("link", ""))
            # None, not {}: httpx *replaces* a URL's query string when given a
            # params mapping, so an empty dict would strip the page cursor out
            # of the Link URL and re-request page 1 forever.
            params = None

    async def graphql(self, query: str, variables: dict) -> dict:
        resp = await self.request(
            "POST",
            "https://api.github.com/graphql",
            resource="graphql",
            json={"query": query, "variables": variables},
        )
        assert resp is not None  # GraphQL is POST; never 304
        payload = resp.json()
        if "errors" in payload:
            # Partial data plus per-node errors is normal when a repo vanished.
            log.debug("graphql errors: %s", payload["errors"][:3])
        return payload.get("data") or {}

    # ------------------------------------------------------------------ raw

    async def raw_file(self, owner: str, repo: str, ref: str, path: str) -> str | None:
        """Fetch file content from raw.githubusercontent.com.

        This host does not draw on the REST rate limit, which is why content
        fetches are cheap. It has its own undocumented throttle, so we keep a
        separate, modest concurrency budget for it.
        """
        url = f"{RAW}/{owner}/{repo}/{ref}/{path}"
        for attempt in range(3):
            try:
                async with self._raw_sem:
                    resp = await self._client.get(url)
            except (httpx.TransportError, httpx.TimeoutException):
                await asyncio.sleep(2**attempt)
                continue
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (404, 451):
                return None
            if resp.status_code == 429:
                await asyncio.sleep(float(resp.headers.get("retry-after", 30)))
                continue
            await asyncio.sleep(2**attempt)
        return None

    def quota_summary(self) -> str:
        parts = []
        for t in self.tokens:
            core = t.bucket("core")
            parts.append(f"{t.label} core={core.remaining}/{core.limit}")
        return "; ".join(parts)


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1].strip():
            return section[0].strip().strip("<>")
    return None
