#!/usr/bin/env python
"""Bulk candidate repository discovery from GH Archive event streams.

This script streams public hourly GitHub event dumps from data.gharchive.org to
identify newly created, pushed, or starred repositories containing agent skills.
Candidate discovery via GH Archive consumes zero GitHub REST API quota.

Usage:
    python gharchive_mine.py data/scale.db 48      # Mine candidate events from last 48 hours
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from skill_engine.config import USER_AGENT
from skill_engine.store import Store

if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
    print(__doc__.strip())
    sys.exit(0)

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scale.db"
HOURS = int(sys.argv[2]) if len(sys.argv) > 2 else 48
SKIP = int(sys.argv[3]) if len(sys.argv) > 3 else 0
CONCURRENCY = int(os.getenv("GHARCHIVE_CONCURRENCY", "3"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gharchive")

# Keywords matching candidate skill repositories
HINTS = (
    "skill", "skills", "agent", "agents", "gemini", "antigravity", "claude", "mcp",
    "prompt", "llm", "anthropic", "copilot", "cursor", "openclaw", "subagent",
    "ai-tool", "aiagent", "google-genai",
)

INTERESTING_EVENTS = {
    "PushEvent", "CreateEvent", "ReleaseEvent", "PublicEvent",
    "ForkEvent", "WatchEvent",
}


async def mine_hour(
    client: httpx.AsyncClient,
    ts: datetime,
    seen: set[str],
    lock: asyncio.Lock,
    stats: dict[str, Any],
) -> list[str]:
    """Downloads and filters one hourly archive file."""
    url = f"https://data.gharchive.org/{ts.year:04d}-{ts.month:02d}-{ts.day:02d}-{ts.hour}.json.gz"
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        raw = gzip.decompress(resp.content)
    except Exception as exc:
        log.warning("%s: %s", url.rsplit("/", 1)[-1], type(exc).__name__)
        return []

    found: list[str] = []
    total = 0
    for line in io.BytesIO(raw):
        try:
            event = json.loads(line)
        except Exception:
            continue
        total += 1
        if event.get("type") not in INTERESTING_EVENTS:
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
    log.info("%s: %d events -> %d new candidates", url.rsplit("/", 1)[-1], total, len(found))
    return found


async def main() -> None:
    store = Store(Path(DB))
    seen = {r["full_name"] for r in store.db.execute("SELECT full_name FROM repos")}
    log.info(
        "Mining %d hours (offset: -%dh); %d repositories already indexed",
        HOURS, SKIP, len(seen),
    )

    lock = asyncio.Lock()
    stats = {"events": 0, "bytes": 0}
    added = 0
    now = datetime.now(timezone.utc) - timedelta(hours=2)
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=180.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        async def one(offset: int) -> list[str]:
            async with sem:
                return await mine_hour(client, now - timedelta(hours=offset), seen, lock, stats)

        for chunk_start in range(SKIP, SKIP + HOURS, CONCURRENCY * 2):
            offsets = range(chunk_start, min(chunk_start + CONCURRENCY * 2, SKIP + HOURS))
            for names in await asyncio.gather(*(one(o) for o in offsets)):
                for name in names:
                    store.ensure_repo_stub(name, "gharchive-mine")
                    store.enqueue(name, "gharchive-mine", 60)
                    added += 1
            store.commit()
            log.info(
                "Progress: +%d candidates | %.1f GB processed | %d total events",
                added, stats["bytes"] / 1e9, stats["events"],
            )

    store.commit()
    log.info(
        "Complete: +%d candidate repositories identified from %d events (%.1f GB)",
        added, stats["events"], stats["bytes"] / 1e9,
    )
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
