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
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine import overrides
from skill_engine.analyze import analyze, available
from skill_engine.confidence import ALLOW, BLOCK, FLAG, decide, explain
from skill_engine.safety import (CRITICAL, HIGH, LOW, MEDIUM, NONE,
                                 _row_metadata, inspect)
from skill_engine.store import Store

log = logging.getLogger("analyze")

GATED_LEVELS = (CRITICAL, HIGH, MEDIUM)




def model_pending(store: Store, args) -> int:
    """Model the skills the crawler has already flagged, and nothing else.

    Every other mode begins by running the rules over the whole corpus. At
    95,725 skills that is eleven minutes; at 4.19 million it is about eight
    hours, which makes a loop impossible and a continuous verification pass
    with it.

    It is also now redundant. The crawler assesses each skill as it is stored,
    on the untruncated body, and records the verdict — so the gated set is a
    column to read rather than a corpus to rescan. `risk_level` defaults to
    'none', so only an assessment can have written one of the gated levels; a
    skill that has not been crawled yet reads 'none' and is correctly left for
    its own re-crawl.

    The verdict is recomputed for the selected rows, not reconstructed from
    `risk_detail`, so the decision always reflects the rules as they are now.
    That costs about 7 ms each over a few thousand rows, against eight hours to
    reach them the other way.
    """
    # Without this the selection scans 4.19M rows on every iteration of a loop.
    try:
        store.db.execute("PRAGMA busy_timeout = 120000")
        store.db.execute("CREATE INDEX IF NOT EXISTS skills_risk_pending "
                         "ON skills(risk_level, risk_analysis)")
        store.commit()
    except sqlite3.OperationalError as exc:
        log.warning("could not create the pending index (%s); "
                    "selection will be slower", exc)

    rank = {CRITICAL: 0, HIGH: 1, MEDIUM: 2}
    rows = store.db.execute(
        "SELECT id, name, repo, description, body, allowed_tools, path, "
        "       metadata, content_hash, risk_level "
        "FROM skills WHERE valid = 1 "
        "  AND risk_level IN ('critical','high','medium') "
        "  AND risk_analysis IS NULL").fetchall()
    if not rows:
        log.info("nothing pending: every flagged skill carries a decision")
        store.close()
        return 0

    # Severity first, and deduplicated by content, exactly as the full pass
    # does — an interrupted loop should have decided the worst, not a prefix.
    rows.sort(key=lambda r: rank.get(r["risk_level"], 3))
    seen: set[str] = set()
    todo = []
    for r in rows:
        key = r["content_hash"] or f"id:{r['id']}"
        if key in seen:
            continue
        seen.add(key)
        todo.append(r)
    if args.limit:
        todo = todo[:args.limit]
    log.info("%d flagged skills await a decision (%d distinct contents); "
             "modelling %d", len(rows), len(seen), len(todo))

    use_model = not args.no_model and available()
    counts = {BLOCK: 0, FLAG: 0, ALLOW: 0}
    escalated = 0
    t0 = time.perf_counter()
    for i, r in enumerate(todo, 1):
        try:
            tools = json.loads(r["allowed_tools"] or "[]")
        except Exception:
            tools = []
        v = inspect(r["name"] or "", r["description"] or "", r["body"] or "",
                    tools, r["path"] or "", _row_metadata(r))
        a = None
        if use_model:
            a = analyze(r["name"] or "", r["description"] or "",
                        r["body"] or "", tools,
                        [f.as_dict() for f in v.findings])
        d = decide(v, a)
        counts[d.action] += 1
        if d.action == BLOCK and v.level != CRITICAL:
            escalated += 1
            log.info("escalated to block: %s@%s — %s",
                     r["name"], r["repo"], explain(d))
        write_decision(store, r, v, d, a)
        if i % 25 == 0 or i == len(todo):
            per = (time.perf_counter() - t0) / i
            log.info("  %d/%d  %.0fs each  eta %.0f min  "
                     "[block %d flag %d allow %d]", i, len(todo), per,
                     (len(todo) - i) * per / 60,
                     counts[BLOCK], counts[FLAG], counts[ALLOW])

    restored = overrides.apply_all(store)
    print(f"\n  blocked {counts[BLOCK]}   flagged {counts[FLAG]}   "
          f"allowed {counts[ALLOW]}")
    if escalated:
        print(f"  {escalated} of those blocks came from the model")
    if restored:
        print(f"  re-applied {restored} human override(s)")
    store.close()
    return 0


