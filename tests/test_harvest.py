"""End-to-end harvest against a mock GitHub, asserting the cost model holds."""

import json

import httpx
import pytest

from skill_engine.config import Config
from skill_engine.github import GitHubClient
from skill_engine.harvest import harvest_repo
from skill_engine.store import Store
from tests.test_github import attach, rl_headers

SKILL_A = """---
name: pdf-processing
description: Extract text and tables from PDF documents, and fill in form fields.
---

# PDF Processing

Use `scripts/extract.py` to pull structured text out of a PDF document.
"""

SKILL_B = """---
name: chart-builder
description: Render charts from tabular data using a consistent visual style.
---

# Chart Builder

Produces SVG charts from CSV input with sensible default styling applied.
"""


class FakeGitHub:
    """A repository whose tree and file contents we can mutate between crawls."""

    def __init__(self):
        self.tree = {
            "sha": "tree1",
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob", "sha": "r1", "size": 10},
                {"path": "skills/pdf-processing/SKILL.md", "type": "blob",
                 "sha": "sha-a1", "size": len(SKILL_A)},
                {"path": "skills/chart-builder/SKILL.md", "type": "blob",
                 "sha": "sha-b1", "size": len(SKILL_B)},
            ],
        }
        self.files = {
            "skills/pdf-processing/SKILL.md": SKILL_A,
            "skills/chart-builder/SKILL.md": SKILL_B,
        }
        self.raw_fetches: list[str] = []
        self.tree_etag = "tree-v1"
        self.sent_etags: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if url.startswith("https://raw.githubusercontent.com/"):
            path = url.split("/main/", 1)[1]
            self.raw_fetches.append(path)
            if path in self.files:
                return httpx.Response(200, text=self.files[path])
            return httpx.Response(404, text="")

        if "/git/trees/" in url:
            inm = request.headers.get("if-none-match")
            self.sent_etags.append(inm)
            if inm == self.tree_etag:
                return httpx.Response(304, headers=rl_headers(4990))
            return httpx.Response(
                200, json=self.tree,
                headers={**rl_headers(4990), "etag": self.tree_etag},
            )

        if "/repos/" in url:
            return httpx.Response(200, json={
                "full_name": "acme/skills", "default_branch": "main",
                "description": "A skill collection", "stargazers_count": 1200,
                "forks_count": 3, "subscribers_count": 44, "open_issues_count": 7,
                "license": {"spdx_id": "MIT"}, "language": "Python",
                "owner": {"type": "Organization", "login": "acme"},
                "topics": ["agent-skills"], "homepage": "https://acme.dev",
                "created_at": "2025-01-05T00:00:00Z",
                "updated_at": "2026-08-21T10:00:00Z",
                "pushed_at": "2026-08-20T10:00:00Z",
                "fork": False, "archived": False, "disabled": False,
                "is_template": False, "has_issues": True, "has_wiki": False,
                "has_pages": False, "has_discussions": True, "size": 400,
            }, headers=rl_headers(4991))

        return httpx.Response(404, json={})


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "t.db")
    fake = FakeGitHub()
    gh = GitHubClient([], etag_store=store)
    attach(gh, fake.handler)
    yield gh, store, fake, Config(db_path=tmp_path / "t.db")
    store.close()


async def test_first_crawl_indexes_every_skill(rig):
    gh, store, fake, cfg = rig
    found = await harvest_repo(gh, store, "acme/skills", cfg, discovered_via="test")

    assert found == 2
    assert sorted(fake.raw_fetches) == [
        "skills/chart-builder/SKILL.md",
        "skills/pdf-processing/SKILL.md",
    ]
    rows = store.db.execute(
        "SELECT name, source_kind, valid, score FROM skills ORDER BY name"
    ).fetchall()
    assert [r["name"] for r in rows] == ["chart-builder", "pdf-processing"]
    assert all(r["valid"] == 1 for r in rows)
    assert all(r["source_kind"] == "skills-dir" for r in rows)
    # Scores are corpus-relative, so harvesting leaves them at zero on purpose;
    # ranking.recompute assigns them once the whole corpus is known.
    assert all(r["score"] == 0 for r in rows)
    assert store.get_repo("acme/skills")["stars"] == 1200


async def test_full_metadata_is_captured(rig):
    gh, store, fake, cfg = rig
    await harvest_repo(gh, store, "acme/skills", cfg)
    r = store.get_repo("acme/skills")
    assert r["stars"] == 1200 and r["forks"] == 3
    assert r["open_issues"] == 7 and r["subscribers"] == 44
    assert r["language"] == "Python" and r["license"] == "MIT"
    assert r["owner_type"] == "Organization"
    assert r["size_kb"] == 400 and r["has_discussions"] == 1
    assert r["created_at"].startswith("2025") and r["pushed_at"].startswith("2026")
    assert json.loads(r["topics"]) == ["agent-skills"]


async def test_ranking_pass_scores_what_harvest_left_at_zero(rig):
    from skill_engine.ranking import recompute

    gh, store, fake, cfg = rig
    await harvest_repo(gh, store, "acme/skills", cfg)
    result = recompute(store)

    assert result["skills_scored"] == 2
    rows = store.db.execute("SELECT score, score_detail FROM skills").fetchall()
    assert all(r["score"] > 0 for r in rows)
    detail = json.loads(rows[0]["score_detail"])
    assert set(detail["families"]) == {
        "repo_standing", "author_standing", "craft", "distinctiveness"}
    assert store.get_repo("acme/skills")["repo_score"] > 0


