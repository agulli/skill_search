"""The ranking layer: turning raw GitHub metadata into a defensible quality prior.

Design rules, each of which exists because the obvious alternative fails:

**Percentile, not magic constants.** Star counts are power-law distributed: the
gap between 10 and 100 stars means far more than the gap between 10,000 and
10,100. Hand-tuned formulas like `9 * log10(stars)` encode a guess about corpus
scale that silently rots as the corpus grows. Instead every heavy-tailed metric
is normalised against the corpus's own distribution, so "top 5% by stars" means
the same thing whether the index holds 500 repositories or 500,000.

**Bounded families.** Signals are grouped into families (popularity, momentum,
maintenance, authority, craft, distinctiveness), each contributing at most its
weight. No single metric can dominate, which is what stops the index from
degenerating into a star-count leaderboard.

**Missing data must not mean zero.** Different endpoints populate different
fields, so a repository discovered via search has no `subscribers` and one that
has never been enriched has no `contributors`. Scoring those as 0 would punish
a repository for *our* crawl budget rather than its own quality. Absent signals
are dropped and their weight is redistributed across the ones we do have.

**Multiplicative trust, additive quality.** Being archived or being a fork is
not "a few points worse" — it is a different category of thing. Those apply as
a multiplier on the whole score, so a fork cannot climb past an original by
accumulating small additive wins elsewhere.

**Explainable.** Every score carries a JSON breakdown of which family
contributed what. A ranking you cannot interrogate is a ranking you cannot
debug, and `skill-engine explain` prints exactly why a result sits where it does.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .metadata import days_since

log = logging.getLogger("skill_engine.ranking")

# Metrics normalised by corpus percentile. Heavy-tailed or scale-dependent
# quantities belong here; bounded ratios and booleans do not.
PERCENTILE_METRICS = (
    "stars", "forks", "subscribers", "open_issues", "size_kb",
    "stars_per_day", "contributors", "releases", "skill_count",
    "body_len", "resource_count",
)


@dataclass
class Weights:
    """Family weights for the composite score. Must be positive; scale is free."""

    # Repository-level families. These decide `repo_score`.
    popularity: float = 0.22
    momentum: float = 0.14
    maintenance: float = 0.16
    authority: float = 0.13

    # Skill-level families, stated explicitly rather than inherited from the
    # repo weights above. Deriving the repo's share by summing the four
    # repository families gave it 65% of a skill's score, which is the wrong
    # balance for a *skill* search engine: it made every skill in one strong
    # repository outrank every skill everywhere else, regardless of how good the
    # individual skill was. The repository is context, not the subject.
    repo_standing: float = 0.32
    author_standing: float = 0.16
    craft: float = 0.33
    distinctiveness: float = 0.19

    # Half-lives in days for the recency curves.
    push_halflife: float = 120.0
    release_halflife: float = 240.0

    # Multiplicative trust penalties.
    archived_factor: float = 0.55
    fork_factor: float = 0.45
    disabled_factor: float = 0.25
    template_factor: float = 0.90
    unlicensed_factor: float = 0.93
    # A repo carrying thousands of SKILL.md files is an aggregator dump, not a
    # curated collection; its individual skills are usually vendored copies.
    dump_threshold: int = 500
    dump_factor: float = 0.70

    # Inorganic-popularity guard. Stars are the cheapest signal to manufacture
    # and the most expensive to ignore, so we cross-check them against the
    # signals that are hard to fake: forks (someone actually took a copy) and
    # contributors (someone actually did work). A repository with thousands of
    # stars and almost no forks is being promoted rather than used.
    # Calibrated against the live corpus rather than guessed. Across repos with
    # >=500 stars the fork/star ratio runs p50=0.104, p25=0.083, p10=0.060,
    # p5=0.048, p1=0.009 — so 0.012 sits just above the 1st percentile and flags
    # ~1.7% of them. Tight enough to catch only the genuine tail; loose enough
    # that ordinary variation never trips it.
    anomaly_min_stars: int = 500
    anomaly_fork_ratio: float = 0.012
    anomaly_factor: float = 0.72


def recency(days: float | None, halflife: float) -> float | None:
    """Exponential decay in [0, 1]. None in, None out — never a silent zero."""
    if days is None:
        return None
    return 0.5 ** (days / halflife)


def blend(components: Iterable[tuple[str, float | None, float]]) -> tuple[float, dict]:
    """Weighted mean over the components that actually have a value.

    Returns (value in [0,1], per-component detail). When a component is None its
    weight is redistributed rather than counted as zero, so a repository is
    never penalised for metadata we did not fetch.
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
        return 0.5, detail  # nothing known: neutral, not zero
    return accumulated / total_weight, detail


