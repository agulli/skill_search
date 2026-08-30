#!/usr/bin/env python
"""Bulk discovery from GH Archive — free, and not rate-limited.

Repository search is capped at 10 requests/minute unauthenticated, which makes
it the binding constraint on how fast the corpus can grow. GH Archive has no
such limit: it publishes hourly dumps of every public GitHub event, and each
hour names tens of thousands of distinct repositories.

Filtering those names for skill-related tokens costs nothing but bandwidth and
CPU, and runs alongside both the search-driven discovery and the harvest without
competing with either. A name match is only a candidate — the harvest confirms
it — but confirming is exactly what the tarball path already does for free.

    python gharchive_mine.py data/scale.db 48      # mine the last 48 hours
"""
import asyncio, gzip, io, json, logging, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx

from skill_engine.config import USER_AGENT
from skill_engine.store import Store

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scale.db"
HOURS = int(sys.argv[2]) if len(sys.argv) > 2 else 48
CONCURRENCY = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s A %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("gharchive")

# Tokens that make a bare repository name worth a confirmation fetch. Broader
# than the crawler's live filter because here a false positive costs one
# quota-free tarball, not an API request.
HINTS = ("skill", "agent", "claude", "mcp", "prompt", "llm", "anthropic",
         "copilot", "cursor", "openclaw", "subagent", "ai-tool", "aiagent")
INTERESTING = {"PushEvent", "CreateEvent", "ReleaseEvent", "PublicEvent",
               "ForkEvent", "WatchEvent"}


async def mine_hour(client, ts, seen, lock, stats):
    url = (f"https://data.gharchive.org/{ts.year:04d}-{ts.month:02d}-"
           f"{ts.day:02d}-{ts.hour}.json.gz")
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        raw = gzip.decompress(resp.content)
    except Exception as exc:
        log.warning("%s: %s", url.rsplit("/", 1)[-1], type(exc).__name__)
        return []

    found, total = [], 0
    for line in io.BytesIO(raw):
        try:
            event = json.loads(line)
        except Exception:
            continue
        total += 1
        if event.get("type") not in INTERESTING:
            continue
        name = (event.get("repo") or {}).get("name")
        if not name:
            continue
        lowered = name.lower()
        if any(h in lowered for h in HINTS):
            async with lock:
                if name not in seen:
                    seen.add(name)
                    found.append(name)
    stats["events"] += total
    stats["bytes"] += len(resp.content)
    log.info("%s: %d events -> %d new candidates",
             url.rsplit("/", 1)[-1], total, len(found))
    return found


async def main():
    store = Store(Path(DB))
    seen = {r["full_name"] for r in store.db.execute("SELECT full_name FROM repos")}
    log.info("mining %d hours; %d repos already known", HOURS, len(seen))

    lock = asyncio.Lock()
    stats = {"events": 0, "bytes": 0}
    added = 0
    now = datetime.now(timezone.utc) - timedelta(hours=2)   # publishing lag
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as client:
        async def one(offset):
            async with sem:
                return await mine_hour(client, now - timedelta(hours=offset),
                                       seen, lock, stats)

        for chunk_start in range(0, HOURS, CONCURRENCY * 2):
            offsets = range(chunk_start, min(chunk_start + CONCURRENCY * 2, HOURS))
            for names in await asyncio.gather(*(one(o) for o in offsets)):
                for name in names:
                    # Low priority: these are name-matched guesses, so they
                    # queue behind anything search actually confirmed.
                    # A queue entry with no repos row is invisible to the
                    # sweep, which joins the two — so create the stub first.
                    store.ensure_repo_stub(name, "gharchive-mine")
                    store.enqueue(name, "gharchive-mine", 60)
                    added += 1
            store.commit()
            log.info("running total: +%d candidates, %.1fGB read, %d events",
                     added, stats["bytes"] / 1e9, stats["events"])

    store.commit()
    log.info("DONE: +%d candidates from %d events (%.1fGB), 0 API requests",
             added, stats["events"], stats["bytes"] / 1e9)
    store.close()

asyncio.run(main())
