import hashlib
import json

import pytest

from skill_engine.parse import classify_path
from skill_engine.search import search, to_fts_query
from skill_engine.store import Store

PDF_DESC = "Extract text and tables from PDF documents and fill form fields."

SKILLS = [
    ("acme/docs", "skills/pdf-processing/SKILL.md", "pdf-processing", PDF_DESC, 88.0),
    ("acme/docs", "skills/xlsx-analysis/SKILL.md", "xlsx-analysis",
     "Read spreadsheets, compute summary statistics and render charts.", 84.0),
    # A fork holding a byte-identical copy: same content hash as the original.
    ("nobody/fork", "SKILL.md", "pdf-processing", PDF_DESC, 20.0),
    # …and one skill that exists only in the fork.
    ("nobody/fork", "skills/legacy-migrate/SKILL.md", "legacy-migrate",
     "Migrate legacy Fortran punchcard archives into a modern warehouse.", 18.0),
    ("team/infra", ".claude/skills/deploy/SKILL.md", "deploy-service",
     "Deploy a service to Kubernetes with health checks and rollback.", 55.0),
]


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "t.db")
    for repo, stars, fork in [("acme/docs", 900, 0), ("nobody/fork", 0, 1),
                              ("team/infra", 40, 0)]:
        st.upsert_repo({
            "full_name": repo, "default_branch": "main", "description": "",
            "stars": stars, "forks": 0, "license": "MIT", "topics": [],
            "pushed_at": "2026-08-01T00:00:00Z", "is_fork": bool(fork),
            "archived": False,
        })
    for repo, path, name, desc, score in SKILLS:
        body = desc + " " + "detailed procedure " * 20
        st.upsert_skill({
            "repo": repo, "path": path, "name": name, "description": desc,
            "body": body, "heading": name,
            "version": None, "license": "MIT", "allowed_tools": "[]",
            "metadata": "{}", "resources": json.dumps(["scripts/run.py"]),
            "source_kind": classify_path(path), "blob_sha": "abc",
            # Content-addressed, as the real parser does: identical files
            # anywhere on GitHub collapse to the same hash.
            "content_hash": hashlib.sha256(body.encode()).hexdigest(),
            "body_len": 500, "score": score, "valid": 1, "invalid_reason": "",
        })
    st.commit()
    yield st
    st.close()


def test_fts_query_escapes_hostile_input():
    # Unbalanced quotes and operators would otherwise raise inside FTS5.
    assert to_fts_query('pdf" OR (x')
    assert to_fts_query("   ") == ""
    assert to_fts_query('"""') == ""


def test_keyword_search_finds_by_description(store):
    hits = search(store, "extract tables from pdf")
    assert hits
    assert hits[0].name == "pdf-processing"
    assert hits[0].repo == "acme/docs"


def test_forks_excluded_by_default(store):
    assert not search(store, "fortran punchcard archives")
    assert search(store, "fortran punchcard archives", filters={"include_forks": True})


def test_identical_copies_collapse_to_the_best_one(store):
    """A zero-star fork must never outrank the repo it copied from."""
    hits = search(store, "pdf documents tables", filters={"include_forks": True})
    pdf = [h for h in hits if h.name == "pdf-processing"]
    assert len(pdf) == 1                 # the fork was folded in, not ranked
    assert pdf[0].repo == "acme/docs"    # and the original survived
    assert pdf[0].duplicates == 1        # with the copy accounted for


def test_distinct_skills_are_not_collapsed(store):
    hits = search(store, "pdf spreadsheets charts documents")
    assert {h.name for h in hits} >= {"pdf-processing", "xlsx-analysis"}
    assert all(h.duplicates == 0 for h in hits)


def test_min_stars_filter(store):
    assert not search(store, "deploy kubernetes", filters={"min_stars": 500})
    assert search(store, "deploy kubernetes", filters={"min_stars": 10})


def test_kind_filter(store):
    hits = search(store, "deploy", filters={"kind": "claude-project"})
    assert len(hits) == 1 and hits[0].name == "deploy-service"


def test_empty_query_returns_nothing(store):
    assert search(store, "") == []


def test_hit_serialises_with_a_github_url(store):
    payload = search(store, "spreadsheets charts")[0].to_dict()
    assert payload["url"].startswith("https://github.com/acme/docs/blob/")
    assert payload["resources"] == ["scripts/run.py"]


def test_hybrid_search_with_hashing_embedder(store):
    from skill_engine.embed import HashingEmbedder, embed_text, pack

    emb = HashingEmbedder()
    rows = store.db.execute("SELECT id, name, description, body, repo FROM skills").fetchall()
    vectors = emb.encode(
        [embed_text(r["name"], r["description"], r["body"], r["repo"]) for r in rows]
    )
    store.db.executemany(
        "INSERT INTO vectors(skill_id, model, dim, vec) VALUES(?,?,?,?)",
        [(r["id"], emb.model, len(v), pack(v)) for r, v in zip(rows, vectors)],
    )
    store.commit()

    hits = search(store, "charts from spreadsheets", embedder_name="hashing")
    assert hits[0].name == "xlsx-analysis"
    assert "vector" in hits[0].matched_by


