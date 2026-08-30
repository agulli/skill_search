"""Properties the ranking layer must hold, not just numbers it happens to produce."""

import json

import pytest

from skill_engine.ranking import (
    CorpusStats,
    Weights,
    blend,
    recency,
    recompute,
    score_repo,
)
from skill_engine.store import Store


def make_repo(store, name, **kw):
    meta = {
        "full_name": name,
        "owner_type": kw.get("owner_type", "User"),
        "default_branch": "main",
        "description": kw.get("description", "a collection of skills"),
        "homepage": kw.get("homepage"),
        "language": kw.get("language", "Python"),
        "stars": kw.get("stars", 10),
        # Default to the fork/star ratio actually observed in the corpus
        # (median ≈ 0.10), so fixtures do not accidentally look inorganic.
        "forks": kw.get("forks", max(1, kw.get("stars", 10) // 10)),
        "subscribers": kw.get("subscribers"),
        "open_issues": kw.get("open_issues", 0),
        "size_kb": kw.get("size_kb", 100),
        "license": kw.get("license", "MIT"),
        "topics": kw.get("topics", ["agent-skills"]),
        "created_at": kw.get("created_at", "2025-01-01T00:00:00Z"),
        "updated_at": "2026-08-01T00:00:00Z",
        "pushed_at": kw.get("pushed_at", "2026-08-20T00:00:00Z"),
        "is_fork": kw.get("is_fork", False),
        "archived": kw.get("archived", False),
        "disabled": kw.get("disabled", False),
        "is_template": kw.get("is_template", False),
        "has_issues": True, "has_wiki": False, "has_pages": False,
        "has_discussions": kw.get("has_discussions", False),
        "contributors": kw.get("contributors"),
        "releases": kw.get("releases"),
        "latest_release": kw.get("latest_release"),
    }
    store.upsert_repo(meta)
    if "skill_count" in kw:
        store.mark_repo(name, skill_count=kw["skill_count"])
    return meta


def make_skill(store, repo, path, **kw):
    store.upsert_skill({
        "repo": repo, "path": path,
        "name": kw.get("name", path.split("/")[-2]),
        "description": kw.get("description", "d" * 120),
        "body": kw.get("body", "body " * 200),
        "heading": "", "version": None, "license": "MIT",
        "allowed_tools": json.dumps(kw.get("tools", ["Read"])),
        "metadata": "{}",
        "resources": json.dumps(kw.get("resources", ["scripts/a.py"])),
        "source_kind": "skills-dir", "blob_sha": "x",
        "content_hash": kw.get("content_hash", path),
        "body_len": kw.get("body_len", 1000),
        "score": 0, "valid": kw.get("valid", 1),
        "invalid_reason": "", "warnings": kw.get("warnings", ""),
    })


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "r.db")
    # A spread of repositories so percentiles have a distribution to work with.
    for i in range(40):
        make_repo(st, f"user{i}/repo{i}", stars=i * 37, forks=i * 3,
                  open_issues=i, subscribers=i * 2)
        make_skill(st, f"user{i}/repo{i}", f"skills/s{i}/SKILL.md")
        st.mark_repo(f"user{i}/repo{i}", skill_count=1)
    st.commit()
    yield st
    st.close()


# ---------------------------------------------------------------- primitives


def test_recency_decays_by_halflife():
    assert recency(0, 120) == 1.0
    assert recency(120, 120) == pytest.approx(0.5)
    assert recency(240, 120) == pytest.approx(0.25)
    assert recency(None, 120) is None  # missing stays missing, never 0


def test_blend_redistributes_weight_of_missing_signals():
    """A repo must not be punished for metadata we simply did not fetch."""
    both, _ = blend([("a", 1.0, 0.5), ("b", 1.0, 0.5)])
    one_missing, detail = blend([("a", 1.0, 0.5), ("b", None, 0.5)])
    assert both == one_missing == 1.0
    assert detail["b"] is None


def test_blend_returns_neutral_when_nothing_is_known():
    value, _ = blend([("a", None, 1.0)])
    assert value == 0.5  # neutral, not zero


def test_blend_is_bounded():
    value, _ = blend([("a", 5.0, 1.0), ("b", -3.0, 1.0)])
    assert 0.0 <= value <= 1.0


# --------------------------------------------------------------- percentiles


def test_percentile_is_corpus_relative(store):
    stats = CorpusStats.compute(store)
    assert stats.pct("stars", 0) < 0.15
    assert stats.pct("stars", 37 * 39) > 0.9
    assert stats.pct("stars", 37 * 20) == pytest.approx(0.5, abs=0.15)


def test_percentile_handles_mass_at_zero(store):
    """Half the corpus having zero stars must not collapse to a single rank."""
    for i in range(40):
        make_repo(store, f"zero{i}/r", stars=0)
    store.commit()
    stats = CorpusStats.compute(store)
    # Mid-rank puts the zero-star cohort at the middle of its own band, not at 0.
    assert 0.0 < stats.pct("stars", 0) < 0.5
    assert stats.pct("stars", 1000) > stats.pct("stars", 0)


def test_percentile_returns_none_for_unknown_metric(store):
    stats = CorpusStats.compute(store)
    assert stats.pct("stars", None) is None
    assert stats.pct("no_such_metric", 5) is None


def test_stats_round_trip_through_the_database(store):
    stats = CorpusStats.compute(store)
    stats.save(store)
    loaded = CorpusStats.load(store)
    assert loaded.quantiles.keys() == stats.quantiles.keys()
    assert loaded.pct("stars", 500) == stats.pct("stars", 500)


# ------------------------------------------------------------ repo behaviour


def test_more_stars_scores_higher_all_else_equal(store):
    stats = CorpusStats.compute(store)
    lo = store.get_repo("user2/repo2")
    hi = store.get_repo("user39/repo39")
    assert score_repo(hi, stats)[0] > score_repo(lo, stats)[0]


def test_popularity_cannot_dominate_everything(store):
    """A 100k-star archived fork must not outrank a healthy mid-size repo."""
    stats = CorpusStats.compute(store)
    make_repo(store, "big/fork", stars=100_000, forks=9000, is_fork=True,
              archived=True, license=None)
    make_repo(store, "good/repo", stars=400, forks=40, owner_type="Organization",
              contributors=25, homepage="https://x.dev",
              latest_release="2026-08-01T00:00:00Z", releases=12)
    store.commit()
    stats = CorpusStats.compute(store)
    assert (score_repo(store.get_repo("good/repo"), stats)[0]
            > score_repo(store.get_repo("big/fork"), stats)[0])


def test_penalties_are_multiplicative_and_reported(store):
    stats = CorpusStats.compute(store)
    make_repo(store, "a/clean", stars=500)
    make_repo(store, "a/archived", stars=500, archived=True)
    store.commit()
    stats = CorpusStats.compute(store)
    clean, clean_d = score_repo(store.get_repo("a/clean"), stats)
    arch, arch_d = score_repo(store.get_repo("a/archived"), stats)
    assert arch < clean
    assert arch_d["penalties"]["archived"] == Weights().archived_factor
    assert clean_d["penalties"] == {}


def test_stale_repo_loses_to_fresh_one_at_equal_popularity(store):
    make_repo(store, "s/stale", stars=500, pushed_at="2024-01-01T00:00:00Z")
    make_repo(store, "s/fresh", stars=500, pushed_at="2026-08-25T00:00:00Z")
    store.commit()
    stats = CorpusStats.compute(store)
    assert (score_repo(store.get_repo("s/fresh"), stats)[0]
            > score_repo(store.get_repo("s/stale"), stats)[0])


def test_score_is_bounded_to_0_100(store):
    stats = CorpusStats.compute(store)
    make_repo(store, "max/everything", stars=10**7, forks=10**6, subscribers=10**5,
              contributors=5000, releases=900, owner_type="Organization",
              homepage="https://x", latest_release="2026-08-28T00:00:00Z",
              topics=["a", "b", "c", "d", "e", "f"])
    store.commit()
    stats = CorpusStats.compute(store)
    for full_name in ("max/everything", "user0/repo0"):
        score, _ = score_repo(store.get_repo(full_name), stats)
        assert 0.0 <= score <= 100.0


def test_missing_metadata_does_not_zero_the_score(store):
    """A bare repo row must still score sensibly rather than bottoming out."""
    store.db.execute(
        "INSERT INTO repos(full_name, owner, name, stars) VALUES('x/y','x','y',300)"
    )
    store.commit()
    stats = CorpusStats.compute(store)
    score, detail = score_repo(store.get_repo("x/y"), stats)
    assert score > 0
    assert detail["families"]["momentum"] is not None


# ----------------------------------------------------------- skill behaviour


def test_duplicates_are_demoted(store):
    make_repo(store, "orig/repo", stars=900)
    make_repo(store, "copy/repo", stars=900)
    for repo in ("orig/repo", "copy/repo"):
        make_skill(store, repo, "skills/dup/SKILL.md", content_hash="SAME")
        store.mark_repo(repo, skill_count=1)
    make_skill(store, "orig/repo", "skills/uniq/SKILL.md", content_hash="UNIQUE")
    store.commit()
    recompute(store)

    dup = store.db.execute(
        "SELECT score, dup_count FROM skills WHERE content_hash='SAME' LIMIT 1"
    ).fetchone()
    uniq = store.db.execute(
        "SELECT score, dup_count FROM skills WHERE content_hash='UNIQUE'"
    ).fetchone()
    assert dup["dup_count"] == 2 and uniq["dup_count"] == 1
    assert uniq["score"] > dup["score"]


def test_aggregator_dump_is_penalised(store):
    make_repo(store, "dump/everything", stars=900, skill_count=6000)
    make_repo(store, "curated/few", stars=900, skill_count=12)
    make_skill(store, "dump/everything", "skills/a/SKILL.md", content_hash="A")
    make_skill(store, "curated/few", "skills/b/SKILL.md", content_hash="B")
    store.commit()
    recompute(store)
    dump = store.db.execute(
        "SELECT score FROM skills WHERE repo='dump/everything'"
    ).fetchone()["score"]
    good = store.db.execute(
        "SELECT score FROM skills WHERE repo='curated/few'"
    ).fetchone()["score"]
    assert good > dump


def test_craft_signals_separate_thin_from_rich_skills(store):
    make_repo(store, "same/repo", stars=500)
    make_skill(store, "same/repo", "skills/rich/SKILL.md", body_len=4000,
               resources=["scripts/a.py", "references/b.md", "assets/c.png"],
               description="d" * 200, content_hash="RICH")
    make_skill(store, "same/repo", "skills/thin/SKILL.md", body_len=60,
               resources=[], tools=[], description="short",
               warnings="name is not a lowercase-hyphen slug", content_hash="THIN")
    store.mark_repo("same/repo", skill_count=2)
    store.commit()
    recompute(store)
    rich = store.db.execute(
        "SELECT score FROM skills WHERE name='rich'"
    ).fetchone()["score"]
    thin = store.db.execute(
        "SELECT score FROM skills WHERE name='thin'"
    ).fetchone()["score"]
    assert rich > thin


def test_recompute_is_idempotent(store):
    first = recompute(store)
    scores1 = [r["score"] for r in store.db.execute(
        "SELECT score FROM skills ORDER BY id")]
    second = recompute(store)
    scores2 = [r["score"] for r in store.db.execute(
        "SELECT score FROM skills ORDER BY id")]
    assert first == second
    assert scores1 == scores2


def test_explanation_is_stored_and_complete(store):
    recompute(store)
    detail = json.loads(store.db.execute(
        "SELECT score_detail FROM skills LIMIT 1"
    ).fetchone()["score_detail"])
    assert {"score", "base", "trust", "families", "craft",
            "distinctiveness", "repo"} <= set(detail)
    # The arithmetic in the breakdown must actually reproduce the score.
    assert detail["score"] == pytest.approx(
        100 * detail["base"] * detail["trust"], abs=0.01
    )


def test_weights_are_configurable(store):
    make_repo(store, "pop/repo", stars=100_000)
    make_skill(store, "pop/repo", "skills/x/SKILL.md", body_len=50, resources=[])
    store.mark_repo("pop/repo", skill_count=1)
    store.commit()

    recompute(store, Weights(popularity=0.6, craft=0.05))
    popular_weighting = store.db.execute(
        "SELECT score FROM skills WHERE repo='pop/repo'").fetchone()["score"]
    recompute(store, Weights(popularity=0.05, craft=0.6))
    craft_weighting = store.db.execute(
        "SELECT score FROM skills WHERE repo='pop/repo'").fetchone()["score"]
    # A thin skill in a hugely popular repo should fare worse once craft rules.
    assert popular_weighting > craft_weighting


def test_inorganic_popularity_is_penalised(store):
    """Stars are cheap to manufacture; forks and contributors are not."""
    stats = CorpusStats.compute(store)
    # 40k stars but almost nobody forked it: promoted, not used.
    make_repo(store, "farm/starred", stars=40_000, forks=12, contributors=1)
    # Same stars, healthy fork ratio and a real contributor base.
    make_repo(store, "real/popular", stars=40_000, forks=3_400, contributors=48)
    store.commit()
    stats = CorpusStats.compute(store)

    farmed, farmed_d = score_repo(store.get_repo("farm/starred"), stats)
    real, real_d = score_repo(store.get_repo("real/popular"), stats)
    assert "inorganic_popularity" in farmed_d["penalties"]
    assert "inorganic_popularity" not in real_d["penalties"]
    assert real > farmed


def test_anomaly_guard_never_fires_on_missing_data(store):
    """An un-enriched repo must not be judged by data we never fetched."""
    stats = CorpusStats.compute(store)
    make_repo(store, "unknown/repo", stars=5_000, forks=400, contributors=None)
    store.commit()
    stats = CorpusStats.compute(store)
    _, detail = score_repo(store.get_repo("unknown/repo"), stats)
    assert "inorganic_popularity" not in detail["penalties"]


def test_small_repos_are_exempt_from_the_anomaly_guard(store):
    """A 30-star repo with 0 forks is normal, not suspicious."""
    stats = CorpusStats.compute(store)
    make_repo(store, "tiny/repo", stars=30, forks=0, contributors=1)
    store.commit()
    stats = CorpusStats.compute(store)
    _, detail = score_repo(store.get_repo("tiny/repo"), stats)
    assert "inorganic_popularity" not in detail["penalties"]


def test_crawl_queue_prefers_high_scoring_repos(store):
    """Scarce quota must be spent on the best-ranked repos first."""
    for name, score, prio in (("low/score", 10.0, 200), ("high/score", 95.0, 100)):
        make_repo(store, name, stars=100)
        store.db.execute("UPDATE repos SET repo_score = ?, last_crawled = NULL, "
                         "tree_sha = NULL WHERE full_name = ?", (score, name))
        store.enqueue(name, "test", prio)
    store.commit()

    by_score = [r["full_name"] for r in store.take(2, strategy="score")]
    assert by_score[0] == "high/score"   # score wins despite lower priority

    by_fifo = [r["full_name"] for r in store.take(2, strategy="fifo")]
    assert by_fifo[0] == "low/score"     # fifo still honours discovery priority


def test_unranked_repos_fall_back_to_discovery_priority(store):
    """Before `rank` has ever run, ordering must not collapse to arbitrary."""
    for name, prio in (("a/unranked", 90), ("b/unranked", 150)):
        store.db.execute(
            "INSERT INTO repos(full_name, owner, name, repo_score) VALUES(?,?,?,0)",
            (name, name.split("/")[0], name.split("/")[1]))
        store.enqueue(name, "test", prio)
    store.commit()
    picked = [r["full_name"] for r in store.take(2, strategy="score")]
    assert picked[0] == "b/unranked"


# ------------------------------------------------------------------- authors


def _publish(store, owner, n, *, dup=1, body_len=1500, stars=50):
    make_repo(store, f"{owner}/skills", stars=stars, skill_count=n)
    for i in range(n):
        make_skill(store, f"{owner}/skills", f"skills/{owner}{i}/SKILL.md",
                   name=f"{owner}-s{i}", body_len=body_len,
                   content_hash=f"shared{i}" if dup > 1 else f"{owner}-uniq{i}")
    store.commit()


def test_originality_separates_authors_who_copy(store):
    """188 vendored skills must not outrank 119 original ones."""
    from skill_engine.authors import get_author

    _publish(store, "original", 12, dup=1)
    _publish(store, "copier", 12, dup=2)
    # A third party holding the same files is what makes the copies duplicates.
    _publish(store, "elsewhere", 12, dup=2)
    recompute(store)

    a = get_author(store, "original")
    b = get_author(store, "copier")
    assert a["original_skills"] == 12
    assert b["original_skills"] == 0
    assert a["author_score"] > b["author_score"]


def test_author_score_is_not_circular(store):
    """Author standing must derive from craft, never from the skill score."""
    import inspect
    from skill_engine import authors as mod
    src = inspect.getsource(mod.build_profiles)
    assert "craft_score" in src
    assert "score_skill" not in src


def test_author_standing_feeds_the_skill_score(store):
    _publish(store, "strong", 10, body_len=4000, stars=900)
    recompute(store)
    detail = json.loads(store.db.execute(
        "SELECT score_detail FROM skills WHERE repo='strong/skills' LIMIT 1"
    ).fetchone()["score_detail"])
    assert "author_standing" in detail["families"]
    assert detail["families"]["author_standing"] is not None
    assert detail["author"]["score"] > 0


def test_missing_author_does_not_zero_the_skill(store):
    """A skill whose author was never profiled keeps a sensible score."""
    from skill_engine.ranking import CorpusStats, score_skill
    make_repo(store, "ghost/repo", stars=100, skill_count=1)
    make_skill(store, "ghost/repo", "skills/x/SKILL.md")
    store.commit()
    stats = CorpusStats.compute(store)
    row = store.db.execute(
        "SELECT * FROM skills WHERE repo='ghost/repo'").fetchone()
    repo = store.get_repo("ghost/repo")
    with_author, _ = score_skill(row, repo, stats, author_score=80.0)
    without, detail = score_skill(row, repo, stats, author_score=None)
    assert without > 0
    assert detail["families"]["author_standing"] is None   # weight redistributed


def test_quantity_alone_does_not_buy_a_high_author_score(store):
    """Bulk publishing is damped; craft and originality carry the weight."""
    from skill_engine.authors import get_author
    _publish(store, "prolific", 60, dup=2, body_len=200)
    _publish(store, "elsewhere2", 60, dup=2, body_len=200)
    _publish(store, "careful", 6, dup=1, body_len=6000)
    recompute(store)
    assert (get_author(store, "careful")["author_score"]
            > get_author(store, "prolific")["author_score"])
