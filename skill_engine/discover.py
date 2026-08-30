"""Discovery: finding repositories that might contain agent skills.

No single source is sufficient, and the obvious one is the weakest:

* **Repository search** is the workhorse. It is capped at 1000 results per
  query, which we defeat by recursively bisecting a `created:` date range until
  every shard fits under the cap. This turns a 1000-result ceiling into
  complete coverage of a query.
* **Code search** finds skills in repos whose name and topics reveal nothing.
  It is rate-limited hard (10 req/min) and also capped at 1000, so we treat it
  as a seed source and shard it by repository size.
* **Awesome-list mining** is the highest-yield source per request: one raw
  README fetch can surface a few hundred curated repositories, and costs no API
  quota at all.
* **GH Archive** is how you stay fresh without polling. Hourly public-event
  dumps let us notice a push to a repo we already index for free, and surface
  brand-new repos whose names look promising.
* **Owner expansion**: someone who published one skill collection usually
  published more.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from .config import NAME_HINTS, SEED_TOPICS, USER_AGENT
from .github import GitHubClient, NotFound
from .metadata import from_api_repo
from .store import Store

log = logging.getLogger("skill_engine.discover")

GHARCHIVE = "https://data.gharchive.org/{y:04d}-{m:02d}-{d:02d}-{h}.json.gz"

# Markdown links to GitHub repos, as they appear in awesome-lists.
REPO_LINK_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]{1,100})"
)
NON_REPO_OWNERS = {"topics", "features", "sponsors", "orgs", "settings", "apps",
                   "collections", "marketplace", "about", "pricing", "explore"}

# Queries that target the vocabulary skill authors actually use.
KEYWORD_QUERIES = [
    '"SKILL.md" in:readme',
    '"agent skills" in:name,description',
    '"claude skills" in:name,description',
    '"agent skill" in:name,description',
    '"claude code skills" in:readme',
    '"allowed-tools" in:readme',
    '".claude/skills" in:readme',
    '"skills marketplace" in:name,description,readme',
    '"skill library" in:name,description',
    "agent-skills in:name",
    "claude-skills in:name",
    "claude-code in:name skills",
    "skills in:name claude",
    "skill in:name agent",
    "awesome claude in:name",
    "awesome agent skills in:name",
]

# Broader sweeps: lower precision, but they reach authors who tag nothing and
# name nothing helpfully. The tree lookup is what actually confirms a hit.
BREADTH_QUERIES = [
    "topic:claude", "topic:anthropic", "topic:claude-ai", "topic:llm-agent",
    "topic:ai-agents topic:tools", "topic:agentic-ai", "topic:mcp",
    "topic:model-context-protocol", "topic:prompt-engineering topic:claude",
    "topic:openclaw", "topic:subagents", "topic:ai-tools topic:productivity",
    '"claude code" in:name,description',
    '"agent" in:name skills',
    'skills in:description agent',
    'claude in:name plugin',
    '"progressive disclosure" in:readme skills',
    '"frontmatter" in:readme skill',
]


def _clean_repo(owner: str, repo: str) -> str | None:
    repo = repo.rstrip(".").removesuffix(".git")
    if owner.lower() in NON_REPO_OWNERS or not repo or repo.startswith("."):
        return None
    return f"{owner}/{repo}"


# --------------------------------------------------------------------- search


async def _repo_search_page(
    gh: GitHubClient, query: str, page: int = 1
) -> tuple[int, list[dict]]:
    resp = await gh.request(
        "GET",
        "/search/repositories",
        resource="search",
        params={"q": query, "per_page": 100, "sort": "stars", "order": "desc",
                "page": page},
    )
    if resp is None:
        return 0, []
    payload = resp.json()
    return payload.get("total_count", 0), payload.get("items", [])


def absorb_search_items(store: Store, items: list[dict], reason: str,
                        priority: int) -> int:
    """Persist the full metadata carried by search results, then enqueue.

    Search items arrive with nearly the complete REST repository shape — stars,
    forks, issues, size, language, licence, topics, timestamps, template and
    archive flags. Writing all of it here means bulk discovery populates the
    ranking signals for free, and the crawler never has to spend a core-bucket
    request just to learn a repository's star count.
    """
    new = 0
    for item in items:
        if not isinstance(item, dict) or "full_name" not in item:
            continue
        # touch_crawled=False: metadata is known, but the tree is not yet
        # harvested, so the repo must stay eligible for the crawl queue.
        store.upsert_repo(
            from_api_repo(item, discovered_via=reason), touch_crawled=False
        )
        if store.enqueue(item["full_name"], reason, priority):
            new += 1
    store.commit()
    return new


async def search_repos(
    gh: GitHubClient,
    store: Store,
    base_query: str,
    *,
    since: date = date(2022, 1, 1),
    until: date | None = None,
    reason: str = "repo-search",
    priority: int = 120,
    max_pages: int = 10,
) -> tuple[int, int]:
    """Run a repository search, bisecting the date range to escape the 1000 cap.

    GitHub reports a `total_count` above 1000 but refuses to paginate past it.
    Splitting `created:` in half repeatedly yields shards that each fit under
    the cap, so their union covers everything the query matches instead of the
    first thousand. Returns (repos seen, repos newly queued).
    """
    until = until or date.today() + timedelta(days=1)
    seen: set[str] = set()
    new = 0
    stack: list[tuple[date, date]] = [(since, until)]

    while stack:
        start, end = stack.pop()
        window = f"{base_query} created:{start.isoformat()}..{end.isoformat()}"
        try:
            total, items = await _repo_search_page(gh, window)
        except NotFound as exc:
            log.debug("search rejected: %s", exc)
            continue

        if total > 1000 and (end - start).days > 1:
            mid = start + (end - start) / 2
            stack.append((start, mid))
            stack.append((mid + timedelta(days=1), end))
            continue

        new += absorb_search_items(store, items, reason, priority)
        seen.update(i["full_name"] for i in items if "full_name" in i)

        # Walk the remaining pages of a shard that fits under the cap.
        pages = min(max_pages, -(-min(total, 1000) // 100))
        for page in range(2, pages + 1):
            try:
                _, more = await _repo_search_page(gh, window, page=page)
            except NotFound:
                break
            if not more:
                break
            new += absorb_search_items(store, more, reason, priority)
            seen.update(i["full_name"] for i in more if "full_name" in i)

    log.info("%s: %d repos seen, %d newly queued", reason, len(seen), new)
    return len(seen), new


async def discover_by_topic(gh: GitHubClient, store: Store) -> int:
    total = 0
    for topic in SEED_TOPICS:
        _, new = await search_repos(
            gh, store, f"topic:{topic}", reason=f"topic:{topic}", priority=140
        )
        total += new
    return total


async def discover_by_keyword(gh: GitHubClient, store: Store) -> int:
    """Repos whose name, description, or README advertises skills."""
    total = 0
    for q in KEYWORD_QUERIES:
        _, new = await search_repos(gh, store, q, reason="keyword", priority=110)
        total += new
    return total


async def mass_discover(
    gh: GitHubClient,
    store: Store,
    *,
    target: int = 10_000,
    queries: list[str] | None = None,
    on_progress=None,
) -> int:
    """Sweep every query until the corpus reaches `target` repositories.

    Runs entirely on the search rate-limit bucket, which is separate from the
    core bucket the crawler needs — so a long discovery sweep costs the harvest
    nothing, and every repository lands with its full metadata already
    populated.
    """
    queries = queries or (
        [f"topic:{t}" for t in SEED_TOPICS] + KEYWORD_QUERIES + BREADTH_QUERIES
    )
    for i, q in enumerate(queries):
        have = store.db.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"]
        if have >= target:
            log.info("target of %d repositories reached", target)
            break
        try:
            await search_repos(gh, store, q, reason=_reason_for(q), priority=120)
        except Exception as exc:
            log.warning("query %r failed: %s", q, exc)
        if on_progress:
            on_progress(i + 1, len(queries),
                        store.db.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"])
    return store.db.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"]


def _reason_for(query: str) -> str:
    if query.startswith("topic:"):
        return query.split()[0]
    return "search"


async def discover_by_code_search(gh: GitHubClient, store: Store) -> int:
    """Find SKILL.md files directly.

    Capped at 1000 results and rate-limited to 10 requests/minute, so this is a
    seed source rather than the crawler's backbone. Sharding by file size
    multiplies the effective ceiling.
    """
    if not gh.tokens or not gh.tokens[0].value:
        log.warning("code search requires authentication; skipping")
        return 0

    size_shards = ["<2000", "2000..5000", "5000..12000", "12000..40000", ">40000"]
    found: set[str] = set()
    for shard in size_shards:
        query = f"path:**/SKILL.md size:{shard}"
        try:
            async for item in gh.paginate(
                "/search/code",
                params={"q": query, "per_page": 100},
                resource="code_search",
                max_pages=10,
            ):
                repo = (item or {}).get("repository", {}).get("full_name")
                if repo:
                    found.add(repo)
        except NotFound as exc:
            log.debug("code search shard %s rejected: %s", shard, exc)
        # Code search is 10 req/min: pace between shards rather than eat a 403.
        await asyncio.sleep(6)

    added = store.enqueue_many((n, "code-search", 150) for n in found)
    log.info("code-search: %d repos matched, %d new", len(found), added)
    return added


# ------------------------------------------------------------- awesome lists


async def mine_awesome_lists(gh: GitHubClient, store: Store, lists: list[str]) -> int:
    """Extract GitHub repo links from curated READMEs.

    Costs no REST quota (raw.githubusercontent) and routinely yields hundreds of
    high-quality candidates per list. The best request-to-discovery ratio of any
    source here.
    """
    found: set[str] = set()
    for entry in lists:
        owner, _, repo = entry.partition("/")
        if not repo:
            continue
        for branch in ("main", "master"):
            for fname in ("README.md", "readme.md"):
                text = await gh.raw_file(owner, repo, branch, fname)
                if text:
                    for m in REPO_LINK_RE.finditer(text):
                        cleaned = _clean_repo(m.group(1), m.group(2))
                        if cleaned and cleaned.lower() != entry.lower():
                            found.add(cleaned)
                    break
            else:
                continue
            break

    added = store.enqueue_many((n, "awesome-list", 130) for n in found)
    log.info("awesome-lists: %d links found, %d new", len(found), added)
    return added


# ------------------------------------------------------------ owner expansion


async def expand_productive_owners(
    gh: GitHubClient, store: Store, min_skills: int = 1, limit: int = 60
) -> int:
    """Crawl the other repos of anyone who already gave us a skill."""
    rows = store.db.execute(
        "SELECT owner, SUM(skill_count) s FROM repos WHERE skill_count >= ? "
        "GROUP BY owner ORDER BY s DESC LIMIT ?",
        (min_skills, limit),
    ).fetchall()

    found: set[str] = set()
    for row in rows:
        owner = row["owner"]
        try:
            async for repo in gh.paginate(
                f"/users/{owner}/repos",
                params={"per_page": 100, "sort": "pushed", "type": "owner"},
                max_pages=3,
            ):
                if isinstance(repo, dict) and not repo.get("fork"):
                    found.add(repo["full_name"])
        except NotFound:
            continue

    added = store.enqueue_many((n, "owner-expansion", 90) for n in found)
    log.info("owner-expansion: %d repos seen, %d new", len(found), added)
    return added


# ----------------------------------------------------------------- gharchive


async def poll_gharchive(store: Store, hours_back: int = 2) -> tuple[int, int]:
    """Read hourly public-event dumps to find updates and new candidates.

    Two distinct jobs. For repos we already index, any PushEvent means re-crawl
    (near-real-time freshness with zero API quota). For repos we do not know,
    the event payload carries no file list, so we can only shortlist by name and
    let the tree lookup confirm — cheap, because the tree call is one request.
    """
    known = {
        r["full_name"]
        for r in store.db.execute(
            "SELECT full_name FROM repos WHERE skill_count > 0"
        ).fetchall()
    }
    refreshed: set[str] = set()
    candidates: set[str] = set()

    now = datetime.now(timezone.utc) - timedelta(hours=1)  # allow publishing lag
    async with httpx.AsyncClient(
        timeout=120.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        for offset in range(hours_back):
            ts = now - timedelta(hours=offset)
            url = GHARCHIVE.format(y=ts.year, m=ts.month, d=ts.day, h=ts.hour)
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    log.debug("gharchive %s -> %s", url, resp.status_code)
                    continue
                raw = gzip.decompress(resp.content)
            except (httpx.HTTPError, OSError, gzip.BadGzipFile) as exc:
                log.warning("gharchive fetch failed for %s: %s", url, exc)
                continue

            for line in io.BytesIO(raw):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") not in ("PushEvent", "CreateEvent", "ReleaseEvent"):
                    continue
                name = (event.get("repo") or {}).get("name")
                if not name:
                    continue
                if name in known:
                    refreshed.add(name)
                elif any(h in name.lower() for h in NAME_HINTS):
                    candidates.add(name)

    for name in refreshed:
        store.enqueue(name, "gharchive-push", 200)  # freshness beats breadth
    n_new = store.enqueue_many((n, "gharchive-new", 80) for n in candidates)
    store.commit()
    log.info(
        "gharchive: %d known repos pushed, %d new name-matched candidates (%d new)",
        len(refreshed), len(candidates), n_new,
    )
    return len(refreshed), n_new


# --------------------------------------------------------------------- seeds


def load_seeds(store: Store, path: str) -> int:
    """Enqueue a hand-maintained list of repos and awesome-lists."""
    names: list[tuple[str, str, int]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line and "/" in line:
                names.append((line, "seed", 160))
    added = store.enqueue_many(names)
    log.info("seeds: %d entries, %d new", len(names), added)
    return added