def test_fts_index_tracks_updates(store):
    store.upsert_skill({
        "repo": "team/infra", "path": ".claude/skills/deploy/SKILL.md",
        "name": "deploy-service",
        "description": "Now about provisioning Terraform stacks instead.",
        "body": "terraform provisioning " * 30, "heading": "", "version": None,
        "license": "MIT", "allowed_tools": "[]", "metadata": "{}", "resources": "[]",
        "source_kind": "claude-project", "blob_sha": "def", "content_hash": "z",
        "body_len": 500, "score": 55.0, "valid": 1, "invalid_reason": "",
    })
    store.commit()
    assert search(store, "terraform provisioning")
    assert not search(store, "kubernetes rollback health")


def _add_pdf_variants(store, repo, n, score):
    for i in range(n):
        store.upsert_skill({
            "repo": repo, "path": f"skills/pdf-{i}/SKILL.md",
            "name": f"pdf-variant-{i}",
            "description": f"Extract tables from PDF documents, variant {i}.",
            "body": "pdf tables extraction " * 40, "heading": "", "version": None,
            "license": "MIT", "allowed_tools": "[]", "metadata": "{}",
            "resources": "[]", "source_kind": "skills-dir", "blob_sha": "x",
            "content_hash": f"{repo}-variant{i}", "body_len": 800, "score": score,
            "valid": 1, "invalid_reason": "",
        })
    store.commit()


def test_diversity_cap_surfaces_a_second_repo(store):
    """A prolific repo must not own every top slot when alternatives exist."""
    _add_pdf_variants(store, "acme/docs", 6, score=90.0)
    _add_pdf_variants(store, "team/infra", 3, score=70.0)

    uncapped = search(store, "pdf documents tables", limit=3, max_per_repo=None)
    capped = search(store, "pdf documents tables", limit=3, max_per_repo=2)

    assert all(h.repo == "acme/docs" for h in uncapped)   # monopoly
    assert any(h.repo == "team/infra" for h in capped)    # broken up
    assert sum(1 for h in capped if h.repo == "acme/docs") == 2


def test_diversity_cap_never_hides_the_only_answers(store):
    """If one repo holds every match, demoted hits must still be returned."""
    _add_pdf_variants(store, "acme/docs", 6, score=90.0)
    hits = search(store, "pdf documents tables", limit=5, max_per_repo=2)
    assert len(hits) == 5  # nothing withheld just to satisfy the cap


def test_diversify_demotes_rather_than_drops():
    from skill_engine.search import Hit, diversify

    hits = [Hit(i, "a/b" if i < 4 else "c/d", "p", f"n{i}", "", "k", None, 0)
            for i in range(6)]
    out = diversify(hits, max_per_repo=2)
    assert len(out) == len(hits)                   # nothing lost
    assert [h.repo for h in out[:3]].count("c/d") >= 1  # other repo surfaces


def test_count_matches_is_unaffected_by_the_display_limit(store):
    from skill_engine.search import count_matches
    _add_pdf_variants(store, "acme/docs", 8, score=80.0)
    shown = search(store, "pdf documents tables", limit=3)
    total = count_matches(store, "pdf documents tables")
    assert len(shown) == 3
    assert total > 3


def test_count_matches_respects_filters(store):
    from skill_engine.search import count_matches
    assert count_matches(store, "pdf") >= 1
    assert count_matches(store, "pdf", {"min_stars": 10_000}) == 0
    assert count_matches(store, "") == 0


def test_facets_tally_the_matched_set(store):
    from skill_engine.search import facet_counts
    f = facet_counts(store, "pdf spreadsheets deploy documents")
    assert set(f) == {"kind", "license", "language"}
    kinds = dict(f["kind"])
    assert kinds.get("skills-dir", 0) >= 1
    assert all(c > 0 for _, c in f["license"])


def test_facets_are_empty_for_an_empty_query(store):
    from skill_engine.search import facet_counts
    assert facet_counts(store, "") == {}


def test_language_and_age_filters(store):
    store.db.execute("UPDATE repos SET language='Python' WHERE full_name='acme/docs'")
    store.db.execute("UPDATE repos SET pushed_at='2020-01-01T00:00:00Z' "
                     "WHERE full_name='team/infra'")
    store.commit()
    assert search(store, "pdf tables", filters={"language": "Python"})
    assert not search(store, "pdf tables", filters={"language": "Rust"})
    assert not search(store, "deploy kubernetes", filters={"max_age_days": 30})


def test_stopwords_are_dropped_from_the_match_expression():
    q = to_fts_query("extract tables from a pdf")
    assert "extract" in q and "tables" in q and "pdf" in q
    assert '"a"' not in q and '"from"' not in q


def test_a_query_of_only_stopwords_still_searches():
    """Dropping everything would silently return nothing for 'how to use'."""
    assert to_fts_query("how to use") != ""


def test_stopword_removal_preserves_relevance(store):
    with_stops = search(store, "extract the tables from a pdf")
    without = search(store, "extract tables pdf")
    assert with_stops and with_stops[0].name == without[0].name
