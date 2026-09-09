#!/usr/bin/env python
"""An MCP server over the skill index.

Exposes the corpus as tools an agent can call, so a model can look a skill up
instead of guessing at one:

    python mcp_server.py --db dist/skills.db

Four tools, chosen because they are the four questions an agent actually has:

    search_skills      find skills matching a description of a task
    get_skill          read one skill in full, including its body
    browse_category    list what exists in a subject, for an agent with no query
    corpus_stats       what this index contains, so the agent can say

The index is opened **read-only**. Anything reaching these tools originated in a
model's output, which is untrusted by construction; a tool that can only read
cannot be talked into writing.

Registering with a client, e.g. Claude Code:

    claude mcp add skill-engine -- /path/to/.venv/bin/python \\
        /path/to/mcp_server.py --db /path/to/dist/skills.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer

from skill_engine.search import browse, category_counts, search
from skill_engine.store import Store
from skill_engine.taxonomy import TAXONOMY

DB_PATH = "dist/skills.db"
_store: Store | None = None

# Well below what the API would allow. Tool output is pasted into a context
# window, so fifty results are not five times more useful than ten — they are
# five times dearer and usually worse.
MAX_RESULTS = 10

SUBJECTS = [c.id for c in TAXONOMY]

mcp = MCPServer(
    name="skill-engine",
    instructions=(
        "Search a corpus of AI agent skills (SKILL.md files) harvested from "
        "public GitHub repositories, ranked by a quality model that scores the "
        "skill's own craft, its repository's standing and its author's track "
        "record. Prefer searching here for an existing, proven approach before "
        "writing instructions from scratch. Always cite the repo and path of a "
        "skill you rely on, and check its licence before reproducing its text."
    ),
)


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(Path(DB_PATH), read_only=True)
    return _store


def _hit(h) -> dict:
    """One result, shaped for a model rather than for a UI.

    Deliberately omits the internal row id: an agent should address a skill by
    `repo` and `path`, which are stable and meaningful, not by an id that
    changes on every rebuild. Quality is rounded because one decimal place is
    all the ranking honestly supports.
    """
    return {
        "name": h.name,
        "description": h.description,
        "repo": h.repo,
        "path": h.path,
        "url": f"https://github.com/{h.repo}/blob/HEAD/{h.path}",
        "quality": round(h.score, 1),
        "stars": h.stars,
        "license": h.license or "unspecified",
        "also_vendored_by": h.duplicates,
    }


@mcp.tool(
    description=(
        "Search AI agent skills by what you want to accomplish. Use this when "
        "you need a proven approach to a task — extracting tables from PDFs, "
        "reviewing Terraform for security, building an MCP server — rather "
        "than writing instructions from scratch. Describe the task in plain "
        "words; this is full-text search over skill names, descriptions and "
        "bodies, ranked by quality. Returns metadata only: follow up with "
        "get_skill to read the instructions."
    )
)
def search_skills(query: str, limit: int = 5, min_stars: int = 0) -> dict:
    """Find skills matching a task description.

    Args:
        query: what the skill should do, e.g. "extract tables from a pdf"
        limit: how many results, 1-10
        min_stars: only skills from repositories with at least this many stars
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 5), MAX_RESULTS))
    filters = {"min_stars": int(min_stars)} if min_stars else None
    hits = search(store(), q, limit=n, filters=filters)
    if not hits:
        return {"query": q, "count": 0, "results": [],
                "hint": "no match — try fewer or more common words"}
    return {"query": q, "count": len(hits), "results": [_hit(h) for h in hits]}


@mcp.tool(
    description=(
        "Read one skill in full, including its instructions. Call this after "
        "search_skills or browse_category, using the repo and path they "
        "returned."
    )
)
def get_skill(repo: str, path: str) -> dict:
    """Fetch the full text of a single skill.

    Args:
        repo: owner/name, e.g. "anthropics/skills"
        path: path within the repository, e.g. "pdf/SKILL.md"
    """
    if not repo or not path:
        return {"error": "both repo and path are required"}
    row = store().db.execute(
        "SELECT name, description, body, license, score FROM skills "
        "WHERE repo = ? AND path = ?", (repo, path)
    ).fetchone()
    if row is None:
        return {"error": f"no skill at {repo}/{path}",
                "hint": "call search_skills first and use the repo and path "
                        "it returns verbatim"}
    return {
        "name": row["name"],
        "description": row["description"],
        "repo": repo,
        "path": path,
        "url": f"https://github.com/{repo}/blob/HEAD/{path}",
        "license": row["license"] or "unspecified",
        "quality": round(row["score"], 1),
        "body": row["body"],
    }


@mcp.tool(
    description=(
        "List the best skills in a subject area. Use this when you have no "
        "specific query and want to see what exists — for example to answer "
        "'what sort of thing can you find?'."
    )
)
def browse_category(category: str, limit: int = 8) -> dict:
    """List top skills in one subject.

    Args:
        category: one of the subject ids reported by corpus_stats
        limit: how many results, 1-10
    """
    cat = (category or "").strip()
    if cat not in set(SUBJECTS):
        return {"error": f"unknown category '{cat}'", "valid": sorted(SUBJECTS)}
    n = max(1, min(int(limit or 8), MAX_RESULTS))
    hits, total = browse(store(), cat, limit=n)
    return {"category": cat, "showing": len(hits), "total_in_category": total,
            "results": [_hit(h) for h in hits]}


@mcp.tool(
    description=(
        "What this index contains — how many skills, repositories and authors, "
        "and the subject breakdown. Use it to tell the user what can and "
        "cannot be searched here."
    )
)
def corpus_stats() -> dict:
    """Report the size and shape of the indexed corpus."""
    db = store().db
    one = lambda s: db.execute(s).fetchone()[0]  # noqa: E731
    cats = category_counts(store())
    return {
        "skills": one("SELECT COUNT(*) FROM skills"),
        "unique_skills": one("SELECT COUNT(DISTINCT content_hash) FROM skills"),
        "repositories": one("SELECT COUNT(*) FROM repos WHERE skill_count > 0"),
        "authors": one("SELECT COUNT(*) FROM authors"),
        "categories": {k: v["total"] for k, v in cats.items()},
        "source": "public GitHub repositories",
    }


def main() -> int:
    global DB_PATH
    ap = argparse.ArgumentParser(description="MCP server over the skill index")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()
    DB_PATH = args.db
    if not Path(DB_PATH).exists():
        print(f"no index at {DB_PATH}", file=sys.stderr)
        return 1
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
