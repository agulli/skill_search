"""Harvesting: turn a repository into indexed skills.

The cost model is what matters here. Per repository:

* **1 request** for metadata (or 0, on a 304).
* **1 request** for the *entire* recursive file tree — this is the move code
  search cannot match. `GET /git/trees/{branch}?recursive=1` returns every path
  in the repo along with each file's blob SHA.
* **0 requests** for any SKILL.md whose blob SHA is unchanged since last crawl.
* **1 raw fetch** (off the REST rate limit entirely) per genuinely changed file.

So a steady-state re-crawl of a repo that has not moved costs a single
conditional request that returns 304 and consumes no quota at all. That is the
difference between a crawler that scales to the whole ecosystem and one that
burns 5,000 requests on a few hundred repos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time


from typing import Any

from .config import Config
from .github import GitHubClient, NotFound
from .metadata import GRAPHQL_REPO_FIELDS, from_api_repo, from_graphql_repo
from .parse import classify_path, parse_skill, skill_slug_from_path
from .store import Store

log = logging.getLogger("skill_engine.harvest")

SKILL_FILENAME = "skill.md"
# Directories worth a targeted walk when a recursive tree comes back truncated.
FALLBACK_DIRS = ["skills", ".claude/skills", ".agent/skills", "plugins", ".cursor/skills"]


def _is_skill_path(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower() == SKILL_FILENAME


async def fetch_repo_meta(gh: GitHubClient, full_name: str) -> dict | None:
    """Repository metadata, conditionally. None means 'unchanged since last time'.

    This is the only source of `subscribers_count` (the true watcher count),
    which repository search omits.
    """
    data = await gh.get_json(f"/repos/{full_name}", etag_key=f"repo:{full_name}")
    if data is None:
        return None
    return from_api_repo(data)


async def enrich_repo(gh: GitHubClient, full_name: str) -> dict:
    """Fetch signals that cost an extra request each.

    Contributor count and release count are strong quality signals — a skill
    collection with fifteen contributors and tagged releases is a different
    proposition from a one-commit dump — but neither is included in any bulk
    endpoint. We get the contributor count from the `Link` header of a
    1-per-page request rather than by paginating the whole list, which turns an
    unbounded walk into a single call.
    """
    out: dict[str, Any] = {"full_name": full_name}
    try:
        resp = await gh.request(
            "GET", f"/repos/{full_name}/contributors",
            params={"per_page": 1, "anon": "true"},
            etag_key=f"contrib:{full_name}",
        )
        if resp is not None:
            last = _last_page(resp.headers.get("link", ""))
            out["contributors"] = last if last else len(resp.json() or [])
    except NotFound:
        pass

    try:
        releases = await gh.get_json(
            f"/repos/{full_name}/releases", params={"per_page": 1},
            etag_key=f"rel:{full_name}",
        )
        if releases:
            out["latest_release"] = releases[0].get("published_at")
    except NotFound:
        pass
    return out


def _last_page(link_header: str) -> int | None:
    """Total count from a paginated endpoint's rel="last" URL."""
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) >= 2 and 'rel="last"' in section[1]:
            url = section[0].strip().strip("<>")
            if "page=" in url:
                try:
                    return int(url.rsplit("page=", 1)[1].split("&")[0])
                except ValueError:
                    return None
    return None


async def fetch_repo_meta_batch(gh: GitHubClient, names: list[str]) -> dict[str, dict]:
    """Fetch metadata for up to 100 repos in one GraphQL request.

    A REST crawl spends one request per repository just to learn its default
    branch and star count. GraphQL aliases let us ask for 100 at once for
    roughly 1 point of the 5,000/hour budget — a ~100x saving on the metadata
    leg of the crawl.
    """
    if not names:
        return {}
    if not gh.tokens or not gh.tokens[0].value:
        return {}  # GraphQL requires authentication

    fragments = []
    variables: dict[str, str] = {}
    for i, full_name in enumerate(names[:100]):
        owner, _, repo = full_name.partition("/")
        variables[f"o{i}"] = owner
        variables[f"n{i}"] = repo
        fragments.append(
            f"r{i}: repository(owner: $o{i}, name: $n{i}) {{{GRAPHQL_REPO_FIELDS}}}"
        )
    decl = ", ".join(f"$o{i}: String!, $n{i}: String!" for i in range(len(names[:100])))
    query = f"query({decl}) {{ {' '.join(fragments)} }}"

    data = await gh.graphql(query, variables)
    out: dict[str, dict] = {}
    for node in (data or {}).values():
        if node and node.get("nameWithOwner"):
            out[node["nameWithOwner"]] = from_graphql_repo(node)
    return out


