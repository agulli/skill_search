#!/usr/bin/env python
"""Continuous discovery, run alongside a harvest rather than after it.

Discovery draws on the search rate-limit bucket; harvesting draws on bandwidth.
Running them in series — as the overnight supervisor does — leaves one resource
idle while the other works, and doubles the wall time to reach a target. This
runs the discovery half independently so both saturate at once.

SQLite WAL permits one writer at a time; both processes take short write
transactions and wait on a 30s busy timeout, so they interleave rather than
collide.
"""
import asyncio, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.config import Config
from skill_engine.discover import (BREADTH_QUERIES, KEYWORD_QUERIES,
                                   SCALE_QUERIES, mine_awesome_lists, search_repos)
from skill_engine.github import GitHubClient
from skill_engine.store import Store

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scale.db"
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 300_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s D %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("skill_engine.github").setLevel(logging.WARNING)
log = logging.getLogger("discover")


async def main():
    cfg = Config(db_path=Path(DB))
    store = Store(cfg.db_path)
    gh = GitHubClient(cfg.tokens, etag_store=store)
    known = lambda: store.db.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"]
    log.info("starting at %d repos, target %d", known(), TARGET)

    # Widest first: the biggest veins are worth the most bisection effort.
    queries = SCALE_QUERIES + KEYWORD_QUERIES + BREADTH_QUERIES
    try:
        for cycle in range(40):
            for q in queries:
                if known() >= TARGET:
                    log.info("target reached: %d repos", known())
                    return
                try:
                    seen, new = await search_repos(gh, store, q, reason="scale",
                                                   priority=115)
                    log.info("%-46s +%-6d total %d", q[:46], new, known())
                except Exception as exc:
                    log.warning("%r failed: %s", q, exc)
            # Awesome-list mining costs no API quota, so it is free to repeat.
            try:
                hubs = [r["full_name"] for r in store.db.execute(
                    "SELECT full_name FROM repos WHERE full_name LIKE '%awesome%' "
                    "ORDER BY stars DESC LIMIT 40")]
                if hubs:
                    await mine_awesome_lists(gh, store, hubs)
                    log.info("awesome-list pass done, total %d", known())
            except Exception as exc:
                log.warning("awesome mining failed: %s", exc)
    finally:
        await gh.aclose()
        store.close()

asyncio.run(main())
