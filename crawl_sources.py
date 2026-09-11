#!/usr/bin/env python
"""Harvest agent skills from multi-forge sources (GitLab and Hugging Face).

Usage:
    python crawl_sources.py --db data/scale.db            # Harvest both GitLab and Hugging Face
    python crawl_sources.py --db data/scale.db --only hf  # Harvest Hugging Face only
    python crawl_sources.py --db data/scale.db --only gitlab

Multi-forge crawlers stream public repository archives without requiring API authentication tokens.
Skills are parsed and deduplicated by content hash into the central SQLite store.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.sources import crawl
from skill_engine.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-forge skill harvester (GitLab / Hugging Face)")
    parser.add_argument("--db", default="data/scale.db", help="Path to target SQLite database")
    parser.add_argument("--only", choices=["gitlab", "hf", "both"], default="both", help="Source to crawl")
    parser.add_argument("--limit", type=int, default=400, help="Maximum repositories to process")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.ERROR)

    store = Store(Path(args.db))
    before_count = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    totals = asyncio.run(crawl(store, which=args.only, limit=args.limit))
    after_count = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]

    print(
        f"\n  GitLab:        {totals['gitlab_repos']:>5} repos  {totals['gitlab_skills']:>6} skills"
    )
    print(
        f"  Hugging Face:  {totals['hf_repos']:>5} repos  {totals['hf_skills']:>6} skills"
    )
    print(f"  Total Index:   {before_count:,} -> {after_count:,} (+{after_count - before_count:,})\n")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
