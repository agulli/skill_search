"""Parser and validation engine for SKILL.md agent skill specifications.

Agent skills are markdown documents initiated with a YAML frontmatter block.
The `name` and `description` fields are mandatory; all additional fields are
optional and vary across agent runtimes (Google Antigravity, Gemini, Claude, Cursor).

Validation follows a two-tier model:
- Hard Errors: Structural defects that prevent indexing (e.g. absent frontmatter,
  missing required fields, unparseable YAML, empty body).
- Soft Warnings: Deviations from standard conventions (e.g. descriptions >1,024 chars,
  non-slug names). These files remain indexed with minor ranking score penalties.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .config import SKILL_DIR_PREFIXES

# Skill names should follow lowercase alphanumeric slug conventions
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024

FRONTMATTER_RE = re.compile(
    r"\A﻿?---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

# Relative links to bundled resources (scripts/, references/, assets/)
RESOURCE_RE = re.compile(r"(?:\]\(|[`'\"])((?:\./)?(?:scripts|references|assets)/[^)`'\"\s]+)")


@dataclass
class ParsedSkill:
    """Represents a parsed and validated agent skill."""

    name: str = ""
    description: str = ""
    version: str | None = None
    license: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    body: str = ""
    body_len: int = 0
    heading: str = ""
    resources: list[str] = field(default_factory=list)
    content_hash: str = ""

    valid: bool = False
    problems: list[str] = field(default_factory=list)  # Hard validation errors
    warnings: list[str] = field(default_factory=list)  # Soft validation warnings

    @property
    def invalid_reason(self) -> str:
        """Returns concatenated hard failure reasons."""
        return "; ".join(self.problems)

    @property
    def notes(self) -> str:
        """Returns concatenated soft warning notes."""
        return "; ".join(self.warnings)


def classify_path(path: str) -> str:
    """Classifies the relative repository location of a skill file.

    Args:
        path: Relative file path in the repository (e.g., 'skills/pdf/SKILL.md').

    Returns:
        String classification label representing the source directory convention.
    """
    lowered = path.lower()
    if "/" not in path:
        return "root"
    if lowered.startswith("plugins/") or "/plugins/" in lowered:
        return "plugin"
    for prefix in sorted(SKILL_DIR_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix) or f"/{prefix}" in lowered:
            return SKILL_DIR_PREFIXES[prefix]
    return "other"


def skill_slug_from_path(path: str) -> str:
    """Extracts the parent directory name as a fallback slug.

    Args:
        path: File path string.

    Returns:
        Extracted slug or empty string.
    """
    parts = path.split("/")
    return parts[-2] if len(parts) >= 2 else ""


def parse_skill(text: str, path: str = "") -> ParsedSkill:
    """Parses markdown content and validates frontmatter attributes.

    Args:
        text: Raw text of the SKILL.md file.
        path: Optional file path within repository for context.

    Returns:
        ParsedSkill instance populated with validation results and metadata.
    """
    out = ParsedSkill()
    out.content_hash = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    match = FRONTMATTER_RE.match(text)
    if not match:
        out.problems.append("no YAML frontmatter")
        out.body = text.strip()
        out.body_len = len(out.body)
        return out

    body = text[match.end():]
    out.body = body.strip()
    out.body_len = len(out.body)

    heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if heading:
        out.heading = heading.group(1).strip()

    out.resources = sorted({m.group(1).lstrip("./") for m in RESOURCE_RE.finditer(body)})

    try:
        meta = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        out.problems.append(f"invalid YAML: {str(exc)[:120]}")
        return out

    if not isinstance(meta, dict):
        out.problems.append("frontmatter is not a mapping")
        return out

    # Normalize underscore and hyphen keys across different agent runtimes
    meta = {str(k).strip(): v for k, v in meta.items()}
    normalized = {k.replace("_", "-"): v for k, v in meta.items()}

    name = normalized.get("name")
    out.name = str(name).strip() if isinstance(name, (str, int)) else ""
    description = normalized.get("description")
    out.description = str(description).strip() if isinstance(description, (str, int)) else ""

    version = normalized.get("version")
    out.version = str(version).strip() if version is not None else None
    lic = normalized.get("license")
    out.license = str(lic).strip() if isinstance(lic, str) else None

    tools = normalized.get("allowed-tools")
    if isinstance(tools, str):
        out.allowed_tools = [t.strip() for t in tools.split(",") if t.strip()]
    elif isinstance(tools, list):
        out.allowed_tools = [str(t).strip() for t in tools if str(t).strip()]

    md = normalized.get("metadata")
    out.metadata = md if isinstance(md, dict) else {}

    known = {"name", "description", "version", "license", "allowed-tools", "metadata"}
    out.extra = {k: v for k, v in normalized.items() if k not in known}

    # Hard validation checks
    if not out.name:
        out.problems.append("missing name")
    if not out.description:
        out.problems.append("missing description")
    if out.body_len < 40:
        out.problems.append("body is essentially empty")

    # Soft validation checks
    if out.name and len(out.name) > NAME_MAX:
        out.warnings.append(f"name longer than {NAME_MAX} chars")
    if out.name and not NAME_RE.match(out.name):
        out.warnings.append("name is not a lowercase-hyphen slug")
    if out.description and len(out.description) > DESCRIPTION_MAX:
        out.warnings.append(f"description longer than {DESCRIPTION_MAX} chars")

    out.valid = not out.problems
    return out


def quality_score(
    skill: ParsedSkill,
    *,
    stars: int = 0,
    is_fork: bool = False,
    archived: bool = False,
    has_license: bool = False,
    days_since_push: float = 9999.0,
    duplicate_count: int = 0,
) -> float:
    """Computes a baseline quality prior score (0-100) for search ranking.

    Args:
        skill: ParsedSkill instance.
        stars: Repository star count.
        is_fork: Whether the repository is a fork.
        archived: Whether the repository is archived.
        has_license: Whether a repository-level license is detected.
        days_since_push: Elapsed days since latest repository push.
        duplicate_count: Total identical copies detected across corpus.

    Returns:
        Float quality score bounded between 0.0 and 100.0.
    """
    import math

    score = 0.0
    score += min(35.0, 9.0 * math.log10(max(stars, 0) + 1))

    # Craft signals: description length, body depth, resources, tools, and license
    dlen = len(skill.description)
    if 40 <= dlen <= 600:
        score += 12.0
    elif dlen:
        score += 5.0
    if skill.body_len >= 400:
        score += 10.0
    elif skill.body_len >= 150:
        score += 5.0
    if skill.resources:
        score += min(8.0, 2.0 * len(skill.resources))
    if skill.allowed_tools:
        score += 3.0
    if has_license or skill.license:
        score += 6.0

    # Recency decay curve
    score += 14.0 * math.exp(-days_since_push / 240.0)

    # Soft warning penalties
    score -= 4.0 * len(skill.warnings)

    if is_fork:
        score -= 12.0
    if archived:
        score -= 10.0
    if duplicate_count > 1:
        score -= min(15.0, 3.0 * math.log2(duplicate_count + 1))

    return round(max(0.0, min(100.0, score)), 2)
