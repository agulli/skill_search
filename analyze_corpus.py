#!/usr/bin/env python
"""Run the full validation pipeline over a corpus and record the decisions.

    python analyze_corpus.py dist/skills.db            # rules + model
    python analyze_corpus.py dist/skills.db --no-model # rules only, minutes
    python analyze_corpus.py dist/skills.db --sample 200

Three stages, in the order their cost demands:

1. **Rules** over everything — 239 skills/sec, so 95,725 in about seven
   minutes. This decides what is worth looking at.
2. **Model** over what the rules flagged — 0.74% of the corpus, roughly ten
   seconds each. Skipping the 99.26% is what makes the model affordable at all.
3. **Decision** fusing both, written to `risk_level`, `risk_confidence`,
   `risk_detail`.

Resumable by construction: each decision is committed as it is made, and a
second run skips anything already carrying a confidence. A two-hour job that
loses everything on interruption is a job nobody runs twice.

The random audit sample is not optional. Stage 2 only sees what stage 1
flagged, so without sampling the skills it *didn't* flag there is no way to
learn the gate's recall — the one number that cannot be measured from the
blocked set alone.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.analyze import analyze, available
from skill_engine.confidence import ALLOW, BLOCK, FLAG, decide, explain
from skill_engine.safety import CRITICAL, HIGH, MEDIUM, NONE, inspect
from skill_engine.store import Store

log = logging.getLogger("analyze")

GATED_LEVELS = (CRITICAL, HIGH, MEDIUM)


def ensure_columns(store: Store) -> None:
    cols = {r["name"] for r in store.db.execute("PRAGMA table_info(skills)")}
    for name, decl in (("risk_confidence", "REAL"),
                       ("risk_action", "TEXT"),
                       ("risk_analysis", "TEXT")):
        if name not in cols:
            store.db.execute(f"ALTER TABLE skills ADD COLUMN {name} {decl}")
    store.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--no-model", action="store_true",
                    help="rules only; leaves confidence at the rule-alone level")
    ap.add_argument("--sample", type=int, default=0,
                    help="also model N randomly chosen *unflagged* skills, to "
                         "estimate what the rule gate misses")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true", default=True)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1

    store = Store(path)
    ensure_columns(store)

    use_model = not args.no_model
    if use_model and not available():
        log.warning("no local model reachable; continuing with rules only")
        use_model = False

    sql = ("SELECT id, name, repo, description, body, allowed_tools, path, "
           "       risk_confidence FROM skills WHERE valid = 1")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = store.db.execute(sql).fetchall()

    # ---- stage 1: rules over everything
    t0 = time.perf_counter()
    verdicts: dict[int, object] = {}
    gated: list = []
    for r in rows:
        try:
            tools = json.loads(r["allowed_tools"] or "[]")
        except Exception:
            tools = []
        v = inspect(r["name"] or "", r["description"] or "",
                    r["body"] or "", tools, r["path"] or "")
        verdicts[r["id"]] = v
        if v.level in GATED_LEVELS:
            gated.append(r)
    log.info("rules: %d skills in %.0fs; %d flagged for review (%.2f%%)",
             len(rows), time.perf_counter() - t0, len(gated),
             100 * len(gated) / max(len(rows), 1))

    # ---- the audit sample: the only way to learn what stage 1 misses
    audit = []
    if args.sample:
        clean = [r for r in rows if verdicts[r["id"]].level == NONE]
        audit = random.Random(7).sample(clean, min(args.sample, len(clean)))
        log.info("audit sample: %d unflagged skills will also be modelled",
                 len(audit))

    # ---- stage 2 + 3
    todo = gated + audit
    if args.resume:
        before = len(todo)
        todo = [r for r in todo if r["risk_confidence"] is None]
        if before != len(todo):
            log.info("resuming: %d already analysed, %d to go",
                     before - len(todo), len(todo))

    counts = {BLOCK: 0, FLAG: 0, ALLOW: 0}
    escalated = audit_hits = 0
    t0 = time.perf_counter()
    for i, r in enumerate(todo, 1):
        v = verdicts[r["id"]]
        a = None
        if use_model:
            try:
                tools = json.loads(r["allowed_tools"] or "[]")
            except Exception:
                tools = []
            a = analyze(r["name"] or "", r["description"] or "",
                        r["body"] or "", tools,
                        [f.as_dict() for f in v.findings])
        d = decide(v, a)
        counts[d.action] += 1
        if d.action == BLOCK and v.level != CRITICAL:
            escalated += 1
            log.info("escalated to block: %s@%s — %s",
                     r["name"], r["repo"], explain(d))
        if v.level == NONE and d.action != ALLOW:
            audit_hits += 1
            log.warning("AUDIT SAMPLE HIT — the rule gate missed this: %s@%s — %s",
                        r["name"], r["repo"], explain(d))

        store.db.execute(
            "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
            "risk_action = ?, risk_detail = ?, risk_analysis = ? WHERE id = ?",
            (v.level, d.confidence, d.action,
             v.as_json() if v.level != NONE else None,
             json.dumps({**d.as_dict(),
                         "analysis": a.as_dict() if a else None}),
             r["id"]))
        # Committed per row: an interrupted two-hour job must not start over.
        store.commit()
        if i % 25 == 0 or i == len(todo):
            rate = i / max(time.perf_counter() - t0, 1e-9)
            log.info("  %d/%d  %.1f/s  eta %.0f min  [block %d flag %d allow %d]",
                     i, len(todo), rate, (len(todo) - i) / rate / 60,
                     counts[BLOCK], counts[FLAG], counts[ALLOW])

    # Everything the rules cleared and the model never saw is allowed at zero
    # confidence — recorded explicitly, so "not assessed" and "assessed clean"
    # are distinguishable later.
    store.db.execute(
        "UPDATE skills SET risk_level = 'none', risk_confidence = 0.0, "
        "risk_action = 'allow' WHERE valid = 1 AND risk_confidence IS NULL")
    store.commit()

    print(f"\n  blocked {counts[BLOCK]}   flagged {counts[FLAG]}   "
          f"allowed {counts[ALLOW]}")
    if escalated:
        print(f"  {escalated} of those blocks came from the model, not the rules")
    if args.sample:
        rate = audit_hits / max(len(audit), 1)
        print(f"  audit sample: {audit_hits}/{len(audit)} unflagged skills would "
              f"have been actioned ({rate:.2%} estimated gate miss rate)")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
