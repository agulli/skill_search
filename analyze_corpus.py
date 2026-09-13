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
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine import overrides
from skill_engine.analyze import analyze, available
from skill_engine.confidence import ALLOW, BLOCK, FLAG, decide, explain
from skill_engine.safety import (CRITICAL, HIGH, LOW, MEDIUM, NONE,
                                 _row_metadata, inspect)
from skill_engine.store import Store

log = logging.getLogger("analyze")

GATED_LEVELS = (CRITICAL, HIGH, MEDIUM)




def redecide(store: Store) -> int:
    """Re-run the fusion layer over decisions already made.

    The expensive half of this pipeline is the model, and its output is stored
    verbatim in `risk_analysis`. So a change to `confidence.py` — a threshold, a
    coherence requirement, a corrected precision figure — does not need any of
    it re-run. Without this, every fusion fix meant discarding hours of model
    work, which is a strong incentive to leave a fusion bug alone.

    The rule verdict is recomputed from the current rules (cheap, and it must
    match the code that produced the levels), but no model call is made.
    """
    rows = store.db.execute(
        "SELECT id, name, repo, description, body, allowed_tools, path, "
        "       metadata, risk_analysis FROM skills "
        "WHERE risk_analysis IS NOT NULL").fetchall()
    log.info("re-deciding %d stored analyses; no model calls", len(rows))

    changed = counts = 0
    moved: dict[tuple[str, str], int] = {}
    for r in rows:
        stored = json.loads(r["risk_analysis"] or "{}")
        before = stored.get("action")
        raw = stored.get("analysis") or {}

        try:
            tools = json.loads(r["allowed_tools"] or "[]")
        except Exception:
            tools = []
        v = inspect(r["name"] or "", r["description"] or "",
                    r["body"] or "", tools, r["path"] or "",
                    _row_metadata(r))

        a = StoredAnalysis(raw) if raw.get("ok") else None
        d = decide(v, a)
        counts += 1
        if d.action != before:
            changed += 1
            moved[(before or "?", d.action)] = moved.get((before or "?", d.action), 0) + 1
            log.info("  %s -> %s  %s@%s — %s",
                     before, d.action, r["name"], r["repo"], explain(d))
        store.db.execute(
            "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
            "risk_action = ?, risk_detail = ?, risk_analysis = ? WHERE id = ?",
            (v.level, d.confidence, d.action,
             v.as_json() if v.level != NONE else None,
             json.dumps({**d.as_dict(), "analysis": raw or None}), r["id"]))
    store.commit()

    restored = overrides.apply_all(store)
    print(f"\n  re-decided {counts}; {changed} changed")
    for (before, after), n in sorted(moved.items()):
        print(f"    {before} -> {after}: {n}")
    if restored:
        print(f"  re-applied {restored} human override(s)")
    store.close()
    return 0


