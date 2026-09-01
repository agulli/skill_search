"""The HTTP layer, including the connection reuse that made it usable at scale."""

import json
import time
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from skill_engine.serve import get_store, make_handler
from skill_engine.store import Store
from tests.test_search import SKILLS, classify_path
import hashlib


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "s.db"
    st = Store(db)
    st.upsert_repo({"full_name": "acme/docs", "default_branch": "main", "stars": 900,
                    "forks": 90, "license": "MIT", "topics": [], "language": "Python",
                    "pushed_at": "2026-08-01T00:00:00Z", "is_fork": False,
                    "archived": False, "disabled": False})
    body = "Extract tables from PDF documents. " * 30
    st.upsert_skill({
        "repo": "acme/docs", "path": "skills/pdf/SKILL.md", "name": "pdf-processing",
        "description": "Extract text and tables from PDF documents.", "body": body,
        "heading": "", "version": None, "license": "MIT", "allowed_tools": '["Read"]',
        "metadata": "{}", "resources": '["scripts/a.py"]', "source_kind": "skills-dir",
        "blob_sha": "x", "content_hash": hashlib.sha256(body.encode()).hexdigest(),
        "body_len": len(body), "score": 88.0, "valid": 1, "invalid_reason": "",
    })
    st.commit(); st.close()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, "none"))
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown(); srv.server_close()


def test_ui_page_renders(server):
    r = httpx.get(server + "/")
    assert r.status_code == 200
    assert "Agent Skills Search by AGI" in r.text
    assert "/api/search" in r.text          # the page drives itself off the API


def test_search_api_returns_totals_and_timing(server):
    d = httpx.get(server + "/api/search", params={"q": "pdf tables"}).json()
    assert d["count"] >= 1
    assert d["total"] >= d["count"]
    assert isinstance(d["took_ms"], int)
    assert d["results"][0]["name"] == "pdf-processing"


def test_search_api_requires_a_query(server):
    assert httpx.get(server + "/api/search").status_code == 400


def test_facets_only_computed_when_asked(server):
    plain = httpx.get(server + "/api/search", params={"q": "pdf"}).json()
    faceted = httpx.get(server + "/api/search",
                        params={"q": "pdf", "facets": "1"}).json()
    assert plain["facets"] == {}
    assert faceted["facets"]["kind"]


def test_filters_flow_through_the_api(server):
    hi = httpx.get(server + "/api/search",
                   params={"q": "pdf", "min_stars": 10_000}).json()
    lo = httpx.get(server + "/api/search",
                   params={"q": "pdf", "min_stars": 10}).json()
    assert hi["count"] == 0 and lo["count"] >= 1


def test_skill_detail_exposes_body_and_score_families(server):
    from skill_engine.ranking import recompute
    sid = httpx.get(server + "/api/search",
                    params={"q": "pdf"}).json()["results"][0]["id"]
    d = httpx.get(f"{server}/api/skill/{sid}").json()
    assert "Extract tables" in d["body"]
    assert d["url"].startswith("https://github.com/acme/docs/")
    assert "score_detail" not in d          # replaced by the parsed families


def test_missing_skill_is_404(server):
    assert httpx.get(server + "/api/skill/999999").status_code == 404
    assert httpx.get(server + "/api/skill/abc").status_code == 400


def test_store_connection_is_reused_per_thread(tmp_path):
    """Reopening per request re-ran the schema and migration on a 2.5GB file."""
    db = tmp_path / "r.db"
    Store(db).close()
    a, b = get_store(db), get_store(db)
    assert a is b


def test_store_sizes_its_cache_for_the_index(tmp_path):
    """SQLite's 2MB default meant nearly every query read from disk."""
    st = Store(tmp_path / "p.db")
    cache = st.db.execute("PRAGMA cache_size").fetchone()[0]
    mmap = st.db.execute("PRAGMA mmap_size").fetchone()[0]
    assert cache < 0 and abs(cache) >= 64 * 1024   # negative == kibibytes
    assert mmap > 0
    st.close()


def test_cache_size_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_ENGINE_CACHE_MB", "32")
    st = Store(tmp_path / "c.db")
    assert st.db.execute("PRAGMA cache_size").fetchone()[0] == -32 * 1024
    st.close()


