"""Non-GitHub sources must produce rows indistinguishable from the crawler's.

The corpus is one population. A second forge that hashed or validated
differently would make the same skill count twice when mirrored, and
percentile normalisation — computed across the whole corpus — would then be
scored against a population that is part real and part double-counted.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skill_engine import sources
from skill_engine.parse import parse_skill
from skill_engine.store import Store

SKILL = """---
name: pdf-extract
description: Extract tables and text from PDF invoices reliably.
---

# PDF extraction

Use pdfplumber for tables, OCR for scans.
"""


def test_skill_paths_are_recognised():
    assert sources.looks_like_skill("SKILL.md")
    assert sources.looks_like_skill("skills/pdf/SKILL.md")
    assert sources.looks_like_skill(".claude/skills/pdf/anything.md")
    assert not sources.looks_like_skill("README.md")
    assert not sources.looks_like_skill("src/skill_helper.py")


def test_hash_matches_the_github_crawler_exactly(tmp_path):
    """The same file from any forge must collapse as one skill."""
    store = Store(tmp_path / "t.db")
    store.ensure_repo_stub("gitlab.com/g/p")
    sources._store_skill(store, "gitlab.com", "gitlab.com/g/p", "SKILL.md",
                         SKILL, "https://gitlab.com/g/p/-/blob/main/SKILL.md")
    store.commit()
    got = store.db.execute("SELECT content_hash FROM skills").fetchone()[0]
    assert got == parse_skill(SKILL, "SKILL.md").content_hash
    store.close()


def test_repositories_are_namespaced_by_host(tmp_path):
    """`owner/name` collides across forges; `full_name` is a primary key."""
    store = Store(tmp_path / "t.db")
    for host, full in [("gitlab.com", "gitlab.com/acme/skills"),
                       ("huggingface.co", "huggingface.co/acme/skills")]:
        sources._ensure_repo(store, host, full, {"via": "test"})
        sources._store_skill(store, host, full, "SKILL.md", SKILL, "http://x")
    store.commit()
    rows = store.db.execute(
        "SELECT full_name, host FROM repos ORDER BY host").fetchall()
    assert [r["host"] for r in rows] == ["gitlab.com", "huggingface.co"]
    assert len({r["full_name"] for r in rows}) == 2
    store.close()


def test_host_defaults_to_github_for_existing_rows(tmp_path):
    """The column is additive: an untouched crawl database stays correct."""
    store = Store(tmp_path / "t.db")
    store.ensure_repo_stub("owner/name")
    store.commit()
    host = store.db.execute(
        "SELECT host FROM repos WHERE full_name='owner/name'").fetchone()[0]
    assert host == "github.com"
    store.close()


def test_stored_url_is_kept_so_a_result_can_be_opened(tmp_path):
    """A GitHub URL built for a GitLab skill would 404."""
    store = Store(tmp_path / "t.db")
    store.ensure_repo_stub("gitlab.com/g/p")
    url = "https://gitlab.com/g/p/-/blob/main/SKILL.md"
    sources._store_skill(store, "gitlab.com", "gitlab.com/g/p", "SKILL.md",
                         SKILL, url)
    store.commit()
    import json
    meta = json.loads(store.db.execute(
        "SELECT metadata FROM skills").fetchone()[0])
    assert meta["url"] == url and meta["host"] == "gitlab.com"
    store.close()


def test_tar_extraction_strips_the_wrapper_directory():
    """Archive paths must match what the GitHub crawler records."""
    import io, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = SKILL.encode()
        info = tarfile.TarInfo("project-abc123/skills/pdf/SKILL.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        junk = tarfile.TarInfo("project-abc123/README.md")
        junk.size = 3
        tar.addfile(junk, io.BytesIO(b"hey"))
    found = list(sources._skills_from_tar(buf.getvalue()))
    assert found == [("skills/pdf/SKILL.md", SKILL)]
