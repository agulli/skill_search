"""BigQuery ingestion must produce rows indistinguishable from the crawler's.

A second ingestion route that validated or hashed differently would put two
populations into one index and quietly invalidate every corpus-relative
statistic — percentile normalisation is computed across the whole corpus, so a
systematically different second source biases every score, not just its own.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("bqi", ROOT / "bigquery_ingest.py")
bqi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bqi)

from skill_engine.store import Store

SKILL = """---
name: pdf-extract
description: Extract tables and text from PDF invoices reliably.
---

# PDF extraction

Use pdfplumber for tables, falling back to OCR for scanned pages.
"""


def test_ingest_matches_the_crawler_shape(tmp_path):
    store = Store(tmp_path / "t.db")
    rows = [{"repo_name": "acme/skills", "path": "skills/pdf/SKILL.md",
             "content": SKILL}]
    stats = bqi.ingest(store, rows)
    assert stats == {"rows": 1, "kept": 1, "invalid": 0, "failed": 0}

    row = store.db.execute(
        "SELECT repo, path, name, description, content_hash, valid, source_kind "
        "FROM skills").fetchone()
    assert row["repo"] == "acme/skills"
    assert row["name"] == "pdf-extract"
    assert row["valid"] == 1
    assert row["content_hash"], "hash must be set, or dedup silently fails"
    store.close()


def test_content_hash_matches_the_crawler_exactly(tmp_path):
    """The same file from either route must collapse as one skill.

    If the two paths hashed differently, every BigQuery skill would look novel
    and the corpus would double-count everything it already had.
    """
    from skill_engine.parse import parse_skill

    store = Store(tmp_path / "t.db")
    bqi.ingest(store, [{"repo_name": "a/b", "path": "SKILL.md", "content": SKILL}])
    stored = store.db.execute("SELECT content_hash FROM skills").fetchone()[0]
    assert stored == parse_skill(SKILL, "SKILL.md").content_hash
    store.close()


def test_creates_repo_stub_for_unknown_repositories(tmp_path):
    """BigQuery reaches repos the crawler never queued; the FK needs a parent."""
    store = Store(tmp_path / "t.db")
    bqi.ingest(store, [{"repo_name": "never/seen", "path": "SKILL.md",
                        "content": SKILL}])
    assert store.db.execute(
        "SELECT COUNT(*) FROM repos WHERE full_name='never/seen'").fetchone()[0] == 1
    store.close()


def test_malformed_content_is_counted_not_fatal(tmp_path):
    """One bad file must not abort a load that costs real money to re-run."""
    store = Store(tmp_path / "t.db")
    stats = bqi.ingest(store, [
        {"repo_name": "a/b", "path": "SKILL.md", "content": None},
        {"repo_name": "a/c", "path": "SKILL.md", "content": "no frontmatter"},
        {"repo_name": "a/d", "path": "SKILL.md", "content": SKILL},
    ])
    assert stats["rows"] == 3
    assert stats["invalid"] >= 1     # the frontmatter-less one is stored, flagged
    store.close()


def test_sample_tables_are_used_when_asked():
    """The cheap tables must actually be the ones queried."""
    assert "sample_files" in bqi.build_query(sample=True)
    assert "sample_contents" in bqi.build_query(sample=True)
    full = bqi.build_query(sample=False)
    assert "sample_" not in full
    assert "github_repos.files" in full and "github_repos.contents" in full


def test_query_filters_to_skill_files_only():
    """Scanning is what costs money; the filter must be in the query."""
    sql = bqi.build_query(sample=False)
    assert "SKILL.md" in sql
    assert "binary = FALSE" in sql