def test_optimize_compacts_and_reports(tmp_path):
    st = Store(tmp_path / "o.db")
    st.upsert_repo({"full_name": "a/b", "default_branch": "main", "stars": 1,
                    "forks": 0, "license": "MIT", "topics": [],
                    "pushed_at": "2026-01-01T00:00:00Z", "is_fork": False,
                    "archived": False, "disabled": False})
    for i in range(40):
        st.upsert_skill({
            "repo": "a/b", "path": f"skills/s{i}/SKILL.md", "name": f"s{i}",
            "description": f"skill number {i} for testing search", "body": "body " * 50,
            "heading": "", "version": None, "license": "MIT", "allowed_tools": "[]",
            "metadata": "{}", "resources": "[]", "source_kind": "skills-dir",
            "blob_sha": "x", "content_hash": f"h{i}", "body_len": 250, "score": 1,
            "valid": 1, "invalid_reason": "",
        })
        st.commit()                       # a segment per commit, as the crawl does
    result = st.optimize()
    assert result["segments_after"] <= result["segments_before"]
    # The index must still work after compaction.
    from skill_engine.search import search
    assert search(st, "skill number testing")
    st.close()


# --------------------------------------------------- public deployment mode


def test_read_only_store_cannot_write(tmp_path):
    """Serving opens the corpus read-only so a handler bug cannot corrupt it."""
    db = tmp_path / "ro.db"
    Store(db).close()                       # create it read-write first
    ro = Store(db, read_only=True)
    assert ro.read_only
    with pytest.raises(Exception):
        ro.db.execute("CREATE TABLE nope(x)")
    # Reads still work, and the tuning pragmas still applied.
    assert ro.db.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    assert ro.db.execute("PRAGMA cache_size").fetchone()[0] < 0
    ro.close()


def test_read_only_open_does_not_create_a_database(tmp_path):
    with pytest.raises(Exception):
        Store(tmp_path / "absent.db", read_only=True)


def test_rate_limiter_allows_a_burst_then_throttles():
    from skill_engine.serve import RateLimiter

    rl = RateLimiter(rate=1.0, burst=5)
    assert all(rl.allow("1.2.3.4")[0] for _ in range(5))    # burst
    ok, wait = rl.allow("1.2.3.4")
    assert not ok and wait > 0                              # then blocked
    assert rl.allow("5.6.7.8")[0]                           # other client fine


def test_rate_limiter_refills_over_time():
    from skill_engine.serve import RateLimiter

    rl = RateLimiter(rate=100.0, burst=1)
    assert rl.allow("x")[0]
    assert not rl.allow("x")[0]
    time.sleep(0.05)                                        # 100/s refills fast
    assert rl.allow("x")[0]


def test_rate_limiter_memory_is_bounded():
    from skill_engine.serve import RateLimiter

    rl = RateLimiter(rate=1.0, burst=1, max_clients=50)
    for i in range(500):
        rl.allow(f"10.0.0.{i}")
    assert len(rl._buckets) <= 51


def test_client_ip_ignores_proxy_headers_unless_trusted():
    from skill_engine.serve import client_ip

    class FakeHandler:
        headers = {"CF-Connecting-IP": "9.9.9.9"}
        client_address = ("10.0.0.1", 1234)

    h = FakeHandler()
    # Spoofable when talking to the origin directly, so untrusted by default.
    assert client_ip(h, trust_proxy=False) == "10.0.0.1"
    assert client_ip(h, trust_proxy=True) == "9.9.9.9"


def test_health_endpoint_is_exempt_from_rate_limiting(server):
    # Health must answer under load, or checks fail exactly when it is busiest.
    for _ in range(60):
        assert httpx.get(server + "/health").status_code == 200


def test_database_predating_a_migrated_column_still_opens(tmp_path):
    """Indexes on migrated columns must be created after the migration.

    A database created before `repo_score` existed failed to open at all: the
    schema script tried to index a column the migration had not yet added, and
    aborted before reaching it.
    """
    import sqlite3

    db = tmp_path / "old.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        "CREATE TABLE repos(full_name TEXT PRIMARY KEY, owner TEXT, name TEXT);"
        "INSERT INTO repos VALUES('a/b','a','b');"
    )
    legacy.commit()
    legacy.close()

    store = Store(db)                       # must not raise
    assert store.get_repo("a/b")["owner"] == "a"
    cols = {r["name"] for r in store.db.execute("PRAGMA table_info(repos)")}
    assert "repo_score" in cols             # migration ran
    idx = {r[1] for r in store.db.execute("PRAGMA index_list(repos)")}
    assert "repos_score" in idx             # and the index followed it
    store.close()