class CorpusStats:
    """Quantile boundaries per metric, so scoring is relative to the corpus."""

    def __init__(self, quantiles: dict[str, list[float]] | None = None,
                 n: int = 0) -> None:
        self.quantiles = quantiles or {}
        self.n = n

    @classmethod
    def compute(cls, store) -> "CorpusStats":
        """Derive quantiles from everything currently indexed."""
        rows = store.db.execute(
            """
            SELECT stars, forks, subscribers, open_issues, size_kb, contributors,
                   releases, skill_count, created_at
            FROM repos WHERE archived = 0 AND disabled = 0
            """
        ).fetchall()
        samples: dict[str, list[float]] = {m: [] for m in PERCENTILE_METRICS}
        for r in rows:
            for m in ("stars", "forks", "subscribers", "open_issues", "size_kb",
                      "contributors", "releases", "skill_count"):
                v = r[m]
                if v is not None:
                    samples[m].append(float(v))
            age = days_since(r["created_at"])
            if age is not None and r["stars"] is not None:
                # Floor the age so a three-day-old repo with 5 stars does not
                # read as the fastest-growing project in the corpus.
                samples["stars_per_day"].append(float(r["stars"]) / max(age, 30.0))

        for m, col in (("body_len", "body_len"), ("resource_count", None)):
            if col:
                samples[m] = [
                    float(x["body_len"])
                    for x in store.db.execute(
                        "SELECT body_len FROM skills WHERE valid = 1"
                    )
                ]
        samples["resource_count"] = [
            float(len(json.loads(x["resources"] or "[]")))
            for x in store.db.execute(
                "SELECT resources FROM skills WHERE valid = 1"
            )
        ]

        quantiles: dict[str, list[float]] = {}
        for metric, values in samples.items():
            if len(values) < 8:  # too few to describe a distribution
                continue
            values.sort()
            quantiles[metric] = [
                values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]
                for p in range(101)
            ]
        return cls(quantiles, n=len(rows))

    def save(self, store) -> None:
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
    def load(cls, store) -> "CorpusStats":
        rows = store.db.execute(
            "SELECT metric, quantiles, n FROM corpus_stats"
        ).fetchall()
        return cls(
            {r["metric"]: json.loads(r["quantiles"]) for r in rows},
            n=rows[0]["n"] if rows else 0,
        )

    def pct(self, metric: str, value: float | None) -> float | None:
        """Mid-rank percentile of `value` within the corpus, in [0, 1].

        Mid-rank (averaging the left and right insertion points) is what makes
        this behave at the bottom of the distribution: roughly half the corpus
        has zero stars, and a plain `bisect_left` would score every one of them
        identically to the single least-popular repository.
        """
        if value is None:
            return None
        qs = self.quantiles.get(metric)
        if not qs:
            return None
        lo = bisect.bisect_left(qs, value)
        hi = bisect.bisect_right(qs, value)
        return ((lo + hi) / 2.0) / (len(qs) - 1)


# ------------------------------------------------------------------- signals


def repo_derived(row: Any) -> dict[str, float | None]:
    """Ratios and rates the raw counts cannot express."""
    stars = row["stars"] or 0
    forks = row["forks"] or 0
    age = days_since(row["created_at"])
    return {
        "age_days": age,
        "days_since_push": days_since(row["pushed_at"]),
        "days_since_release": days_since(row["latest_release"]),
        "stars_per_day": (stars / max(age, 30.0)) if age is not None else None,
        # High fork-to-star ratio marks templates and tutorials — things people
        # copy rather than depend on.
        "fork_ratio": (forks / stars) if stars >= 10 else None,
    }


def trust_multiplier(row: Any, w: Weights,
                     derived: dict | None = None) -> tuple[float, dict]:
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
        # Applied only when a hard-to-fake signal is present and contradicts the
        # star count — never on absent data, which would punish un-enriched rows.
        starved_of_forks = fork_ratio is not None and fork_ratio < w.anomaly_fork_ratio
        solo_but_huge = (
            contributors is not None and contributors <= 1 and stars >= 2000
        )
        if starved_of_forks or solo_but_huge:
            apply("inorganic_popularity", w.anomaly_factor)

    return factor, applied


