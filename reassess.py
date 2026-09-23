#!/usr/bin/env python3
"""Bring stored risk verdicts back in line with the current rules.

`risk_level` is written when a skill is crawled, so every rule added since
leaves stored levels stale. A skill the rules would gate today can sit in the
corpus recorded as `none`, invisible to the verifier -- which reads the stored
column -- and therefore never reviewed. Measured on a 40,000-skill sample:
0.66% of levels disagree with the current rules, and 0.028% are stored ungated
while the rules gate them, which extrapolates to roughly 1,100 skills.

Why this is not `--topup`. That mode selects `risk_analysis IS NULL AND
risk_confidence IS NOT NULL`: rows a previous full pass decided by rules alone.
This corpus was rebuilt by crawling, and crawl-time assessment writes
`risk_level` without ever writing `risk_confidence`, so 98.6% of rows have
neither and `--topup` examines exactly zero of them. It would run for hours and
change nothing.

Why not `assess_corpus()` either. It fetches every row -- bodies included --
before inspecting any, which is about 17GB resident at this size, and it
finishes by clearing stale verdicts with `id NOT IN (<every gated id>)`, a step
that is only correct when it can see the whole corpus at once. Both are fine
for a release build of a few hundred thousand rows and wrong for four million.

So this streams, writes only what changed, and clears a decision whenever the
level moves -- because a decision recorded against one verdict says nothing
about a different one.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.safety import CRITICAL, HIGH, MEDIUM, inspect
from skill_engine.store import Store

log = logging.getLogger("reassess")
CHUNK = 20_000
GATED = (MEDIUM, HIGH, CRITICAL)


def _metadata(row) -> str:
    try:
        return row["metadata"] or ""
    except (IndexError, KeyError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N skills, for a quick sample")
    ap.add_argument("--min-score", type=float, default=None, metavar="S",
                    help="only skills scoring at least S. Scopes the pass to a "
                         "release tier: a demo build blocked on a handful of "
                         "newly gated skills does not need the whole corpus "
                         "re-inspected first.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1

    store = Store(path)
    store.db.execute("PRAGMA busy_timeout = 900000")

    where = "WHERE valid = 1"
    params: tuple = ()
    if args.min_score is not None:
        where += " AND score >= ?"
        params = (args.min_score,)
        log.info("scoped to skills scoring >= %.1f", args.min_score)
    cur = store.db.execute(
        "SELECT id, name, description, body, allowed_tools, path, metadata, "
        "       COALESCE(risk_level,'none') AS stored, risk_action "
        f"FROM skills {where}", params)

    moved: collections.Counter = collections.Counter()
    seen = newly_gated = 0
    writes: list[tuple] = []
    t0 = time.perf_counter()

    def flush() -> None:
        if not writes or args.dry_run:
            writes.clear()
            return
        # Retry rather than abort: this pass is hours long and the crawler
        # holds the write lock in bursts. Giving up discards everything since
        # the last flush.
        for attempt in range(12):
            try:
                store.db.executemany(
                    "UPDATE skills SET risk_level = ?, risk_detail = ?, "
                    # A changed verdict retires the decision built on the old
                    # one. Without this a skill re-gated by a new rule keeps
                    # its previous `allow` and is never re-reviewed.
                    "    risk_action = NULL, risk_confidence = NULL, "
                    "    risk_analysis = NULL "
                    "WHERE id = ?", writes)
                store.commit()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 11:
                    raise
                wait = min(60, 5 * (attempt + 1))
                log.warning("corpus locked (%s); retrying in %ds", exc, wait)
                time.sleep(wait)
        writes.clear()

    while True:
        rows = cur.fetchmany(CHUNK)
        if not rows:
            break
        for r in rows:
            seen += 1
            try:
                tools = tuple(json.loads(r["allowed_tools"] or "[]"))
            except Exception:
                tools = ()
            v = inspect(r["name"] or "", r["description"] or "", r["body"] or "",
                        tools, r["path"] or "", _metadata(r))
            if v.level == r["stored"]:
                continue
            moved[f"{r['stored']} -> {v.level}"] += 1
            if r["stored"] not in GATED and v.level in GATED:
                newly_gated += 1
            writes.append((v.level,
                           v.as_json() if v.level != "none" else None,
                           r["id"]))
            if len(writes) >= 2_000:
                flush()
        if args.limit and seen >= args.limit:
            break
        if seen % 200_000 < CHUNK:
            rate = seen / max(time.perf_counter() - t0, 1e-9)
            log.info("  %d skills, %d changed, %d newly gated (%.0f/s)",
                     seen, sum(moved.values()), newly_gated, rate)
    flush()
    store.close()

    verb = "would change" if args.dry_run else "changed"
    print(f"\n  {seen:,} skills inspected")
    print(f"  {sum(moved.values()):,} {verb}")
    print(f"  {newly_gated:,} newly gated -- these now await a model verdict")
    print("\n  transitions:")
    for k, c in moved.most_common(15):
        print(f"    {c:>7,}  {k}")
    if not args.dry_run and newly_gated:
        print(f"\n  The verifier picks these up on its next pass; nothing else "
              f"to run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
