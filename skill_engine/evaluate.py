"""Retrieval quality evaluation.

Every other test in this project asserts a *property* — that duplicates
collapse, that missing data does not zero a score, that a fork cannot outrank
its original. None of them answer the question that actually matters: **when
someone searches, do they get the right skill?**

Answering that normally needs human relevance judgements, which do not exist for
this corpus. What follows are the evaluations that can be run without them,
each measuring something a labelled set would otherwise tell us.

### Known-item retrieval — the primary metric

The strongest label-free signal in information retrieval. Take a skill that is
already in the index, construct a query from its *description* (never its name,
which would make the task trivial), and check where the engine ranks that exact
skill. The correct answer is known by construction, so precision, recall and
MRR are all computable.

Two details make it honest:

* Queries are built from mid-frequency terms. Rare terms make retrieval trivial
  (one document contains them); common terms make it impossible. The middle
  band is where real queries live.
* A hit counts when the returned skill shares the target's **content hash**.
  The engine deliberately collapses duplicates, so demanding the exact row id
  would score correct behaviour as failure.

### Supporting evaluations

* **Category coherence** — do results for a subject-specific query belong to
  that subject, judged against the independently-built taxonomy?
* **Robustness** — does the same intent expressed with reordered words, extra
  filler, or fewer terms return the same thing?
* **Ranking sanity** — among equally relevant results, do better skills win?
* **Latency distribution** — percentiles across a realistic query mix, not one
  cherry-picked query.
"""

from __future__ import annotations

import random
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from .search import STOPWORDS, search
from .store import Store

WORD_RE = re.compile(r"[a-z][a-z0-9+#.-]{2,}")


@dataclass
class Result:
    name: str
    n: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- known item


def _document_frequencies(store: Store, sample: int = 20_000) -> dict[str, int]:
    """How many skills contain each term, from a sample of the corpus."""
    df: dict[str, int] = {}
    for row in store.db.execute(
        "SELECT description FROM skills WHERE valid = 1 AND description != '' "
        "LIMIT ?", (sample,)
    ):
        for term in set(WORD_RE.findall((row["description"] or "").lower())):
            df[term] = df.get(term, 0) + 1
    return df


def build_query(description: str, df: dict[str, int], n_terms: int,
                lo: int = 20, hi: int = 4000) -> str | None:
    """Make a query from a description using mid-frequency terms only.

    Terms appearing in fewer than `lo` skills make retrieval trivial; terms in
    more than `hi` carry no signal. Sampling from the band between them is what
    keeps the benchmark honest — it approximates the vocabulary a real user
    reaches for.
    """
    terms = [
        t for t in dict.fromkeys(WORD_RE.findall(description.lower()))
        if t not in STOPWORDS and lo <= df.get(t, 0) <= hi
    ]
    if len(terms) < n_terms:
        return None
    return " ".join(terms[:n_terms])


def known_item(store: Store, *, n: int = 300, n_terms: int = 5,
               limit: int = 10, seed: int = 7, min_desc: int = 80) -> Result:
    """Can the engine find a specific skill from a description of it?"""
    rng = random.Random(seed)
    df = _document_frequencies(store)

    rows = store.db.execute(
        "SELECT id, name, description, content_hash FROM skills "
        "WHERE valid = 1 AND LENGTH(description) > ? "
        "ORDER BY id LIMIT 60000", (min_desc,)
    ).fetchall()
    if not rows:
        return Result("known-item", 0, notes=["no valid skills to sample"])

    targets = rng.sample(rows, min(n * 3, len(rows)))
    ranks: list[int | None] = []
    skipped = 0

    for row in targets:
        if len(ranks) >= n:
            break
        query = build_query(row["description"], df, n_terms)
        if not query:
            skipped += 1
            continue
        hits = search(store, query, limit=limit, max_per_repo=None)
        # Duplicates are collapsed by design, so match on content, not row id.
        wanted = row["content_hash"]
        rank = next(
            (i + 1 for i, h in enumerate(hits)
             if h.content_hash == wanted or h.skill_id == row["id"]),
            None,
        )
        ranks.append(rank)

    found = [r for r in ranks if r]
    total = len(ranks) or 1
    mrr = sum(1 / r for r in found) / total
    return Result(
        f"known-item ({n_terms} terms)",
        total,
        {
            "recall@1": sum(1 for r in found if r == 1) / total,
            "recall@3": sum(1 for r in found if r <= 3) / total,
            "recall@5": sum(1 for r in found if r <= 5) / total,
            f"recall@{limit}": len(found) / total,
            "MRR": mrr,
            "median_rank": statistics.median(found) if found else None,
        },
        [f"{skipped} skills skipped (too few mid-frequency terms)"],
    )


# ------------------------------------------------------------ supporting


def category_coherence(store: Store, *, per_category: int = 8,
                       limit: int = 10) -> Result:
    """Do subject queries return skills the taxonomy filed under that subject?

    The taxonomy is built from patterns and the index from BM25; they share no
    machinery, so agreement between them is genuine corroboration rather than
    the same signal counted twice.
    """
    from .taxonomy import TAXONOMY

    scores: dict[str, float] = {}
    for cat in TAXONOMY:
        queries = [s.label.lower() for s in cat.subs][:per_category]
        if not queries:
            continue
        agree = 0.0
        for q in queries:
            hits = search(store, q, limit=limit)
            if not hits:
                continue
            ids = [h.skill_id for h in hits]
            marks = store.db.execute(
                f"SELECT category FROM skills WHERE id IN "
                f"({','.join('?' * len(ids))})", ids
            ).fetchall()
            if marks:
                agree += sum(1 for m in marks if m["category"] == cat.id) / len(marks)
        scores[cat.id] = agree / len(queries)

    return Result("category coherence", len(scores),
                  {"mean_agreement": statistics.mean(scores.values()) if scores else 0,
                   "per_category": {k: round(v, 3) for k, v in
                                    sorted(scores.items(), key=lambda x: -x[1])}})