class StoredAnalysis:
    """A stored model response, shaped like `Analysis` for `decide`.

    Deliberately not the real dataclass: this reads back what was recorded,
    and a field the recording never had must read as absent rather than as a
    default that happens to look like evidence.
    """

    def __init__(self, raw: dict):
        self.ok = bool(raw.get("ok"))
        self.harm_if_followed = raw.get("harm_if_followed") or "none"
        self.purpose_mismatch = bool(raw.get("purpose_mismatch"))
        self.addresses_reviewer = bool(raw.get("addresses_reviewer"))
        self.mismatch_explanation = raw.get("mismatch_explanation") or ""
        self.framing = raw.get("framing") or "unclear"
        self.claimed_purpose = raw.get("claimed_purpose") or ""
        self.instructed_actions = raw.get("instructed_actions") or []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--no-model", action="store_true",
                    help="rules only; leaves confidence at the rule-alone level")
    ap.add_argument("--sample", type=int, default=0,
                    help="also model N randomly chosen *unflagged* skills, to "
                         "estimate what the rule gate misses")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--topup", action="store_true",
                    help="clear the recorded decision for any skill the *current* "
                         "rules gate but which was never modelled, so a resumed "
                         "run picks up only what new rules newly flagged")
    ap.add_argument("--redecide", action="store_true",
                    help="re-run only the fusion layer, from the model output "
                         "already stored; makes no model calls")
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

    if args.redecide:
        return redecide(store)

    if args.topup:
        # Adding a rule re-gates the corpus, and the rows it newly flags are
        # already marked `allow` at zero confidence by the previous pass's
        # closing statement — so `--resume` skips exactly the skills the new
        # rule was written to catch. Clearing their decision, and only theirs,
        # turns a five-hour re-run into modelling the difference.
        cleared = 0
        cur = store.db.execute(
            "SELECT id, name, description, body, allowed_tools, path, metadata "
            "FROM skills WHERE valid = 1 AND risk_analysis IS NULL "
            "  AND risk_confidence IS NOT NULL")
        while True:
            chunk = cur.fetchmany(2000)
            if not chunk:
                break
            for r in chunk:
                try:
                    tools = json.loads(r["allowed_tools"] or "[]")
                except Exception:
                    tools = []
                v = inspect(r["name"] or "", r["description"] or "",
                            r["body"] or "", tools, r["path"] or "",
                            _row_metadata(r))
                if v.level in GATED_LEVELS:
                    store.db.execute(
                        "UPDATE skills SET risk_confidence = NULL WHERE id = ?",
                        (r["id"],))
                    cleared += 1
        store.commit()
        log.info("top-up: %d skills are gated by the current rules and were "
                 "never modelled; their decision has been cleared", cleared)

    use_model = not args.no_model
    if use_model and not available():
        log.warning("no local model reachable; continuing with rules only")
        use_model = False

    sql = ("SELECT id, name, repo, description, body, allowed_tools, path, "
           "       metadata, content_hash, risk_confidence "
           "FROM skills WHERE valid = 1")
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
    rng = random.Random(7)
    seen = 0
    # Split the audit budget between the two bands below the gate. `low` gets
    # the larger share despite being the smaller population: it is the
    # near-miss band, so a hole is likelier there and each draw is worth more.
    low_budget = min(args.sample // 2, args.sample) if args.sample else 0
    sample_budget = {NONE: args.sample - low_budget, LOW: low_budget}
    reservoirs: dict[str, list] = {NONE: [], LOW: []}
    counts_below: dict[str, int] = {NONE: 0, LOW: 0}
    for r in store.db.execute(sql):
        seen += 1
        try:
            tools = json.loads(r["allowed_tools"] or "[]")
        except Exception:
            tools = []
        v = inspect(r["name"] or "", r["description"] or "",
                    r["body"] or "", tools, r["path"] or "",
                    _row_metadata(r))
        if v.level in GATED_LEVELS:
            verdicts[r["id"]] = v
            gated.append(r)
        elif args.sample and v.level in (NONE, LOW):
            # Two reservoirs, not one. The audit exists to find holes in the
            # gate, and a uniform sample of everything below it spends almost
            # every draw on the 93,652 rows the rules found nothing in at all.
            # The `low` band — some signal, not enough to gate — is where a
            # hole would actually be, and it is 70x rarer, so sampling the two
            # bands separately buys far more information for the same model
            # time. Reported separately too, because a hit in each means a
            # different thing.
            which = v.level
            counts_below[which] += 1
            pool = reservoirs[which]
            seen_n = counts_below[which]
            budget = sample_budget[which]
            if len(pool) < budget:
                pool.append(r)
                verdicts[r["id"]] = v
            else:
                j = rng.randrange(seen_n)
                if j < budget:
                    verdicts.pop(pool[j]["id"], None)
                    pool[j] = r
                    verdicts[r["id"]] = v
    log.info("rules: %d skills in %.0fs; %d flagged for review (%.2f%%)",
             seen, time.perf_counter() - t0, len(gated),
             100 * len(gated) / max(seen, 1))
    audit = reservoirs[NONE] + reservoirs[LOW]
    if args.sample:
        log.info("audit sample: %d of %d clean skills and %d of %d low-signal "
                 "skills will also be modelled",
                 len(reservoirs[NONE]), counts_below[NONE],
                 len(reservoirs[LOW]), counts_below[LOW])

    # ---- one analysis per distinct content
    #
    # The corpus is heavily vendored: 85 of 726 gated rows are byte-identical
    # copies of another. Analysing each copy separately is not just 12% wasted
    # model time, it allows two copies of one skill to end up with *different*
    # decisions. A decision belongs to the content — which is already how the
    # override table is keyed — so one representative is modelled and the
    # result is written to every row sharing its hash.
    # Severity first, not corpus order.
    #
    # At four million skills the gate selects around 40,000, which is well over
    # a hundred hours of local inference — so the order in which they are
    # modelled decides what is protected on day one rather than day five. A
    # rule-critical skill is already withheld by the rules; a `high` is one
    # model answer away from being withheld; a `medium` is the largest band and
    # the least likely to move. Working in corpus order spends the first day on
    # whatever happened to be crawled first.
    #
    # The run stays resumable either way, so an interrupted pass has decided
    # the most consequential skills rather than an arbitrary prefix.
    rank = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, NONE: 4}
    gated.sort(key=lambda r: rank.get(verdicts[r["id"]].level, 5))
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

    # Sequential by default, and the reason is a measurement that did not
    # survive being repeated in the right conditions.
    #
    # Where the time goes: one 8B Q4 analysis is 1.38s of prefill at 870 tok/s
    # and 15.0s of decode at 33.9 tok/s — 72% of it decoding ~509 output
    # tokens. Decode on Apple Silicon is memory-bandwidth-bound, so concurrent
    # requests looked like the obvious win, and in isolation they were: two
    # together took 24.9s against 36.8s sequentially, a clean 1.48x.
    #
    # In the pipeline, alongside the crawler that is the normal operating
    # condition, it vanished. Two runs of eight skills: 133s then 152s with one
    # worker, 148s then 142s with two — the ordering flipped, so the difference
    # is noise. The idle-machine figure measured headroom that is not there
    # when anything else is running, which is the same mistake as measuring
    # precision on an attack-enriched sample.
    #
    # Left at one, because the cost of being wrong is not symmetric: each slot
    # holds its own KV cache beside a 9.6 GB model on 24 GB of shared memory,
    # and this pipeline has already been stalled once by pushing this machine
    # into swap — where everything sat at 0% CPU and looked hung rather than
    # slow. `SKILL_ENGINE_ANALYZER_WORKERS=2` is there for an otherwise idle
    # machine, where the 1.48x is real.
    #
    # SQLite stays single-threaded either way: workers only call the model, and
    # every `decide` and `UPDATE` happens here, in severity order, one at a
    # time.
    workers = max(1, int(os.getenv("SKILL_ENGINE_ANALYZER_WORKERS", "1")))

    def analyse(row):
        v = verdicts[row["id"]]
        if not use_model:
            return row, v, None
        try:
            tools = json.loads(row["allowed_tools"] or "[]")
        except Exception:
            tools = []
        return row, v, analyze(row["name"] or "", row["description"] or "",
                               row["body"] or "", tools,
                               [f.as_dict() for f in v.findings])

    if use_model and workers > 1:
        pool = ThreadPoolExecutor(workers)
        results = pool.map(analyse, todo)
    else:
        pool = None
        results = (analyse(r) for r in todo)

    for i, (r, v, a) in enumerate(results, 1):
        d = decide(v, a)
        counts[d.action] += 1
        if d.action == BLOCK and v.level != CRITICAL:
            escalated += 1
            log.info("escalated to block: %s@%s — %s",
                     r["name"], r["repo"], explain(d))
        if v.level in (NONE, LOW) and d.action != ALLOW:
            audit_hits += 1
            log.warning("AUDIT HIT (%s band) — below the gate but actioned: "
                        "%s@%s — %s", v.level, r["name"], r["repo"], explain(d))

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
            # Seconds per skill, not skills per second. The model takes ~24s
            # each, which printed as "0.0/s" — a progress line that cannot
            # show progress is how a stalled run passes for a slow one, and
            # that mistake cost hours earlier in this work.
            elapsed = max(time.perf_counter() - t0, 1e-9)
            per = elapsed / i
            log.info("  %d/%d  %.0fs each  eta %.0f min  "
                     "[block %d flag %d allow %d]",
                     i, len(todo), per, (len(todo) - i) * per / 60,
                     counts[BLOCK], counts[FLAG], counts[ALLOW])

    if pool is not None:
        pool.shutdown(wait=True)

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
        print(f"  audit sample: {audit_hits}/{len(audit)} skills below the gate "
              f"would have been actioned ({rate:.2%})")
        print(f"    drawn from {counts_below[NONE]:,} clean and "
              f"{counts_below[LOW]:,} low-signal skills")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
