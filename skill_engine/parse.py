"""Parse and validate a SKILL.md file.

The Agent Skills format is a Markdown file opening with a YAML frontmatter
block. `name` and `description` are required; everything else is optional and
varies by runtime.

Validation is split into two tiers, and the split matters. **Hard problems**
mean the file is not a skill at all — no frontmatter, no name, no description,
unparseable YAML — and those are excluded from search. **Soft warnings** mean
the file is a real skill that bends the spec: a description over the 1024-char
limit, a name that is not a clean slug, a thin body. Those stay indexed and
lose points instead.

The distinction is not pedantry. Enforcing the letter of the spec as an
admission test silently drops genuinely useful skills — including first-party
ones that run past the description limit — and a search engine that cannot
find real, working skills has failed at its only job. Unknown keys are kept
verbatim so the index survives additions to the spec.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .config import SKILL_DIR_PREFIXES

# Skill names are directory-safe slugs: lowercase alphanumerics and hyphens.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024

FRONTMATTER_RE = re.compile(
    r"\A﻿?---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

# Links to bundled resources: the spec's progressive-disclosure pattern, where
# SKILL.md points at scripts/ and references/ loaded only when needed.
RESOURCE_RE = re.compile(r"(?:\]\(|[`'\"])((?:\./)?(?:scripts|references|assets)/[^)`'\"\s]+)")


@dataclass
class ParsedSkill:
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
    problems: list[str] = field(default_factory=list)   # hard: not a skill
    warnings: list[str] = field(default_factory=list)   # soft: a flawed skill

    @property
    def invalid_reason(self) -> str:
        return "; ".join(self.problems)

    @property
    def notes(self) -> str:
        return "; ".join(self.warnings)


def classify_path(path: str) -> str:
    """Where in the repo this skill lives — a decent proxy for how it is used."""
    lowered = path.lower()
    if "/" not in path:
        return "root"
    # A plugin bundles skills under plugins/<name>/skills/<skill>/, so it has to
    # be tested before the bare skills/ prefix that it also contains.
    if lowered.startswith("plugins/") or "/plugins/" in lowered:
        return "plugin"
    # Longest prefix first: ".claude/skills/" also ends with "skills/", and the
    # more specific match is the informative one.
    for prefix in sorted(SKILL_DIR_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix) or f"/{prefix}" in lowered:
            return SKILL_DIR_PREFIXES[prefix]
    return "other"


def skill_slug_from_path(path: str) -> str:
    """The directory a skill lives in is its de facto name when frontmatter lies."""
    parts = path.split("/")
    return parts[-2] if len(parts) >= 2 else ""


def parse_skill(text: str, path: str = "") -> ParsedSkill:
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

    # Normalise the two spellings runtimes accept for the same field.
    meta = {str(k).strip(): v for k, v in meta.items()}
    normalised = {k.replace("_", "-"): v for k, v in meta.items()}

    name = normalised.get("name")
    out.name = str(name).strip() if isinstance(name, (str, int)) else ""
    description = normalised.get("description")
    out.description = str(description).strip() if isinstance(description, (str, int)) else ""

    version = normalised.get("version")
    out.version = str(version).strip() if version is not None else None
    lic = normalised.get("license")
    out.license = str(lic).strip() if isinstance(lic, str) else None

    tools = normalised.get("allowed-tools")
    if isinstance(tools, str):
        out.allowed_tools = [t.strip() for t in tools.split(",") if t.strip()]
    elif isinstance(tools, list):
        out.allowed_tools = [str(t).strip() for t in tools if str(t).strip()]

    md = normalised.get("metadata")
    out.metadata = md if isinstance(md, dict) else {}

    known = {"name", "description", "version", "license", "allowed-tools", "metadata"}
    out.extra = {k: v for k, v in normalised.items() if k not in known}

    # --- validation -------------------------------------------------------
    # Hard: the required fields are absent, so this is not a skill.
    if not out.name:
        out.problems.append("missing name")
    if not out.description:
        out.problems.append("missing description")
    if out.body_len < 40:
        out.problems.append("body is essentially empty")

    # Soft: present but out of spec. Still a skill; just a scruffier one.
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
    """A 0–100 ranking prior, blended into search scores.

    Popularity is logarithmic so a 40k-star monorepo does not bury a focused
    300-star skill collection. Everything else is a small nudge.
    """
    import math

    score = 0.0
    score += min(35.0, 9.0 * math.log10(max(stars, 0) + 1))  # 35 pts at ~7k stars

    # Craft signals: a description in the useful band, real prose, bundled files.
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

    # Freshness, decaying over roughly a year.
    score += 14.0 * math.exp(-days_since_push / 240.0)

    # Spec deviations cost points rather than an index entry.
    score -= 4.0 * len(skill.warnings)

    if is_fork:
        score -= 12.0
    if archived:
        score -= 10.0
    # A file copied verbatim into many repos is usually a vendored template.
    if duplicate_count > 1:
        score -= min(15.0, 3.0 * math.log2(duplicate_count + 1))

    return round(max(0.0, min(100.0, score)), 2)
