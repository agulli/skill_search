"""Quality ranking engine: computes corpus-calibrated priors from repository and skill metadata.

Architecture Principles:
1. Percentile Normalization: Heavy-tailed signals (stars, forks, repo size) are normalized
   against the empirical corpus distribution rather than arbitrary log constants.
2. Decoupled Families: Signals are organized into decoupled families (popularity, momentum,
   maintenance, authority, craft, distinctiveness) with bounded weights.
3. Missing Data Neutrality: When specific metadata fields are missing, weights are dynamically
   redistributed across available signals rather than penalizing missing data with zeros.
4. Multiplicative Trust Penalties: Structural status (archived, forks, disabled, aggregator dumps)
   applies multiplicatively to the entire composite score.
5. Explainability: Every score retains a full JSON breakdown of family contributions.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .metadata import days_since

log = logging.getLogger("skill_engine.ranking")

PERCENTILE_METRICS = (
    "stars", "forks", "subscribers", "open_issues", "size_kb",
    "stars_per_day", "contributors", "releases", "skill_count",
    "body_len", "resource_count",
)


@dataclass
class Weights:
    """Weights and parameters for composite quality scoring."""

    # Repository-level scoring weights
    popularity: float = 0.22
    momentum: float = 0.14
    maintenance: float = 0.16
    authority: float = 0.13

    # Skill-level composite weights
    repo_standing: float = 0.32
    author_standing: float = 0.16
    craft: float = 0.33
    distinctiveness: float = 0.19

    # Recency half-life parameters in days
    push_halflife: float = 120.0
    release_halflife: float = 240.0

    # Multiplicative trust penalty factors
    archived_factor: float = 0.55
    fork_factor: float = 0.45
    disabled_factor: float = 0.25
    template_factor: float = 0.90
    unlicensed_factor: float = 0.93

    # Aggregator repository thresholds
    dump_threshold: int = 500
    dump_factor: float = 0.70

    # Synthetic popularity guard thresholds
    anomaly_min_stars: int = 500
    anomaly_fork_ratio: float = 0.012
    anomaly_factor: float = 0.72


def recency(days: float | None, halflife_days: float) -> float | None:
    """Computes exponential half-life decay.

    Args:
        days: Elapsed days since event, or None.
        halflife_days: Half-life in days where score equals 0.5.

    Returns:
        Decayed score in (0, 1] or None if days is None.
    """
    if days is None:
        return None
    return math.exp(-max(days, 0.0) * (math.log(2.0) / max(halflife_days, 1.0)))


def blend(components: Iterable[tuple[str, float | None, float]]) -> tuple[float, dict[str, Any]]:
    """Weighted sum of available signals, redistributing weights for missing components.

    Args:
        components: Tuples of (name, value_or_None, weight).

    Returns:
        Tuple of (normalized_score, detail_dictionary).
    """
    total_weight = 0.0
    accumulated = 0.0
    detail: dict[str, Any] = {}
    for name, value, weight in components:
        if value is None:
            detail[name] = None
            continue
        value = max(0.0, min(1.0, float(value)))
        detail[name] = round(value, 4)
        accumulated += value * weight
        total_weight += weight
    if total_weight == 0.0:
        return 0.5, detail
    return accumulated / total_weight, detail


class CorpusStats:
    """Manages empirical quantile distributions for corpus-relative scoring."""

    def __init__(
        self,
        quantiles: dict[str, list[float]] | None = None,
        n: int = 0,
    ) -> None:
        self.quantiles = quantiles or {}
        self.n = n

    @classmethod
    def compute(cls, store: Any) -> "CorpusStats":
        """Calculates percentile thresholds from active database records."""
        rows = store.db.execute(
            """
            SELECT stars, forks, subscribers, open_issues, size_kb, contributors,
                   releases, skill_count, created_at
            FROM repos WHERE archived = 0 AND disabled = 0
            """
        ).fetchall()
        samples: dict[str, list[float]] = {m: [] for m in PERCENTILE_METRICS}
        for r in rows:
            for m in (
                "stars", "forks", "subscribers", "open_issues", "size_kb",
                "contributors", "releases", "skill_count",
            ):
                v = r[m]
                if v is not None:
                    samples[m].append(float(v))
            age = days_since(r["created_at"])
            if age is not None and r["stars"] is not None:
                samples["stars_per_day"].append(float(r["stars"]) / max(age, 30.0))

        for m, col in (("body_len", "body_len"), ("resource_count", None)):
            if col:
                samples[m] = [
                    float(x["body_len"])
                    for x in store.db.execute("SELECT body_len FROM skills WHERE valid = 1")
                ]
        samples["resource_count"] = [
            float(len(json.loads(x["resources"] or "[]")))
            for x in store.db.execute("SELECT resources FROM skills WHERE valid = 1")
        ]

        quantiles: dict[str, list[float]] = {}
        for metric, values in samples.items():
            if len(values) < 8:
                continue
            values.sort()
            quantiles[metric] = [
                values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]
                for p in range(101)
            ]
        return cls(quantiles, n=len(rows))

    def save(self, store: Any) -> None:
        """Persists computed quantile boundaries to database."""
        now = time.time()
        for metric, qs in self.quantiles.items():
            store.db.execute(
                "INSERT INTO corpus_stats(metric, quantiles, n, computed_at) "
                "VALUES(?,?,?,?) ON CONFLICT(metric) DO UPDATE SET "
                "quantiles=excluded.quantiles, n=excluded.n, computed_at=excluded.computed_at",
                (metric, json.dumps(qs), self.n, now),
            )
        store.commit()

    @classmethod
    def load(cls, store: Any) -> "CorpusStats":
        """Loads quantile distributions from database."""
        rows = store.db.execute("SELECT metric, quantiles, n FROM corpus_stats").fetchall()
        return cls(
            {r["metric"]: json.loads(r["quantiles"]) for r in rows},
            n=rows[0]["n"] if rows else 0,
        )

    def pct(self, metric: str, value: float | None) -> float | None:
        """Returns mid-rank percentile of value within corpus distribution in range [0, 1]."""
        if value is None:
            return None
        qs = self.quantiles.get(metric)
        if not qs:
            return None
        lo = bisect.bisect_left(qs, value)
        hi = bisect.bisect_right(qs, value)
        return ((lo + hi) / 2.0) / (len(qs) - 1)


def repo_derived(row: Any) -> dict[str, float | None]:
    """Extracts derived rates and metrics from raw repository attributes."""
    stars = row["stars"] or 0
    forks = row["forks"] or 0
    age = days_since(row["created_at"])
    return {
        "age_days": age,
        "days_since_push": days_since(row["pushed_at"]),
        "days_since_release": days_since(row["latest_release"]),
        "stars_per_day": (stars / max(age, 30.0)) if age is not None else None,
        "fork_ratio": (forks / stars) if stars >= 10 else None,
    }


def trust_multiplier(
    row: Any,
    w: Weights,
    derived: dict[str, Any] | None = None,
) -> tuple[float, dict[str, float]]:
    """Calculates multiplicative trust factor based on repo status flags."""
    factor = 1.0
    applied: dict[str, float] = {}

    def apply(name: str, value: float) -> None:
        nonlocal factor
        factor *= value
        applied[name] = value

    if row["archived"]:
        apply("archived", w.archived_factor)
    if row["is_fork"]:
        apply("fork", w.fork_factor)
    if row["disabled"]:
        apply("disabled", w.disabled_factor)
    if row["is_template"]:
        apply("template", w.template_factor)
    if not row["license"]:
        apply("unlicensed", w.unlicensed_factor)
    if (row["skill_count"] or 0) > w.dump_threshold:
        apply("aggregator_dump", w.dump_factor)

    d = derived if derived is not None else repo_derived(row)
    stars = row["stars"] or 0
    if stars >= w.anomaly_min_stars:
        fork_ratio = d.get("fork_ratio")
        contributors = row["contributors"]
        starved_of_forks = fork_ratio is not None and fork_ratio < w.anomaly_fork_ratio
        solo_but_huge = contributors is not None and contributors <= 1 and stars >= 2000
        if starved_of_forks or solo_but_huge:
            apply("inorganic_popularity", w.anomaly_factor)

    return factor, applied


def score_repo(
    row: Any,
    stats: CorpusStats,
    w: Weights = Weights(),
) -> tuple[float, dict[str, Any]]:
    """Computes normalized quality score (0-100) for a repository."""
    d = repo_derived(row)
    topics = json.loads(row["topics"] or "[]")

    popularity, pop_detail = blend([
        ("stars", stats.pct("stars", row["stars"]), 0.60),
        ("forks", stats.pct("forks", row["forks"]), 0.25),
        ("subscribers", stats.pct("subscribers", row["subscribers"]), 0.15),
    ])

    momentum, mom_detail = blend([
        ("stars_per_day", stats.pct("stars_per_day", d["stars_per_day"]), 0.65),
        ("push_recency", recency(d["days_since_push"], w.push_halflife), 0.35),
    ])

    maintenance, main_detail = blend([
        ("push_recency", recency(d["days_since_push"], w.push_halflife), 0.50),
        ("release_recency", recency(d["days_since_release"], w.release_halflife), 0.20),
        ("release_count", stats.pct("releases", row["releases"]), 0.10),
        ("issues_enabled", 1.0 if row["has_issues"] else 0.0, 0.10),
        ("issue_load", 1.0 - (stats.pct("open_issues", row["open_issues"]) or 0.5), 0.10),
    ])

    authority, auth_detail = blend([
        ("org_owned", 1.0 if row["owner_type"] == "Organization" else 0.35, 0.25),
        ("licensed", 1.0 if row["license"] else 0.0, 0.20),
        ("described", 1.0 if (row["description"] or "").strip() else 0.0, 0.15),
        ("topics_curated", min(len(topics) / 5.0, 1.0), 0.15),
        ("homepage", 1.0 if row["homepage"] else 0.0, 0.05),
        ("contributors", stats.pct("contributors", row["contributors"]), 0.20),
    ])

    base, detail = blend([
        ("popularity", popularity, w.popularity),
        ("momentum", momentum, w.momentum),
        ("maintenance", maintenance, w.maintenance),
        ("authority", authority, w.authority),
    ])
    trust, trust_detail = trust_multiplier(row, w, d)
    score = round(100.0 * base * trust, 2)

    return score, {
        "score": score,
        "base": round(base, 4),
        "trust": round(trust, 4),
        "families": detail,
        "popularity": pop_detail,
        "momentum": mom_detail,
        "maintenance": main_detail,
        "authority": auth_detail,
        "penalties": trust_detail,
        "derived": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()},
    }


def craft_score(skill: Any, stats: CorpusStats) -> tuple[float, dict[str, Any]]:
    """Evaluates the structural craft quality of a single SKILL.md file."""
    resources = json.loads(skill["resources"] or "[]")
    tools = json.loads(skill["allowed_tools"] or "[]")
    warnings = [x for x in (skill["warnings"] or "").split("; ") if x]
    dlen = len(skill["description"] or "")

    if dlen == 0:
        desc_fit = 0.0
    elif dlen < 40:
        desc_fit = 0.35
    elif dlen <= 700:
        desc_fit = 1.0
    elif dlen <= 1024:
        desc_fit = 0.8
    else:
        desc_fit = 0.55

    return blend([
        ("valid", 1.0 if skill["valid"] else 0.0, 0.28),
        ("description_fit", desc_fit, 0.22),
        ("body_depth", stats.pct("body_len", skill["body_len"]), 0.20),
        ("bundled_resources", stats.pct("resource_count", float(len(resources))), 0.15),
        ("declares_tools", 1.0 if tools else 0.0, 0.07),
        ("spec_clean", 1.0 if not warnings else max(0.0, 1.0 - 0.34 * len(warnings)), 0.08),
    ])


def score_skill(
    skill: Any,
    repo: Any,
    stats: CorpusStats,
    w: Weights = Weights(),
    *,
    dup_count: int = 1,
    owner_count: int = 1,
    name_collisions: int = 1,
    author_score: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Computes the composite quality score for a skill across craft, repo, and author signals."""
    repo_score, repo_detail = score_repo(repo, stats)
    craft, craft_detail = craft_score(skill, stats)

    owners = max(int(owner_count or 1), 1)
    copies_per_owner = max(dup_count, 1) / owners

    distinct, dist_detail = blend([
        ("adoption", 0.5 + 0.5 * min(1.0, math.log2(1 + owners) / 11.0), 0.40),
        ("not_sprawl", 1.0 / (1.0 + math.log2(max(copies_per_owner, 1.0))), 0.20),
        ("name_uniqueness", 1.0 / (1.0 + 0.5 * math.log2(max(name_collisions, 1))), 0.20),
        ("repo_focus", 1.0 if (repo["skill_count"] or 1) <= 60 else max(0.25, 60.0 / (repo["skill_count"] or 1)), 0.20),
    ])

    base, families = blend([
        ("repo_standing", repo_score / 100.0, w.repo_standing),
        ("author_standing", None if author_score is None else author_score / 100.0, w.author_standing),
        ("craft", craft, w.craft),
        ("distinctiveness", distinct, w.distinctiveness),
    ])
    trust, trust_detail = trust_multiplier(repo, w)

    # Safety is multiplicative for the same reason the other trust penalties
    # are: a skill that instructs an agent to exfiltrate credentials must not
    # be able to climb back past a safe one by being well-written.
    from .safety import penalty as safety_penalty
    level = (skill["risk_level"] if "risk_level" in skill.keys() else None) or "none"
    safety = safety_penalty(level)
    if safety < 1.0:
        trust_detail = {**trust_detail, "safety": {"level": level,
                                                   "factor": safety}}
    score = round(100.0 * base * trust * safety, 2)

    return score, {
        "score": score,
        "base": round(base, 4),
        "trust": round(trust, 4),
        "families": families,
        "craft": craft_detail,
        "distinctiveness": dist_detail,
        "repo": {"score": repo_score, "families": repo_detail["families"]},
        "author": {"score": author_score},
        "penalties": trust_detail,
        "dup_count": dup_count,
        "name_collisions": name_collisions,
    }


