"""Fusing the rules and the model into one decision.

The governing rule is that **the model escalates but never exonerates**, and it
is easy to violate by accident: the first version of `decide` was an if/elif
ladder ordered by precision, with `addresses_reviewer` (0.10) above
`rule_medium` (0.30). Any rule-medium skill whose model output mentioned a
reviewer therefore took the weaker basis and dropped from `flag` to `allow` —
28 of the first 47 real decisions. The signal with a 30.2% false-positive rate
was clearing skills the rules had flagged.

So the invariant is tested exhaustively rather than by example: over every
combination of model outputs, a verdict's confidence must never fall below what
the rules alone would have given it.
"""

import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skill_engine.confidence import (ALLOW, BLOCK, BLOCK_THRESHOLD, FLAG,
                                     decide, explain)
from skill_engine.safety import CRITICAL, HIGH, LOW, MEDIUM, NONE, Verdict


class FakeAnalysis:
    """Stands in for a model response, including incoherent ones."""

    def __init__(self, harm="none", mismatch=False, addressed=False,
                 explanation="", ok=True):
        self.ok = ok
        self.harm_if_followed = harm
        self.purpose_mismatch = mismatch
        self.addresses_reviewer = addressed
        self.mismatch_explanation = explanation
        self.framing = "unclear"
        self.claimed_purpose = ""
        self.instructed_actions = []


HARMS = ("none", "minor", "serious", "severe")
LEVELS = (NONE, LOW, MEDIUM, HIGH, CRITICAL)


def every_analysis():
    for harm, mm, addr, ok in itertools.product(HARMS, (False, True),
                                                (False, True), (True, False)):
        yield FakeAnalysis(harm, mm, addr, "it does more than it claims", ok)


# ------------------------------------------------- the exonerating-model bug


def test_the_model_never_lowers_a_rule_verdict():
    """The invariant. Exhaustive, because the bug it caught was an ordering slip."""
    for level in LEVELS:
        floor = decide(Verdict(level=level), None).confidence
        for a in every_analysis():
            d = decide(Verdict(level=level), a)
            assert d.confidence >= floor, (
                f"{level} fell from {floor} to {d.confidence} via {d.basis}")


def test_addresses_reviewer_cannot_clear_a_flagged_skill():
    """The exact regression: a 30.2%-false-positive signal must not exonerate."""
    v = Verdict(level=MEDIUM)
    alone = decide(v, None)
    with_signal = decide(v, FakeAnalysis(addressed=True))
    assert alone.action == FLAG
    assert with_signal.action == FLAG
    assert with_signal.basis == "rule_medium_alone"


def test_addresses_reviewer_alone_never_acts():
    """It fires on 30% of clean skills, so on its own it is informational."""
    d = decide(Verdict(level=NONE), FakeAnalysis(addressed=True))
    assert d.action == ALLOW
    assert "addressed to a reviewer" in " ".join(d.reasons)


# ------------------------------------------------------------- must block


def test_rule_critical_blocks_even_when_the_model_sees_nothing():
    """Six labelled attacks had the model report harm=none. Silence is not proof."""
    d = decide(Verdict(level=CRITICAL), FakeAnalysis(harm="none"))
    assert d.action == BLOCK
    assert d.confidence >= BLOCK_THRESHOLD


def test_the_model_can_escalate_what_the_rules_only_suspected():
    """Where the model adds recall the rules do not have."""
    assert decide(Verdict(level=MEDIUM), None).action == FLAG
    assert decide(Verdict(level=MEDIUM),
                  FakeAnalysis(harm="severe")).action == BLOCK


def test_agreement_is_more_confident_than_the_rules_alone():
    rules_only = decide(Verdict(level=CRITICAL), None)
    agreed = decide(Verdict(level=CRITICAL), FakeAnalysis(harm="severe"))
    assert agreed.confidence > rules_only.confidence


# ----------------------------------------------------------- must not block


