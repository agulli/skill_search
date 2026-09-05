

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
