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
