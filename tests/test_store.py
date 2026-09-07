

def test_crawl_body_cap_truncates_without_corrupting_identity(tmp_path):
    """The cap must save space without breaking dedup or the recorded length.

    `content_hash` and `body_len` are computed upstream from the full text; if
    the cap recomputed either from the truncated body, duplicate collapsing and
    every length-based statistic would silently change meaning.
    """
    import skill_engine.store as store_mod
    from skill_engine.store import Store

    db = Store(tmp_path / "t.db")
    db.db.execute(
        "INSERT INTO repos(full_name, owner, name) VALUES('a/b','a','b')")
    long_body = "x" * 50_000
    rec = {
        "repo": "a/b", "path": "SKILL.md", "name": "n", "description": "d",
        "body": long_body, "heading": "", "version": "", "license": "",
        "allowed_tools": "", "metadata": "", "resources": "",
        "source_kind": "tarball", "blob_sha": "s", "content_hash": "HASH",
        "body_len": len(long_body), "score": 0.0, "valid": 1,
        "invalid_reason": "", "warnings": "",
    }
    db.upsert_skill(rec)
    db.commit()

    row = db.db.execute(
        "SELECT body, body_len, content_hash FROM skills").fetchone()
    assert len(row["body"]) == store_mod.CRAWL_BODY_CAP
    assert row["body_len"] == 50_000        # true length, not the stored one
    assert row["content_hash"] == "HASH"    # dedup identity untouched
    # The caller's dict must not be mutated — it is reused for other writes.
    assert len(rec["body"]) == 50_000
    db.close()


def test_adaptive_backoff_slows_globally_and_recovers():
    """A refusal must slow every worker, and success must walk it back.

    The previous behaviour slept inside the one task that was refused, leaving
    the other workers at the unchanged rate — so refusals repeated and
    throughput sawtoothed rather than settling.
    """
    from skill_engine.tarball import TarballFetcher

    f = TarballFetcher(concurrency=4, max_bytes=1 << 20, min_delay=0.05)
    assert f.min_delay == 0.05

    f._slow_down()
    assert f.min_delay > 0.05                 # multiplicative decrease
    assert f._pause_until > 0                 # and a pause for everyone
    assert f.stats["throttled"] == 1
    slowed = f.min_delay

    for _ in range(3):
        f._slow_down()
    assert f.min_delay <= f.max_delay         # bounded

    for _ in range(1_000_000):
        f._speed_up()
    assert f.min_delay == f.base_delay        # recovers to the floor, not below
    assert slowed > f.base_delay


def test_forbidden_breaker_trips_only_on_repeated_403s():
    """One 403 is a repository; a run of them is us. Only the latter stops."""
    from skill_engine.tarball import TarballFetcher

    f = TarballFetcher(concurrency=2, max_bytes=1 << 20, min_delay=0.05)
    assert f.blocked is False

    for _ in range(f.forbidden_limit - 1):
        f._forbidden()
    assert f.blocked is False, "a few 403s must not halt a multi-day crawl"

    f._forbidden()
    assert f.blocked is True
    assert f.stats["forbidden"] == f.forbidden_limit


def test_429_and_403_are_handled_differently():
    """Backing off clears a throttle; it does not clear a block."""
    from skill_engine.tarball import TarballFetcher

    f = TarballFetcher(concurrency=2, max_bytes=1 << 20, min_delay=0.05)
    f._slow_down()
    assert f.min_delay > 0.05 and f.blocked is False   # 429: slow, keep going

    g = TarballFetcher(concurrency=2, max_bytes=1 << 20, min_delay=0.05)
    for _ in range(g.forbidden_limit):
        g._forbidden()
    assert g.blocked is True                            # 403: stop


def test_retry_after_is_honoured_when_the_server_sends_one():
    """If the server states how long to wait, guessing is worse behaved."""
    import time
    from skill_engine.tarball import TarballFetcher

    f = TarballFetcher(concurrency=2, max_bytes=1 << 20, min_delay=0.05)
    before = time.monotonic()
    f._slow_down(retry_after="120")
    assert f._pause_until - before >= 119
    assert f.stats["retry_after_honoured"] == 1


def test_retry_after_garbage_does_not_crash_the_crawler():
    """A bad header must fall back, not take down a multi-day run."""
    from skill_engine.tarball import TarballFetcher

    for bad in ("Wed, 21 Oct 2026 07:28:00 GMT", "", "soon", None, "-5"):
        f = TarballFetcher(concurrency=2, max_bytes=1 << 20, min_delay=0.05)
        f._slow_down(retry_after=bad)          # must not raise
        assert f._pause_until > 0


def test_retry_after_is_bounded():
    """A hostile or mistaken header must not park the crawler for a day."""
    from skill_engine.tarball import TarballFetcher

    f = TarballFetcher(concurrency=2, max_bytes=1 << 20, min_delay=0.05)
    import time
    before = time.monotonic()
    f._slow_down(retry_after="999999")
    assert f._pause_until - before <= 301


def test_recovery_only_accelerates_after_a_sustained_clean_run():
    """The first success after a refusal is evidence of nothing."""
    import time
    from skill_engine.tarball import TarballFetcher

    f = TarballFetcher(concurrency=2, max_bytes=1 << 20, min_delay=0.05)
    f._slow_down()
    slowed = f.min_delay

    f._clean_since = time.monotonic()          # just recovered: no acceleration
    f._speed_up()
    fresh_step = slowed - f.min_delay

    f.min_delay = slowed
    f._clean_since = time.monotonic() - 3600   # an hour clean: faster taper
    f._speed_up()
    quiet_step = slowed - f.min_delay

    assert quiet_step > fresh_step
    assert quiet_step <= fresh_step * 4 + 1e-9, "taper, not a probe"


def test_sweep_orders_by_priority_before_score(tmp_path):
    """Source quality must outrank repository popularity when choosing work.

    Ordering by repo_score first assumes popularity predicts whether a repo
    contains skills. Measured across discovery sources it does not: yields
    differ 10x while maximum scores are identical, so a high-volume low-yield
    source monopolises the head of the queue.
    """
    from skill_engine.store import Store

    db = Store(tmp_path / "t.db")
    # A popular repo from a bad source, and an unremarkable one from a good source.
    db.db.execute("INSERT INTO repos(full_name,owner,name,repo_score,disabled) "
                  "VALUES('junk/popular','junk','popular',98.0,0)")
    db.db.execute("INSERT INTO repos(full_name,owner,name,repo_score,disabled) "
                  "VALUES('good/modest','good','modest',12.0,0)")
    db.db.execute("INSERT INTO queue(full_name,priority,attempts) "
                  "VALUES('junk/popular',40,0)")
    db.db.execute("INSERT INTO queue(full_name,priority,attempts) "
                  "VALUES('good/modest',140,0)")
    db.commit()

    rows = db.db.execute("""
        SELECT q.full_name FROM queue q JOIN repos r ON r.full_name = q.full_name
        WHERE q.attempts < 4 AND r.tree_sha IS NULL AND r.disabled = 0
        ORDER BY q.priority DESC, r.repo_score DESC LIMIT 1""").fetchall()
    assert rows[0]["full_name"] == "good/modest"
    db.close()
