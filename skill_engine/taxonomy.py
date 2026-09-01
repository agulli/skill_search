"""A browsable subject taxonomy for the skill corpus.

Search answers "I know what I want". A directory answers "show me what exists" —
a different and, for a corpus nobody has seen before, often more useful
question. This module provides the second.

**Why rules and not a model.** Classifying 100,000 skills with an LLM would cost
real money and take hours, and would have to be re-run on every crawl. The
categories here are instead matched by weighted patterns over a skill's name,
description, path and its repository's topics. That is deterministic, runs over
the whole corpus in seconds, costs nothing, and — importantly for a directory —
is *explainable*: you can always say why something landed where it did.

The taxonomy itself was derived from the corpus rather than invented. Term
frequencies over 95,725 skill names and descriptions surfaced the real clusters
(review, design, api, analysis, audit, content, product, planning, security,
mcp, research, architecture, testing), and the categories below follow them.

**Scoring, and why it is IDF-weighted.** A skill accumulates weight per category
from every pattern it matches, with the name weighted above the description
because a skill's name is its most deliberate signal.

Counting raw matches does not work, and the failure is instructive: it put
**69.7% of the corpus into a single category**. No individual term was to blame
— the most common, "agent", appears in under 10% of skills. The problem was
that a category with twenty patterns simply has more chances to accumulate than
one with eleven, so breadth beat relevance.

Each pattern is therefore weighted by its inverse document frequency, measured
against the corpus itself. A term appearing in 10% of skills contributes far
less than one appearing in 0.1%, so "model context protocol" outweighs "agent",
and categories are compared on the specificity of what they matched rather than
on how many patterns their author happened to write.

The best-scoring category becomes primary; every category clearing a floor is
retained, since real skills often belong in two places. Anything matching
nothing lands in `uncategorised` rather than being silently dropped — an honest
directory shows its own gaps.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

# Weights: how much a match in each field counts.
W_NAME = 3.0
W_DESC = 1.0
W_PATH = 1.5
W_TOPIC = 2.0

# A category is retained as secondary above this share of the winning score.
SECONDARY_FLOOR = 0.55


@dataclass(frozen=True)
class Subcategory:
    id: str
    label: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    icon: str
    blurb: str
    patterns: tuple[str, ...]
    subs: tuple[Subcategory, ...] = field(default_factory=tuple)


def _s(id_: str, label: str, *patterns: str) -> Subcategory:
    return Subcategory(id_, label, patterns)


TAXONOMY: tuple[Category, ...] = (
    Category(
        "engineering", "Software Engineering", "⚙️",
        "Writing, reviewing, refactoring and debugging code.",
        ("code review", "refactor", "debug", "codebase", "pull request",
         r"\bpr\b", "linting", "code quality", "technical debt", "legacy code",
         "pair program", "commit", r"\bgit\b", "merge conflict", "changelog",
         "monorepo", "compiler", "static analysis"),
        (
            _s("code-review", "Code Review", "code review", "review", "pull request", r"\bpr\b", "critique"),
            _s("refactoring", "Refactoring", "refactor", "technical debt", "legacy", "cleanup", "simplif"),
            _s("debugging", "Debugging", "debug", "stack trace", "root cause", "bug", "troubleshoot"),
            _s("version-control", "Git & Version Control", r"\bgit\b", "commit", "branch", "merge", "rebase", "changelog"),
            _s("architecture", "Architecture", "architecture", "design pattern", "system design", "microservice", "modular"),
        ),
    ),
    Category(
        "testing", "Testing & Quality", "🧪",
        "Test authoring, coverage, QA and release confidence.",
        ("test", "testing", "unit test", "integration test", "e2e", "coverage",
         "qa", "quality assurance", "regression", "playwright", "cypress",
         "pytest", "jest", "vitest", "fuzz", "benchmark", "assertion"),
        (
            _s("unit-testing", "Unit Testing", "unit test", "pytest", "jest", "vitest", "mock", "assertion"),
            _s("e2e", "End-to-End & Browser", "e2e", "playwright", "cypress", "selenium", "browser test", "puppeteer"),
            _s("coverage", "Coverage & QA", "coverage", "quality assurance", r"\bqa\b", "regression", "smoke test"),
            _s("performance", "Performance & Benchmarks", "benchmark", "performance test", "load test", "profil"),
        ),
    ),
    Category(
        "devops", "DevOps & Infrastructure", "🚢",
        "Deploying, provisioning and operating systems.",
        ("deploy", "docker", "kubernetes", "k8s", "terraform", "helm", "ansible",
         "ci/cd", "pipeline", "infrastructure", "provision", "aws", "gcp",
         "azure", "cloudflare", "serverless", "observability", "monitoring",
         "incident", "sre", "container", "nginx", "github actions"),
        (
            _s("containers", "Containers", "docker", "kubernetes", "k8s", "helm", "container", "podman"),
            _s("iac", "Infrastructure as Code", "terraform", "pulumi", "ansible", "cloudformation", "provision"),
            _s("cicd", "CI/CD", "ci/cd", "github actions", "pipeline", "continuous integration", "build system"),
            _s("cloud", "Cloud Platforms", r"\baws\b", r"\bgcp\b", "azure", "cloudflare", "serverless", "lambda"),
            _s("observability", "Monitoring & Incidents", "observability", "monitoring", "incident", "logging", "alert", "on-call"),
        ),
    ),
    Category(
        "data", "Data & Analytics", "📊",
        "Querying, transforming, analysing and visualising data.",
        ("data analysis", "dataset", r"\bsql\b", "database", "postgres", "mysql",
         "sqlite", "bigquery", "snowflake", "pandas", "dataframe", "etl",
         "spreadsheet", "excel", "xlsx", "csv", "analytics", "dashboard",
         "visualisation", "visualization", "chart", "statistics", "duckdb"),
        (
            _s("sql", "SQL & Databases", r"\bsql\b", "postgres", "mysql", "sqlite", "database", "query optim", "duckdb"),
            _s("spreadsheets", "Spreadsheets", "spreadsheet", "excel", "xlsx", r"\bcsv\b", "google sheets"),
            _s("pipelines", "Pipelines & ETL", "etl", "pipeline", "airflow", "dbt", "ingest", "warehouse"),
            _s("visualisation", "Charts & Dashboards", "chart", "visuali", "dashboard", "plot", "graph", "matplotlib"),
            _s("statistics", "Statistics & ML", "statistic", "regression", "machine learning", "model training", "forecast"),
        ),
    ),
    Category(
        "ai", "AI & Agents", "🤖",
        "MCP servers, retrieval, fine-tuning and model plumbing.",
        # Deliberately narrow. "agent", "claude", "prompt", "token" and "eval"
        # describe what this whole corpus *is*, so they identify no subject
        # within it — including them put 54% of every skill in this one bucket.
        # What remains are genuine AI-engineering topics.
        (r"\bmcp\b", "model context protocol", "retrieval augmented", r"\brag\b",
         "embedding", "vector database", "vector store", "fine-tun",
         "hallucination", "context window", "semantic search", "reranker",
         "langchain", "llamaindex", "ollama", "inference server", "quantiz"),
        (
            _s("mcp", "MCP Servers", r"\bmcp\b", "model context protocol", "mcp server"),
            _s("prompting", "Prompt Engineering", "system prompt", "few-shot", "chain of thought", "context engineering", "prompt template"),
            _s("rag", "RAG & Retrieval", r"\brag\b", "retrieval", "embedding", "vector", "semantic search", "chunk"),
            _s("agents", "Agent Orchestration", "subagent", "multi-agent", "orchestrat", "handoff", "agent loop"),
            _s("evaluation", "Evals & Guardrails", "guardrail", "hallucination", "red team", "llm judge", "eval harness"),
        ),
    ),
    Category(
        "web", "Web & Frontend", "🌐",
        "Building interfaces and web applications.",
        ("react", "vue", "svelte", "angular", "next.js", "nextjs", "frontend",
         "css", "tailwind", "html", "component", "responsive", "accessibility",
         "a11y", "browser", "dom", "typescript", "javascript", "shadcn",
         "landing page", "website"),
        (
            _s("react", "React & Frameworks", "react", "next.js", "nextjs", "vue", "svelte", "angular", "remix"),
            _s("styling", "CSS & Styling", r"\bcss\b", "tailwind", "styling", "design token", "shadcn", "theme"),
            _s("accessibility", "Accessibility", "accessibility", "a11y", "wcag", "screen reader", "aria"),
            _s("performance-web", "Web Performance", "core web vital", "lighthouse", "bundle size", "page speed"),
        ),
    ),
    Category(
        "documents", "Documents & Files", "📄",
        "Reading, generating and converting document formats.",
        (r"\bpdf\b", "docx", "pptx", "powerpoint", "word document", "markdown",
         "latex", "epub", "ocr", "document", "slide", "presentation",
         "spreadsheet export", "file conversion", "parse", "extract text"),
        (
            _s("pdf", "PDF", r"\bpdf\b", "ocr", "form fill", "extract text"),
            _s("office", "Office Formats", "docx", "pptx", "powerpoint", "word document", "xlsx", "office"),
            _s("slides", "Slides & Decks", "slide", "presentation", "deck", "keynote", "pitch deck"),
            _s("markup", "Markdown & LaTeX", "markdown", "latex", "asciidoc", "restructured", "mermaid"),
        ),
    ),
    Category(
        "design", "Design & Creative", "🎨",
        "Visual design, branding, images and video.",
        ("design", "ui design", "ux", "figma", "brand", "logo", "typography",
         "colour", "color palette", "illustration", "image generation", "video",
         "animation", "3d", "photo", "canvas", "poster", "icon"),
        (
            _s("ui-ux", "UI & UX", "ui design", r"\bux\b", "wireframe", "figma", "interface design", "usability"),
            _s("brand", "Brand & Identity", "brand", "logo", "typography", "style guide", "identity"),
            _s("images", "Images & Graphics", "image", "photo", "illustration", "svg", "icon", "poster"),
            _s("video", "Video & Motion", "video", "animation", "motion", "ffmpeg", "editing", "subtitle"),
        ),
    ),
    Category(
        "security", "Security", "🔒",
        "Auditing, hardening and responding to threats.",
        ("security", "vulnerability", "pentest", "penetration test", "exploit",
         "owasp", "cve", "threat model", "secrets", "encryption", "auth",
         "authentication", "authorization", "compliance", "audit", "hardening",
         "malware", "forensic", "ctf"),
        (
            _s("appsec", "Application Security", "owasp", "vulnerability", "injection", "xss", "secure coding", "sast"),
            _s("audit", "Auditing & Compliance", "audit", "compliance", r"\bsoc ?2\b", "gdpr", "policy", "governance"),
            _s("offensive", "Offensive & CTF", "pentest", "penetration test", "exploit", "ctf", "red team", "recon"),
            _s("secrets", "Secrets & Crypto", "secret", "encryption", "credential", "key management", "certificate"),
        ),
    ),
    Category(
        "product", "Product & Planning", "🗺️",
        "Specs, roadmaps, research and delivery planning.",
        ("product", "roadmap", "prd", "requirement", "user story", "backlog",
         "sprint", "planning", "prioriti", "stakeholder", "okr", "discovery",
         "user research", "persona", "feature spec", "jira", "linear"),
        (
            _s("specs", "Specs & PRDs", "prd", "requirement", "user story", "spec", "acceptance criteria"),
            _s("roadmap", "Roadmaps & Strategy", "roadmap", "strategy", "okr", "prioriti", "vision"),
            _s("agile", "Agile & Delivery", "sprint", "backlog", "scrum", "standup", "retrospective", "jira", "linear"),
            _s("user-research", "User Research", "user research", "persona", "interview", "usability test", "survey"),
        ),
    ),
    Category(
        "writing", "Writing & Content", "✍️",
        "Drafting, editing and publishing prose.",
        ("writing", "editing", "copywriting", "blog", "article", "newsletter",
         "documentation", "technical writing", "summari", "translation",
         "proofread", "tone of voice", "storytelling", "script", "email"),
        (
            _s("docs", "Documentation", "documentation", "technical writing", "readme", "api docs", "changelog"),
            _s("copy", "Copywriting", "copywriting", "marketing copy", "headline", "tagline", "landing copy"),
            _s("editing", "Editing & Style", "editing", "proofread", "tone of voice", "style guide", "grammar"),
            _s("summarisation", "Summarising", "summari", "digest", "tl;dr", "abstract", "condense"),
        ),
    ),
    Category(
        "business", "Business & Marketing", "📈",
        "Growth, sales, finance and operations.",
        ("marketing", "seo", "growth", "sales", "crm", "customer", "revenue",
         "pricing", "finance", "accounting", "invoice", "legal", "contract",
         "recruit", "hiring", "hr", "startup", "pitch", "competitor",
         "linkedin", "social media", "advertis"),
        (
            _s("marketing", "Marketing & SEO", "marketing", "seo", "campaign", "advertis", "social media", "content strategy"),
            _s("sales", "Sales & CRM", "sales", "crm", "lead", "outreach", "pipeline", "prospect"),
            _s("finance", "Finance & Legal", "finance", "accounting", "invoice", "legal", "contract", "tax", "budget"),
            _s("people", "Hiring & People", "recruit", "hiring", "interview", "resume", r"\bcv\b", r"\bhr\b", "onboarding"),
        ),
    ),
    Category(
        "research", "Research & Science", "🔬",
        "Academic literature, experiments and scientific computing.",
        ("research", "paper", "academic", "citation", "literature review",
         "scientific", "experiment", "hypothesis", "arxiv", "pubmed", "thesis",
         "bioinformatic", "chemistry", "physics", "genomic", "clinical",
         "simulation", "peer review"),
        (
            _s("literature", "Literature & Citations", "literature", "citation", "paper", "arxiv", "pubmed", "bibliograph"),
            _s("experiments", "Experiments", "experiment", "hypothesis", "protocol", "lab", "reproducib"),
            _s("scientific-computing", "Scientific Computing", "simulation", "numerical", "matlab", "computational", "solver"),
            _s("life-sciences", "Life & Health Sciences", "bioinformatic", "genomic", "clinical", "medical", "protein", "drug"),
        ),
    ),
    Category(
        "productivity", "Productivity & Workflow", "⚡",
        "Personal workflow, notes, automation and everyday tasks.",
        ("workflow", "automation", "productivity", "note", "obsidian", "notion",
         "todo", "task management", "calendar", "meeting", "reminder",
         "knowledge base", "second brain", "journal", "habit", "inbox",
         "slack", "email triage"),
        (
            _s("notes", "Notes & Knowledge", "note", "obsidian", "notion", "knowledge base", "second brain", "zettel"),
            _s("automation", "Automation", "automation", "script", "cron", "webhook", "zapier", "integration"),
            _s("meetings", "Meetings & Comms", "meeting", "slack", "email", "standup", "minutes", "transcript"),
            _s("personal", "Personal Organisation", "todo", "task management", "calendar", "habit", "journal", "planner"),
        ),
    ),
    Category(
        "mobile", "Mobile & Desktop Apps", "📱",
        "Native and cross-platform application development.",
        ("ios", "android", "swift", "kotlin", "flutter", "react native",
         "mobile app", "xcode", "app store", "electron", "desktop app",
         "tauri", "macos", "windows app"),
        (
            _s("ios", "iOS & macOS", r"\bios\b", "swift", "swiftui", "xcode", "macos", "app store"),
            _s("android", "Android", "android", "kotlin", "jetpack", "play store", "gradle"),
            _s("cross-platform", "Cross-Platform", "flutter", "react native", "electron", "tauri", "capacitor"),
        ),
    ),
    Category(
        "gaming", "Games & Simulation", "🎮",
        "Game development, engines and interactive worlds.",
        ("game", "unity", "unreal", "godot", "gamedev", "level design",
         "shader", "sprite", "roguelike", "procedural generation", "physics engine"),
        (
            _s("engines", "Engines", "unity", "unreal", "godot", "bevy", "phaser"),
            _s("gameplay", "Design & Gameplay", "level design", "game design", "mechanic", "balance", "narrative design"),
            _s("graphics", "Graphics & Shaders", "shader", "sprite", "procedural", "render", "particle"),
        ),
    ),
)

UNCATEGORISED = Category(
    "uncategorised", "Uncategorised", "❓",
    "Skills the classifier could not place. An honest directory shows its gaps.",
    (),
)

BY_ID = {c.id: c for c in TAXONOMY}
ALL_CATEGORIES = TAXONOMY + (UNCATEGORISED,)


@functools.lru_cache(maxsize=8192)
def _compile(patterns: tuple[str, ...]) -> re.Pattern | None:
    if not patterns:
        return None
    # Bare words are wrapped so "test" does not match "latest"; patterns that
    # already carry their own anchors are used verbatim.
    parts = []
    for p in patterns:
        if p.startswith(("\\b", "(")):
            parts.append(p)                       # already anchored by hand
        elif len(p) >= 6:
            # Long stems are matched as prefixes on purpose, so "refactor"
            # catches "refactoring" and "visuali" catches "visualisation".
            parts.append(rf"\b{re.escape(p)}")
        else:
            # Short words must match whole. Without the trailing boundary
            # "auth" matched "authoring" and filed a Neo4j connector under
            # Security; "test" would likewise match "latest".
            parts.append(rf"\b{re.escape(p)}\b")
    return re.compile("|".join(parts), re.IGNORECASE)


_CAT_RE = {c.id: _compile(c.patterns) for c in TAXONOMY}
_SUB_RE = {(c.id, s.id): _compile(s.patterns) for c in TAXONOMY for s in c.subs}


def compute_pattern_idf(store, sample: int = 40_000) -> dict[str, float]:
    """Inverse document frequency for every pattern, measured on the corpus.

    Computed once per categorisation run and cached in `corpus_stats`, so the
    weighting is reproducible and can be inspected. Sampling keeps a full pass
    off the critical path: IDF only needs to rank terms, and 40k documents
    settles that ordering.
    """
    import math

    rows = [
        f"{r['name']} {r['description']}"
        for r in store.db.execute(
            "SELECT name, description FROM skills WHERE valid = 1 LIMIT ?", (sample,)
        )
    ]
    n = max(len(rows), 1)

    patterns: set[str] = set()
    for cat in TAXONOMY:
        patterns.update(cat.patterns)
        for sub in cat.subs:
            patterns.update(sub.patterns)

    idf: dict[str, float] = {}
    for pattern in patterns:
        rx = _compile((pattern,))
        if rx is None:
            continue
        df = sum(1 for text in rows if rx.search(text))
        # Smoothed so a pattern matching nothing does not become infinite, and
        # floored so a very common one still counts a little.
        idf[pattern] = max(0.35, math.log((n + 1) / (df + 1)))
    return idf


# A description may list a dozen features; only the strongest few should speak
# for the skill's subject. Without this, "securing the graph with JWT" in item
# six of seven outvoted what the skill is actually for.
MAX_DESC_MATCHES = 2


def _score_patterns(patterns: tuple[str, ...], haystacks, idf) -> float:
    """Score a pattern set, capping how much any one field may contribute."""
    per_field: dict[int, list[float]] = {}
    for pattern in patterns:
        rx = _compile((pattern,))
        if rx is None:
            continue
        weight = idf.get(pattern, 1.0) if idf else 1.0
        for i, (text, field_weight) in enumerate(haystacks):
            if text and rx.search(text):
                per_field.setdefault(i, []).append(weight * field_weight)

    total = 0.0
    for i, values in per_field.items():
        values.sort(reverse=True)
        # Field index 1 is the description — the only one long enough to stack
        # unrelated mentions. Name, path and topics are short and deliberate.
        total += sum(values[:MAX_DESC_MATCHES] if i == 1 else values)
    return total


def classify(name: str, description: str, path: str = "",
             topics: tuple[str, ...] = (),
             idf: dict[str, float] | None = None
             ) -> tuple[str, str | None, list[str], dict]:
    """Return (primary category, subcategory, secondary categories, scores)."""
    haystacks = (
        (name or "", W_NAME),
        (description or "", W_DESC),
        (path or "", W_PATH),
        (" ".join(topics), W_TOPIC),
    )

    scores: dict[str, float] = {}
    for cat in TAXONOMY:
        total = _score_patterns(cat.patterns, haystacks, idf)
        if total:
            scores[cat.id] = total

    if not scores:
        return UNCATEGORISED.id, None, [], {}

    primary = max(scores, key=lambda k: scores[k])
    best = scores[primary]
    secondary = sorted(
        (c for c, v in scores.items() if c != primary and v >= best * SECONDARY_FLOOR),
        key=lambda c: -scores[c],
    )[:2]

    sub = None
    sub_best = 0.0
    for s in BY_ID[primary].subs:
        total = _score_patterns(s.patterns, haystacks, idf)
        if total > sub_best:
            sub_best, sub = total, s.id

    return primary, sub, secondary, {k: round(v, 1) for k, v in scores.items()}


def category_tree() -> list[dict]:
    """The taxonomy as plain data, for the API and the browse UI."""
    return [
        {
            "id": c.id, "label": c.label, "icon": c.icon, "blurb": c.blurb,
            "subs": [{"id": s.id, "label": s.label} for s in c.subs],
        }
        for c in ALL_CATEGORIES
    ]


def categorise_corpus(store, *, batch: int = 5000) -> dict:
    """Classify every skill in the corpus. Deterministic, offline, seconds.

    Re-runnable: a change to the taxonomy is applied by running this again,
    which is the whole reason the classifier is rules rather than a model.
    """
    import json as _json

    rows = store.db.execute(
        "SELECT s.id, s.name, s.description, s.path, r.topics "
        "FROM skills s LEFT JOIN repos r ON r.full_name = s.repo"
    ).fetchall()

    idf = compute_pattern_idf(store)
    updates = []
    counts: dict[str, int] = {}
    for row in rows:
        try:
            topics = tuple(_json.loads(row["topics"] or "[]"))
        except (ValueError, TypeError):
            topics = ()
        primary, sub, secondary, _ = classify(
            row["name"], row["description"], row["path"], topics, idf
        )
        counts[primary] = counts.get(primary, 0) + 1
        updates.append((primary, sub, _json.dumps(secondary), row["id"]))

    store.db.executemany(
        "UPDATE skills SET category = ?, subcategory = ?, categories = ? "
        "WHERE id = ?",
        updates,
    )
    store.db.execute(
        "CREATE INDEX IF NOT EXISTS skills_category ON skills(category, subcategory)"
    )
    store.commit()
    build_catalogue(store)
    return {"classified": len(updates), "counts": counts}


def build_catalogue(store) -> dict:
    """Precompute the browsable directory and store it.

    The directory is derived from a GROUP BY over every skill, and it changes
    only when the index is rebuilt — yet it was recomputed on every page load,
    measured at 49-67ms each time on a 100k corpus and far worse at a million.
    Computing it once here turns each page load into a single row read.
    """
    import json as _json

    from .search import category_counts

    counts = category_counts(store)
    tree = []
    for cat in category_tree():
        entry = counts.get(cat["id"], {"total": 0, "subs": {}})
        if not entry["total"]:
            continue
        cat["count"] = entry["total"]
        for sub in cat["subs"]:
            sub["count"] = entry["subs"].get(sub["id"], 0)
        cat["subs"] = [s for s in cat["subs"] if s["count"]]
        cat["subs"].sort(key=lambda s: -s["count"])
        tree.append(cat)
    tree.sort(key=lambda c: -c["count"])

    payload = {"categories": tree, "total": sum(c["count"] for c in tree)}
    store.put_meta("catalogue", _json.dumps(payload))
    return payload
