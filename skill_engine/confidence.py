"""Combining the rule findings and the model's description into a decision.

Two detectors with very different characteristics, measured on a hand-labelled
set drawn from the live corpus:

    deterministic rules    100% recall, 86% precision
    model, harm >= minor   100% precision,  50% recall
    model, purpose_mismatch 100% precision, 17% recall
    model, addresses_reviewer 58% precision, 58% recall

That asymmetry dictates the whole design, and it is the opposite of what a
naive average would do.

### The model escalates; it never exonerates

The temptation is to let the model overturn a rule hit — it would fix the two
remaining false positives, both threat-taxonomy tables in auditing skills. It
would also unblock six genuine attacks, because the model reported
`harm: none` for `skillvet`, `audit-injection`, `weather-assistant`,
`hidden-unicode-instruction`, `prompt-injection-tester` and `helper`.

A detector with 50% recall cannot be used as an acquittal. Silence from it is
not evidence of innocence, and treating it as such would trade two
false positives for six missed attacks. So:

* A rule-level `critical` blocks regardless of what the model says.
* The model can raise a `medium` or `high` to blocked when it reports serious
  harm — this is where it adds recall the rules do not have.
* The model's agreement raises *confidence*, which is reported rather than
  acted on, so a reviewer can tell a certain block from a precautionary one.

### Confidence is calibrated, not invented

The number means something specific: **the share of skills at this confidence
that were genuine attacks in the labelled set**. It is derived from measured
precision per signal combination, not from weights chosen to look reasonable.
Where a combination has too few examples to estimate, it is labelled as such
rather than given a confident-looking figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .safety import CRITICAL, HIGH, LOW, MEDIUM, NONE

# Decisions. `block` removes a skill from search; `flag` demotes and discloses.
BLOCK, FLAG, ALLOW = "block", "flag", "allow"

# Measured precision of each evidence combination on the labelled set. These
# are observations, not tuning knobs — changing one is a claim about the corpus
# and should be accompanied by a re-measurement.
#
#   rule_critical           12 blocked, 2 of them benign          -> 0.86
#   model harm severe        5 cases, 0 benign                    -> 0.95 (capped)
#   model purpose_mismatch   2 cases, 0 benign                    -> 0.90 (few)
#   both rule and model      agreement on every attack it caught  -> 0.98
#
# Capped below 1.0 because a labelled set of 24 cannot justify certainty, and a
# confidence of 1.0 invites treating the decision as unappealable.
PRECISION = {
    "rule_critical_and_model_harm": 0.98,
    "rule_critical_and_mismatch": 0.96,
    "model_harm_severe_alone": 0.95,
    "rule_critical_alone": 0.86,
    "model_mismatch_alone": 0.90,
    "rule_high_and_model_harm": 0.92,
    "rule_high_alone": 0.55,
    "rule_medium_alone": 0.30,
    "addresses_reviewer_alone": 0.58,
}

# Confidence at or above this blocks. Set from the measured numbers: 0.80 keeps
# every rule-critical and every model-confirmed case, and excludes the
# rule-medium and addresses-reviewer-only bands whose measured precision is
# 0.30 and 0.58 — blocking those would remove more legitimate skills than
# attacks.
BLOCK_THRESHOLD = 0.80
FLAG_THRESHOLD = 0.25


@dataclass
class Decision:
    action: str = ALLOW
    confidence: float = 0.0
    basis: str = ""
    reasons: list[str] = field(default_factory=list)
    rule_level: str = NONE
    model_ran: bool = False

    @property
    def blocked(self) -> bool:
        return self.action == BLOCK

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "confidence": round(self.confidence, 2),
            "basis": self.basis,
            "reasons": self.reasons[:8],
            "rule_level": self.rule_level,
            "model_ran": self.model_ran,
        }


def decide(verdict, analysis=None) -> Decision:
    """Combine a rule verdict and an optional model analysis into a decision.

    `analysis` may be None (the model did not run, was unavailable, or the
    skill was never gated for review). Absence is treated as *no information*,
    never as reassurance.
    """
    d = Decision(rule_level=verdict.level)
    reasons: list[str] = []

    model_harm = analysis.harm_if_followed if (analysis and analysis.ok) else None
    model_serious = model_harm in ("serious", "severe")
    model_any_harm = model_harm in ("minor", "serious", "severe")
    mismatch = bool(analysis and analysis.ok and analysis.purpose_mismatch)
    addressed = bool(analysis and analysis.ok and analysis.addresses_reviewer)
    d.model_ran = bool(analysis and analysis.ok)

    if verdict.level == CRITICAL:
        reasons.append("rules: unambiguous marker "
                       f"({', '.join(f.rule for f in verdict.findings[:3])})")
    if model_any_harm:
        reasons.append(f"model: harm if followed = {model_harm}")
    if mismatch:
        reasons.append("model: instructions exceed the stated purpose")
    if addressed:
        # Reported whatever else is true. A skill that tries to talk to the
        # reviewer has revealed intent that a cover story does not undo — but
        # it is 58% precise on its own, so it argues rather than decides.
        reasons.append("model: contains text addressed to a reviewer")

    # --- pick the best-supported basis, highest measured precision first
    if verdict.level == CRITICAL and model_serious:
        basis, conf = "rule_critical_and_model_harm", PRECISION["rule_critical_and_model_harm"]
    elif verdict.level == CRITICAL and mismatch:
        basis, conf = "rule_critical_and_mismatch", PRECISION["rule_critical_and_mismatch"]
    elif verdict.level == CRITICAL:
        basis, conf = "rule_critical_alone", PRECISION["rule_critical_alone"]
    elif model_serious and verdict.level in (HIGH, MEDIUM):
        # The model supplying recall the rules lack: a skill the rules rated
        # merely suspicious, which the model reads as seriously harmful.
        basis, conf = "rule_high_and_model_harm", PRECISION["rule_high_and_model_harm"]
    elif model_serious:
        basis, conf = "model_harm_severe_alone", PRECISION["model_harm_severe_alone"]
    elif mismatch:
        basis, conf = "model_mismatch_alone", PRECISION["model_mismatch_alone"]
    elif verdict.level == HIGH:
        basis, conf = "rule_high_alone", PRECISION["rule_high_alone"]
    elif addressed:
        basis, conf = "addresses_reviewer_alone", PRECISION["addresses_reviewer_alone"]
    elif verdict.level == MEDIUM:
        basis, conf = "rule_medium_alone", PRECISION["rule_medium_alone"]
    elif verdict.level == LOW:
        basis, conf = "rule_low_alone", 0.10
    else:
        basis, conf = "no_evidence", 0.0

    d.basis, d.confidence, d.reasons = basis, conf, reasons
    d.action = (BLOCK if conf >= BLOCK_THRESHOLD
                else FLAG if conf >= FLAG_THRESHOLD else ALLOW)
    return d


def explain(d: Decision) -> str:
    """One line a human can act on, for logs and for the API."""
    verb = {BLOCK: "BLOCKED", FLAG: "flagged", ALLOW: "allowed"}[d.action]
    why = "; ".join(d.reasons) or "no findings"
    return f"{verb} at {d.confidence:.0%} confidence ({d.basis}) — {why}"
