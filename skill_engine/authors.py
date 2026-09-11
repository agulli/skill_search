"""Author reputation model: aggregates developer portfolio signals and integrity metrics."""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from typing import Any

from .metadata import days_since
from .ranking import CorpusStats, blend, craft_score, recency

log = logging.getLogger("skill_engine.authors")

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    login             TEXT PRIMARY KEY,
    type              TEXT,
    repos             INTEGER DEFAULT 0,
    skill_repos       INTEGER DEFAULT 0,
    skills            INTEGER DEFAULT 0,
    original_skills   INTEGER DEFAULT 0,
    total_stars       INTEGER DEFAULT 0,
    total_forks       INTEGER DEFAULT 0,
    median_craft      REAL,
    best_repo_score   REAL,
    licensed_rate     REAL,
    described_rate    REAL,
    first_repo_at     TEXT,
    last_push_at      TEXT,
    followers         INTEGER,
    public_repos      INTEGER,
    bio               TEXT,
    author_score      REAL DEFAULT 0,
    score_detail      TEXT,
    computed_at       REAL
);
CREATE INDEX IF NOT EXISTS authors_score ON authors(author_score DESC);
"""

# Quantiles for author-level aggregate metrics
AUTHOR_METRICS = ("author_stars", "author_skills", "author_followers")


def ensure_schema(store: Any) -> None:
    """Ensures authors table and indexes exist."""
    store.db.executescript(SCHEMA)
    store.db.commit()


def build_profiles(store: Any, stats: CorpusStats) -> int:
    """Aggregates all repository and skill data by author login."""
    ensure_schema(store)

    repos: dict[str, list[Any]] = {}
    for row in store.db.execute("SELECT * FROM repos"):
        repos.setdefault(row["owner"], []).append(row)

    crafts: dict[str, list[float]] = {}
    originals: dict[str, int] = {}
    totals: dict[str, int] = {}
    for skill in store.db.execute(
        "SELECT repo, resources, allowed_tools, warnings, description, body_len, "
        "valid, dup_count FROM skills"
    ):
        owner = skill["repo"].split("/", 1)[0]
        value, _ = craft_score(skill, stats)
        crafts.setdefault(owner, []).append(value)
        totals[owner] = totals.get(owner, 0) + 1
        if (skill["dup_count"] or 1) <= 1:
            originals[owner] = originals.get(owner, 0) + 1

    now = time.time()
    rows = []
    for owner, owned in repos.items():
        with_skills = [r for r in owned if (r["skill_count"] or 0) > 0]
        if not with_skills and owner not in totals:
            continue

        created = [r["created_at"] for r in owned if r["created_at"]]
        pushed = [r["pushed_at"] for r in owned if r["pushed_at"]]
        skills = totals.get(owner, 0)
        rows.append((
            owner,
            owned[0]["owner_type"],
            len(owned),
            len(with_skills),
            skills,
            originals.get(owner, 0),
            sum(r["stars"] or 0 for r in owned),
            sum(r["forks"] or 0 for r in owned),
            statistics.median(crafts[owner]) if crafts.get(owner) else None,
            max((r["repo_score"] or 0) for r in owned),
            sum(1 for r in owned if r["license"]) / len(owned),
            sum(1 for r in owned if (r["description"] or "").strip()) / len(owned),
            min(created) if created else None,
            max(pushed) if pushed else None,
            now,
        ))

    store.db.executemany(
        """INSERT INTO authors(login, type, repos, skill_repos, skills,
               original_skills, total_stars, total_forks, median_craft,
               best_repo_score, licensed_rate, described_rate, first_repo_at,
               last_push_at, computed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(login) DO UPDATE SET
               type=excluded.type, repos=excluded.repos,
               skill_repos=excluded.skill_repos, skills=excluded.skills,
               original_skills=excluded.original_skills,
               total_stars=excluded.total_stars, total_forks=excluded.total_forks,
               median_craft=excluded.median_craft,
               best_repo_score=excluded.best_repo_score,
               licensed_rate=excluded.licensed_rate,
               described_rate=excluded.described_rate,
               first_repo_at=excluded.first_repo_at,
               last_push_at=excluded.last_push_at,
               computed_at=excluded.computed_at""",
        rows,
    )
    store.db.commit()
    return len(rows)


def author_quantiles(store: Any) -> dict[str, list[float]]:
    """Derives empirical percentile distributions across all profiled authors."""
    samples: dict[str, list[float]] = {m: [] for m in AUTHOR_METRICS}
    for r in store.db.execute(
        "SELECT total_stars, skills, followers FROM authors WHERE skills > 0"
    ):
        samples["author_stars"].append(float(r["total_stars"] or 0))
        samples["author_skills"].append(float(r["skills"] or 0))
        if r["followers"] is not None:
            samples["author_followers"].append(float(r["followers"]))

    out: dict[str, list[float]] = {}
    for metric, values in samples.items():
        if len(values) < 8:
            continue
        values.sort()
        out[metric] = [
            values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]
            for p in range(101)
        ]
    return out


def score_author(row: Any, stats: CorpusStats) -> tuple[float, dict[str, Any]]:
    """Computes composite author standing score (0-100) with diagnostic breakdown."""
    skills = row["skills"] or 0
    originality = (row["original_skills"] / skills) if skills else None
    body_of_work = min(1.0, math.log10(skills + 1) / 2.0) if skills else 0.0

    consistency, cons_detail = blend([
        ("licensed", row["licensed_rate"], 0.45),
        ("described", row["described_rate"], 0.35),
        ("multi_repo", min(1.0, (row["skill_repos"] or 0) / 3.0), 0.20),
    ])

    tenure_days = days_since(row["first_repo_at"])
    longevity = None if tenure_days is None else min(1.0, tenure_days / 540.0)

    value, detail = blend([
        ("craft", row["median_craft"], 0.30),
        ("originality", originality, 0.22),
        ("reach", stats.pct("author_stars", row["total_stars"]), 0.16),
        ("followers", stats.pct("author_followers", row["followers"]), 0.08),
        ("body_of_work", body_of_work, 0.10),
        ("consistency", consistency, 0.08),
        ("longevity", longevity, 0.03),
        ("still_active", recency(days_since(row["last_push_at"]), 180.0), 0.03),
    ])

    score = round(100.0 * value, 2)
    return score, {
        "score": score,
        "families": detail,
        "consistency": cons_detail,
        "facts": {
            "skills": skills,
            "original_skills": row["original_skills"],
            "originality": None if originality is None else round(originality, 3),
            "repos_with_skills": row["skill_repos"],
            "total_stars": row["total_stars"],
            "followers": row["followers"],
            "tenure_days": None if tenure_days is None else round(tenure_days),
        },
    }


def recompute_authors(store: Any, stats: CorpusStats, *, keep_detail: bool = True) -> dict[str, Any]:
    """Builds author profiles and computes composite scores."""
    built = build_profiles(store, stats)
    merged = CorpusStats({**stats.quantiles, **author_quantiles(store)}, n=stats.n)

    updates = []
    for row in store.db.execute("SELECT * FROM authors"):
        score, detail = score_author(row, merged)
        updates.append((score, json.dumps(detail) if keep_detail else None, row["login"]))
    store.db.executemany(
        "UPDATE authors SET author_score = ?, score_detail = ? WHERE login = ?",
        updates,
    )
    store.db.commit()
    log.info("Profiled and scored %d authors", len(updates))
    return {"authors": built, "scored": len(updates)}


def author_scores(store: Any) -> dict[str, float]:
    """Returns mapping of author logins to computed author scores."""
    ensure_schema(store)
    return {
        r["login"]: r["author_score"]
        for r in store.db.execute("SELECT login, author_score FROM authors WHERE author_score > 0")
    }


def get_author(store: Any, login: str) -> dict[str, Any] | None:
    """Fetches profile details and score breakdown for a specific author login."""
    ensure_schema(store)
    row = store.db.execute("SELECT * FROM authors WHERE login = ?", (login,)).fetchone()
    if not row:
        return None
    data = dict(row)
    raw = data.pop("score_detail", None)
    if raw:
        try:
            data["breakdown"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
    return data