def test_a_clean_skill_with_no_evidence_is_allowed():
    d = decide(Verdict(level=NONE), FakeAnalysis())
    assert d.action == ALLOW and d.confidence == 0.0 and d.basis == "no_evidence"


def test_an_unavailable_model_is_no_information_not_reassurance():
    """A failed call must leave the rule verdict exactly where it was."""
    failed = FakeAnalysis(ok=False)
    for level in LEVELS:
        assert (decide(Verdict(level=level), failed).confidence
                == decide(Verdict(level=level), None).confidence)


def test_confidence_never_reaches_certainty():
    """24 labelled cases cannot justify 1.0, and 1.0 invites unappealability."""
    for level in LEVELS:
        for a in every_analysis():
            assert decide(Verdict(level=level), a).confidence < 1.0


def test_explain_names_the_action_and_the_evidence():
    d = decide(Verdict(level=CRITICAL), FakeAnalysis(harm="severe"))
    line = explain(d)
    assert "BLOCKED" in line and "harm if followed = severe" in line


def test_model_ran_distinguishes_unassessed_from_assessed_clean():
    assert decide(Verdict(level=NONE), None).model_ran is False
    assert decide(Verdict(level=NONE), FakeAnalysis()).model_ran is True
    assert decide(Verdict(level=NONE), FakeAnalysis(ok=False)).model_ran is False


# --------------------------------------- honest dual-use tooling is disclosed


class Verdicts:
    """Verdicts carrying capabilities, which the dual-use path reads."""

    @staticmethod
    def offensive(level):
        return Verdict(level=level, capabilities=["declared_offensive_purpose"])


def test_declared_offensive_tooling_is_flagged_not_blocked():
    """`hunt-rce`: 'built from 67 public bug bounty reports', body matches."""
    d = decide(Verdicts.offensive(HIGH), FakeAnalysis(harm="severe"))
    assert d.action == FLAG
    assert "declared offensive-security purpose" in " ".join(d.reasons)


def test_the_dual_use_path_never_reaches_allow():
    """Demoted and disclosed — never silently cleared."""
    for harm in ("minor", "serious", "severe"):
        d = decide(Verdicts.offensive(HIGH), FakeAnalysis(harm=harm))
        assert d.action in (FLAG, BLOCK)


def test_an_unambiguous_marker_blocks_whatever_the_skill_claims():
    """A declared purpose is not a bypass: CRITICAL is untouched."""
    d = decide(Verdicts.offensive(CRITICAL), FakeAnalysis(harm="severe"))
    assert d.action == BLOCK


def test_a_declared_purpose_does_not_excuse_a_mismatch():
    """The exemption is for tools that do what they say."""
    d = decide(Verdicts.offensive(HIGH),
               FakeAnalysis(harm="severe", mismatch=True))
    assert d.action == BLOCK


def test_an_undeclared_skill_gets_no_exemption():
    """The same evidence without the declaration still blocks."""
    assert decide(Verdict(level=HIGH), FakeAnalysis(harm="severe")).action == BLOCK


# ------------------------------------------------ the mismatch coherence guard


def test_a_mismatch_with_no_harm_does_not_block():
    """`news-monitoring`: mismatch=True, harm=none, no explanation, 0.90."""
    d = decide(Verdict(level=MEDIUM), FakeAnalysis(mismatch=True, harm="none"))
    assert d.action == FLAG
    assert d.basis == "rule_medium_alone"


def test_a_mismatch_with_harm_still_blocks():
    """The two labelled attacks that triggered mismatch both said severe."""
    d = decide(Verdict(level=NONE), FakeAnalysis(mismatch=True, harm="severe"))
    assert d.action == BLOCK


def test_a_mismatch_with_minor_harm_is_enough():
    d = decide(Verdict(level=NONE), FakeAnalysis(mismatch=True, harm="minor"))
    assert d.action == BLOCK and d.basis == "model_mismatch_alone"