def audit_index(store: Store, args) -> int:
    """Model a random sample of the *curated index*, flagged or not.

    Every other mode looks only at skills the rules already flagged, which
    can measure precision and can never measure a false negative: asking the
    model to confirm the rules' findings cannot discover what the rules never
    found. This samples the served tier at random instead, so a skill reaches
    the model precisely because it was selected — not because something was
    suspicious about it.

    That makes the model's judgement here unprimed. `build_prompt` names the
    flagged patterns, so agreement on a gated skill is weak evidence; with no
    findings to name, a harm verdict is the model's own.

    The sample is drawn over distinct content hashes, because the model judges
    text and identical text is one judgement. Decisions are written, so the
    pass builds real coverage as well as measuring it, and is resumable —
    re-running skips what already carries an analysis.

    Reports a Clopper-Pearson upper bound, not a point estimate. With zero
    findings in n draws the honest statement is "at most p% with 95%
    confidence", and that bound is what a claim about the index rests on.
    """
    floor = args.score_floor
    # The continuous verifier holds write locks on the same corpus. Without a
    # timeout the first collision throws away a model call that has already
    # been paid for.
    try:
        store.db.execute("PRAGMA busy_timeout = 300000")
    except sqlite3.OperationalError as exc:
        log.warning("could not set a busy timeout (%s)", exc)
    log.info("selecting the curated tier (score >= %d)…", floor)
    rows = store.db.execute(
        "SELECT id, name, repo, description, body, allowed_tools, path, "
        "       metadata, content_hash, risk_level, risk_analysis, score "
        "FROM skills WHERE valid = 1 AND score >= ?", (floor,)).fetchall()

    # One row per distinct content, preferring one that is not yet decided so
    # a resumed run spends the model on new text.
    by_hash: dict[str, Any] = {}
    for r in rows:
        key = r["content_hash"] or f"id:{r['id']}"
        cur = by_hash.get(key)
        if cur is None or (cur["risk_analysis"] and not r["risk_analysis"]):
            by_hash[key] = r
    pool = list(by_hash.values())
    undecided = [r for r in pool if not r["risk_analysis"]]
    log.info("%d skills, %d distinct contents, %d without a model decision",
             len(rows), len(pool), len(undecided))

    rng = random.Random(args.seed)
    rng.shuffle(undecided)
    todo = undecided[:args.audit] if args.audit > 0 else undecided
    if not todo:
        print("  every distinct content in this tier already carries a decision")
        store.close()
        return 0

    use_model = not args.no_model and available()
    if not use_model:
        print("  no model available; an audit without one measures nothing",
              file=sys.stderr)
        store.close()
        return 1

    counts = {BLOCK: 0, FLAG: 0, ALLOW: 0}
    harmful = []
    t0 = time.perf_counter()
    for i, r in enumerate(todo, 1):
        try:
            tools = json.loads(r["allowed_tools"] or "[]")
        except Exception:
            tools = []
        v = inspect(r["name"] or "", r["description"] or "", r["body"] or "",
                    tools, r["path"] or "", _row_metadata(r))
        a = analyze(r["name"] or "", r["description"] or "", r["body"] or "",
                    tools, [f.as_dict() for f in v.findings])
        d = decide(v, a)
        counts[d.action] += 1
        if d.action != ALLOW:
            harmful.append((r["name"], r["repo"], v.level, d.action, explain(d)))
            log.info("audit finding: %s@%s — rules said %s, decision %s — %s",
                     r["name"], r["repo"], v.level, d.action, explain(d))
        # A lost write costs a 20-second model call, so retry rather than
        # abort; the measurement survives a contended corpus either way,
        # because the verdict is already counted above.
        for attempt in range(4):
            try:
                write_decision(store, r, v, d, a)
                break
            except sqlite3.OperationalError as exc:
                if attempt == 3:
                    log.warning("could not record %s@%s (%s); the audit "
                                "counts it but the corpus will not",
                                r["name"], r["repo"], exc)
                else:
                    time.sleep(2 * (attempt + 1))
        if i % 10 == 0 or i == len(todo):
            per = (time.perf_counter() - t0) / i
            log.info("  %d/%d  %.0fs each  eta %.1f h  [flagged %d]",
                     i, len(todo), per, (len(todo) - i) * per / 3600,
                     len(harmful))

    n = len(todo)
    k = len(harmful)
    # Clopper-Pearson upper bound at 95%. With k=0 it reduces to 1-0.05**(1/n).
    try:
        from scipy.stats import beta  # type: ignore
        upper = 1.0 if k == n else beta.ppf(0.975, k + 1, n - k)
    except Exception:
        upper = 1 - 0.05 ** (1 / n) if k == 0 else None

    overrides.apply_all(store)
    print(f"\n  audited {n} distinct contents from the score>={floor} tier")
    print(f"  block {counts[BLOCK]}   flag {counts[FLAG]}   allow {counts[ALLOW]}")
    if upper is not None:
        print(f"  contamination: {k}/{n} = {100*k/n:.2f}%   "
              f"95% upper bound {100*upper:.2f}%")
        print(f"  => this tier is at least {100*(1-upper):.2f}% clean, "
              f"with 95% confidence")
    for name, repo, lvl, act, why in harmful[:25]:
        print(f"    {act:<6} {lvl:<8} {name}@{repo} — {why}")
    store.close()
    return 0


