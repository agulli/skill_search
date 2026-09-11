#!/usr/bin/env python
"""Unattended supervisor pipeline: alternates discovery and harvesting.

Coordinates two independent ingestion loops:
  1. Sweep: Streams repository archives over codeload (zero API quota, bandwidth-bound).
  2. Discover: Expands repository queue via search bucket when queue depth drops.

All progress checkpoints in SQLite WAL. The supervisor can be safely stopped
and resumed at any point without lost state.

Usage:
    python overnight.py 100000 data/big.db
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.config import Config
from skill_engine.discover import (
    BREADTH_QUERIES,
    KEYWORD_QUERIES,
    SCALE_QUERIES,
    search_repos,
)
from skill_engine.github import GitHubClient
from skill_engine.ranking import recompute
from skill_engine.store import Store
from skill_engine.tarball import run_tarball_crawl

if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
    print(__doc__.strip())
    sys.exit(0)

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
DB = sys.argv[2] if len(sys.argv) > 2 else "data/big.db"
MAX_MB = int(os.getenv("SKILL_ENGINE_MAX_MB", "10"))
CONCURRENCY = int(os.getenv("SKILL_ENGINE_SWEEP_CONCURRENCY", "5"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("skill_engine.github").setLevel(logging.WARNING)
log = logging.getLogger("overnight")


def get_counts(store: Store) -> tuple[int, int]:
    """Returns total indexed skills and remaining queued repositories."""
    skills = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    queued = store.db.execute(
        "SELECT COUNT(*) c FROM queue q JOIN repos r ON r.full_name = q.full_name "
        "WHERE q.attempts < 4 AND r.tree_sha IS NULL AND COALESCE(r.size_kb,0) <= ?",
        (MAX_MB * 1024,),
    ).fetchone()["c"]
    return skills, queued


async def phase_sweep(store: Store, cfg: Config, target: int) -> int:
    """Processes queued repositories via high-throughput archive streams."""
    started = time.time()

    def progress(totals: dict[str, Any], indexed: int, rate: float, dl: dict[str, Any]) -> None:
        log.info(
            "Sweep: %6d repos | %7d skills | %5.0f repos/hr | %5.1f GB | "
            "Skipped: %d | Failed: %d",
            totals["repos"],
            indexed,
            rate,
            dl["bytes"] / 1e9,
            dl["too_big"],
            dl["failed"],
        )

    totals = await run_tarball_crawl(
        store,
        cfg,
        target_skills=target,
        concurrency=CONCURRENCY,
        max_mb=MAX_MB,
        batch=int(os.getenv("SKILL_ENGINE_SWEEP_BATCH", "400")),
        rerank_every=int(os.getenv("SKILL_ENGINE_RERANK_EVERY", "2000")),
        on_progress=progress,
    )
    skills, _ = get_counts(store)
    log.info(
        "Sweep complete: %d repos in %.1f min (%d skills total, %.1f GB downloaded)",
        totals["repos"],
        (time.time() - started) / 60,
        skills,
        totals.get("download_stats", {}).get("bytes", 0) / 1e9,
    )
    return skills


async def phase_discover(store: Store, cfg: Config, rounds: int) -> int:
    """Expands the queue using search endpoints (uses separate search rate-limit bucket)."""
    gh = GitHubClient(cfg.tokens, etag_store=store)
    added = 0
    try:
        queries = SCALE_QUERIES + KEYWORD_QUERIES + BREADTH_QUERIES
        for q in queries[:rounds]:
            try:
                _, new = await search_repos(gh, store, q, reason="overnight", priority=110)
                added += new
                log.info("Discovery: %-46s +%d (Total new: %d)", q[:46], new, added)
            except Exception as exc:
                log.warning("Discovery query %r failed: %s", q, exc)
    finally:
        await gh.aclose()
    return added


async def main() -> None:
    cfg = Config(db_path=Path(DB))
    cfg.max_skills_per_repo = 1500
    store = Store(cfg.db_path)

    skills, queued = get_counts(store)
    log.info("Starting pipeline: %d skills indexed, %d repos queued, target: %d", skills, queued, TARGET)

    round_no = 0
    stalled = 0
    while skills < TARGET and stalled < 3:
        round_no += 1
        before = skills

        try:
            skills = await phase_sweep(store, cfg, TARGET)
        except Exception as exc:
            log.exception("Harvest phase exception: %s", exc)

        if skills >= TARGET:
            break

        _, queued = get_counts(store)
        if queued < 3000:
            log.info("Queue depth below threshold (%d); initiating discovery pass", queued)
            try:
                await phase_discover(store, cfg, rounds=len(SCALE_QUERIES))
                recompute(store)
            except Exception as exc:
                log.exception("Discovery phase exception: %s", exc)

        skills, queued = get_counts(store)
        gained = skills - before
        stalled = stalled + 1 if gained < 50 else 0
        log.info(
            "Round %d finished: +%d skills (Total: %d, Queued: %d, Stalled: %d)",
            round_no,
            gained,
            skills,
            queued,
            stalled,
        )

    try:
        recompute(store)
    except Exception as exc:
        log.warning("Final ranking pass failed: %s", exc)

    skills, queued = get_counts(store)
    valid = store.db.execute("SELECT COUNT(*) c FROM skills WHERE valid=1").fetchone()["c"]
    uniq = store.db.execute("SELECT COUNT(DISTINCT content_hash) c FROM skills").fetchone()["c"]
    repos = store.db.execute("SELECT COUNT(*) c FROM repos WHERE tree_sha IS NOT NULL").fetchone()["c"]
    log.info(
        "Ingestion finished: %d skills (%d valid, %d unique) from %d repos; %d remaining in queue",
        skills,
        valid,
        uniq,
        repos,
        queued,
    )
    store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Execution interrupted by user. State is checkpointed in SQLite.")
