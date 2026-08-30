"""The quota-free archive path, including the failure modes that matter overnight."""

import io
import tarfile

import httpx
import pytest

from skill_engine.config import Config
from skill_engine.store import Store
from skill_engine.tarball import (
    TarballFetcher,
    extract_skills,
    harvest_repo_tarball,
    run_tarball_crawl,
)

SKILL = """---
name: {name}
description: A skill that does {name} things, described at sufficient length here.
---

# {name}

Body content for {name}, long enough to clear the minimum-length check easily.
"""


def make_tar(files: dict[str, str], prefix: str = "repo-abc123") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{prefix}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extract_strips_the_archive_prefix():
    blob = make_tar({
        "README.md": "# hi",
        "skills/alpha/SKILL.md": SKILL.format(name="alpha"),
        "skills/beta/SKILL.md": SKILL.format(name="beta"),
    })
    found = dict(extract_skills(blob))
    assert set(found) == {"skills/alpha/SKILL.md", "skills/beta/SKILL.md"}
    assert "alpha" in found["skills/alpha/SKILL.md"]


def test_extract_is_case_insensitive_and_ignores_lookalikes():
    blob = make_tar({
        "a/Skill.md": SKILL.format(name="a"),
        "b/SKILL.markdown": SKILL.format(name="b"),
        "c/MY-SKILL.md": SKILL.format(name="c"),
    })
    paths = [p for p, _ in extract_skills(blob)]
    assert paths == ["a/Skill.md"]


def test_extract_survives_a_corrupt_archive():
    assert extract_skills(b"not a tarball at all") == []
    truncated = make_tar({"skills/x/SKILL.md": SKILL.format(name="x")})[:80]
    assert extract_skills(truncated) == []  # no exception escapes


def test_extract_skips_absurdly_large_members():
    blob = make_tar({"skills/big/SKILL.md": "x" * (600 * 1024)})
    assert extract_skills(blob) == []


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_repo({
        "full_name": "acme/skills", "default_branch": "main", "stars": 100,
        "forks": 10, "license": "MIT", "topics": [], "size_kb": 500,
        "created_at": "2025-01-01T00:00:00Z", "pushed_at": "2026-08-01T00:00:00Z",
        "is_fork": False, "archived": False, "disabled": False,
    }, touch_crawled=False)
    store.enqueue("acme/skills", "test", 100)
    store.commit()
    yield store, Config(db_path=tmp_path / "t.db")
    store.close()


def fetcher_returning(blob, status=200):
    def handler(request):
        if blob is None:
            return httpx.Response(status)
        return httpx.Response(200, content=blob)

    f = TarballFetcher(concurrency=2, min_delay=0)
    f.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return f


async def test_harvest_indexes_every_skill_from_one_download(rig):
    store, cfg = rig
    blob = make_tar({f"skills/s{i}/SKILL.md": SKILL.format(name=f"s{i}")
                     for i in range(5)})
    f = fetcher_returning(blob)
    n = await harvest_repo_tarball(f, store, "acme/skills", cfg,
                                   store.get_repo("acme/skills"))
    await f.aclose()

    assert n == 5
    assert store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"] == 5
    assert store.get_repo("acme/skills")["skill_count"] == 5
    assert store.queue_depth() == 0          # dequeued, will not be redone
    assert f.stats["downloads"] == 1         # one request for the whole repo


async def test_unavailable_archive_returns_none_for_rest_fallback(rig):
    store, cfg = rig
    f = fetcher_returning(None, status=404)
    result = await harvest_repo_tarball(f, store, "acme/skills", cfg,
                                        store.get_repo("acme/skills"))
    await f.aclose()
    # None, not 0: "could not read" must stay distinct from "read, found none".
    assert result is None
    assert store.queue_depth() == 1          # left queued for the REST path


async def test_empty_repo_is_recorded_not_deferred(rig):
    store, cfg = rig
    f = fetcher_returning(make_tar({"README.md": "nothing here"}))
    result = await harvest_repo_tarball(f, store, "acme/skills", cfg,
                                        store.get_repo("acme/skills"))
    await f.aclose()
    assert result == 0
    assert store.queue_depth() == 0


