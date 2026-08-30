"""Configuration, read from the environment with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "skill-engine/0.1 (+https://github.com/agulli/skill-engine)"

# Directories that hold agent skills, by convention. A path is a skill candidate
# when its basename is SKILL.md; these prefixes only classify where it came from.
SKILL_DIR_PREFIXES = {
    "skills/": "skills-dir",
    ".claude/skills/": "claude-project",
    ".agent/skills/": "agent-dir",
    ".cursor/skills/": "cursor",
    ".opencode/skill/": "opencode",
    ".config/skills/": "config-dir",
}

# Repository topics that authors actually use to tag skills.
SEED_TOPICS = [
    "agent-skills",
    "agent-skill",
    "claude-skills",
    "claude-skill",
    "claude-code-skills",
    "anthropic-skills",
    "skill-md",
    "agentskills",
]

# Words that make a bare repository name worth a tree lookup. Used to filter the
# GH Archive firehose down to something payable.
NAME_HINTS = ("skill", "skills", "agent-skills", "claude", "subagent")


def _tokens() -> list[str]:
    raw = os.getenv("GITHUB_TOKENS") or os.getenv("GITHUB_TOKEN") or ""
    return [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]


@dataclass
class Config:
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("SKILL_ENGINE_DB", "data/skills.db"))
    )
    tokens: list[str] = field(default_factory=_tokens)

    # Politeness. GitHub's documented ceiling is 100 concurrent requests; we sit
    # well under it because secondary rate limits trigger on burstiness, not just
    # on volume.
    concurrency: int = int(os.getenv("SKILL_ENGINE_CONCURRENCY", "6"))
    raw_concurrency: int = int(os.getenv("SKILL_ENGINE_RAW_CONCURRENCY", "12"))

    # How many SKILL.md files to fetch content for from a single repository.
    # This is a *wall-clock* budget, not a rate-limit one: content comes from
    # raw.githubusercontent.com, which does not draw on the REST quota at all.
    # The old value of 400 was calibrated as though it protected quota, so it
    # discarded thousands of real skills to save a resource that was never at
    # risk. The true file count is always recorded regardless of this cap, so
    # the ranker's aggregator-dump penalty still sees reality.
    max_skills_per_repo: int = int(os.getenv("SKILL_ENGINE_MAX_SKILLS_PER_REPO", "1500"))
    max_skill_bytes: int = 512 * 1024

    # Refresh cadence for repos we have already seen, in hours.
    refresh_hours: int = int(os.getenv("SKILL_ENGINE_REFRESH_HOURS", "72"))

    embedder: str = os.getenv("SKILL_ENGINE_EMBEDDER", "none")

    @property
    def has_auth(self) -> bool:
        return bool(self.tokens)


def load() -> Config:
    cfg = Config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg
