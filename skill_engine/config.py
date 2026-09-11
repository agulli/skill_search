"""Configuration parameters loaded from environment variables with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "skill-engine/0.1 (+https://github.com/agulli/skill-engine)"

# Known directory prefixes that contain agent skill specifications.
SKILL_DIR_PREFIXES = {
    "skills/": "skills-dir",
    ".agents/skills/": "agent-dir",
    ".gemini/skills/": "gemini-project",
    ".claude/skills/": "claude-project",
    ".agent/skills/": "agent-dir",
    ".cursor/skills/": "cursor",
    ".opencode/skill/": "opencode",
    ".config/skills/": "config-dir",
}

# Repository topics used to index skills across GitHub and other forges.
SEED_TOPICS = [
    "agent-skills",
    "agent-skill",
    "gemini-skills",
    "antigravity-skills",
    "claude-skills",
    "claude-skill",
    "claude-code-skills",
    "anthropic-skills",
    "skill-md",
    "agentskills",
]

# Keywords used to filter GH Archive event candidates.
NAME_HINTS = ("skill", "skills", "agent-skills", "gemini", "antigravity", "claude", "subagent")


def _tokens() -> list[str]:
    """Parses comma or newline-separated GitHub personal access tokens."""
    raw = os.getenv("GITHUB_TOKENS") or os.getenv("GITHUB_TOKEN") or ""
    return [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]


@dataclass
class Config:
    """Runtime configuration for crawler, storage, and search components."""

    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("SKILL_ENGINE_DB", "data/skills.db"))
    )
    tokens: list[str] = field(default_factory=_tokens)

    # Concurrency controls for REST API and raw file fetching
    concurrency: int = int(os.getenv("SKILL_ENGINE_CONCURRENCY", "6"))
    raw_concurrency: int = int(os.getenv("SKILL_ENGINE_RAW_CONCURRENCY", "12"))

    # Content ingestion limits per repository
    max_skills_per_repo: int = int(os.getenv("SKILL_ENGINE_MAX_SKILLS_PER_REPO", "1500"))
    max_skill_bytes: int = 512 * 1024

    # Minimum age in hours before re-crawling an existing repository
    refresh_hours: int = int(os.getenv("SKILL_ENGINE_REFRESH_HOURS", "72"))

    # Embedding backend: "none", "hashing", "local", "voyage"
    embedder: str = os.getenv("SKILL_ENGINE_EMBEDDER", "none")

    @property
    def has_auth(self) -> bool:
        """Returns True if at least one API token is configured."""
        return bool(self.tokens)


def load() -> Config:
    """Loads configuration and ensures database directory exists."""
    cfg = Config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg
