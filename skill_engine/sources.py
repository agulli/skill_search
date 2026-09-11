"""Multi-forge ingestion connectors for GitLab and Hugging Face repositories."""


from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
from typing import Any, Iterable

import httpx

from .config import USER_AGENT
from .parse import classify_path, parse_skill, skill_slug_from_path

log = logging.getLogger(__name__)

SKILL_NAMES = ("SKILL.md", "skill.md")
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_REPO = 400


def looks_like_skill(path: str) -> bool:
    tail = path.rsplit("/", 1)[-1]
    return tail in SKILL_NAMES or "/.claude/skills/" in f"/{path}"


def _store_skill(store, host: str, full_name: str, path: str, text: str,
                 url: str, license_: str | None = None) -> bool:
    """Parse and store one file. Returns True if it was stored."""
    parsed = parse_skill(text, path)
    store.upsert_skill({
        "repo": full_name,
        "path": path,
        "name": parsed.name or skill_slug_from_path(path),
        "description": parsed.description,
        "body": parsed.body[:200_000],
        "heading": parsed.heading,
        "version": parsed.version,
        "license": parsed.license or license_,
        "allowed_tools": json.dumps(parsed.allowed_tools),
        "metadata": json.dumps({**parsed.metadata, **parsed.extra,
                                "host": host, "url": url}, default=str),
        "resources": json.dumps(parsed.resources),
        "source_kind": classify_path(path),
        "blob_sha": "",
        "content_hash": parsed.content_hash,
        "body_len": parsed.body_len,
        "score": 0.0,
        "valid": int(parsed.valid),
        "invalid_reason": parsed.invalid_reason,
        "warnings": parsed.notes,
    })
    return True


def _ensure_repo(store, host: str, full_name: str, meta: dict[str, Any]) -> None:
    """Create or update the repository row a skill hangs off."""
    store.ensure_repo_stub(full_name, discovered_via=meta.get("via", host))
    store.db.execute(
        "UPDATE repos SET host = ?, description = COALESCE(?, description), "
        "stars = COALESCE(?, stars), license = COALESCE(?, license), "
        "homepage = COALESCE(?, homepage), updated_at = COALESCE(?, updated_at) "
        "WHERE full_name = ?",
        (host, meta.get("description"), meta.get("stars"), meta.get("license"),
         meta.get("homepage"), meta.get("updated_at"), full_name),
    )


def _skills_from_tar(blob: bytes) -> Iterable[tuple[str, str]]:
    """Yield (path, text) for skill files inside a .tar.gz archive."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        found = 0
        for member in tar:
            if not member.isfile() or member.size > 1_000_000:
                continue
            # Archives are wrapped in a top-level directory; strip it so paths
            # match what the GitHub crawler records for the same repository.
            path = member.name.split("/", 1)[-1] if "/" in member.name else member.name
            if not looks_like_skill(path):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                yield path, fh.read().decode("utf-8", "replace")
            except Exception:                       # unreadable member
                continue
            found += 1
            if found >= MAX_FILES_PER_REPO:
                return


# --------------------------------------------------------------- GitLab

GITLAB_API = "https://gitlab.com/api/v4"


async def gitlab_discover(client: httpx.AsyncClient, terms: list[str],
                          per_term: int = 100) -> list[dict]:
    """Public GitLab projects matching skill-ish search terms."""
    seen: dict[str, dict] = {}
    for term in terms:
        try:
            r = await client.get(
                f"{GITLAB_API}/projects",
                params={"search": term, "per_page": min(per_term, 100),
                        "order_by": "last_activity_at", "simple": "false",
                        "archived": "false"},
            )
            if r.status_code != 200:
                log.warning("gitlab search %r: HTTP %s", term, r.status_code)
                continue
            for p in r.json():
                seen.setdefault(p["path_with_namespace"], p)
        except Exception as exc:
            log.warning("gitlab search %r failed: %s: %s", term,
                        type(exc).__name__, exc)
    return list(seen.values())


async def gitlab_harvest(client: httpx.AsyncClient, store, project: dict) -> int:
    """Download one project's archive and store any skills in it."""
    pid = project["id"]
    slug = project["path_with_namespace"]
    full_name = f"gitlab.com/{slug}"
    try:
        r = await client.get(f"{GITLAB_API}/projects/{pid}/repository/archive.tar.gz",
                             follow_redirects=True)
        if r.status_code != 200 or len(r.content) > MAX_ARCHIVE_BYTES:
            return 0
        files = list(_skills_from_tar(r.content))
    except Exception as exc:
        log.debug("gitlab archive %s: %s", slug, type(exc).__name__)
        return 0
    if not files:
        return 0

    lic = (project.get("license") or {}).get("nickname") if project.get("license") else None
    _ensure_repo(store, "gitlab.com", full_name, {
        "via": "gitlab", "description": project.get("description"),
        "stars": project.get("star_count"), "license": lic,
        "homepage": project.get("web_url"),
        "updated_at": project.get("last_activity_at"),
    })
    branch = project.get("default_branch") or "HEAD"
    for path, text in files:
        _store_skill(store, "gitlab.com", full_name, path, text,
                     f"{project.get('web_url')}/-/blob/{branch}/{path}", lic)
    store.mark_repo(full_name, tree_sha=f"gl:{len(r.content)}",
                    skill_count=len(files), error=None)
    store.dequeue(full_name)
    store.commit()
    return len(files)


