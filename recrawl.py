#!/usr/bin/env python
"""Re-queue repositories whose stored skills predate the safety pipeline.

    python recrawl.py data/scale.db --limit 400      # a trial batch
    python recrawl.py data/scale.db                  # everything

Why this is necessary rather than a re-assessment. The crawler caps stored
bodies at 4,000 characters, and until today it did so *before* anything looked
at them. Sampled across `scale.db`, 93.7% of skills were truncated, and the
mean skill kept 3,928 of 14,325 characters — so about 72% of the average
document was discarded at crawl time. Re-running the rules over what is stored
would inspect a quarter of each skill and call it done; the rest is not unread,
it is gone.

What the re-crawl buys, per skill:

* the rules see the whole body, and a payload past character 4,000 becomes
  visible for the first time;
* files bundled beside the skill are inspected — `setup.sh`, `analyze.py` —
  which no rule against the SKILL.md can reach;
* a flagged skill keeps its full body, so the model and a human reviewer both
  have the real text.

`tree_sha` is cleared for each re-queued repository, because the queue prunes
anything already harvested and that is the honest way to say our harvest is no
longer adequate. Nothing is deleted: the existing skills stay until their
repository is re-read, and an interrupted run simply leaves the remainder
queued.

Ordering is deliberate. The crawler selects by priority then `repo_score`, so
seeding everything at one priority refreshes the best-regarded repositories
first — which means a run stopped halfway has improved what people actually
retrieve rather than an arbitrary slice.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.store import Store


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--limit", type=int, default=0,
                    help="re-queue only the N best-scored repositories")
    ap.add_argument("--priority", type=int, default=140,
                    help="queue priority; above the crawler's floor so these "
                         "are taken before fresh discovery")
    ap.add_argument("--reason", default="recrawl:full-body-assessment")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1
    store = Store(path)

    t0 = time.perf_counter()
    sql = ("SELECT r.full_name FROM repos r "
           "WHERE r.skill_count > 0 AND r.tree_sha IS NOT NULL "
           "  AND COALESCE(r.archived, 0) = 0 "
           "  AND COALESCE(r.disabled, 0) = 0 "
           "ORDER BY COALESCE(r.repo_score, 0) DESC")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    names = [r["full_name"] for r in store.db.execute(sql)]
    print(f"  {len(names):,} repositories hold skills and would be re-read "
          f"({time.perf_counter() - t0:.0f}s to select)")

    if args.dry_run:
        for n in names[:15]:
            print(f"     {n}")
        if len(names) > 15:
            print(f"     … and {len(names) - 15:,} more")
        store.close()
        return 0

    queued = 0
    for i in range(0, len(names), 5000):
        chunk = names[i:i + 5000]
        store.enqueue_many((n, args.reason, args.priority) for n in chunk)
        # Cleared after enqueueing, so an interruption leaves rows queued
        # rather than repositories that merely look unharvested.
        store.db.executemany(
            "UPDATE repos SET tree_sha = NULL WHERE full_name = ?",
            [(n,) for n in chunk])
        store.commit()
        queued += len(chunk)
        print(f"    re-queued {queued:,}/{len(names):,}", flush=True)

    depth = store.db.execute("SELECT COUNT(*) c FROM queue").fetchone()["c"]
    print(f"\n  queue depth now {depth:,}")
    print("  the crawler will take these before fresh discovery, best-scored "
          "repositories first")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
