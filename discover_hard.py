#!/usr/bin/env python
"""Aggressive repository discovery — the only path that is not saturated.

Measured before writing this: mining GH Archive for windows older than about a
week yields *zero* new candidates, because the earlier crawl already covered
them. Repository search, by contrast, still returns 29% repositories we have
never seen. Discovery, not harvesting, is the bottleneck, and search is where
the remaining headroom is.

The 1,000-result cap is escaped two ways at once. `search_repos` bisects
`created:` ranges, which covers one query completely. This module adds the
other half: many queries that *partition the space differently* — by language,
by star band, by topic, by filename — so that repositories missed by one
framing are caught by another. A query returning only repositories we already
know costs a request and teaches us nothing; the run reports novelty per query
so the useless framings are visible rather than assumed.
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
from skill_engine.discover import (BREADTH_QUERIES, KEYWORD_QUERIES,
                                   SCALE_QUERIES, search_repos)
from skill_engine.github import GitHubClient
from skill_engine.store import Store

DB = sys.argv[1] if len(sys.argv) > 1 else "data/scale.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s D %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("skill_engine.github").setLevel(logging.WARNING)
log = logging.getLogger("discover")

# Language partitions. The same concept ("agent skill") is published in
# different ecosystems, and a language filter reaches repositories that rank
# too low to surface in an unfiltered query.
LANGS = ["Python", "TypeScript", "JavaScript", "Go", "Rust", "Java", "Ruby",
         "Shell", "Markdown", "Jupyter Notebook", "C#", "PHP", "Kotlin", "Swift"]

# Star bands. Sorting by stars only ever shows the head; banding forces the
# long tail into view, and the tail is where undiscovered repositories live.
STARS = ["stars:0", "stars:1..2", "stars:3..5", "stars:6..15", "stars:16..50",
         "stars:51..200", "stars:>200"]

CORE = ["SKILL.md in:path", "skills in:path", ".claude in:path",
        "agent skill", "claude skill", "claude code skill", "agent skills",
        "skill manifest", "mcp server", "subagent", "claude plugin",
        "ai agent tool", "llm tool definition", "prompt library"]

TOPICS = ["claude", "claude-ai", "claude-code", "claude-skills", "agent-skills",
          "ai-agents", "llm-agent", "mcp", "mcp-server", "anthropic",
          "prompt-engineering", "agent-framework", "ai-tools", "llm-tools",
          "autonomous-agents", "agentic-ai", "copilot", "cursor"]


def query_plan() -> list[str]:
    """Framings that actually yield skills, measured rather than assumed.

    This began as 132 queries built by crossing the core terms with languages
    and star bands, on the theory that partitioning the space differently
    reaches repositories a plain query ranks too low to surface. It does — and
    they are the wrong repositories. Measured by discovery source after 736,308
    of them were harvested:

        topic:claude-skills   87.2% productive   10.25 skills/repo
        topic:agent-skills    90.6%               8.60
        date-sharded search   56.8%               4.36
        GH Archive            24.0%               3.92
        broad cross-products   8.9%               0.64

    The cross-products queued 1.3M repositories at 0.64 skills each and, being
    93% of the queue, crowded everything else out: one six-hour stretch
    harvested 11,848 repositories for five productive ones. `language:` and
    `stars:` partition GitHub, not the skill corpus — a popular Python
    repository matches "agent language:Python" and contains no skills.

    Kept: the terms that name the thing being looked for. Dropped: the
    partitions that merely enumerate GitHub.
    """
    qs: list[str] = []
    qs += CORE
    qs += [f"topic:{t}" for t in TOPICS]
    qs += list(KEYWORD_QUERIES) + list(SCALE_QUERIES)
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


async def main() -> int:
    cfg = Config(db_path=Path(DB))
    store = Store(Path(DB))
    plan = query_plan()
    log.info("%d distinct queries; %d repos already known",
             len(plan), store.db.execute("SELECT COUNT(*) FROM repos").fetchone()[0])

    started = time.time()
    total_new = 0
    async with GitHubClient(cfg.tokens, concurrency=cfg.concurrency) as gh:
        for cycle in range(1, 100):
            for i, q in enumerate(plan, 1):
                try:
                    seen, new = await search_repos(
                        gh, store, q, since=date(2021, 1, 1),
                        # Below the proven sources (topics 140, search/archive
                        # 130). This was 130 — tied with them — which is how a
                        # source yielding 0.64 skills/repo came to monopolise a
                        # queue it shared with sources yielding 10.
                        reason="discover-hard", priority=110,
                    )
                except Exception as exc:                       # one bad query
                    log.warning("query %r failed: %s", q[:40], type(exc).__name__)
                    continue
                total_new += new
                if new or i % 10 == 0:
                    rate = total_new / max((time.time() - started) / 3600, 1e-6)
                    log.info("c%d %3d/%d %-38s seen %4d new %4d | total +%d (%.0f/h)",
                             cycle, i, len(plan), q[:38], seen, new, total_new, rate)
            log.info("=== cycle %d complete: +%d new repos ===", cycle, total_new)
    store.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
