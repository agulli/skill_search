"""Hybrid retrieval: BM25 over FTS5, fused with optional vector similarity.

Ranking is three signals combined:

1. **BM25** from SQLite's FTS5, with per-column weights — a query term matching
   a skill's `name` means far more than the same term buried in its body.
2. **Vector cosine**, when embeddings are enabled, for queries phrased
   differently from the skill's own vocabulary.
3. **A quality prior** from `parse.quality_score` — stars, freshness, licence,
   whether the frontmatter validates.

BM25 and vector scores live on incomparable scales, so we fuse them with
weighted Reciprocal Rank Fusion rather than trying to normalise and add them.
RRF only looks at rank position, which makes it robust to exactly the scale
mismatch that breaks naive weighted sums.

Crucially, quality goes into the *same* fusion as a third ranked list rather
than being blended in afterwards as a 0–1 number. Mixing the two spaces does
not work: RRF scores are compressed (with k=60, first place beats second by
1.6%), so any prior on a full 0–1 scale silently overrules the retrievers and
you end up ranking by popularity with a search box attached. Ranking the
candidates by quality and fusing that list keeps every signal in rank space,
where the weights mean what they say.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .embed import build as build_embedder, cosine, unpack
from .store import Store

# The damping constant. The literature's k=60 is tuned for fusing long TREC-scale
# result lists; we care about discrimination inside the top ten, where a smaller
# k keeps real separation between the first few positions.
RRF_K = 20

WEIGHT_KEYWORD = 1.0
WEIGHT_VECTOR = 0.9   # slightly below keyword: exact terms are strong evidence here
WEIGHT_QUALITY = 0.5  # a tie-breaker among relevant hits, never a substitute

# FTS5 column weights: name, description, body, repo, path.
COLUMN_WEIGHTS = (10.0, 6.0, 1.0, 2.0, 1.5)

FTS_SPECIAL = re.compile(r'["*():^-]')

# Words that appear in almost every SKILL.md and so separate nothing. Kept
# deliberately small: over-pruning a domain vocabulary is far more damaging than
# leaving a common word in.
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "by", "for", "from", "with", "into", "as", "is", "are", "was", "be", "been",
    "it", "its", "this", "that", "these", "those", "i", "me", "my", "we", "our",
    "you", "your", "how", "do", "does", "can", "will", "would", "should",
    "use", "using", "used", "when", "want", "need", "make", "get",
}


@dataclass
class Hit:
    skill_id: int
    repo: str
    path: str
    name: str
    description: str
    source_kind: str
    license: str | None
    score: float
    stars: int = 0
    content_hash: str = ""
    duplicates: int = 0
    author_score: float | None = None

    @property
    def author(self) -> str:
        return self.repo.split("/", 1)[0]
    snippet: str = ""
    resources: list[str] = field(default_factory=list)
    rank: float = 0.0
    matched_by: str = ""

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/blob/HEAD/{self.path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "repo": self.repo,
            "path": self.path,
            "url": self.url,
            "kind": self.source_kind,
            "license": self.license,
            "stars": self.stars,
            "quality": round(self.score, 1),
            "rank": round(self.rank, 5),
            "matched_by": self.matched_by,
            "duplicates": self.duplicates,
            "author": self.author,
            "author_score": (None if self.author_score is None
                             else round(self.author_score, 1)),
            "snippet": self.snippet,
            "resources": self.resources,
        }


def to_fts_query(raw: str) -> str:
    """Turn user text into an FTS5 MATCH expression that cannot throw.

    FTS5 raises on unbalanced quotes and stray operators, so every term is
    quoted. Terms are OR-ed with prefix matching, which behaves far better than
    the implicit AND for natural-language queries where not every word appears.

    Stopwords are dropped, and that is a performance fix as much as a relevance
    one: under OR semantics, "extract tables from a pdf" matched 86,036 skills
    because *a* and *from* appear in nearly every document, and ranking that set
    took 1.6 seconds. Removing them leaves the terms that actually discriminate.
    Dropped only when something survives — a search for "the" should still
    search for "the" rather than silently return nothing.
    """
    terms = [t for t in re.split(r"\s+", FTS_SPECIAL.sub(" ", raw).strip()) if t]
    if not terms:
        return ""
    kept = [t for t in terms if t.lower() not in STOPWORDS]
    terms = kept or terms
    return " OR ".join(f'"{t}"*' if len(t) > 2 else f'"{t}"' for t in terms)


def _where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if filters.get("valid_only", True):
        clauses.append("s.valid = 1")
    if filters.get("min_stars"):
        clauses.append("r.stars >= ?")
        params.append(int(filters["min_stars"]))
    if filters.get("license"):
        clauses.append("(s.license = ? OR r.license = ?)")
        params.extend([filters["license"], filters["license"]])
    if filters.get("kind"):
        clauses.append("s.source_kind = ?")
        params.append(filters["kind"])
    if filters.get("language"):
        clauses.append("r.language = ?")
        params.append(filters["language"])
    if filters.get("min_score"):
        clauses.append("s.score >= ?")
        params.append(float(filters["min_score"]))
    if filters.get("max_age_days"):
        # Compare ISO-8601 strings directly: they sort chronologically.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(filters["max_age_days"]))
        ).isoformat().replace("+00:00", "Z")
        clauses.append("r.pushed_at >= ?")
        params.append(cutoff)
    if filters.get("owner_type"):
        clauses.append("r.owner_type = ?")
        params.append(filters["owner_type"])
    if filters.get("repo"):
        clauses.append("s.repo LIKE ?")
        params.append(f"%{filters['repo']}%")
    if not filters.get("include_forks", False):
        clauses.append("r.is_fork = 0")
    if not filters.get("include_archived", False):
        clauses.append("r.archived = 0")
    return (" AND ".join(clauses) or "1=1"), params


def count_matches(store: Store, query: str, filters: dict | None = None) -> int:
    """How many skills match, before ranking or truncation.

    Kept separate from `keyword_search` because it needs no bm25 ordering and no
    snippet extraction — the two expensive parts — so it stays cheap enough to
    show a live result count.
    """
    fts = to_fts_query(query)
    if not fts:
        return 0
    where, params = _where(filters or {})
    try:
        row = store.db.execute(
            f"""SELECT COUNT(*) c FROM skills_fts
                JOIN skills s ON s.id = skills_fts.rowid
                JOIN repos  r ON r.full_name = s.repo
                WHERE skills_fts MATCH ? AND {where}""",
            [fts, *params],
        ).fetchone()
        return row["c"] if row else 0
    except Exception:
        return 0


def facet_counts(store: Store, query: str, filters: dict | None = None,
                 limit: int = 4000, top: int = 10) -> dict[str, list[tuple[str, int]]]:
    """Group the matching set by the dimensions worth filtering on.

    Matches **once** and tallies in Python. Running one grouped query per facet
    re-executes the FTS match each time, which measured 3.4s against 0.35s for
    this shape — a 10x difference, and the difference between a UI that feels
    instant and one that feels broken.

    Bounded by `limit` rows: on a 100k-skill corpus an exact facet count over a
    broad query costs more than the search it decorates, and the leading
    categories stabilise long before the tail is counted.
    """
    fts = to_fts_query(query)
    if not fts:
        return {}
    where, params = _where(filters or {})
    try:
        rows = store.db.execute(
            f"""WITH m AS (
                    SELECT rowid AS rid FROM skills_fts
                    WHERE skills_fts MATCH ? LIMIT ?
                )
                SELECT s.source_kind AS kind, s.license AS license,
                       r.language AS language
                FROM m
                JOIN skills s ON s.id = m.rid
                JOIN repos  r ON r.full_name = s.repo
                WHERE {where}""",
            [fts, limit, *params],
        ).fetchall()
    except Exception:
        return {}

    tallies: dict[str, dict[str, int]] = {"kind": {}, "license": {}, "language": {}}
    for row in rows:
        for key in tallies:
            value = row[key]
            if value:
                tallies[key][value] = tallies[key].get(value, 0) + 1
    return {
        key: sorted(counts.items(), key=lambda kv: -kv[1])[:top]
        for key, counts in tallies.items()
    }


def keyword_search(
    store: Store, query: str, limit: int = 100, filters: dict | None = None
) -> list[Hit]:
    fts = to_fts_query(query)
    if not fts:
        return []
    where, params = _where(filters or {})
    weights = ",".join(str(w) for w in COLUMN_WEIGHTS)

    sql = f"""
        SELECT s.id, s.repo, s.path, s.name, s.description, s.source_kind,
               s.license, s.score, s.resources, s.content_hash, r.stars,
               snippet(skills_fts, 2, '[', ']', ' … ', 18) AS snip,
               bm25(skills_fts, {weights}) AS bm25
        FROM skills_fts
        JOIN skills s ON s.id = skills_fts.rowid
        JOIN repos  r ON r.full_name = s.repo
        WHERE skills_fts MATCH ? AND {where}
        ORDER BY bm25
        LIMIT ?
    """
    try:
        rows = store.db.execute(sql, [fts, *params, limit]).fetchall()
    except Exception:
        return []

    return [
        Hit(
            skill_id=r["id"], repo=r["repo"], path=r["path"], name=r["name"],
            description=r["description"], source_kind=r["source_kind"],
            license=r["license"], score=r["score"], stars=r["stars"] or 0,
            content_hash=r["content_hash"] or "",
            snippet=(r["snip"] or "").replace("\n", " ")[:280],
            resources=json.loads(r["resources"] or "[]"),
            matched_by="keyword",
        )
        for r in rows
    ]


def vector_search(
    store: Store, query: str, embedder: Any, limit: int = 100,
    filters: dict | None = None,
) -> list[Hit]:
    """Brute-force cosine over stored vectors.

    Exact scan is the right call at this corpus size: tens of thousands of
    512–1024-dim vectors compare in well under a second, and it avoids an ANN
    index that would need rebuilding on every crawl. Swap in FAISS or
    sqlite-vec only once a measurement says this is the bottleneck.
    """
    if embedder is None:
        return []
    encode_query = getattr(embedder, "encode_query", None)
    qvec = encode_query(query) if encode_query else embedder.encode([query])[0]

    where, params = _where(filters or {})
    rows = store.db.execute(
        f"""
        SELECT s.id, s.repo, s.path, s.name, s.description, s.source_kind,
               s.license, s.score, s.resources, s.content_hash, r.stars, v.vec
        FROM vectors v
        JOIN skills s ON s.id = v.skill_id
        JOIN repos  r ON r.full_name = s.repo
        WHERE v.model = ? AND {where}
        """,
        [embedder.model, *params],
    ).fetchall()

    scored = []
    for r in rows:
        sim = cosine(qvec, unpack(r["vec"]))
        if sim > 0.05:
            scored.append((sim, r))
    scored.sort(key=lambda t: -t[0])

    return [
        Hit(
            skill_id=r["id"], repo=r["repo"], path=r["path"], name=r["name"],
            description=r["description"], source_kind=r["source_kind"],
            license=r["license"], score=r["score"], stars=r["stars"] or 0,
            content_hash=r["content_hash"] or "",
            snippet=(r["description"] or "")[:280],
            resources=json.loads(r["resources"] or "[]"),
            rank=sim, matched_by="vector",
        )
        for sim, r in scored[:limit]
    ]


def collapse_duplicates(hits: dict[int, Hit]) -> dict[int, Hit]:
    """Keep one representative per identical SKILL.md, the highest-quality one.

    Forks and vendored copies mean the same file appears in many repositories.
    Ranking them against each other is both useless to the user and actively
    harmful: BM25 length normalisation can seat a zero-star fork above the
    original it was copied from, because the fork's repo and path fields happen
    to be shorter. Collapsing on the content hash removes the contest entirely
    and lets the surviving copy carry a count of the others.
    """
    best: dict[str, Hit] = {}
    counts: dict[str, int] = {}
    passthrough: dict[int, Hit] = {}

    for hit in hits.values():
        if not hit.content_hash:
            passthrough[hit.skill_id] = hit  # unknown hash: never merge blindly
            continue
        counts[hit.content_hash] = counts.get(hit.content_hash, 0) + 1
        incumbent = best.get(hit.content_hash)
        if incumbent is None or hit.score > incumbent.score:
            best[hit.content_hash] = hit

    for chash, hit in best.items():
        hit.duplicates = counts[chash] - 1
        passthrough[hit.skill_id] = hit
    return passthrough


def search(
    store: Store,
    query: str,
    *,
    limit: int = 20,
    filters: dict | None = None,
    embedder_name: str = "none",
    quality_weight: float = WEIGHT_QUALITY,
    max_per_repo: int | None = 3,
) -> list[Hit]:
    """Retrieve with every available strategy, then fuse the rankings."""
    filters = filters or {}
    kw = keyword_search(store, query, limit=limit * 5, filters=filters)

    vec: list[Hit] = []
    if embedder_name and embedder_name != "none":
        try:
            embedder = build_embedder(embedder_name)
            vec = vector_search(store, query, embedder, limit=limit * 5, filters=filters)
        except Exception:
            vec = []  # a missing optional dependency must not break search

    fused: dict[int, Hit] = {}
    sources: dict[int, set[str]] = {}
    for ranked in (kw, vec):
        for hit in ranked:
            sources.setdefault(hit.skill_id, set()).add(hit.matched_by)
            existing = fused.get(hit.skill_id)
            # Prefer the keyword record: it carries the highlighted snippet.
            if existing is None or (hit.matched_by == "keyword" and hit.snippet):
                fused[hit.skill_id] = hit

    if not fused:
        return []

    fused = collapse_duplicates(fused)

    # Quality becomes a ranked list over exactly the candidates we retrieved, so
    # it can reorder relevant results without ever promoting an irrelevant one.
    by_quality = sorted(fused.values(), key=lambda h: -h.score)

    scores: dict[int, float] = {}
    for ranked, weight in (
        (kw, WEIGHT_KEYWORD),
        (vec, WEIGHT_VECTOR),
        (by_quality, quality_weight),
    ):
        for position, hit in enumerate(ranked):
            scores[hit.skill_id] = scores.get(hit.skill_id, 0.0) + weight / (
                RRF_K + position + 1
            )

    for skill_id, hit in fused.items():
        hit.rank = scores.get(skill_id, 0.0)
        hit.matched_by = "+".join(sorted(sources[skill_id]))

    ordered = sorted(fused.values(), key=lambda h: -h.rank)
    if max_per_repo:
        ordered = diversify(ordered, max_per_repo)
    ordered = ordered[:limit]
    attach_author_scores(store, ordered)
    return ordered


def attach_author_scores(store: Store, hits: list[Hit]) -> None:
    """Look up author standing for the results being returned, in one query."""
    logins = sorted({h.author for h in hits})
    if not logins:
        return
    placeholders = ",".join("?" * len(logins))
    try:
        scores = {
            r["login"]: r["author_score"]
            for r in store.db.execute(
                f"SELECT login, author_score FROM authors "
                f"WHERE login IN ({placeholders})", logins)
        }
    except Exception:
        return  # authors table not built yet; the field simply stays absent
    for hit in hits:
        hit.author_score = scores.get(hit.author)


def diversify(hits: list[Hit], max_per_repo: int) -> list[Hit]:
    """Cap how many results one repository may occupy near the top.

    A single well-made collection can hold hundreds of skills, and on a broad
    query it will legitimately win every one of the top slots — leaving a result
    page that answers "which repo is best" when the user asked "which skill do I
    want". Surplus hits are demoted rather than dropped, so nothing is lost;
    they simply queue behind the first result from every other repository.
    """
    kept: list[Hit] = []
    overflow: list[Hit] = []
    seen: dict[str, int] = {}
    for hit in hits:
        seen[hit.repo] = seen.get(hit.repo, 0) + 1
        (kept if seen[hit.repo] <= max_per_repo else overflow).append(hit)
    return kept + overflow


def get_skill(store: Store, skill_id: int) -> dict | None:
    row = store.db.execute(
        "SELECT s.*, r.stars, r.description AS repo_description, r.topics, "
        "r.pushed_at, r.default_branch "
        "FROM skills s JOIN repos r ON r.full_name = s.repo WHERE s.id = ?",
        (skill_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    for key in ("allowed_tools", "metadata", "resources", "topics"):
        try:
            data[key] = json.loads(data.get(key) or "[]")
        except json.JSONDecodeError:
            data[key] = []
    branch = data.get("default_branch") or "HEAD"
    data["url"] = f"https://github.com/{data['repo']}/blob/{branch}/{data['path']}"
    data["raw_url"] = (
        f"https://raw.githubusercontent.com/{data['repo']}/{branch}/{data['path']}"
    )
    return data


# ------------------------------------------------------------------ browsing


def category_counts(store: Store, filters: dict | None = None) -> dict[str, dict]:
    """How many skills sit in each category and subcategory.

    Browsing needs counts up front — a directory entry with no size is a link
    into the dark. One grouped pass over an indexed column answers the whole
    tree, so the landing page costs a single query.
    """
    where, params = _where({**(filters or {}), "valid_only": True})
    try:
        rows = store.db.execute(
            f"""SELECT s.category AS c, s.subcategory AS sub, COUNT(*) AS n
                FROM skills s JOIN repos r ON r.full_name = s.repo
                WHERE {where} AND s.category IS NOT NULL
                GROUP BY c, sub""",
            params,
        ).fetchall()
    except Exception:
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        entry = out.setdefault(row["c"], {"total": 0, "subs": {}})
        entry["total"] += row["n"]
        if row["sub"]:
            entry["subs"][row["sub"]] = entry["subs"].get(row["sub"], 0) + row["n"]
    return out


def browse(store: Store, category: str, subcategory: str | None = None, *,
           limit: int = 30, offset: int = 0, sort: str = "quality",
           filters: dict | None = None) -> tuple[list[Hit], int]:
    """List a category's skills, best first. Returns (page, total).

    Browsing is not searching: there is no query to be relevant to, so ordering
    falls back to the quality prior the ranker already computed. Duplicates are
    collapsed and the per-repository cap applies here too — without them a
    category page is just one prolific repository fifteen times over.
    """
    where, params = _where({**(filters or {}), "valid_only": True})
    clause = "s.category = ?"
    args: list[Any] = [category]
    if subcategory:
        clause += " AND s.subcategory = ?"
        args.append(subcategory)

    order = {
        "quality": "s.score DESC",
        "stars": "r.stars DESC, s.score DESC",
        "recent": "r.pushed_at DESC, s.score DESC",
        "name": "s.name ASC",
    }.get(sort, "s.score DESC")

    total = store.db.execute(
        f"SELECT COUNT(*) c FROM skills s JOIN repos r ON r.full_name = s.repo "
        f"WHERE {clause} AND {where}", [*args, *params]
    ).fetchone()["c"]

    # Over-fetch so collapsing duplicates and capping per repo still fills a page.
    rows = store.db.execute(
        f"""SELECT s.id, s.repo, s.path, s.name, s.description, s.source_kind,
                   s.license, s.score, s.resources, s.content_hash, r.stars
            FROM skills s JOIN repos r ON r.full_name = s.repo
            WHERE {clause} AND {where}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        [*args, *params, (limit + offset) * 4, 0],
    ).fetchall()

    hits = [
        Hit(skill_id=r["id"], repo=r["repo"], path=r["path"], name=r["name"],
            description=r["description"], source_kind=r["source_kind"],
            license=r["license"], score=r["score"], stars=r["stars"] or 0,
            content_hash=r["content_hash"] or "",
            snippet=(r["description"] or "")[:240],
            resources=json.loads(r["resources"] or "[]"),
            rank=r["score"], matched_by="browse")
        for r in rows
    ]
    deduped = list(collapse_duplicates({h.skill_id: h for h in hits}).values())
    deduped.sort(key=lambda h: -h.rank)
    page = diversify(deduped, 3)[offset:offset + limit]
    attach_author_scores(store, page)
    return page, total
