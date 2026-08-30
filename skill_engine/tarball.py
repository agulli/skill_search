"""Quota-free harvesting via codeload tarballs.

The REST path costs one core request per repository for the tree listing, which
caps an unauthenticated crawler at ~30 repos/hour and a single-token crawler at
~5,000/hour. `codeload.github.com` is not part of the REST API and does not draw
on that budget at all: one download returns every file in the repository,
including every `SKILL.md`, for zero quota.

Combined with the fact that repository search already gave us complete metadata
(default branch included), a tarball harvest needs **no API requests
whatsoever**. Throughput stops being quota-bound and becomes bandwidth-bound,
which is a difference of two orders of magnitude.

The trade is that we download the whole repository to read a handful of files,
so this path is chosen by size: `size_kb` is known for every repo before we
fetch anything, and oversized ones fall back to the tree API. Downloads are
capped, streamed rather than buffered, and deliberately limited in concurrency —
this endpoint is a courtesy, not an entitlement, and hammering it is how you
lose access to it.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
import time
from typing import Any

import httpx

from .config import USER_AGENT
from .parse import classify_path, parse_skill, skill_slug_from_path

log = logging.getLogger("skill_engine.tarball")

CODELOAD = "https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}"


class TarballFetcher:
    """Polite, bounded downloader for repository archives."""

    def __init__(
        self,
        concurrency: int = 4,
        max_bytes: int = 120 * 1024 * 1024,
        min_delay: float = 0.15,
        timeout: float = 180.0,
    ) -> None:
        self.max_bytes = max_bytes
        self.min_delay = min_delay
        self._sem = asyncio.Semaphore(concurrency)
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
            limits=httpx.Limits(max_connections=concurrency * 2),
        )
        self.stats = {"downloads": 0, "bytes": 0, "too_big": 0, "failed": 0}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _pace(self) -> None:
        """Keep a floor between request starts, regardless of concurrency."""
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_delay:
                await asyncio.sleep(self.min_delay - gap)
            self._last = time.monotonic()

    async def fetch(self, owner: str, repo: str, ref: str) -> bytes | None:
        """Download an archive, aborting early if it exceeds the size cap."""
        url = CODELOAD.format(owner=owner, repo=repo, ref=ref)
        async with self._sem:
            await self._pace()
            try:
                async with self.client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        if resp.status_code in (403, 429):
                            # Back off hard: this endpoint has no quota header
                            # to consult, so the only safe response is patience.
                            log.warning("codeload %s on %s/%s; pausing",
                                        resp.status_code, owner, repo)
                            await asyncio.sleep(30)
                        self.stats["failed"] += 1
                        return None

                    declared = resp.headers.get("content-length")
                    if declared and int(declared) > self.max_bytes:
                        self.stats["too_big"] += 1
                        return None

                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(65536):
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            # Most archives do not declare a length; stop as
                            # soon as one grows past the budget.
                            self.stats["too_big"] += 1
                            return None
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                log.debug("codeload %s/%s failed: %s", owner, repo, exc)
                self.stats["failed"] += 1
                return None

        self.stats["downloads"] += 1
        self.stats["bytes"] += len(buf)
        return bytes(buf)


def extract_skills(blob: bytes, max_files: int = 5000) -> list[tuple[str, str]]:
    """Pull every SKILL.md out of an archive as (repo-relative path, text).

    Streams through the archive rather than extracting it: nothing is written to
    disk, so a hostile archive cannot escape a directory it was never given.
    """
    out: list[tuple[str, str]] = []
    try:
        tar = tarfile.open(fileobj=io.BytesIO(blob), mode="r|gz")
    except tarfile.TarError as exc:
        log.debug("bad archive: %s", exc)
        return out

    try:
        for member in tar:
            if not member.isfile() or len(out) >= max_files:
                continue
            name = member.name
            if name.rsplit("/", 1)[-1].lower() != "skill.md":
                continue
            if member.size > 512 * 1024:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            raw = handle.read()
            # GitHub archives nest everything under "<repo>-<sha>/"; strip it
            # so paths match what the tree API would have reported.
            path = name.split("/", 1)[1] if "/" in name else name
            out.append((path, raw.decode("utf-8", "replace")))
    except (tarfile.TarError, EOFError, OSError) as exc:
        # A truncated archive still yields whatever was read before the break.
        log.debug("archive read stopped early: %s", exc)
    finally:
        try:
            tar.close()
        except Exception:
            pass
    return out


async def harvest_repo_tarball(
    fetcher: TarballFetcher, store, full_name: str, cfg, repo_row: Any
) -> int | None:
    """Index a repository from its archive. Returns None if the path bailed out.

    None means "try the REST path instead" — an oversized or unavailable
    archive — and is deliberately distinct from 0, which means "downloaded fine,
    contains no skills".
    """
    owner, _, repo = full_name.partition("/")
    branch = (repo_row["default_branch"] if repo_row else None) or "HEAD"

    blob = await fetcher.fetch(owner, repo, branch)
    if blob is None and branch != "HEAD":
        blob = await fetcher.fetch(owner, repo, "HEAD")
    if blob is None:
        return None

    found = extract_skills(blob)
    total = len(found)
    if total > cfg.max_skills_per_repo:
        found = found[: cfg.max_skills_per_repo]

    for path, text in found:
        parsed = parse_skill(text, path)
        store.upsert_skill({
            "repo": full_name,
            "path": path,
            "name": parsed.name or skill_slug_from_path(path),
            "description": parsed.description,
            "body": parsed.body[:200_000],
            "heading": parsed.heading,
            "version": parsed.version,
            "license": parsed.license or (repo_row["license"] if repo_row else None),
            "allowed_tools": json.dumps(parsed.allowed_tools),
            "metadata": json.dumps({**parsed.metadata, **parsed.extra}, default=str),
            "resources": json.dumps(parsed.resources),
            "source_kind": classify_path(path),
            # Archives carry no git blob SHAs; the content hash serves the same
            # change-detection role, just computed by us instead of by git.
            "blob_sha": "",
            "content_hash": parsed.content_hash,
            "body_len": parsed.body_len,
            "score": 0.0,
            "valid": int(parsed.valid),
            "invalid_reason": parsed.invalid_reason,
            "warnings": parsed.notes,
        })

    store.mark_repo(full_name, tree_sha=f"tar:{len(blob)}", skill_count=total,
                    error=None)
    store.dequeue(full_name)
    store.commit()
    return total


async def run_tarball_crawl(
    store,
    cfg,
    *,
    limit: int = 100_000,
    target_skills: int | None = None,
    concurrency: int = 4,
    max_mb: int = 120,
    batch: int = 24,
    rerank_every: int = 400,
    on_progress=None,
) -> dict:
    """Sweep the queue over codeload, spending no API quota at all.

    Built to run unattended: every failure mode is caught per-repository, the
    queue is the checkpoint so a kill and restart resumes exactly where it left
    off, and scores are recomputed periodically so ordering keeps improving as
    the corpus grows.
    """
    from .ranking import recompute

    fetcher = TarballFetcher(concurrency=concurrency, max_bytes=max_mb * 1024 * 1024)
    totals = {"repos": 0, "skills": 0, "empty": 0, "fallback": 0, "errors": 0}
    started = time.time()
    since_rerank = 0

    try:
        while totals["repos"] < limit:
            rows = store.db.execute(
                """
                SELECT q.full_name, r.default_branch, r.license, r.size_kb,
                       COALESCE(r.repo_score, 0) AS repo_score
                FROM queue q
                JOIN repos r ON r.full_name = q.full_name
                WHERE q.attempts < 4
                  AND r.tree_sha IS NULL
                  AND COALESCE(r.size_kb, 0) <= ?
                  AND r.disabled = 0
                ORDER BY r.repo_score DESC, q.priority DESC
                LIMIT ?
                """,
                (max_mb * 1024, batch),
            ).fetchall()
            if not rows:
                log.info("queue drained for the tarball path")
                break

            store.db.executemany(
                "UPDATE queue SET attempts = attempts + 1 WHERE full_name = ?",
                [(r["full_name"],) for r in rows],
            )
            store.commit()

            results = await asyncio.gather(
                *(harvest_repo_tarball(fetcher, store, r["full_name"], cfg, r)
                  for r in rows),
                return_exceptions=True,
            )

            for row, res in zip(rows, results):
                totals["repos"] += 1
                since_rerank += 1
                if isinstance(res, Exception):
                    totals["errors"] += 1
                    log.debug("%s: %s", row["full_name"], res)
                elif res is None:
                    # Archive unavailable or oversized; leave it queued so the
                    # REST path can pick it up when quota allows.
                    totals["fallback"] += 1
                elif res == 0:
                    totals["empty"] += 1
                else:
                    totals["skills"] += res

            indexed = store.db.execute(
                "SELECT COUNT(*) c FROM skills").fetchone()["c"]
            rate = totals["repos"] / max(time.time() - started, 1) * 3600
            if on_progress:
                on_progress(totals, indexed, rate, fetcher.stats)

            if target_skills and indexed >= target_skills:
                log.info("target of %d skills reached", target_skills)
                break

            if since_rerank >= rerank_every:
                since_rerank = 0
                try:
                    recompute(store)
                    log.info("rescored corpus; ordering refreshed")
                except Exception as exc:
                    log.warning("rerank failed (continuing): %s", exc)
    finally:
        await fetcher.aclose()
        try:
            recompute(store)
        except Exception as exc:
            log.warning("final rerank failed: %s", exc)

    totals["download_stats"] = fetcher.stats
    return totals
