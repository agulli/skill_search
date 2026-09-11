#!/usr/bin/env python
"""Multi-partition repository discovery pipeline.

Executes prioritized search queries across GitHub topics, paths, and keywords.
Applies recursive date bisection to bypass the 1,000-result search limit and
maximize coverage of new agent skill repositories.

Usage:
    python discover_hard.py data/scale.db
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.config import Config
from skill_engine.discover import (
    BREADTH_QUERIES,
    KEYWORD_QUERIES,
    SCALE_QUERIES,
    search_repos,
)
from skill_engine.github import GitHubClient
from skill_engine.store import Store


if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
    print(__doc__.strip())
    sys.exit(0)

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scale.db"



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("skill_engine.github").setLevel(logging.WARNING)
log = logging.getLogger("discover")

CORE_PATTERNS = [
    "SKILL.md in:path",
    "skills in:path",
    ".agents in:path",
    ".gemini in:path",
    ".claude in:path",
    "agent skill",
    "gemini agent skill",
    "claude skill",
    "claude code skill",
    "agent skills",
    "skill manifest",
    "mcp server",
    "subagent",
    "ai agent tool",
    "llm tool definition",
    "prompt library",
]

TOPICS = [
    "agent-skills",
    "gemini-skills",
    "antigravity-skills",
    "claude",
    "claude-ai",
    "claude-code",
    "claude-skills",
    "ai-agents",
    "llm-agent",
    "mcp",
    "mcp-server",
    "anthropic",
    "prompt-engineering",
    "agent-framework",
    "ai-tools",
    "llm-tools",
    "autonomous-agents",
    "agentic-ai",
    "copilot",
    "cursor",
]


def query_plan() -> list[str]:
    """Builds a deduplicated list of productive search query strings."""
    queries: list[str] = []
    queries += CORE_PATTERNS
    queries += [f"topic:{t}" for t in TOPICS]
    queries += list(KEYWORD_QUERIES) + list(SCALE_QUERIES)

    seen: set[str] = set()
    ordered_plan: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            ordered_plan.append(q)
    return ordered_plan


async def main() -> int:
    cfg = Config(db_path=Path(DB))
    store = Store(Path(DB))
    plan = query_plan()
    log.info(
        "Loaded %d distinct discovery queries; %d repositories currently indexed",
        len(plan),
        store.db.execute("SELECT COUNT(*) FROM repos").fetchone()[0],
    )

    started = time.time()
    total_new = 0
    async with GitHubClient(cfg.tokens, concurrency=cfg.concurrency) as gh:
        for cycle in range(1, 100):
            # Reopen per cycle. A connection held across cycles pins the WAL
            # snapshot, so SQLite cannot checkpoint and the log grows without
            # bound — measured at 33.7 GB after eight hours, which stopped
            # writes entirely. Closing between cycles lets a checkpoint run.
            if cycle > 1:
                store.close()
                store = Store(Path(DB))
                store.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            for i, q in enumerate(plan, 1):
                try:
                    seen, new = await search_repos(
                        gh,
                        store,
                        q,
                        since=date(2021, 1, 1),
                        reason="discover-hard",
                        # Level with the other proven sources. The earlier
                        # demotion to 110 targeted the broad `language:` and
                        # `stars:` cross-products, which yielded 0.64
                        # skills/repo and monopolised the queue. Those queries
                        # are gone; what remains measured 87-90% productive, and
                        # holding it below the sweep's reach starves the sweep
                        # instead of protecting it.
                        priority=130,
                    )
                except Exception as exc:
                    log.warning("Query %r failed: %s: %s", q[:40],
                                type(exc).__name__, exc)
                    continue
                total_new += new
                if new or i % 10 == 0:
                    rate = total_new / max((time.time() - started) / 3600, 1e-6)
                    log.info(
                        "Cycle %d [%d/%d] %-38s (Seen: %4d, New: %4d) | Total: +%d (%.0f/hr)",
                        cycle,
                        i,
                        len(plan),
                        q[:38],
                        seen,
                        new,
                        total_new,
                        rate,
                    )
            log.info("=== Discovery Cycle %d complete (+%d new repos) ===", cycle, total_new)
    store.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
