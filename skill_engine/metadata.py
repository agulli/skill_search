"""Normalise repository metadata from GitHub's several differently-shaped sources.

Three endpoints return "a repository" with different field sets and different
names for the same thing:

* **Repository search** (`/search/repositories`) — nearly the full REST shape,
  and *free of core rate-limit cost* because search has its own bucket. This is
  the single best metadata source for bulk discovery: 100 fully-populated repos
  per request.
* **The repo endpoint** (`/repos/{o}/{r}`) — adds `subscribers_count`, the real
  watcher count, which search omits.
* **GraphQL** — different names again (`stargazerCount`, `diskUsage`), but 100
  repositories per request for ~1 point of a 5,000/hour budget.

Everything funnels through `from_api_repo` / `from_graphql_repo` so the store
and the ranker only ever see one vocabulary.

### A note on "usage metrics"

GitHub's API does **not** expose what people usually mean by usage:

* **Dependents / "Used by"** is rendered in HTML only. There is no API for it.
* **Traffic** (`/traffic/views`, `/traffic/clones`) requires *push* access, so
  it is unavailable for repositories you do not own.
* **Package downloads** exist only for repos publishing to a registry.

What is available, and what the ranker therefore uses, are proxies: stars,
forks, subscribers, open issues, contributor count, release cadence, and the
*derivatives* of those over the repository's lifetime. `stars_per_day` and
`fork_ratio` carry real signal precisely because they are ratios the raw counts
cannot express.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _iso(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400.0)


def from_api_repo(item: dict, *, discovered_via: str = "") -> dict:
    """Map a REST repository object (search item or full repo) to our schema."""
    lic = item.get("license") or {}
    spdx = lic.get("spdx_id") if isinstance(lic, dict) else None
    owner = item.get("owner") or {}

    meta: dict[str, Any] = {
        "full_name": item["full_name"],
        "owner_type": owner.get("type"),
        "default_branch": item.get("default_branch") or "main",
        "description": item.get("description"),
        "homepage": item.get("homepage") or None,
        "language": item.get("language"),
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "open_issues": item.get("open_issues_count", 0),
        "size_kb": item.get("size", 0),
        "license": None if spdx in (None, "NOASSERTION") else spdx,
        "topics": item.get("topics") or [],
        "created_at": _iso(item.get("created_at")),
        "updated_at": _iso(item.get("updated_at")),
        "pushed_at": _iso(item.get("pushed_at")),
        "is_fork": bool(item.get("fork")),
        "archived": bool(item.get("archived")),
        "disabled": bool(item.get("disabled")),
        "is_template": bool(item.get("is_template")),
        "has_issues": bool(item.get("has_issues")),
        "has_wiki": bool(item.get("has_wiki")),
        "has_pages": bool(item.get("has_pages")),
        "has_discussions": bool(item.get("has_discussions")),
    }
    # Only the full repo endpoint carries the true watcher count; search does
    # not, and `watchers_count` there is an alias of stars, so it is useless.
    if "subscribers_count" in item:
        meta["subscribers"] = item["subscribers_count"]
    if discovered_via:
        meta["discovered_via"] = discovered_via
    return meta


def from_graphql_repo(node: dict, *, discovered_via: str = "") -> dict:
    """Map a GraphQL repository node to our schema."""
    lic = (node.get("licenseInfo") or {}).get("spdxId")
    topics = [
        n["topic"]["name"]
        for n in (node.get("repositoryTopics") or {}).get("nodes", [])
        if n.get("topic")
    ]
    releases = node.get("releases") or {}
    latest = (releases.get("nodes") or [{}])
    meta = {
        "full_name": node["nameWithOwner"],
        "owner_type": "Organization" if (node.get("owner") or {}).get(
            "__typename") == "Organization" else "User",
        "default_branch": (node.get("defaultBranchRef") or {}).get("name") or "main",
        "description": node.get("description"),
        "homepage": node.get("homepageUrl") or None,
        "language": (node.get("primaryLanguage") or {}).get("name"),
        "stars": node.get("stargazerCount", 0),
        "forks": node.get("forkCount", 0),
        "subscribers": (node.get("watchers") or {}).get("totalCount"),
        "open_issues": (node.get("issues") or {}).get("totalCount", 0),
        "size_kb": node.get("diskUsage") or 0,
        "license": None if lic in (None, "NOASSERTION") else lic,
        "topics": topics,
        "created_at": _iso(node.get("createdAt")),
        "updated_at": _iso(node.get("updatedAt")),
        "pushed_at": _iso(node.get("pushedAt")),
        "is_fork": bool(node.get("isFork")),
        "archived": bool(node.get("isArchived")),
        "disabled": bool(node.get("isDisabled")),
        "is_template": bool(node.get("isTemplate")),
        "has_issues": bool(node.get("hasIssuesEnabled")),
        "has_wiki": bool(node.get("hasWikiEnabled")),
        "has_discussions": bool(node.get("hasDiscussionsEnabled")),
        "releases": releases.get("totalCount"),
        "latest_release": _iso((latest[0] or {}).get("publishedAt")) if latest else None,
    }
    if discovered_via:
        meta["discovered_via"] = discovered_via
    return meta


GRAPHQL_REPO_FIELDS = """
  nameWithOwner description homepageUrl createdAt updatedAt pushedAt
  stargazerCount forkCount diskUsage
  isFork isArchived isDisabled isTemplate
  hasIssuesEnabled hasWikiEnabled hasDiscussionsEnabled
  owner { __typename }
  primaryLanguage { name }
  defaultBranchRef { name }
  licenseInfo { spdxId }
  watchers { totalCount }
  issues(states: OPEN) { totalCount }
  releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) {
    totalCount nodes { publishedAt }
  }
  repositoryTopics(first: 20) { nodes { topic { name } } }
"""
