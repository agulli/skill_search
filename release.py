#!/usr/bin/env python
"""Build a deployable index from a crawl database.

A crawl database is not servable. Scores are zero because reranking is disabled
during harvesting (it rewrites every row and would halve throughput), categories
are unassigned, and full skill bodies make the file several times larger than it
needs to be. This turns one into a release artifact, measuring each step so the
cost of every stage is visible rather than assumed.

    python release.py data/scale.db dist/skills.db

Stages, in order, and why that order:

1. **Snapshot** — `VACUUM INTO` gives a transactionally consistent copy, so the
   crawl can keep running while the release is built.
2. **Rank** — needs the whole corpus present, because percentile normalisation
   is corpus-relative.
3. **Categorise** — needs final scores only for ordering, but runs here so the
   catalogue ships complete.
4. **Trim bodies** — measured at 61% smaller, 21x faster on broad queries, and
   *better* on every retrieval metric: truncating removes spurious matches deep
   in long documents. Triggers are dropped first so a 600k-row rewrite does not
   churn the index once per row, then the index is rebuilt once.
5. **Reclaim** — a second `VACUUM` returns the freed pages to the filesystem.

The full body stays in the crawl database, which is what the detail drawer reads
locally; only the shipped copy is trimmed.
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
from skill_engine.store import Store
from skill_engine.taxonomy import categorise_corpus

BODY_CAP = int(os.getenv("SKILL_ENGINE_BODY_CAP", "2000"))


def gb(path: Path) -> float:
    return path.stat().st_size / 1e9


def step(label: str):
    print(f"\n==> {label}", flush=True)
    return time.time()


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "data/scale.db")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "dist/skills.db")
    if not src.exists():
        print(f"no such database: {src}")
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)

    print(f"building {dst} from {src} ({gb(src):.2f} GB)")
    print(f"body cap: {BODY_CAP:,} characters")

    # 1. Consistent snapshot; the crawl may still be writing to the source.
    t = step("snapshot")
    for stale in (dst, Path(f"{dst}-wal"), Path(f"{dst}-shm")):
        stale.unlink(missing_ok=True)
    subprocess.run(["sqlite3", str(src), f"VACUUM INTO '{dst}'"], check=True)
    print(f"    {gb(dst):.2f} GB in {time.time()-t:.0f}s")

    store = Store(dst)

    t = step("rank (corpus-relative, so it needs everything present)")
    result = recompute(store)
    print(f"    {result['repos_scored']:,} repos, {result.get('authors_scored',0):,} "
          f"authors, {result['skills_scored']:,} skills in {time.time()-t:.0f}s")

    t = step("categorise")
    cats = categorise_corpus(store)
    top = sorted(cats["counts"].items(), key=lambda x: -x[1])[:5]
    print(f"    {cats['classified']:,} skills in {time.time()-t:.0f}s")
    print("    " + ", ".join(f"{k} {v:,}" for k, v in top))

    t = step(f"trim bodies to {BODY_CAP:,} chars and rebuild the index")
    # Dropping the triggers first turns a per-row index churn into one bulk
    # update followed by a single rebuild.
    for trg in ("skills_ai", "skills_ad", "skills_au"):
        store.db.execute(f"DROP TRIGGER IF EXISTS {trg}")
    n = store.db.execute(
        "SELECT COUNT(*) FROM skills WHERE LENGTH(body) > ?", (BODY_CAP,)
    ).fetchone()[0]
    store.db.execute(
        "UPDATE skills SET body = SUBSTR(body, 1, ?) WHERE LENGTH(body) > ?",
        (BODY_CAP, BODY_CAP),
    )
    store.db.commit()
    store.db.execute("INSERT INTO skills_fts(skills_fts) VALUES('rebuild')")
    store.db.commit()
    # Recreate the triggers so the shipped file stays correct if ever written to.
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
    print(f"    {n:,} bodies truncated in {time.time()-t:.0f}s")
    store.close()

    t = step("reclaim freed pages")
    tmp = Path(f"{dst}.compact")
    tmp.unlink(missing_ok=True)
    subprocess.run(["sqlite3", str(dst), f"VACUUM INTO '{tmp}'"], check=True)
    tmp.replace(dst)
    for stale in (Path(f"{dst}-wal"), Path(f"{dst}-shm")):
        stale.unlink(missing_ok=True)
    print(f"    {gb(dst):.2f} GB in {time.time()-t:.0f}s")

    # Report what the deployment actually needs to provision.
    store = Store(dst, read_only=True)
    q = lambda s: store.db.execute(s).fetchone()[0]  # noqa: E731
    size = gb(dst)
    print(f"\n{'='*56}\nRELEASE READY: {dst}")
    print(f"  {q('SELECT COUNT(*) FROM skills'):,} skills "
          f"({q('SELECT COUNT(*) FROM skills WHERE valid=1'):,} valid, "
          f"{q('SELECT COUNT(DISTINCT content_hash) FROM skills'):,} unique)")
    print(f"  {q('SELECT COUNT(*) FROM repos WHERE skill_count>0'):,} repos, "
          f"{q('SELECT COUNT(*) FROM authors'):,} authors")
    print(f"  {size:.2f} GB  -> provision a {max(3, int(size*1.7)+1)} GB volume")
    print(f"  gzipped for upload: roughly {size*0.38:.2f} GB")
    store.close()
    print(f"\nnext:  ./deploy.sh app  &&  SKILL_ENGINE_DB={dst} ./deploy.sh data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
