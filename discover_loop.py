#!/usr/bin/env python
"""Continuous repository discovery process.

Runs repository search and awesome-list mining concurrently with harvesters.
Uses SQLite WAL concurrency to safely interleave queue writes without blocking
active crawler workers.

Usage:
    python discover_loop.py data/scale.db 300000
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.config import Config
from skill_engine.discover import (
    BREADTH_QUERIES,
    KEYWORD_QUERIES,
    SCALE_QUERIES,
    mine_awesome_lists,
    search_repos,
)
from skill_engine.github import GitHubClient
from skill_engine.store import Store

if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
    print(__doc__.strip())
    sys.exit(0)

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scale.db"

TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 300_000


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("skill_engine.github").setLevel(logging.WARNING)
log = logging.getLogger("discover")


async def main() -> None:
    cfg = Config(db_path=Path(DB))
    store = Store(cfg.db_path)
    gh = GitHubClient(cfg.tokens, etag_store=store)

    def known_count() -> int:
        return store.db.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"]

    log.info("Starting discovery loop: %d known repositories, target: %d", known_count(), TARGET)

    queries = SCALE_QUERIES + KEYWORD_QUERIES + BREADTH_QUERIES
    try:
        for cycle in range(40):
            for q in queries:
                if known_count() >= TARGET:
                    log.info("Target reached: %d repositories", known_count())
                    return
                try:
                    _, new = await search_repos(gh, store, q, reason="scale", priority=115)
                    log.info("%-46s +%-6d (Total: %d)", q[:46], new, known_count())
                except Exception as exc:
                    log.warning("Discovery query %r failed: %s", q, exc)

            # Awesome list mining (zero API quota)
            try:
                hubs = [
                    r["full_name"]
                    for r in store.db.execute(
                        "SELECT full_name FROM repos WHERE full_name LIKE '%awesome%' "
                        "ORDER BY stars DESC LIMIT 40"
                    )
                ]
                if hubs:
                    await mine_awesome_lists(gh, store, hubs)
                    log.info("Awesome-list pass complete. Total: %d", known_count())
            except Exception as exc:
                log.warning("Awesome mining failed: %s", exc)
    finally:
        await gh.aclose()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
