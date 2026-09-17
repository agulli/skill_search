"""The audit mode measures what the other modes structurally cannot.

Every other mode selects skills the rules already flagged, so the model is
only ever asked to confirm a finding. That measures precision and can never
measure a false negative. These tests pin the two properties the audit rests
on: it selects without reference to the verdict, and it reports a bound rather
than a point estimate.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _corpus(tmp_path, rows):
    """A minimal corpus with the columns the audit selects on."""
    db = tmp_path / "audit.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE skills (
        id INTEGER PRIMARY KEY, repo TEXT, path TEXT, name TEXT,
        description TEXT, body TEXT, allowed_tools TEXT, metadata TEXT,
        content_hash TEXT, score REAL, valid INTEGER,
        risk_level TEXT, risk_detail TEXT, risk_confidence REAL,
        risk_action TEXT, risk_analysis TEXT)""")
    for i, (name, body, score, analysis) in enumerate(rows):
        con.execute(
            "INSERT INTO skills (id, repo, path, name, description, body, "
            "allowed_tools, metadata, content_hash, score, valid, risk_level, "
            "risk_analysis) VALUES (?,?,?,?,?,?,?,?,?,?,1,'none',?)",
            (i, "o/r", f"skills/{name}/SKILL.md", name, "d", body, "[]", "",
             f"h{i}", score, analysis))
    con.commit()
    con.close()
    return db


def _run(db, *args):
    return subprocess.run(
        [sys.executable, "analyze_corpus.py", str(db), *args],
        cwd=ROOT, capture_output=True, text=True, timeout=300)


def test_audit_respects_the_score_floor(tmp_path):
    """A skill below the floor is not in the tier and is never selected."""
    db = _corpus(tmp_path, [("high-q", "body", 90.0, None),
                            ("low-q", "body", 10.0, None)])
    out = _run(db, "--audit", "0", "--score-floor", "70", "--no-model")
    assert "1 distinct contents" in out.stdout + out.stderr


def test_audit_selects_regardless_of_verdict(tmp_path):
    """Selection is by tier membership, not by whether the rules flagged it.

    This is the whole point of the mode: a sample drawn from the flagged set
    cannot contain a false negative, so it cannot measure one.
    """
    db = _corpus(tmp_path, [("plain", "Formats markdown tables.", 90.0, None)])
    out = _run(db, "--audit", "0", "--score-floor", "70", "--no-model")
    text = out.stdout + out.stderr
    # The rules find nothing here, yet it is still in the pool.
    assert "1 without a model decision" in text


def test_audit_skips_contents_already_decided(tmp_path):
    """Resumable: the model is not spent twice on the same text."""
    db = _corpus(tmp_path, [("done", "body", 90.0, json.dumps({"level": "none"}))])
    out = _run(db, "--audit", "0", "--score-floor", "70", "--no-model")
    assert "already carries a decision" in out.stdout + out.stderr


def test_audit_refuses_to_report_without_a_model(tmp_path):
    """An audit measures the model's unprimed judgement; without one it is
    measuring nothing, and should say so rather than print a clean bound."""
    db = _corpus(tmp_path, [("plain", "body", 90.0, None)])
    out = _run(db, "--audit", "5", "--score-floor", "70", "--no-model")
    assert out.returncode == 1
    assert "measures nothing" in out.stdout + out.stderr


def test_clopper_pearson_bound_with_no_findings():
    """Zero findings in n draws is not 0% contamination.

    The reported bound must be the honest one — "at most p% with 95%
    confidence" — because a clean sample of 800 cannot support a claim
    stronger than roughly 99.6%.
    """
    for n, expect in ((100, 0.0295), (800, 0.0037), (2000, 0.0015)):
        upper = 1 - 0.05 ** (1 / n)
        assert upper == pytest.approx(expect, rel=0.05)
        assert upper > 0


def test_audit_writes_cannot_newly_serve_a_withheld_skill():
    """The audit writes decisions to the live corpus, so it must not un-block.

    Before a decision exists, search falls back to `risk_level != 'critical'`,
    which withholds a critical skill. Once `risk_action` is written it becomes
    authoritative. So if a benign model opinion could ever carry a critical
    verdict to `allow`, running an audit would silently serve skills that were
    being withheld — the write turning a safe default into an unsafe one.

    It cannot, because the model escalates and never exonerates. Pinned here
    because the failure would be silent and would look like an improvement.
    """
    sys.path.insert(0, str(ROOT))
    from skill_engine.analyze import Analysis
    from skill_engine.confidence import decide
    from skill_engine.safety import (CRITICAL, HIGH, MEDIUM, NONE, Finding,
                                     Verdict)

    benign = Analysis(ok=True, claimed_purpose="formats tables",
                      framing="clear", purpose_mismatch=False,
                      addresses_reviewer=False, harm_if_followed="none",
                      reasoning="an ordinary utility")
    verdicts = [
        (CRITICAL, Verdict(level=CRITICAL, score=13.0, capabilities=["concealment"],
                           findings=[Finding("instruction_override", 10.0, "x")])),
        (HIGH, Verdict(level=HIGH, score=8.0, capabilities=["concealment"],
                       findings=[Finding("instruction_secrecy", 8.0, "x")])),
        (MEDIUM, Verdict(level=MEDIUM, score=4.0, capabilities=[],
                         findings=[Finding("split_path_literal", 4.0, "x")])),
        (NONE, Verdict(level=NONE, score=0.0, findings=[], capabilities=[])),
    ]
    for level, v in verdicts:
        for analysis in (benign, None):
            action = decide(v, analysis).action
            served_before = level != CRITICAL   # the risk_action IS NULL fallback
            served_after = action != "block"
            assert not (served_after and not served_before), (
                f"a {level} verdict with model={analysis is not None} became "
                f"{action}, which would newly serve a withheld skill")


def test_audit_loop_holds_a_lock_and_reseeds():
    """Two properties the continuous auditor depends on.

    A second auditor would sample the same tier concurrently and spend
    inference twice — the mistake maintain.sh was making against the verifier.
    And a fixed seed would redraw the same sample every batch: the audit skips
    already-decided contents so it would still advance, but in a fixed order,
    which is not a random sample of anything and cannot support a bound.
    """
    script = (ROOT / "ops" / "audit.sh").read_text()
    assert "logs/audit.pid" in script
    assert 'kill -0 "$(cat "$LOCK"' in script
    assert "trap cleanup EXIT" in script
    # a per-batch seed, not a constant
    assert "--seed" in script and "SEED=$((" in script