def recompute(store: Any, w: Weights = Weights(), *, keep_detail: bool = True) -> dict[str, Any]:
    """Recomputes scores for all repositories and skills in the store."""
    stats = CorpusStats.compute(store)
    stats.save(store)
    log.info("Corpus statistics computed over %d repositories, %d metrics", stats.n, len(stats.quantiles))

    repos = {r["full_name"]: r for r in store.db.execute("SELECT * FROM repos")}
    updates = []
    for full_name, row in repos.items():
        score, detail = score_repo(row, stats, w)
        updates.append((score, json.dumps(detail) if keep_detail else None, full_name))
    store.db.executemany(
        "UPDATE repos SET repo_score = ?, score_detail = ? WHERE full_name = ?",
        updates,
    )

    dup_counts = {}
    owner_counts = {}
    for r in store.db.execute(
        "SELECT s.content_hash AS h, COUNT(*) AS c, "
        "       COUNT(DISTINCT substr(s.repo, 1, instr(s.repo,'/') - 1)) AS o "
        "FROM skills s WHERE s.content_hash != '' GROUP BY s.content_hash"
    ):
        dup_counts[r["h"]] = r["c"]
        owner_counts[r["h"]] = r["o"]

    store.db.executemany(
        "UPDATE skills SET dup_count = ? WHERE content_hash = ?",
        [(c, h) for h, c in dup_counts.items()],
    )
    store.commit()

    from .authors import author_scores, recompute_authors

    try:
        recompute_authors(store, stats, keep_detail=keep_detail)
        authors = author_scores(store)
    except Exception as exc:
        log.warning("Author scoring failed (continuing without it): %s", exc)
        authors = {}

    name_counts = {
        r["name"]: r["c"]
        for r in store.db.execute(
            "SELECT name, COUNT(DISTINCT repo) c FROM skills WHERE name != '' GROUP BY name"
        )
    }

    skill_updates = []
    for s in store.db.execute("SELECT * FROM skills"):
        repo = repos.get(s["repo"])
        if repo is None:
            continue
        dups = dup_counts.get(s["content_hash"], 1)
        score, detail = score_skill(
            s,
            repo,
            stats,
            w,
            dup_count=dups,
            owner_count=owner_counts.get(s["content_hash"], 1),
            name_collisions=name_counts.get(s["name"], 1),
            author_score=authors.get(s["repo"].split("/", 1)[0]),
        )
        skill_updates.append((
            score, json.dumps(detail) if keep_detail else None, dups, s["id"]
        ))
    store.db.executemany(
        "UPDATE skills SET score = ?, score_detail = ?, dup_count = ? WHERE id = ?",
        skill_updates,
    )
    store.commit()

    return {
        "repos_scored": len(updates),
        "skills_scored": len(skill_updates),
        "authors_scored": len(authors),
        "metrics": len(stats.quantiles),
        "corpus_n": stats.n,
    }


def default_weights() -> dict[str, Any]:
    """Returns default ranking weights as dictionary."""
    return asdict(Weights())
