#!/usr/bin/env python
"""Measure the blocking gate against the hand-labelled set.

    python eval_gate.py data/big.db              # rules only, ~11 min
    python eval_gate.py data/big.db --model      # rules + local model
    python eval_gate.py data/big.db --blocks     # also read every block

Written because this measurement was being re-typed by hand every time the
rules changed, and a measurement you retype is a measurement you eventually
skip. Three things it reports, none of which is derivable from the others:

**Recall** on labelled attacks — an attack rated `none` is invisible to the
whole pipeline, since the model only ever sees what the rules gate.

**False positives** on labelled legitimate skills, all of which were chosen
because a naive detector blocks them: defensive skills that quote the attack,
scanners that contain their own signatures, i18n skills full of bidi marks,
honestly-described red-team tooling.

**The blocking set in full.** Blocking is the only irreversible action here, so
`--blocks` prints every critical with its evidence. Reading all 32 is how seven
false positives were found that sampling would have missed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures.safety_evalset import ATTACKS, BENIGN          # noqa: E402
from skill_engine.safety import CRITICAL, HIGH, MEDIUM, NONE, inspect  # noqa: E402
from skill_engine.store import Store                          # noqa: E402

GATED = (CRITICAL, HIGH, MEDIUM)


def verdict_for(store, row):
    try:
        tools = json.loads(row["allowed_tools"] or "[]")
    except Exception:
        tools = []
    return inspect(row["name"] or "", row["description"] or "",
                   row["body"] or "", tools, row["path"] or "")


def lookup(store, key):
    name, repo = key.split("@", 1)
    return store.db.execute(
        "SELECT * FROM skills WHERE name = ? AND repo = ?", (name, repo)
    ).fetchone()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--model", action="store_true",
                    help="also run the local model and the fusion layer")
    ap.add_argument("--blocks", action="store_true",
                    help="print every blocked skill with its evidence")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1
    store = Store(path)

    decide = analyze = None
    if args.model:
        from skill_engine.analyze import analyze as _analyze, available
        from skill_engine.confidence import decide as _decide
        if not available():
            print("no local model reachable; run without --model", file=sys.stderr)
            return 1
        analyze, decide = _analyze, _decide

    def assess(row):
        v = verdict_for(store, row)
        if not args.model:
            return v, None
        try:
            tools = json.loads(row["allowed_tools"] or "[]")
        except Exception:
            tools = []
        a = analyze(row["name"] or "", row["description"] or "",
                    row["body"] or "", tools, [f.as_dict() for f in v.findings])
        return v, decide(v, a)

    # ---------------------------------------------------------- the labelled set
    print(f"\n  labelled set, measured against {path}")
    print("  " + "-" * 74)
    missed, absent_a = [], []
    for key in sorted(ATTACKS):
        row = lookup(store, key)
        if row is None:
            absent_a.append(key)
            continue
        v, d = assess(row)
        # `low` is NOT caught. It sits below the review gate, so the model
        # never sees it and the fusion layer allows it at 0.10 — indistinguish-
        # able in effect from `none`. Counting it as caught reported 18/18
        # while two attacks were in fact being served.
        caught = v.level in GATED or (d and d.action != "allow")
        if not caught:
            missed.append(key)
        mark = "   <-- MISSED" if not caught else ""
        extra = f"  {d.action}@{d.confidence:.2f}" if d else ""
        print(f"  attack  {v.level:9}{extra:16}{key[:44]}{mark}")

    blocked_benign, absent_b = [], []
    for key in sorted(BENIGN):
        row = lookup(store, key)
        if row is None:
            absent_b.append(key)
            continue
        v, d = assess(row)
        bad = v.level == CRITICAL or (d and d.action == "block")
        if bad:
            blocked_benign.append(key)
        mark = "   <-- FALSE POSITIVE" if bad else ""
        extra = f"  {d.action}@{d.confidence:.2f}" if d else ""
        print(f"  benign  {v.level:9}{extra:16}{key[:44]}{mark}")

    n_a = len(ATTACKS) - len(absent_a)
    n_b = len(BENIGN) - len(absent_b)
    print("  " + "-" * 74)
    print(f"  attacks caught      {n_a - len(missed)}/{n_a}")
    print(f"  benign not blocked  {n_b - len(blocked_benign)}/{n_b}")
    if absent_a or absent_b:
        print(f"  not in this corpus  {len(absent_a) + len(absent_b)}")
    if missed:
        print(f"  MISSED: {missed}")
    if blocked_benign:
        print(f"  FALSE POSITIVES: {blocked_benign}")

    # ---------------------------------------- the independent benchmark
    #
    # Several skill-vetting projects ship purpose-built malicious skills under
    # `**/malicious/**`, one per attack class. This is the honest recall
    # measure, and it exists because the hand-labelled set above was not one:
    # that set was assembled from attacks found by looking at what the gate
    # flagged, so it reported 21/21 while an independent benchmark of 225
    # attacks was 87% allowed.
    print(f"\n  independent benchmark (skills under a '/malicious/' path)")
    print("  " + "-" * 74)
    bench = store.db.execute(
        "SELECT name, repo, description, body, allowed_tools, path FROM skills "
        "WHERE path LIKE '%/malicious/%' AND valid = 1").fetchall()
    levels: Counter = Counter()
    below = []
    for row in bench:
        v = verdict_for(store, row)
        levels[v.level] += 1
        if v.level not in GATED:
            below.append((row["path"], row["description"] or ""))
    gated_n = len(bench) - len(below)
    for level in (CRITICAL, HIGH, MEDIUM, "low", NONE):
        if levels[level]:
            print(f"    {level:10}{levels[level]:>6}")
    if bench:
        print(f"  reaching the review gate: {gated_n}/{len(bench)} "
              f"({100 * gated_n / len(bench):.0f}%)")
    if below and args.blocks:
        print(f"\n  below the gate, and so never modelled ({len(below)}):")
        for path, desc in below[:40]:
            print(f"    {path[:60]:<62}{desc[:40]}")

    # ------------------------------------------------------ the whole corpus
    print(f"\n  scanning the corpus…")
    t0 = time.perf_counter()
    counts: Counter = Counter()
    criticals = []
    cur = store.db.execute(
        "SELECT name, repo, description, body, allowed_tools, path "
        "FROM skills WHERE valid = 1")
    seen = 0
    while True:
        chunk = cur.fetchmany(2000)
        if not chunk:
            break
        for row in chunk:
            seen += 1
            v = verdict_for(store, row)
            counts[v.level] += 1
            if v.level == CRITICAL:
                criticals.append((f"{row['name']}@{row['repo']}",
                                  (row["description"] or "")[:64],
                                  [(f.rule, (f.evidence or "")[:48])
                                   for f in v.findings if f.weight > 0]))
    gated = sum(counts[l] for l in GATED)
    print(f"  {seen:,} skills in {time.perf_counter() - t0:.0f}s")
    for level in (CRITICAL, HIGH, MEDIUM, "low", NONE):
        n = counts[level]
        print(f"    {level:10}{n:>8,}  {100 * n / max(seen, 1):6.3f}%")
    print(f"  gated for model review: {gated:,} ({100 * gated / max(seen, 1):.2f}%)")

    if args.blocks:
        print(f"\n  every blocked skill ({len(criticals)}):")
        for who, desc, ev in criticals:
            print(f"  === {who[:64]}")
            print(f"      {desc}")
            for rule, e in ev[:3]:
                print(f"      {rule:26}{e!r}")

    store.close()
    return 1 if (missed or blocked_benign) else 0


if __name__ == "__main__":
    sys.exit(main())
