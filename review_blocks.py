#!/usr/bin/env python
"""Review, explain and override blocking decisions.

Blocking on suspicion is defensible only if the blocks can be inspected and
reversed. A gate with 92% precision is wrong about roughly one skill in twelve,
and without a way to see and correct that, the errors are permanent and
invisible — which is a worse property than the attacks the gate prevents.

    python review_blocks.py list dist/skills.db
    python review_blocks.py list dist/skills.db --action flag --min 0.8
    python review_blocks.py show dist/skills.db owner/repo path/SKILL.md
    python review_blocks.py allow dist/skills.db owner/repo path/SKILL.md "why"
    python review_blocks.py reblock dist/skills.db owner/repo path/SKILL.md

An override is recorded, not just applied: `overrides` keeps who decided what
and why, keyed on the skill's **content hash** rather than its path. That
matters because a decision about a path would silently carry over to whatever
replaced it, and the thing reviewed was the content.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine import overrides
from skill_engine.store import Store


def cmd_list(store: Store, args) -> int:
    where = ["COALESCE(risk_action,'allow') != 'allow'"]
    params: list = []
    if args.action:
        where = ["risk_action = ?"]
        params.append(args.action)
    if args.min is not None:
        where.append("COALESCE(risk_confidence,0) >= ?")
        params.append(args.min)
    rows = store.db.execute(
        f"SELECT repo, path, name, risk_level, risk_action, risk_confidence, "
        f"       risk_analysis FROM skills WHERE {' AND '.join(where)} "
        f"ORDER BY risk_confidence DESC, repo LIMIT ?",
        (*params, args.limit)).fetchall()
    if not rows:
        print("  nothing matches")
        return 0
    print(f"  {'conf':<6}{'action':<7}{'rule':<10}{'skill':<30}repo")
    print("  " + "-" * 86)
    for r in rows:
        conf = r["risk_confidence"]
        print(f"  {conf if conf is not None else 0:<6.2f}"
              f"{(r['risk_action'] or '?'):<7}{(r['risk_level'] or '?'):<10}"
              f"{(r['name'] or '')[:28]:<30}{(r['repo'] or '')[:34]}")
    print(f"\n  {len(rows)} shown. Inspect one with:"
          f"\n    python review_blocks.py show <db> <repo> <path>")
    return 0


def cmd_show(store: Store, args) -> int:
    r = store.db.execute(
        "SELECT id, name, repo, path, description, body, content_hash, "
        "       risk_level, risk_action, risk_confidence, risk_detail, "
        "       risk_analysis FROM skills WHERE repo = ? AND path = ?",
        (args.repo, args.path)).fetchone()
    if r is None:
        print(f"  no skill at {args.repo}/{args.path}")
        return 1

    print(f"\n  {r['name']}   ({r['repo']}/{r['path']})")
    print(f"  {(r['description'] or '')[:150]}")
    print(f"\n  rule level : {r['risk_level'] or 'unassessed'}")
    print(f"  decision   : {r['risk_action'] or 'unassessed'}"
          f"   confidence {r['risk_confidence'] if r['risk_confidence'] is not None else '—'}")

    detail = json.loads(r["risk_detail"] or "{}")
    if detail.get("findings"):
        print("\n  what the rules matched:")
        for f in detail["findings"][:10]:
            ev = (f.get("evidence") or "").replace("\n", " ")[:70]
            print(f"    {f['rule']:<28}{f['weight']:>5}  {ev!r}")

    analysis = json.loads(r["risk_analysis"] or "{}")
    if analysis.get("reasons"):
        print("\n  why it was actioned:")
        for reason in analysis["reasons"]:
            print(f"    - {reason}")
    a = analysis.get("analysis") or {}
    if a.get("ok"):
        print(f"\n  the model's reading:")
        print(f"    claims to    : {a.get('claimed_purpose','')[:100]}")
        print(f"    instructs    : {'; '.join(a.get('instructed_actions') or [])[:110]}")
        print(f"    harm         : {a.get('harm_if_followed')}")
        print(f"    mismatch     : {a.get('purpose_mismatch')}"
              f"  {a.get('mismatch_explanation','')[:60]}")
        print(f"    addresses reviewer: {a.get('addresses_reviewer')}")

    ov = store.db.execute("SELECT * FROM overrides WHERE content_hash = ?",
                          (r["content_hash"],)).fetchone()
    if ov:
        print(f"\n  OVERRIDDEN to '{ov['action']}' by {ov['decided_by']}: {ov['reason']}")

    # The body last and excerpted: the point of a review is to read the thing.
    body = r["body"] or ""
    print(f"\n  body ({len(body):,} chars, first 700):")
    for line in body[:700].splitlines()[:22]:
        print(f"    {line[:100]}")
    return 0


def cmd_allow(store: Store, args) -> int:
    r = store.db.execute(
        "SELECT content_hash, name FROM skills WHERE repo = ? AND path = ?",
        (args.repo, args.path)).fetchone()
    if r is None:
        print(f"  no skill at {args.repo}/{args.path}")
        return 1
    who = args.by or os.getenv("USER") or "unknown"
    overrides.record(store, r["content_hash"], args.action, args.reason, who,
                     args.repo, args.path)
    # Applied to every copy of the same content, since the review was of the
    # content and the corpus is full of vendored duplicates.
    n = store.db.execute(
        "UPDATE skills SET risk_action = ? WHERE content_hash = ?",
        (args.action, r["content_hash"])).rowcount
    store.commit()
    print(f"  {r['name']}: set to '{args.action}' across {n} cop"
          f"{'y' if n == 1 else 'ies'} of the same content")
    print(f"  recorded: {who} — {args.reason}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="blocked and flagged skills, worst first")
    p.add_argument("db"); p.add_argument("--action", choices=["block", "flag"])
    p.add_argument("--min", type=float); p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("show", help="the full evidence behind one decision")
    p.add_argument("db"); p.add_argument("repo"); p.add_argument("path")

    p = sub.add_parser("allow", help="override a decision to allow")
    p.add_argument("db"); p.add_argument("repo"); p.add_argument("path")
    p.add_argument("reason"); p.add_argument("--by")

    p = sub.add_parser("reblock", help="override a decision back to block")
    p.add_argument("db"); p.add_argument("repo"); p.add_argument("path")
    p.add_argument("reason", nargs="?", default="re-blocked after review")
    p.add_argument("--by")

    args = ap.parse_args()
    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1
    store = Store(path)
    overrides.ensure(store)
    try:
        if args.cmd == "list":
            return cmd_list(store, args)
        if args.cmd == "show":
            return cmd_show(store, args)
        args.action = "allow" if args.cmd == "allow" else "block"
        return cmd_allow(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
