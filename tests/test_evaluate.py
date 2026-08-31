"""The evaluation harness, and the defect it found."""

import pytest

from skill_engine.evaluate import (
    REAL_QUERIES,
    build_query,
    coverage,
    known_item,
    latency,
    robustness,
)
from skill_engine.search import STOPWORDS, to_fts_query
from tests.test_search import store  # noqa: F401  (fixture)


def test_conversational_filler_is_stripped():
    """`please` reordered a third of all results before this.

    It is query-side noise, not a corpus-common word, so IDF cannot catch it —
    being rare in skill descriptions makes the ranker treat it as highly
    discriminating, which is exactly backwards.
    """
    plain = to_fts_query("extract tables from a pdf")
    polite = to_fts_query("hi please could someone help extract tables from a pdf")
    assert "please" not in polite and "someone" not in polite
    assert set(plain.split(" OR ")) <= set(polite.split(" OR "))


def test_real_subject_words_are_not_mistaken_for_filler():
    """`best practices` and `simple` are genuine subject matter, not noise."""
    for word in ("best", "simple", "easy", "quick", "good", "help"):
        assert word not in STOPWORDS


def test_query_builder_uses_mid_frequency_terms_only():
    """Rare terms make the benchmark trivial, common ones make it impossible."""
    df = {"kubernetes": 500, "the": 50_000, "zzzrare": 2, "helm": 300}
    q = build_query("the kubernetes zzzrare helm deployment", df, n_terms=2)
    assert q == "kubernetes helm"          # both extremes excluded
    assert build_query("the zzzrare", df, n_terms=2) is None


def test_known_item_finds_a_skill_from_its_description(store):  # noqa: F811
    # The fixture's descriptions are short, and its vocabulary tiny, so the
    # frequency band has to be widened for a 5-document corpus.
    result = known_item(store, n=3, n_terms=2, limit=10, min_desc=20)
    assert result.name.startswith("known-item")
    assert "MRR" in result.detail and "recall@1" in result.detail
    assert 0.0 <= result.detail["MRR"] <= 1.0


def test_coverage_reports_empty_and_thin_queries(store):  # noqa: F811
    result = coverage(store, ["pdf tables", "zzzz nonexistent qqqq"])
    assert result.detail["empty_rate"] > 0     # the nonsense query found nothing


def test_robustness_is_perfect_under_reordering(store):  # noqa: F811
    result = robustness(store, ["extract tables from pdf documents"])
    # OR semantics make word order irrelevant; a regression here would mean
    # query construction had become order-dependent.
    assert result.detail["reordered"] == 1.0


def test_latency_reports_percentiles(store):  # noqa: F811
    result = latency(store, REAL_QUERIES[:3])
    assert result.detail["p50_ms"] <= result.detail["p99_ms"]