def score_repo(row: Any, stats: CorpusStats, w: Weights = Weights()) -> tuple[float, dict]:
    """Query-independent quality of a repository, 0–100, with a breakdown."""
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
        # An unbounded open-issue pile on a small project reads as abandonment.
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
        "derived": {k: (round(v, 3) if isinstance(v, float) else v)
                    for k, v in d.items()},
    }


def craft_score(skill: Any, stats: CorpusStats) -> tuple[float, dict]:
    """How well-made this skill is, judged only on the file itself.

    Deliberately independent of the repository and the author. That is what
    lets `authors.py` aggregate craft into an author's reputation without
    circularity: author standing feeds the skill score, so the skill signal it
    is built from must not already contain author standing.
    """
    resources = json.loads(skill["resources"] or "[]")
    tools = json.loads(skill["allowed_tools"] or "[]")
    warnings = [x for x in (skill["warnings"] or "").split("; ") if x]
    dlen = len(skill["description"] or "")

    # A description is a retrieval surface: too short says nothing, too long
    # stops being a summary. The spec's own limit is 1024.
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
    name_collisions: int = 1,
    author_score: float | None = None,
) -> tuple[float, dict]:
    """Quality of one skill: its own craft, its repository, and its author."""
    repo_score, repo_detail = score_repo(repo, stats)
    craft, craft_detail = craft_score(skill, stats)

    distinct, dist_detail = blend([
        # Copies dilute: 1 copy is unique, 10 copies is boilerplate.
        ("uniqueness", 1.0 / (1.0 + math.log2(max(dup_count, 1))), 0.60),
        ("name_uniqueness", 1.0 / (1.0 + 0.5 * math.log2(max(name_collisions, 1))), 0.20),
        # A repo of 12 curated skills beats one of 4,000 scraped ones.
        ("repo_focus", 1.0 if (repo["skill_count"] or 1) <= 60 else
                       max(0.25, 60.0 / (repo["skill_count"] or 1)), 0.20),
    ])

    base, families = blend([
        ("repo_standing", repo_score / 100.0, w.repo_standing),
        # None when the author has not been profiled: `blend` redistributes the
        # weight rather than scoring them zero for our missing data.
        ("author_standing",
         None if author_score is None else author_score / 100.0, w.author_standing),
        ("craft", craft, w.craft),
        ("distinctiveness", distinct, w.distinctiveness),
    ])
    trust, trust_detail = trust_multiplier(repo, w)
    score = round(100.0 * base * trust, 2)

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


# --------------------------------------------------------------- recompute


def recompute(store, w: Weights = Weights(), *, keep_detail: bool = True) -> dict:
    """Rescore every repository and skill from stored data. No network access.

    Deliberately a separate pass rather than something done during the crawl:
    percentile normalisation needs the whole corpus, so a score assigned while
    crawling would be computed against a distribution that no longer exists by
    the time the crawl ends. Re-run this after any significant ingest.
    """
    stats = CorpusStats.compute(store)
    stats.save(store)
    log.info("corpus stats over %d repos, %d metrics", stats.n, len(stats.quantiles))

    repos = {r["full_name"]: r for r in store.db.execute("SELECT * FROM repos")}
    updates = []
    for full_name, row in repos.items():
        score, detail = score_repo(row, stats, w)
        updates.append((score, json.dumps(detail) if keep_detail else None, full_name))
    store.db.executemany(
        "UPDATE repos SET repo_score = ?, score_detail = ? WHERE full_name = ?",
        updates,
    )

    dup_counts = {
        r["content_hash"]: r["c"]
        for r in store.db.execute(
            "SELECT content_hash, COUNT(*) c FROM skills "
            "WHERE content_hash != '' GROUP BY content_hash"
        )
    }

    # Duplicate counts must be written before authors are profiled: originality
    # is measured from them, and a stale dup_count would credit a wholesale
    # copier with original work.
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
        log.warning("author scoring failed (continuing without it): %s", exc)
        authors = {}
    name_counts = {
        r["name"]: r["c"]
        for r in store.db.execute(
            "SELECT name, COUNT(DISTINCT repo) c FROM skills "
            "WHERE name != '' GROUP BY name"
        )
    }

    skill_updates = []
    for s in store.db.execute("SELECT * FROM skills"):
        repo = repos.get(s["repo"])
        if repo is None:
            continue
        dups = dup_counts.get(s["content_hash"], 1)
        score, detail = score_skill(
            s, repo, stats, w,
            dup_count=dups,
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


def default_weights() -> dict:
    return asdict(Weights())