def write_decision(store: Store, row, verdict, decision, analysis) -> None:
    """Record one decision against every copy of the same content."""
    payload = (verdict.level, decision.confidence, decision.action,
               verdict.as_json() if verdict.level != NONE else None,
               json.dumps({**decision.as_dict(),
                           "analysis": analysis.as_dict() if analysis else None}))
    if row["content_hash"]:
        store.db.execute(
            "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
            "risk_action = ?, risk_detail = ?, risk_analysis = ? "
            "WHERE content_hash = ?", (*payload, row["content_hash"]))
    else:
        store.db.execute(
            "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
            "risk_action = ?, risk_detail = ?, risk_analysis = ? "
            "WHERE id = ?", (*payload, row["id"]))
    # Committed per row: an interrupted pass must not start over.
    store.commit()


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
        "SELECT id, name, repo, description, body, body_len, allowed_tools, "
        "       path, metadata, content_hash, risk_level, risk_detail, "
        "       risk_analysis FROM skills "
        "WHERE risk_analysis IS NOT NULL").fetchall()

    changed = counts = partial = 0
    moved: dict[tuple[str, str], int] = {}

    # Grouped by content, and the representative is the copy with the most
    # text. A decision belongs to the content — that is how `--pending` and the
    # override table are both keyed — and copies of one skill do not all have
    # the same stored body: whichever repository has been re-crawled holds the
    # whole thing, the rest still hold a 4,000-character prefix. Judging each
    # row against its own prefix is what downgraded 824 of them.
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["content_hash"] or f"id:{r['id']}", []).append(r)
    log.info("re-deciding %d stored analyses across %d distinct contents; "
             "no model calls", len(rows), len(groups))

    for key, members in groups.items():
        best = max(members, key=lambda m: len(m["body"] or ""))
        stored = json.loads(best["risk_analysis"] or "{}")
        before = stored.get("action")
        raw = stored.get("analysis") or {}

        body = best["body"] or ""
        complete = len(body) >= (best["body_len"] or len(body))
        if complete:
            try:
                tools = json.loads(best["allowed_tools"] or "[]")
            except Exception:
                tools = []
            v = inspect(best["name"] or "", best["description"] or "", body,
                        tools, best["path"] or "", _row_metadata(best))
        else:
            # Nothing here holds the whole document, so the verdict already
            # recorded is the best evidence available. Reconstructed rather
            # than recomputed: a rule that matched past the cut would vanish.
            v = StoredVerdict(best["risk_level"], best["risk_detail"])
            partial += 1

        a = StoredAnalysis(raw) if raw.get("ok") else None
        d = decide(v, a)
        counts += len(members)
        if d.action != before:
            changed += len(members)
            moved[(before or "?", d.action)] = \
                moved.get((before or "?", d.action), 0) + len(members)
            log.info("  %s -> %s  %s@%s (%d cop%s) — %s", before, d.action,
                     best["name"], best["repo"], len(members),
                     "y" if len(members) == 1 else "ies", explain(d))

        payload = (v.level, d.confidence, d.action,
                   v.as_json() if v.level != NONE else None,
                   json.dumps({**d.as_dict(), "analysis": raw or None}))
        if best["content_hash"]:
            store.db.execute(
                "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
                "risk_action = ?, risk_detail = ?, risk_analysis = ? "
                "WHERE content_hash = ?", (*payload, best["content_hash"]))
        else:
            store.db.execute(
                "UPDATE skills SET risk_level = ?, risk_confidence = ?, "
                "risk_action = ?, risk_detail = ?, risk_analysis = ? "
                "WHERE id = ?", (*payload, best["id"]))
    store.commit()

    restored = overrides.apply_all(store)
    print(f"\n  re-decided {counts}; {changed} changed")
    if partial:
        print(f"    {partial} had a truncated body and kept their recorded "
              f"verdict; only the fusion was re-run")
    for (before, after), n in sorted(moved.items()):
        print(f"    {before} -> {after}: {n}")
    if restored:
        print(f"  re-applied {restored} human override(s)")
    store.close()
    return 0