async def test_unchanged_tree_costs_nothing(rig):
    """The core efficiency claim: a repo that has not moved refetches nothing."""
    gh, store, fake, cfg = rig
    await harvest_repo(gh, store, "acme/skills", cfg)
    fake.raw_fetches.clear()

    found = await harvest_repo(gh, store, "acme/skills", cfg)

    assert found == 2
    assert fake.raw_fetches == []          # no content refetched
    assert fake.sent_etags[-1] == "tree-v1"  # we asked conditionally
    assert gh.stats["not_modified"] >= 1     # and were told 304, free of charge


async def test_only_the_changed_file_is_refetched(rig):
    """Even when the tree moves, an unchanged blob SHA skips the fetch."""
    gh, store, fake, cfg = rig
    await harvest_repo(gh, store, "acme/skills", cfg)
    fake.raw_fetches.clear()

    fake.files["skills/pdf-processing/SKILL.md"] = SKILL_A.replace(
        "Extract text and tables", "Extract text, tables and annotations"
    )
    fake.tree["tree"][1]["sha"] = "sha-a2"
    fake.tree["sha"] = "tree2"
    fake.tree_etag = "tree-v2"

    await harvest_repo(gh, store, "acme/skills", cfg)

    assert fake.raw_fetches == ["skills/pdf-processing/SKILL.md"]
    desc = store.db.execute(
        "SELECT description FROM skills WHERE name='pdf-processing'"
    ).fetchone()["description"]
    assert "annotations" in desc


async def test_deleted_skill_is_removed_from_the_index(rig):
    gh, store, fake, cfg = rig
    await harvest_repo(gh, store, "acme/skills", cfg)

    fake.tree["tree"] = [e for e in fake.tree["tree"] if "chart-builder" not in e["path"]]
    fake.tree_etag = "tree-v3"
    found = await harvest_repo(gh, store, "acme/skills", cfg)

    assert found == 1
    names = [r["name"] for r in store.db.execute("SELECT name FROM skills").fetchall()]
    assert names == ["pdf-processing"]
    # And the FTS index must forget it too, not just the base table.
    from skill_engine.search import search
    assert not search(store, "chart builder SVG styling")


async def test_repo_with_no_skills_is_recorded_not_retried(rig):
    gh, store, fake, cfg = rig
    fake.tree["tree"] = [{"path": "README.md", "type": "blob", "sha": "r1", "size": 10}]

    assert await harvest_repo(gh, store, "acme/skills", cfg) == 0
    assert store.get_repo("acme/skills")["skill_count"] == 0
    assert store.queue_depth() == 0  # dequeued, so it will not be picked up again


async def test_invalid_skill_is_kept_but_flagged(rig):
    gh, store, fake, cfg = rig
    fake.files["skills/chart-builder/SKILL.md"] = "# no frontmatter here at all\n\nwords " * 20

    await harvest_repo(gh, store, "acme/skills", cfg)
    row = store.db.execute(
        "SELECT valid, invalid_reason, name FROM skills WHERE path LIKE '%chart%'"
    ).fetchone()
    assert row["valid"] == 0
    assert "frontmatter" in row["invalid_reason"]
    assert row["name"] == "chart-builder"  # recovered from the directory name


async def test_true_skill_count_survives_the_fetch_cap(rig):
    """The cap must not hide the size signal the dump penalty depends on."""
    gh, store, fake, cfg = rig
    cfg.max_skills_per_repo = 1
    fake.tree["tree"].append(
        {"path": "skills/third/SKILL.md", "type": "blob", "sha": "c1",
         "size": len(SKILL_A)}
    )
    fake.files["skills/third/SKILL.md"] = SKILL_A

    await harvest_repo(gh, store, "acme/skills", cfg)

    # Only one file fetched, but the repo's real size is recorded in full.
    assert store.get_repo("acme/skills")["skill_count"] == 3
    assert store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"] == 1


async def test_capped_repo_does_not_prune_the_uncrawled_remainder(rig):
    """Entries past the cap were never inspected; they are not 'missing'."""
    gh, store, fake, cfg = rig
    await harvest_repo(gh, store, "acme/skills", cfg)     # index both skills
    assert store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"] == 2

    cfg.max_skills_per_repo = 1
    fake.tree_etag = "tree-v9"
    await harvest_repo(gh, store, "acme/skills", cfg)

    # The skill beyond the cap stays indexed rather than being deleted.
    assert store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"] == 2


async def test_dump_penalty_fires_on_the_true_count(rig):
    from skill_engine.ranking import recompute

    gh, store, fake, cfg = rig
    cfg.max_skills_per_repo = 1
    await harvest_repo(gh, store, "acme/skills", cfg)
    store.db.execute("UPDATE repos SET skill_count = 2118 WHERE full_name = ?",
                     ("acme/skills",))
    store.commit()
    recompute(store)

    detail = json.loads(store.get_repo("acme/skills")["score_detail"])
    assert "aggregator_dump" in detail["penalties"]
