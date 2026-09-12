"""Human decisions that outrank the automated ones.

The gate blocks on suspicion, which means it is sometimes wrong, which means a
person must be able to overrule it — and the overruling has to *stick*. A
reviewer who clears a skill on Monday and finds it blocked again after Tuesday's
analysis pass has not been given a review tool; they have been given a form that
throws away its input.

So overrides live in their own table and are re-asserted after every pass that
writes `risk_action`. Two properties matter:

**Keyed on content, not path.** `repo/path/SKILL.md` names a location whose
contents change. A decision was made about text, so it is recorded against the
hash of that text — and it therefore applies to every vendored copy of the same
skill in the corpus at once, which is what a reviewer means anyway. If the file
is later replaced with different content, the override stops applying, which is
the correct default: nobody reviewed the replacement.

**Recorded, not just applied.** Who, when and why, kept alongside the action.
An unexplained allow is indistinguishable from a mistake six months later.
"""

from __future__ import annotations

import os
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS overrides (
    content_hash TEXT PRIMARY KEY,
    action       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    decided_by   TEXT,
    decided_at   REAL NOT NULL,
    repo         TEXT,
    path         TEXT
);
"""


def ensure(store) -> None:
    """Create the overrides table and the decision columns if absent."""
    store.db.executescript(SCHEMA)
    cols = {r["name"] for r in store.db.execute("PRAGMA table_info(skills)")}
    for name, decl in (("risk_confidence", "REAL"), ("risk_action", "TEXT"),
                       ("risk_analysis", "TEXT")):
        if name not in cols:
            store.db.execute(f"ALTER TABLE skills ADD COLUMN {name} {decl}")
    store.commit()


def record(store, content_hash: str, action: str, reason: str,
           who: str | None = None, repo: str = "", path: str = "") -> None:
    """Record a human decision. Does not apply it; call `apply_all` for that."""
    store.db.execute(
        "INSERT INTO overrides(content_hash, action, reason, decided_by, "
        "decided_at, repo, path) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(content_hash) DO UPDATE SET action=excluded.action, "
        "reason=excluded.reason, decided_by=excluded.decided_by, "
        "decided_at=excluded.decided_at",
        (content_hash, action, reason,
         who or os.getenv("USER") or "unknown", time.time(), repo, path))


def apply_all(store) -> int:
    """Re-assert every recorded override over the automated decisions.

    Called at the end of any pass that writes `risk_action`. Returns the number
    of skill rows changed — normally larger than the number of overrides,
    because one decision covers every copy of that content.
    """
    try:
        n = store.db.execute(
            "UPDATE skills SET risk_action = ("
            "  SELECT o.action FROM overrides o "
            "  WHERE o.content_hash = skills.content_hash) "
            "WHERE content_hash IN (SELECT content_hash FROM overrides) "
            "  AND risk_action IS NOT ("
            "  SELECT o.action FROM overrides o "
            "  WHERE o.content_hash = skills.content_hash)").rowcount
    except Exception:
        return 0
    store.commit()
    return max(n, 0)


def count(store) -> dict[str, int]:
    """How many decisions of each kind are on record."""
    try:
        rows = store.db.execute(
            "SELECT action, COUNT(*) n FROM overrides GROUP BY action").fetchall()
    except Exception:
        return {}
    return {r["action"]: r["n"] for r in rows}
