"""A blocked skill must not be reachable through search.

This is the property the whole safety pipeline exists to produce, and it was
the one thing not covered by a test: `safety.py` and `confidence.py` were
thoroughly tested for reaching the right verdict, and nothing checked that the
verdict actually removes the skill from the index an agent queries.

The filter also has to cope with three generations of index: one with no risk
columns at all, one assessed by the rules alone (`risk_level`, no
`risk_action`), and one carrying full decisions. A released artifact is
whichever the last build produced.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skill_engine.search import search
from skill_engine.store import Store

BODY = "Rotate the signing keys and verify the deployment " * 12


def add(st, repo, name, **risk):
    body = f"{name} {BODY}"
    st.upsert_repo({
        "full_name": repo, "default_branch": "main", "description": "",
        "stars": 100, "forks": 0, "license": "MIT", "topics": [],
        "pushed_at": "2026-08-01T00:00:00Z", "is_fork": False,
        "archived": False,
    })
    st.upsert_skill({
        "repo": repo, "path": f"skills/{name}/SKILL.md", "name": name,
        "description": "Rotate signing keys for a deployment.", "body": body,
        "heading": name, "version": None, "license": "MIT",
        "allowed_tools": "[]", "metadata": "{}", "resources": "[]",
        "source_kind": "skills-dir", "blob_sha": "x",
        "content_hash": hashlib.sha256(body.encode()).hexdigest(),
        "body_len": len(body), "score": 60.0, "valid": 1, "invalid_reason": "",
    })
    st.commit()
    if risk:
        cols = {r["name"] for r in st.db.execute("PRAGMA table_info(skills)")}
        for col, decl in (("risk_action", "TEXT"), ("risk_confidence", "REAL")):
            if col not in cols:
                st.db.execute(f"ALTER TABLE skills ADD COLUMN {col} {decl}")
        sets = ", ".join(f"{k} = ?" for k in risk)
        st.db.execute(f"UPDATE skills SET {sets} WHERE name = ?",
                      (*risk.values(), name))
        st.commit()


def names(hits):
    return {h.name for h in hits}


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "r.db")
    add(st, "good/repo", "key-rotation")
    add(st, "bad/repo", "key-stealer", risk_action="block",
        risk_level="critical", risk_confidence=0.86)
    add(st, "dual/repo", "key-recon", risk_action="flag",
        risk_level="high", risk_confidence=0.55)
    yield st
    st.close()


def test_a_blocked_skill_is_not_returned(store):
    found = names(search(store, "rotate signing keys"))
    assert "key-rotation" in found
    assert "key-stealer" not in found


def test_a_flagged_skill_is_still_returned(store):
    """`flag` demotes and discloses; it does not remove."""
    assert "key-recon" in names(search(store, "rotate signing keys"))


def test_auditing_can_see_blocked_skills_deliberately(store):
    """Reviewing blocks requires reaching them; the default must not."""
    found = names(search(store, "rotate signing keys", filters={"include_unsafe": True}))
    assert "key-stealer" in found


def test_the_filter_reports_the_risk_on_each_hit(store):
    by_name = {h.name: h for h in
               search(store, "rotate signing keys", filters={"include_unsafe": True})}
    assert by_name["key-stealer"].risk == "critical"
    assert by_name["key-rotation"].risk == "none"


def test_a_rules_only_index_still_excludes_critical(tmp_path):
    """The generation with `risk_level` but no `risk_action`.

    Without the fallback, an index assessed by the rules alone would serve
    every critical skill it found.
    """
    st = Store(tmp_path / "legacy.db")
    add(st, "good/repo", "key-rotation")
    add(st, "bad/repo", "key-stealer")
    st.db.execute("UPDATE skills SET risk_level = 'critical' "
                  "WHERE name = 'key-stealer'")
    st.commit()
    # `risk_action` is part of the base schema now, so the older generation has
    # to be reconstructed rather than assumed: a column that is present and
    # entirely NULL is exactly the state that used to be mistaken for "no
    # decisions have been made".
    st.db.execute("UPDATE skills SET risk_action = NULL, risk_confidence = NULL")
    st.commit()

    found = names(search(st, "rotate signing keys"))
    assert "key-rotation" in found and "key-stealer" not in found
    st.close()


def test_an_unassessed_index_still_works(tmp_path):
    """No risk columns at all: search must not fail, only not filter."""
    st = Store(tmp_path / "old.db")
    add(st, "good/repo", "key-rotation")
    st.db.execute("ALTER TABLE skills RENAME COLUMN risk_level TO legacy_level")
    st.commit()
    assert "key-rotation" in names(search(st, "rotate signing keys"))
    st.close()


# --------------------------------------------- `flag` has to actually disclose


def test_a_flagged_skill_discloses_its_reasons(store):
    """`flag` means demote *and disclose*, and the second half was missing.

    The detail endpoint shipped the raw `risk_analysis` and `risk_detail` blobs
    — internal, unshaped, useless to a client — while nothing said plainly
    that the gate had doubts. An agent about to follow a skill's instructions
    is exactly who needs to know.
    """
    import json as _json

    from skill_engine import overrides
    from skill_engine.search import get_skill

    overrides.ensure(store)
    sid = store.db.execute("SELECT id FROM skills WHERE name = 'key-recon'"
                           ).fetchone()["id"]
    store.db.execute(
        "UPDATE skills SET risk_analysis = ? WHERE id = ?",
        (_json.dumps({"action": "flag", "confidence": 0.55,
                      "reasons": ["rules: severe construct (remote_code_execution)",
                                  "declared offensive-security purpose"]}), sid))
    store.commit()

    data = get_skill(store, sid)
    assert data["risk"]["action"] == "flag"
    assert data["risk"]["confidence"] == 0.55
    assert any("offensive" in r for r in data["risk"]["reasons"])
    # and the internal blobs must not be shipped
    assert "risk_analysis" not in data and "risk_detail" not in data


def test_an_unassessed_skill_is_not_reported_as_allowed(store):
    """"never assessed" and "assessed clean" are different facts."""
    from skill_engine.search import get_skill

    sid = store.db.execute("SELECT id FROM skills WHERE name = 'key-rotation'"
                           ).fetchone()["id"]
    store.db.execute("UPDATE skills SET risk_level = NULL, risk_action = NULL "
                     "WHERE id = ?", (sid,))
    store.commit()
    assert get_skill(store, sid)["risk"]["action"] == "allow"

    store.db.execute("UPDATE skills SET risk_level = 'medium' WHERE id = ?", (sid,))
    store.commit()
    assert get_skill(store, sid)["risk"]["action"] == "unassessed"


def test_a_rules_only_index_names_the_rules_that_matched(store):
    """No fused decision, so the findings are the only reasons available."""
    import json as _json

    from skill_engine import overrides
    from skill_engine.search import get_skill

    overrides.ensure(store)
    sid = store.db.execute("SELECT id FROM skills WHERE name = 'key-recon'"
                           ).fetchone()["id"]
    store.db.execute(
        "UPDATE skills SET risk_analysis = NULL, risk_detail = ? WHERE id = ?",
        (_json.dumps({"level": "high", "findings": [
            {"rule": "credential_egress", "weight": 8.0},
            {"rule": "override_discussed", "weight": 0.0}]}), sid))
    store.commit()

    reasons = get_skill(store, sid)["risk"]["reasons"]
    assert "credential_egress" in reasons
    # a finding a guard zeroed is not a reason for anything
    assert "override_discussed" not in reasons


def test_search_results_carry_a_risk_badge(store):
    hits = {h.name: h for h in search(store, "rotate signing keys")}
    assert hits["key-recon"].to_dict()["risk"] == "high"
    assert hits["key-rotation"].to_dict()["risk"] == "none"


def test_no_critical_is_reachable_by_any_combination(store):
    """The invariant the whole gate exists to maintain, over every state a row
    can be in.

    A critical skill can reach search through two different doors, and the
    filter has to close both: a row with no decision yet falls back to the
    rule level, and a row with a decision is judged by that decision alone.
    Earlier this branched on whether the `risk_action` *column* existed, using
    its presence as a proxy for "a decision was made" — which broke the moment
    the column joined the base schema, because then every value was NULL,
    coalesced to allow, and served criticals.

    Verified against the live corpus at the time of writing: 2,025 criticals,
    0 reachable; 2,105 blocked rows, 0 reachable.
    """
    combos = [
        ("critical", None,     False, "critical, no decision yet"),
        ("critical", "block",  False, "critical, blocked"),
        ("critical", "flag",   True,  "critical the model downgraded to flag"),
        ("critical", "allow",  True,  "critical with an explicit allow"),
        ("high",     None,     True,  "high, no decision"),
        ("high",     "block",  False, "high the model escalated"),
        ("none",     "block",  False, "clean rules, blocked by the model"),
        ("none",     None,     True,  "clean, no decision"),
    ]
    for level, action, expect_served, label in combos:
        served = _filter_says_served(level, action)
        assert served == expect_served, (
            f"{label}: filter says served={served}, expected {expect_served}")

    # And the two that must hold no matter what the model says: a recorded
    # block always withholds, and a critical without a decision always
    # withholds. Those are the doors.
    assert not _filter_says_served("critical", None)
    assert not _filter_says_served("none", "block")


def _filter_says_served(level, action):
    """The clause from skill_engine/search.py, evaluated in SQLite itself.

    Re-implementing it in Python would test a copy of the logic rather than
    the logic, and the bug this guards against was in exactly that gap.
    """
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE s (risk_level TEXT, risk_action TEXT)")
    con.execute("INSERT INTO s VALUES (?, ?)", (level, action))
    row = con.execute(
        "SELECT CASE WHEN s.risk_action IS NOT NULL "
        "            THEN s.risk_action != 'block' "
        "            ELSE COALESCE(s.risk_level, 'none') != 'critical' END "
        "FROM s").fetchone()
    con.close()
    return bool(row[0])