class StoredVerdict:
    """The rule verdict already recorded, for a row whose body is truncated.

    Shaped like `Verdict` for `decide`. Reconstructed rather than recomputed
    because recomputing would read a prefix of the document the verdict
    describes — and a rule that matched past the cut would simply vanish.
    """

    def __init__(self, level: str, detail: str | None):
        self.level = level or NONE
        parsed = {}
        if detail:
            try:
                parsed = json.loads(detail) or {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}
        self.score = parsed.get("score", 0.0)
        self.capabilities = list(parsed.get("capabilities") or [])
        self.findings = [StoredFinding(f) for f in (parsed.get("findings") or [])]

    def as_json(self) -> str:
        return json.dumps({
            "level": self.level, "score": self.score,
            "capabilities": sorted(self.capabilities),
            "findings": [f.as_dict() for f in self.findings[:12]],
        })


class StoredFinding:
    """One recorded finding, with the fields `decide` and `as_json` read."""

    def __init__(self, raw: dict):
        self.rule = raw.get("rule") or ""
        self.weight = raw.get("weight") or 0.0
        self.evidence = raw.get("evidence") or ""

    def as_dict(self) -> dict:
        return {"rule": self.rule, "weight": self.weight,
                "evidence": self.evidence}


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
    ap.add_argument("--pending", action="store_true",
                    help="model only the skills the crawler already flagged, "
                         "read from the stored verdict instead of re-running "
                         "the rules over the corpus; the only mode that is "
                         "cheap enough to run in a loop")
    ap.add_argument("--topup", action="store_true",
                    help="clear the recorded decision for any skill the *current* "
                         "rules gate but which was never modelled, so a resumed "
                         "run picks up only what new rules newly flagged")
    ap.add_argument("--redecide", action="store_true",
                    help="re-run only the fusion layer, from the model output "
                         "already stored; makes no model calls")
    ap.add_argument("--audit", type=int, default=-1, metavar="N",
                    help="model N randomly chosen skills from the curated tier "
                         "regardless of whether the rules flagged them, to "
                         "measure false negatives and report a confidence "
                         "bound on how clean the tier is; --audit 0 audits "
                         "the tier exhaustively")
    ap.add_argument("--score-floor", type=int, default=70,
                    help="quality floor defining the curated tier for --audit")
    ap.add_argument("--seed", type=int, default=0,
                    help="sampling seed, so an audit can be reproduced")
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

    if args.audit >= 0:
        return audit_index(store, args)
    if args.redecide:
        return redecide(store)

    if args.pending:
        return model_pending(store, args)

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