async def test_oversized_archive_is_abandoned_mid_stream():
    f = TarballFetcher(concurrency=1, max_bytes=1000, min_delay=0)
    big = b"x" * 50_000

    def handler(request):
        return httpx.Response(200, content=big)  # no content-length declared

    f.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await f.fetch("a", "b", "main") is None
    assert f.stats["too_big"] == 1
    await f.aclose()


async def test_true_count_recorded_when_capped(rig):
    store, cfg = rig
    cfg.max_skills_per_repo = 2
    blob = make_tar({f"skills/s{i}/SKILL.md": SKILL.format(name=f"s{i}")
                     for i in range(9)})
    f = fetcher_returning(blob)
    n = await harvest_repo_tarball(f, store, "acme/skills", cfg,
                                   store.get_repo("acme/skills"))
    await f.aclose()
    assert n == 9                                                    # truth
    assert store.get_repo("acme/skills")["skill_count"] == 9          # truth
    assert store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"] == 2


async def test_sweep_stops_at_the_target_and_stays_resumable(rig):
    store, cfg = rig
    for i in range(6):
        store.upsert_repo({
            "full_name": f"o{i}/r{i}", "default_branch": "main", "stars": i,
            "forks": 0, "license": "MIT", "topics": [], "size_kb": 100,
            "created_at": "2025-01-01T00:00:00Z",
            "pushed_at": "2026-08-01T00:00:00Z",
            "is_fork": False, "archived": False, "disabled": False,
        }, touch_crawled=False)
        store.enqueue(f"o{i}/r{i}", "test", 100)
    store.commit()

    blob = make_tar({f"skills/s{i}/SKILL.md": SKILL.format(name=f"s{i}")
                     for i in range(4)})
    import skill_engine.tarball as tb
    original = tb.TarballFetcher
    tb.TarballFetcher = lambda **kw: fetcher_returning(blob)
    try:
        totals = await run_tarball_crawl(
            store, cfg, target_skills=8, batch=2, concurrency=2)
    finally:
        tb.TarballFetcher = original

    indexed = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    assert indexed >= 8
    assert totals["repos"] < 7          # stopped early rather than draining
    assert store.queue_depth() > 0      # remainder still queued for a resume


async def test_name_only_candidates_become_harvestable(rig):
    """A queue entry with no repos row is invisible to the sweep.

    GH Archive mining and awesome-list scraping yield bare names. Enqueuing
    one without a repos row silently drops it, because the sweep joins the two
    — 8,218 mined candidates were lost to exactly this before the stub existed.
    """
    store, cfg = rig
    store.db.execute("DELETE FROM queue")
    assert store.ensure_repo_stub("someone/found-by-name", "gharchive-mine")
    store.enqueue("someone/found-by-name", "gharchive-mine", 60)
    store.commit()

    visible = store.db.execute(
        "SELECT COUNT(*) c FROM queue q JOIN repos r ON r.full_name = q.full_name "
        "WHERE r.tree_sha IS NULL"
    ).fetchone()["c"]
    assert visible == 1

    # The stub carries no metadata; the tarball path must cope with that.
    row = store.get_repo("someone/found-by-name")
    assert row["default_branch"] is None
    blob = make_tar({"skills/x/SKILL.md": SKILL.format(name="x")})
    f = fetcher_returning(blob)
    assert await harvest_repo_tarball(f, store, "someone/found-by-name", cfg, row) == 1
    await f.aclose()


def test_repo_stub_is_idempotent(rig):
    store, _ = rig
    assert store.ensure_repo_stub("a/b") is True
    assert store.ensure_repo_stub("a/b") is False      # already present
    assert store.ensure_repo_stub("not-a-repo") is False


def test_stub_does_not_clobber_real_metadata(rig):
    """A later stub must never blank out metadata search already fetched."""
    store, _ = rig
    store.ensure_repo_stub("acme/skills", "gharchive-mine")
    assert store.get_repo("acme/skills")["stars"] == 100   # from the fixture
