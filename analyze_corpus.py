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

from skill_engine import overrides
from skill_engine.analyze import analyze, available
from skill_engine.confidence import ALLOW, BLOCK, FLAG, decide, explain
from skill_engine.safety import CRITICAL, HIGH, MEDIUM, NONE, inspect
from skill_engine.store import Store

log = logging.getLogger("analyze")

GATED_LEVELS = (CRITICAL, HIGH, MEDIUM)




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
    overrides.ensure(store)

    use_model = not args.no_model
    if use_model and not available():
        log.warning("no local model reachable; continuing with rules only")
        use_model = False

    sql = ("SELECT id, name, repo, description, body, allowed_tools, path, "
           "       content_hash, risk_confidence FROM skills WHERE valid = 1")
    if args.limit:
        sql += f" LIMIT {args.limit}"

    # ---- stage 1: rules over everything
    #
    # Streamed, not fetched. The obvious `fetchall()` holds every body and every
    # verdict in memory in order to use 772 of them, and on a 95,725-skill
    # corpus that was enough to push a 24 GB machine into swap — 17.5 GB of it,
    # at which point the model server thrashed and the whole pass stalled at 0%
    # CPU. Only the gated rows and the audit reservoir are retained.
    t0 = time.perf_counter()
    verdicts: dict[int, object] = {}
    gated: list = []
    audit: list = []
    rng = random.Random(7)
    seen = clean_seen = 0
    for r in store.db.execute(sql):
        seen += 1
        try:
            tools = json.loads(r["allowed_tools"] or "[]")
        except Exception:
            tools = []
        v = inspect(r["name"] or "", r["description"] or "",
                    r["body"] or "", tools, r["path"] or "")
        if v.level in GATED_LEVELS:
            verdicts[r["id"]] = v
            gated.append(r)
        elif v.level == NONE and args.sample:
            # Reservoir sample over the unflagged rows: a uniform sample of a
            # population we are deliberately not keeping.
            clean_seen += 1
            if len(audit) < args.sample:
                audit.append(r)
                verdicts[r["id"]] = v
            else:
                j = rng.randrange(clean_seen)
                if j < args.sample:
                    verdicts.pop(audit[j]["id"], None)
                    audit[j] = r
                    verdicts[r["id"]] = v
    log.info("rules: %d skills in %.0fs; %d flagged for review (%.2f%%)",
             seen, time.perf_counter() - t0, len(gated),
             100 * len(gated) / max(seen, 1))
    if args.sample:
        log.info("audit sample: %d of %d unflagged skills will also be modelled",
                 len(audit), clean_seen)

    # ---- one analysis per distinct content
    #
    # The corpus is heavily vendored: 85 of 726 gated rows are byte-identical
    # copies of another. Analysing each copy separately is not just 12% wasted
    # model time, it allows two copies of one skill to end up with *different*
    # decisions. A decision belongs to the content — which is already how the
    # override table is keyed — so one representative is modelled and the
    # result is written to every row sharing its hash.
    todo = gated + audit
    by_content: dict[str, list] = {}
    for r in todo:
        by_content.setdefault(r["content_hash"] or f"id:{r['id']}", []).append(r)
    if len(by_content) != len(todo):
        log.info("%d rows collapse to %d distinct contents",
                 len(todo), len(by_content))
    todo = [rows[0] for rows in by_content.values()]

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

        payload = (v.level, d.confidence, d.action,
                   v.as_json() if v.level != NONE else None,
                   json.dumps({**d.as_dict(),
                               "analysis": a.as_dict() if a else None}))
        if r["content_hash"]:
            store.db.execute(
                "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
                "risk_action = ?, risk_detail = ?, risk_analysis = ? "
                "WHERE content_hash = ?", (*payload, r["content_hash"]))
        else:
            store.db.execute(
                "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
                "risk_action = ?, risk_detail = ?, risk_analysis = ? "
                "WHERE id = ?", (*payload, r["id"]))
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
    # Skipped under --limit: a partial pass must not mark the whole corpus
    # assessed, or the next full run resumes over rows nothing ever inspected.
    if not args.limit:
        store.db.execute(
            "UPDATE skills SET risk_level = 'none', risk_confidence = 0.0, "
            "risk_action = 'allow' WHERE valid = 1 AND risk_confidence IS NULL")
        store.commit()
    else:
        log.info("--limit given; leaving the rest of the corpus unassessed")

    # Human decisions outrank this entire pipeline, and are re-asserted last so
    # that re-running the analysis never silently reverses a review.
    restored = overrides.apply_all(store)
    if restored:
        log.info("re-applied %d human override(s) over the automated decisions",
                 restored)

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
