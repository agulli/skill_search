#!/usr/bin/env python
"""Unattended overnight run: harvest toward a skill target, then keep going.

Alternates two phases that use disjoint resources, so neither starves the other:

  sweep     codeload tarballs — no API quota at all, bandwidth-bound
  discover  repository search — its own rate-limit bucket, expands the queue

Both are checkpointed in SQLite, so killing this at any moment loses nothing:
the queue records what still needs doing and a restart resumes there. Every
phase is wrapped so that one failure cannot end the run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.config import Config
from skill_engine.discover import (BREADTH_QUERIES, KEYWORD_QUERIES,
                                   SCALE_QUERIES, search_repos)
from skill_engine.github import GitHubClient
from skill_engine.ranking import recompute
from skill_engine.store import Store
from skill_engine.tarball import run_tarball_crawl

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
DB = sys.argv[2] if len(sys.argv) > 2 else "data/big.db"
MAX_MB = int(os.getenv("SKILL_ENGINE_MAX_MB", "10"))
CONCURRENCY = int(os.getenv("SKILL_ENGINE_SWEEP_CONCURRENCY", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname).1s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("skill_engine.github").setLevel(logging.WARNING)
log = logging.getLogger("overnight")


def counts(store) -> tuple[int, int]:
    skills = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    queued = store.db.execute(
        "SELECT COUNT(*) c FROM queue q JOIN repos r ON r.full_name = q.full_name "
        "WHERE q.attempts < 4 AND r.tree_sha IS NULL AND COALESCE(r.size_kb,0) <= ?",
        (MAX_MB * 1024,),
    ).fetchone()["c"]
    return skills, queued


async def phase_sweep(store, cfg, target: int) -> int:
    """Drain the queue over codeload. Returns skills indexed afterwards."""
    started = time.time()

    def progress(totals, indexed, rate, dl):
        log.info(
            "sweep %6d repos | %7d skills | %5.0f repos/h | %5.1fGB | "
            "skip %d fail %d",
            totals["repos"], indexed, rate, dl["bytes"] / 1e9,
            dl["too_big"], dl["failed"],
        )

    totals = await run_tarball_crawl(
        store, cfg,
        target_skills=target,
        concurrency=CONCURRENCY,
        max_mb=MAX_MB,
        batch=24,
        rerank_every=2000,
        on_progress=progress,
    )
    skills, _ = counts(store)
    log.info(
        "sweep done: %d repos in %.1f min, %d skills total, %.1fGB downloaded",
        totals["repos"], (time.time() - started) / 60, skills,
        totals.get("download_stats", {}).get("bytes", 0) / 1e9,
    )
    return skills


async def phase_discover(store, cfg, rounds: int) -> int:
    """Widen the queue using the search bucket. Costs no core quota."""
    gh = GitHubClient(cfg.tokens, etag_store=store)
    added = 0
    try:
        queries = SCALE_QUERIES + KEYWORD_QUERIES + BREADTH_QUERIES
        for q in queries[: rounds]:
            try:
                _, new = await search_repos(gh, store, q, reason="overnight",
                                            priority=110)
                added += new
                log.info("discover %-46s +%d (total new %d)", q[:46], new, added)
            except Exception as exc:
                log.warning("discover %r failed: %s", q, exc)
    finally:
        await gh.aclose()
    return added


async def main() -> None:
    cfg = Config(db_path=Path(DB))
    cfg.max_skills_per_repo = 1500
    store = Store(cfg.db_path)

    skills, queued = counts(store)
    log.info("START: %d skills, %d repos queued, target %d", skills, queued, TARGET)

    round_no = 0
    stalled = 0
    while skills < TARGET and stalled < 3:
        round_no += 1
        before = skills

        try:
            skills = await phase_sweep(store, cfg, TARGET)
        except Exception as exc:
            log.exception("sweep phase failed, continuing: %s", exc)

        if skills >= TARGET:
            break

        _, queued = counts(store)
        if queued < 3000:
            log.info("queue low (%d); widening via search", queued)
            try:
                await phase_discover(store, cfg, rounds=len(SCALE_QUERIES))
                recompute(store)          # rank the newcomers before sweeping
            except Exception as exc:
                log.exception("discovery phase failed, continuing: %s", exc)

        skills, queued = counts(store)
        gained = skills - before
        stalled = stalled + 1 if gained < 50 else 0
        log.info("round %d: +%d skills (now %d), %d queued, stalled=%d",
                 round_no, gained, skills, queued, stalled)

    try:
        recompute(store)
    except Exception as exc:
        log.warning("final rank failed: %s", exc)

    skills, queued = counts(store)
    valid = store.db.execute(
        "SELECT COUNT(*) c FROM skills WHERE valid=1").fetchone()["c"]
    uniq = store.db.execute(
        "SELECT COUNT(DISTINCT content_hash) c FROM skills").fetchone()["c"]
    repos = store.db.execute(
        "SELECT COUNT(*) c FROM repos WHERE tree_sha IS NOT NULL").fetchone()["c"]
    log.info("FINISHED: %d skills (%d valid, %d unique) from %d repos; %d still queued",
             skills, valid, uniq, repos, queued)
    store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted — state is checkpointed, rerun to resume")
