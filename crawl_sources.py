#!/usr/bin/env python
"""Harvest agent skills from forges other than GitHub.

    python crawl_sources.py --db data/scale.db            # GitLab + Hugging Face
    python crawl_sources.py --db data/scale.db --only hf

Neither needs a token. Both go through the same parser and store as the GitHub
crawler, so a skill mirrored across forges collapses on its content hash rather
than being counted twice.
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/scale.db")
    ap.add_argument("--only", choices=["gitlab", "hf", "both"], default="both")
    ap.add_argument("--limit", type=int, default=400)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.ERROR)

    store = Store(Path(args.db))
    before = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    totals = asyncio.run(crawl(store, which=args.only, limit=args.limit))
    after = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]

    print(f"\n  gitlab       {totals['gitlab_repos']:>5} repos  "
          f"{totals['gitlab_skills']:>6} skills")
    print(f"  huggingface  {totals['hf_repos']:>5} repos  "
          f"{totals['hf_skills']:>6} skills")
    print(f"  corpus       {before:,} -> {after:,}  (+{after - before:,})")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
