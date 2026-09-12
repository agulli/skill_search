#!/usr/bin/env python
"""Builds a compacted, deployable search index artifact from a crawl database.

Execution Pipeline:
1. Snapshot: Creates a consistent copy via `VACUUM INTO` while crawler is live.
2. Ranking: Computes corpus-relative percentile scores for repos, authors, and skills.
3. Categorization: Runs IDF-weighted pattern matching across the taxonomy.
4. Body Truncation: Caps skill body text at 2,000 characters for search index efficiency.
5. FTS5 Index Rebuild: Reconstructs full-text search index and triggers.
6. Compaction: Compacts pages with a second `VACUUM INTO`.

Usage:
    python release.py data/scale.db dist/skills.db
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.ranking import recompute
from skill_engine.safety import assess_corpus
from skill_engine.store import Store
from skill_engine.taxonomy import categorise_corpus

BODY_CAP = int(os.getenv("SKILL_ENGINE_BODY_CAP", "2000"))


def format_gb(path: Path) -> float:
    """Returns file size in gigabytes."""
    return path.stat().st_size / 1e9


def log_step(label: str) -> float:
    """Logs pipeline step banner and returns start timestamp."""
    print(f"\n==> {label}", flush=True)
    return time.time()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "data/scale.db")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "dist/skills.db")
    if not src.exists():
        print(f"Error: Source database does not exist: {src}", file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)


    print(f"Building deployable index at {dst} from {src} ({format_gb(src):.2f} GB)")
    print(f"Skill body character limit: {BODY_CAP:,}")

    # 1. Consistent database snapshot
    t = log_step("Creating database snapshot")
    for stale in (dst, Path(f"{dst}-wal"), Path(f"{dst}-shm")):
        stale.unlink(missing_ok=True)
    subprocess.run(["sqlite3", str(src), f"VACUUM INTO '{dst}'"], check=True)
    print(f"    Snapshot created ({format_gb(dst):.2f} GB) in {time.time()-t:.0f}s")

    store = Store(dst)

    # 2. Temporarily drop triggers to optimize bulk update performance
    t = log_step("Disabling FTS triggers for bulk compute")
    for trg in ("skills_ai", "skills_ad", "skills_au"):
        store.db.execute(f"DROP TRIGGER IF EXISTS {trg}")
    store.db.commit()
    print(f"    Completed in {time.time()-t:.0f}s")

    # 3. Compute corpus-relative quality rankings
    t = log_step("Inspecting what each skill instructs an agent to do")
    # Inherit any decision already made against the untruncated crawl database
    # rather than recomputing from what is about to be trimmed.
    risk = assess_corpus(store, skip_assessed=True)
    counts = risk["counts"]
    print(f"    " + ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items())))
    if counts.get("critical"):
        print(f"    {counts['critical']:,} withheld from search")
    print(f"    Completed in {time.time()-t:.0f}s")
    t = log_step("Computing corpus-calibrated quality scores")
    result = recompute(store)
    print(
        f"    Scored {result['repos_scored']:,} repos, {result.get('authors_scored', 0):,} "
        f"authors, {result['skills_scored']:,} skills in {time.time()-t:.0f}s"
    )

    # 4. Classify skills into taxonomy categories
    t = log_step("Categorizing skills via IDF pattern weights")
    cats = categorise_corpus(store)
    top_cats = sorted(cats["counts"].items(), key=lambda x: -x[1])[:5]
    print(f"    Classified {cats['classified']:,} skills in {time.time()-t:.0f}s")
    print("    Top categories: " + ", ".join(f"{k} ({v:,})" for k, v in top_cats))

    # 5. Truncate long bodies for search performance
    t = log_step(f"Truncating bodies to {BODY_CAP:,} characters")
    truncated_count = store.db.execute(
        "SELECT COUNT(*) FROM skills WHERE LENGTH(body) > ?", (BODY_CAP,)
    ).fetchone()[0]
    store.db.execute(
        "UPDATE skills SET body = SUBSTR(body, 1, ?) WHERE LENGTH(body) > ?",
        (BODY_CAP, BODY_CAP),
    )
    store.db.commit()
    print(f"    {truncated_count:,} bodies truncated in {time.time()-t:.0f}s")

    # 6. Rebuild FTS5 index
    t = log_step("Rebuilding FTS5 full-text index")
    store.db.execute("INSERT INTO skills_fts(skills_fts) VALUES('rebuild')")
    store.db.commit()
    print(f"    Completed in {time.time()-t:.0f}s")

    # 7. Restore triggers
    t = log_step("Restoring FTS synchronization triggers")
    store.db.executescript("""
    CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
      INSERT INTO skills_fts(rowid,name,description,body,repo,path)
      VALUES (new.id,new.name,new.description,new.body,new.repo,new.path);
    END;
    CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
      INSERT INTO skills_fts(skills_fts,rowid,name,description,body,repo,path)
      VALUES ('delete',old.id,old.name,old.description,old.body,old.repo,old.path);
    END;
    CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
      INSERT INTO skills_fts(skills_fts,rowid,name,description,body,repo,path)
      VALUES ('delete',old.id,old.name,old.description,old.body,old.repo,old.path);
      INSERT INTO skills_fts(rowid,name,description,body,repo,path)
      VALUES (new.id,new.name,new.description,new.body,new.repo,new.path);
    END;""")
    store.db.commit()
    print(f"    Completed in {time.time()-t:.0f}s")
    store.close()

    # 8. Vacuum into final artifact
    t = log_step("Compacting database pages")
    tmp = Path(f"{dst}.compact")
    tmp.unlink(missing_ok=True)
    subprocess.run(["sqlite3", str(dst), f"VACUUM INTO '{tmp}'"], check=True)
    tmp.replace(dst)
    for stale in (Path(f"{dst}-wal"), Path(f"{dst}-shm")):
        stale.unlink(missing_ok=True)
    print(f"    Final size: {format_gb(dst):.2f} GB (Finished in {time.time()-t:.0f}s)")

    # Report metrics
    store = Store(dst, read_only=True)
    one = lambda q: store.db.execute(q).fetchone()[0]  # noqa: E731
    final_size = format_gb(dst)
    print(f"\n{'='*56}\nRELEASE ARTIFACT READY: {dst}")
    print(
        f"  {one('SELECT COUNT(*) FROM skills'):,} skills "
        f"({one('SELECT COUNT(*) FROM skills WHERE valid=1'):,} valid, "
        f"{one('SELECT COUNT(DISTINCT content_hash) FROM skills'):,} unique)"
    )
    print(
        f"  {one('SELECT COUNT(*) FROM repos WHERE skill_count>0'):,} repos | "
        f"{one('SELECT COUNT(*) FROM authors'):,} authors"
    )
    print(f"  Disk footprint: {final_size:.2f} GB (Compressed transfer: ~{final_size*0.38:.2f} GB)")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