async def list_skill_paths(
    gh: GitHubClient, full_name: str, branch: str
) -> tuple[list[dict], str | None, bool]:
    """One request for the whole tree. Returns (skill entries, tree sha, cached)."""
    try:
        tree = await gh.get_json(
            f"/repos/{full_name}/git/trees/{branch}",
            params={"recursive": "1"},
            etag_key=f"tree:{full_name}:{branch}",
        )
    except NotFound:
        return [], None, False

    if tree is None:
        return [], None, True  # 304: the tree is byte-identical to last crawl

    entries = [
        e for e in tree.get("tree", [])
        if e.get("type") == "blob" and _is_skill_path(e.get("path", ""))
    ]

    if tree.get("truncated"):
        # Huge monorepo: the recursive listing was cut off. Walk the directories
        # that conventionally hold skills instead of giving up on the repo.
        log.info("%s: tree truncated, walking conventional dirs", full_name)
        seen = {e["path"] for e in entries}
        for extra in await _walk_targeted(gh, full_name, branch):
            if extra["path"] not in seen:
                entries.append(extra)
                seen.add(extra["path"])

    return entries, tree.get("sha"), False


async def _walk_targeted(gh: GitHubClient, full_name: str, branch: str) -> list[dict]:
    """Breadth-first walk of known skill directories, bounded to stay cheap.

    Each frontier item is (tree-ish ref, path prefix) so we can rebuild the full
    repo-relative path — a subtree listing only reports names relative to itself.
    """
    found: list[dict] = []
    frontier: list[tuple[str, str]] = [
        (f"{branch}:{d}", d) for d in FALLBACK_DIRS
    ]
    budget = 40

    while frontier and budget > 0:
        ref, prefix = frontier.pop(0)
        budget -= 1
        try:
            tree = await gh.get_json(f"/repos/{full_name}/git/trees/{ref}")
        except NotFound:
            continue  # that conventional directory simply does not exist here
        if not tree:
            continue
        for entry in tree.get("tree", []):
            name = entry.get("path", "")
            full_path = f"{prefix}/{name}" if prefix else name
            if entry.get("type") == "blob" and name.lower() == SKILL_FILENAME:
                found.append(dict(entry, path=full_path))
            elif entry.get("type") == "tree" and len(frontier) < budget:
                frontier.append((entry["sha"], full_path))
    return found


