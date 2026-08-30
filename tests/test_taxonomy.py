"""The subject taxonomy — the failures here are what shaped its design."""

import pytest

from skill_engine.taxonomy import (
    ALL_CATEGORIES,
    BY_ID,
    _compile,
    category_tree,
    classify,
)


def test_short_words_match_whole_only():
    """`auth` matching `authoring` filed a Neo4j connector under Security."""
    rx = _compile(("auth",))
    assert not rx.search("Cypher authoring guide")
    assert rx.search("configure auth for the API")


def test_long_stems_still_prefix_match():
    """Prefix matching is wanted for stems: refactor -> refactoring."""
    assert _compile(("refactor",)).search("refactoring legacy code")
    assert _compile(("visuali",)).search("data visualisation")


def test_name_outweighs_description():
    cat_by_name, _, _, _ = classify("pdf-extractor", "A general purpose helper.")
    assert cat_by_name == "documents"


def test_a_long_feature_list_cannot_outvote_the_subject():
    """Passing mentions stacked up and mis-filed skills; the cap stops that."""
    primary, _, _, _ = classify(
        "apollo-router",
        "Configure and run Apollo Router for federated GraphQL. Covers "
        "deployment, docker images, kubernetes helm charts, CI/CD pipelines, "
        "and securing the graph with JWT authorization directives.",
    )
    assert primary == "devops"


def test_unmatched_skills_are_surfaced_not_dropped():
    primary, sub, secondary, scores = classify("zzz", "qqq wwww")
    assert primary == "uncategorised"
    assert sub is None and secondary == [] and scores == {}


def test_secondary_categories_are_reported():
    _, _, secondary, _ = classify(
        "terraform-security-audit",
        "Audit terraform infrastructure for security misconfiguration and "
        "compliance against CIS benchmarks.",
    )
    assert secondary


def test_subcategory_is_chosen_within_the_primary():
    primary, sub, _, _ = classify(
        "ctf-reverse", "Reverse engineering challenges for CTF competitions."
    )
    assert primary == "security" and sub == "offensive"


def test_idf_stops_broad_categories_winning_on_pattern_count():
    """Raw counting put 69.7% of the corpus in one category."""
    idf = {"docker": 6.0, "kubernetes": 6.0, "agent": 0.4, "prompt": 0.4}
    primary, _, _, _ = classify(
        "deploy-helper",
        "Agent skill. Uses docker and kubernetes to deploy services.",
        idf=idf,
    )
    assert primary == "devops"


def test_every_category_is_reachable_and_well_formed():
    seen = set()
    for cat in ALL_CATEGORIES:
        assert cat.id and cat.label and cat.icon and cat.blurb
        assert cat.id not in seen
        seen.add(cat.id)
        sub_ids = [s.id for s in cat.subs]
        assert len(sub_ids) == len(set(sub_ids))


def test_category_tree_is_serialisable():
    import json

    tree = category_tree()
    assert json.loads(json.dumps(tree))
    assert {c["id"] for c in tree} == {c.id for c in ALL_CATEGORIES}


def test_classification_is_deterministic():
    args = ("sql-optimiser", "Optimise slow postgres queries and indexes.")
    assert classify(*args) == classify(*args)