# --------------------------------------------------------- Hugging Face

HF_API = "https://huggingface.co/api"


async def hf_discover(client: httpx.AsyncClient, terms: list[str],
                      kinds: tuple[str, ...] = ("spaces", "models"),
                      per_term: int = 100) -> list[dict]:
    """Hub repositories whose card or id suggests agent skills."""
    seen: dict[str, dict] = {}
    for kind in kinds:
        for term in terms:
            try:
                r = await client.get(f"{HF_API}/{kind}",
                                     params={"search": term, "limit": per_term,
                                             "full": "true"})
                if r.status_code != 200:
                    log.warning("hf %s %r: HTTP %s", kind, term, r.status_code)
                    continue
                for item in r.json():
                    item["_kind"] = kind
                    seen.setdefault(f"{kind}:{item['id']}", item)
            except Exception as exc:
                log.warning("hf %s %r failed: %s: %s", kind, term,
                            type(exc).__name__, exc)
    return list(seen.values())


async def hf_harvest(client: httpx.AsyncClient, store, item: dict) -> int:
    """Fetch skill files a Hub repository lists in its siblings."""
    kind, rid = item.get("_kind", "models"), item["id"]
    # `siblings` is the Hub's own file listing, so candidate paths are known
    # before a single file is fetched — no archive download, no guessing.
    paths = [s["rfilename"] for s in (item.get("siblings") or [])
             if looks_like_skill(s.get("rfilename", ""))][:MAX_FILES_PER_REPO]
    if not paths:
        return 0

    prefix = "" if kind == "models" else f"{kind.rstrip('s')}s/"
    web = f"https://huggingface.co/{prefix}{rid}"
    raw_base = f"https://huggingface.co/{prefix}{rid}/resolve/main"
    full_name = f"huggingface.co/{rid}"
    lic = (item.get("cardData") or {}).get("license")

    stored = 0
    for path in paths:
        try:
            r = await client.get(f"{raw_base}/{path}", follow_redirects=True)
            if r.status_code != 200 or len(r.content) > 1_000_000:
                continue
            text = r.text
        except Exception:
            continue
        if stored == 0:
            _ensure_repo(store, "huggingface.co", full_name, {
                "via": f"huggingface-{kind}",
                "description": (item.get("cardData") or {}).get("summary"),
                "stars": item.get("likes"), "license": lic, "homepage": web,
                "updated_at": item.get("lastModified"),
            })
        _store_skill(store, "huggingface.co", full_name, path, text,
                     f"{web}/blob/main/{path}", lic)
        stored += 1

    if stored:
        store.mark_repo(full_name, tree_sha=f"hf:{stored}",
                        skill_count=stored, error=None)
        store.dequeue(full_name)
        store.commit()
    return stored


# ------------------------------------------------------------------ run

TERMS = ["claude skill", "agent skills", "skill.md", "ai agent skill",
         "claude-code", "mcp server"]


async def crawl(store, which: str = "both", terms: list[str] | None = None,
                limit: int = 400, concurrency: int = 4) -> dict:
    """Discover and harvest from the non-GitHub forges."""
    terms = terms or TERMS
    totals = {"gitlab_repos": 0, "gitlab_skills": 0,
              "hf_repos": 0, "hf_skills": 0}
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    ) as client:

        async def guarded(fn, item):
            async with sem:
                try:
                    return await fn(client, store, item)
                except Exception as exc:
                    log.warning("%s failed: %s: %s", fn.__name__,
                                type(exc).__name__, exc)
                    return 0

        if which in ("both", "gitlab"):
            projects = (await gitlab_discover(client, terms))[:limit]
            log.info("gitlab: %d candidate projects", len(projects))
            for n in await asyncio.gather(*(guarded(gitlab_harvest, p)
                                            for p in projects)):
                totals["gitlab_skills"] += n
                totals["gitlab_repos"] += 1 if n else 0

        if which in ("both", "hf"):
            items = (await hf_discover(client, terms))[:limit]
            log.info("huggingface: %d candidate repositories", len(items))
            for n in await asyncio.gather(*(guarded(hf_harvest, i)
                                            for i in items)):
                totals["hf_skills"] += n
                totals["hf_repos"] += 1 if n else 0

    return totals