def robustness(store: Store, queries: list[str], *, limit: int = 10) -> Result:
    """Does the same intent, phrased differently, return the same results?

    Each query is perturbed the way real users vary: reordered words, added
    filler, a dropped term. A retrieval system that swings wildly under these
    is fitting the phrasing rather than the intent.
    """
    rng = random.Random(11)
    overlaps: dict[str, list[float]] = {"reordered": [], "filler": [], "dropped": []}

    for q in queries:
        base = [h.skill_id for h in search(store, q, limit=limit)]
        if not base:
            continue
        words = q.split()
        variants = {
            "reordered": " ".join(rng.sample(words, len(words))),
            "filler": f"how do i {q} please",
            "dropped": " ".join(words[:-1]) if len(words) > 2 else q,
        }
        for kind, variant in variants.items():
            got = [h.skill_id for h in search(store, variant, limit=limit)]
            if got:
                overlaps[kind].append(len(set(base) & set(got)) / len(base))

    return Result("robustness", len(queries),
                  {k: statistics.mean(v) if v else 0.0 for k, v in overlaps.items()})


def ranking_sanity(store: Store, queries: list[str], *, limit: int = 10) -> Result:
    """Among retrieved results, do better-scoring skills sit higher?

    Reported as Spearman-style rank agreement between result position and the
    quality prior. Perfect agreement is not the goal — relevance should
    dominate — but a negative correlation would mean the prior is inverted.
    """
    agreements = []
    for q in queries:
        hits = search(store, q, limit=limit)
        if len(hits) < 4:
            continue
        scores = [h.score for h in hits]
        pairs = concordant = 0
        for i in range(len(scores)):
            for j in range(i + 1, len(scores)):
                if scores[i] == scores[j]:
                    continue
                pairs += 1
                concordant += scores[i] > scores[j]
        if pairs:
            agreements.append(concordant / pairs)
    return Result("ranking sanity", len(agreements),
                  {"mean_concordance": statistics.mean(agreements) if agreements else 0})


def latency(store: Store, queries: list[str], *, limit: int = 20) -> Result:
    """Percentiles over a realistic query mix, not one convenient query."""
    times = []
    for q in queries:
        search(store, q, limit=limit)              # warm
        t = time.perf_counter()
        search(store, q, limit=limit)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()

    def pct(p):
        return times[min(len(times) - 1, int(p / 100 * len(times)))]

    return Result("latency", len(times),
                  {"p50_ms": pct(50), "p90_ms": pct(90), "p99_ms": pct(99),
                   "max_ms": times[-1]})


def coverage(store: Store, queries: list[str], *, limit: int = 10) -> Result:
    """How often a query returns nothing, or too little to choose from."""
    empty = thin = 0
    for q in queries:
        n = len(search(store, q, limit=limit))
        empty += n == 0
        thin += 0 < n < 3
    total = len(queries) or 1
    return Result("coverage", total,
                  {"empty_rate": empty / total, "thin_rate": thin / total})


REAL_QUERIES = [
    "extract tables from a pdf invoice", "write terraform modules for aws",
    "review a react component for accessibility", "summarise a research paper",
    "build a slide deck from notes", "analyse a spreadsheet and chart it",
    "kubernetes deployment troubleshooting", "security audit of a python codebase",
    "generate unit tests with pytest", "scrape a website and clean the data",
    "convert markdown to a word document", "optimise slow sql queries",
    "write a product requirements document", "refactor legacy javascript",
    "set up continuous integration", "debug a memory leak",
    "design a database schema", "create a marketing email campaign",
    "explain a git merge conflict", "build an mcp server",
    "transcribe and summarise a meeting", "generate api documentation",
    "audit dependencies for vulnerabilities", "plan a sprint backlog",
    "make a chart from csv data", "write a dockerfile",
    "improve website seo", "translate documentation to spanish",
    "review terraform for security issues", "extract text from a scanned image",
]


def run_all(store: Store, *, quick: bool = False) -> list[Result]:
    n = 100 if quick else 300
    results = [
        known_item(store, n=n, n_terms=5),
        known_item(store, n=n, n_terms=3),
        known_item(store, n=n, n_terms=8),
        coverage(store, REAL_QUERIES),
        robustness(store, REAL_QUERIES[:15]),
        ranking_sanity(store, REAL_QUERIES),
        latency(store, REAL_QUERIES),
    ]
    if not quick:
        results.append(category_coherence(store))
    return results


def format_results(results: list[Result]) -> str:
    lines = []
    for r in results:
        lines.append(f"\n{r.name}  (n={r.n})")
        for k, v in r.detail.items():
            if isinstance(v, dict):
                lines.append(f"  {k}:")
                for kk, vv in list(v.items())[:20]:
                    lines.append(f"    {kk:<22} {vv}")
            elif isinstance(v, float):
                fmt = f"{v:.3f}" if v < 10 else f"{v:,.1f}"
                lines.append(f"  {k:<22} {fmt}")
            else:
                lines.append(f"  {k:<22} {v}")
        for note in r.notes:
            lines.append(f"  note: {note}")
    return "\n".join(lines)
