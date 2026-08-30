"""Rate-limit and conditional-request behaviour, exercised against a mock GitHub."""

import time

import httpx
import pytest

from skill_engine.github import GitHubClient, NotFound
from skill_engine.store import Store


def attach(gh: GitHubClient, handler) -> None:
    """Swap in a mock transport without changing the client's own configuration."""
    gh._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )


def rl_headers(remaining: int, resource: str = "core", limit: int = 5000) -> dict:
    return {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int(time.time()) + 3600),
        "x-ratelimit-resource": resource,
    }


async def test_etag_is_sent_and_304_costs_nothing(tmp_path):
    store = Store(tmp_path / "t.db")
    seen_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if "if-none-match" in request.headers:
            return httpx.Response(304, headers=rl_headers(4999))
        return httpx.Response(200, json={"ok": True},
                              headers={**rl_headers(4999), "etag": 'W/"abc123"'})

    gh = GitHubClient([], etag_store=store)
    attach(gh, handler)

    first = await gh.get_json("/repos/a/b", etag_key="repo:a/b")
    assert first == {"ok": True}
    assert store.get_etag("repo:a/b") == 'W/"abc123"'

    second = await gh.get_json("/repos/a/b", etag_key="repo:a/b")
    assert second is None                      # 304 -> caller knows nothing changed
    assert seen_headers[1]["if-none-match"] == 'W/"abc123"'
    assert gh.stats["not_modified"] == 1

    await gh.aclose()
    store.close()


async def test_secondary_rate_limit_backs_off_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit"},
                headers={**rl_headers(4000), "retry-after": "1"},
            )
        return httpx.Response(200, json={"ok": True}, headers=rl_headers(3999))

    gh = GitHubClient(["tok_aaaa", "tok_bbbb"])
    attach(gh, handler)

    started = time.time()
    assert await gh.get_json("/repos/a/b") == {"ok": True}
    assert calls["n"] == 2
    # The token that tripped the limit is parked for the full retry-after window,
    # and the retry went straight to the healthy sibling rather than sleeping.
    parked = [t for t in gh.tokens if t.blocked_until > started]
    assert len(parked) == 1 and parked[0].label.endswith("aaaa")
    assert time.time() - started < 0.5
    await gh.aclose()


async def test_single_token_waits_out_a_secondary_limit():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit"},
                headers={**rl_headers(4000), "retry-after": "1"},
            )
        return httpx.Response(200, json={"ok": True}, headers=rl_headers(3999))

    gh = GitHubClient(["tok_only"])
    attach(gh, handler)
    started = time.time()
    assert await gh.get_json("/x") == {"ok": True}
    assert time.time() - started >= 1.0  # obeyed retry-after rather than hammering
    await gh.aclose()


async def test_404_is_terminal_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"message": "Not Found"}, headers=rl_headers(10))

    gh = GitHubClient(["tok_aaaa"])
    attach(gh, handler)
    with pytest.raises(NotFound):
        await gh.get_json("/repos/gone/away")
    assert calls["n"] == 1
    await gh.aclose()


async def test_server_error_is_retried(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, text="bad gateway", headers=rl_headers(10))
        return httpx.Response(200, json={"ok": 1}, headers=rl_headers(9))

    gh = GitHubClient(["tok_aaaa"], max_retries=4)
    attach(gh, handler)
    assert await gh.get_json("/x") == {"ok": 1}
    assert calls["n"] == 3
    await gh.aclose()


async def test_token_pool_routes_to_the_healthiest_token():
    gh = GitHubClient(["tok_aaaa", "tok_bbbb", "tok_cccc"])
    gh.tokens[0].bucket("core").remaining = 5
    gh.tokens[1].bucket("core").remaining = 4200
    gh.tokens[2].bucket("core").remaining = 900
    assert (await gh._pick("core")).label == gh.tokens[1].label
    await gh.aclose()


async def test_rate_limit_headers_update_the_right_bucket():
    gh = GitHubClient(["tok_aaaa"])

    def handler(request):
        return httpx.Response(200, json=[], headers=rl_headers(7, resource="search", limit=30))

    attach(gh, handler)
    await gh.get_json("/search/repositories", resource="search")
    assert gh.tokens[0].bucket("search").remaining == 7
    assert gh.tokens[0].bucket("core").remaining == 5000  # untouched
    await gh.aclose()


async def test_pagination_follows_link_header():
    def handler(request):
        page = request.url.params.get("page", "1")
        if page == "1":
            body = [{"i": 1}]
            link = '<https://api.github.com/x?page=2>; rel="next"'
        else:
            body, link = [{"i": 2}], ""
        return httpx.Response(200, json=body, headers={**rl_headers(100), "link": link})

    gh = GitHubClient(["tok_aaaa"])
    attach(gh, handler)
    got = [item async for item in gh.paginate("/x")]
    assert got == [{"i": 1}, {"i": 2}]
    await gh.aclose()