def test_server_starts_without_a_corpus(tmp_path):
    """Deployment deadlocks otherwise.

    The machine cannot start without the database, and the database cannot be
    uploaded without a running machine. Liveness must therefore not depend on
    the corpus being present — the first deploy hit exactly this.
    """
    import threading
    from http.server import ThreadingHTTPServer
    from skill_engine.serve import make_handler

    missing = tmp_path / "absent.db"
    # read_only matters: a writable open would *create* the database and the
    # test would silently assert nothing. Production serves read-only anyway.
    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(missing, "none", read_only=True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        health = httpx.get(base + "/health")
        assert health.status_code == 200          # stays alive
        assert health.json()["index"] is False    # but says it has no corpus

        # Everything that needs the corpus fails clearly, not by crashing.
        r = httpx.get(base + "/api/search", params={"q": "pdf"})
        assert r.status_code == 503
        assert "index not loaded" in r.json()["error"]
        assert not missing.exists()      # and never created it
    finally:
        srv.shutdown(); srv.server_close()


def test_a_truncated_corpus_degrades_instead_of_crashing(tmp_path):
    """A half-written database is worse than none.

    The file exists, so the server looks ready; then every request dies opening
    it, which flaps the health check, stops the machine, and leaves no running
    VM to upload a replacement to. That deadlocked a real deployment.
    """
    import threading
    from http.server import ThreadingHTTPServer
    from skill_engine.serve import make_handler

    good = tmp_path / "good.db"
    Store(good).close()
    truncated = tmp_path / "truncated.db"
    truncated.write_bytes(good.read_bytes()[:3000])   # a partial upload

    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(truncated, "none", read_only=True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert httpx.get(base + "/health").json()["index"] is False
        assert httpx.get(base + "/api/search", params={"q": "x"}).status_code == 503
        assert httpx.get(base + "/health").status_code == 200   # still alive
    finally:
        srv.shutdown(); srv.server_close()


def test_static_page_is_cacheable_but_searches_are_not(server):
    """Cloudflare reported cf-cache-status: DYNAMIC and proxied everything.

    Without an explicit header a CDN assumes nothing is cacheable, so a 21KB
    page identical for every visitor was fetched from the origin every time —
    the difference between ~20ms and ~250ms.
    """
    page = httpx.get(server + "/")
    assert "s-maxage" in page.headers["cache-control"]

    cats = httpx.get(server + "/api/categories")
    assert "s-maxage" in cats.headers["cache-control"]

    # Results depend on filters and must never be served to another visitor.
    hits = httpx.get(server + "/api/search", params={"q": "pdf"})
    assert hits.headers["cache-control"] == "no-store"

    # Liveness must always reflect the current process.
    assert "cache-control" not in httpx.get(server + "/health").headers


def test_catalogue_is_precomputed_not_derived_per_request(tmp_path):
    """It was a GROUP BY over every skill, run on every page load.

    The directory changes only when the index is rebuilt, so deriving it per
    request cost 49ms on a 100k corpus and far more at a million. Reading the
    stored copy is 0.02ms.
    """
    from skill_engine.taxonomy import build_catalogue

    db = tmp_path / "c.db"
    store = Store(db)
    store.upsert_repo({"full_name": "a/b", "default_branch": "main", "stars": 5,
                       "forks": 1, "license": "MIT", "topics": [],
                       "pushed_at": "2026-01-01T00:00:00Z", "is_fork": False,
                       "archived": False, "disabled": False})
    store.upsert_skill({
        "repo": "a/b", "path": "skills/s/SKILL.md", "name": "pdf-extract",
        "description": "Extract tables and text from PDF documents reliably.",
        "body": "b" * 400, "heading": "", "version": None, "license": "MIT",
        "allowed_tools": "[]", "metadata": "{}", "resources": "[]",
        "source_kind": "skills-dir", "blob_sha": "x", "content_hash": "h",
        "body_len": 400, "score": 1, "valid": 1, "invalid_reason": "",
    })
    store.db.execute("UPDATE skills SET category='documents', subcategory='pdf'")
    store.commit()

    assert store.get_meta("catalogue") is None      # nothing stored yet
    build_catalogue(store)
    stored = store.get_meta("catalogue")
    assert stored and "documents" in stored
    store.close()


def test_catalogue_is_inlined_into_the_page(server):
    """No fetch on first paint, which also makes the stale-render race
    structurally impossible — nothing is in flight to arrive late."""
    page = httpx.get(server + "/").text
    assert "__CATALOGUE__" not in page          # placeholder was substituted


def test_missing_catalogue_does_not_break_the_page(tmp_path):
    """An index built before the catalogue existed must still serve."""
    import threading
    from http.server import ThreadingHTTPServer
    from skill_engine.serve import make_handler

    db = tmp_path / "old.db"
    Store(db).close()                            # no catalogue stored
    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(db, "none", read_only=True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = httpx.get(base + "/")
        assert page.status_code == 200
        assert "null" in page.text               # falls back cleanly
        assert httpx.get(base + "/api/categories").status_code == 200
    finally:
        srv.shutdown(); srv.server_close()