async def harvest_repo(
    gh: GitHubClient,
    store: Store,
    full_name: str,
    cfg: Config,
    *,
    meta: dict | None = None,
    discovered_via: str = "",
) -> int:
    """Index one repository. Returns the number of skills found."""
    cached = store.get_repo(full_name)

    if meta is None:
        try:
            meta = await fetch_repo_meta(gh, full_name)
        except NotFound as exc:
            store.db.execute(
                "UPDATE repos SET last_crawled = ?, last_error = ? WHERE full_name = ?",
                (time.time(), str(exc), full_name),
            )
            store.dequeue(full_name)
            store.commit()
            return 0
        if meta is None:  # 304: our stored metadata is still current
            if cached is None:
                return 0
            meta = {"full_name": full_name,
                    "default_branch": cached["default_branch"] or "main"}

    if discovered_via:
        meta.setdefault("discovered_via", discovered_via)
    store.upsert_repo(meta)

    branch = meta.get("default_branch") or "main"
    entries, tree_sha, unchanged = await list_skill_paths(gh, full_name, branch)

    if unchanged:
        # Nothing in the repo moved. This path costs zero rate-limit quota.
        store.mark_repo(full_name, error=None)
        store.dequeue(full_name)
        store.commit()
        return cached["skill_count"] if cached else 0

    if not entries:
        store.mark_repo(full_name, tree_sha=tree_sha, skill_count=0, error=None)
        store.delete_missing_skills(full_name, set())
        store.dequeue(full_name)
        store.commit()
        return 0

    # Record what the repository *actually* holds before any truncation. The
    # cap used to be applied first and the truncated length stored as
    # skill_count, which silently defeated the aggregator-dump penalty: a repo
    # with 2,118 SKILL.md files recorded 400, sailed under the 500 threshold,
    # and scored as though it were a curated collection.
    total_skill_files = len(entries)
    if total_skill_files > cfg.max_skills_per_repo:
        log.warning(
            "%s: %d SKILL.md files, fetching the first %d",
            full_name, total_skill_files, cfg.max_skills_per_repo,
        )
        entries = entries[: cfg.max_skills_per_repo]

    known_shas = store.known_blob_shas(full_name)
    owner, _, repo_name = full_name.partition("/")

    # The blob SHA is git's own content hash: unchanged SHA means unchanged file,
    # so we can skip the fetch entirely and just record that we still see it.
    changed = [e for e in entries if known_shas.get(e["path"]) != e.get("sha")]
    changed_paths = {e["path"] for e in changed}
    for entry in entries:
        if entry["path"] not in changed_paths:
            store.touch_skill(full_name, entry["path"])

    async def ingest(entry: dict) -> bool:
        path = entry["path"]
        if entry.get("size", 0) > cfg.max_skill_bytes:
            return False
        text = await gh.raw_file(owner, repo_name, branch, path)
        if text is None:
            return False

        parsed = parse_skill(text, path)
        # Frontmatter is authoritative, but a missing name is recoverable from
        # the directory the skill lives in — that is how runtimes address it.
        display_name = parsed.name or skill_slug_from_path(path)
        # Scoring is deliberately NOT done here. Percentile normalisation needs
        # the whole corpus, and a score computed mid-crawl would be measured
        # against a distribution that no longer exists once the crawl finishes.
        # `ranking.recompute` assigns real scores in one pass afterwards.
        store.upsert_skill({
            "repo": full_name,
            "path": path,
            "name": display_name,
            "description": parsed.description,
            "body": parsed.body[:200_000],
            "heading": parsed.heading,
            "version": parsed.version,
            "license": parsed.license or meta.get("license"),
            "allowed_tools": json.dumps(parsed.allowed_tools),
            "metadata": json.dumps({**parsed.metadata, **parsed.extra}, default=str),
            "resources": json.dumps(parsed.resources),
            "source_kind": classify_path(path),
            "blob_sha": entry.get("sha", ""),
            "content_hash": parsed.content_hash,
            "body_len": parsed.body_len,
            "score": 0.0,
            "valid": int(parsed.valid),
            "invalid_reason": parsed.invalid_reason,
            "warnings": parsed.notes,
        })
        return True

    results = await asyncio.gather(*(ingest(e) for e in changed), return_exceptions=True)
    for res in results:
        if isinstance(res, Exception):
            log.debug("%s: ingest error %s", full_name, res)

    if total_skill_files <= cfg.max_skills_per_repo:
        # Only prune when we saw the whole tree; otherwise the entries beyond
        # the cap would be deleted as though the repo had dropped them.
        store.delete_missing_skills(full_name, {e["path"] for e in entries})
    store.mark_repo(
        full_name, tree_sha=tree_sha, skill_count=total_skill_files, error=None
    )
    store.dequeue(full_name)
    store.commit()

    fetched = sum(1 for r in results if r is True)
    capped = (
        f", {total_skill_files - len(entries)} beyond cap"
        if total_skill_files > len(entries) else ""
    )
    log.info(
        "%s: %d skills (%d refetched, %d unchanged%s)",
        full_name, len(entries), fetched, len(entries) - len(changed), capped,
    )
    return len(entries)


async def run_crawl(
    gh: GitHubClient, store: Store, cfg: Config, limit: int, batch: int = 100,
    strategy: str = "score",
) -> dict:
    """Drain the queue, batching metadata through GraphQL where possible."""
    totals = {"repos": 0, "skills": 0, "errors": 0}

    while totals["repos"] < limit:
        take = min(batch, limit - totals["repos"])
        rows = store.take(
            take, refresh_after_hours=cfg.refresh_hours, strategy=strategy
        )
        if not rows:
            break

        names = [r["full_name"] for r in rows]
        reasons = {r["full_name"]: r["reason"] for r in rows}
        try:
            metas = await fetch_repo_meta_batch(gh, names)
        except Exception as exc:  # GraphQL is an optimisation, not a dependency
            log.warning("graphql batch failed (%s); falling back to REST", exc)
            metas = {}

        for name in names:
            try:
                found = await harvest_repo(
                    gh, store, name, cfg,
                    meta=metas.get(name),
                    discovered_via=reasons.get(name, ""),
                )
                totals["skills"] += found
            except NotFound:
                store.dequeue(name)
                totals["errors"] += 1
            except Exception as exc:
                log.warning("%s: %s", name, exc)
                store.mark_repo(name, error=str(exc)[:200])
                totals["errors"] += 1
            totals["repos"] += 1

        store.commit()
        log.info(
            "progress: %d repos, %d skills | %s",
            totals["repos"], totals["skills"], gh.quota_summary(),
        )

    # Scores are corpus-relative, so they are assigned in one pass here rather
    # than per-repo during the crawl.
    from .ranking import recompute

    recompute(store)
    return totals
