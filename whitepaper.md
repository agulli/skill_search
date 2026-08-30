# skill-engine: a search engine for AI agent skills on GitHub

**Design and implementation notes**

---

## Abstract

skill-engine discovers, harvests, validates, ranks and serves the public corpus
of AI agent skills — `SKILL.md` files — published on GitHub. It indexes
**100,006 skills** (95,725 valid, 87,033 unique after content-hash dedupe) from
**6,147 repositories** by **4,889 authors**, drawn from a discovered pool of
**23,367 repositories**. Search returns in **60–210 ms** over that corpus.

The results that matter, and what produced them:

| Result | Figure | Mechanism |
|---|---|---|
| Harvest throughput | **16,000 repos/hour at zero API quota** | codeload archives instead of the REST tree API |
| Full 100k harvest | **65 minutes, 8.9 GB, 0 API requests** | the same |
| Steady-state re-crawl of an unchanged repo | **0 rate-limit quota** | ETags (304s are free) + blob-SHA diffing |
| Crawl yield per request | **60.8 skills/repo vs 3.72 random — 16.3x** | crawl ordered by predicted repository quality |
| Discovery beyond GitHub's 1,000-result cap | **7,427 repos from one query** | recursive `created:` date bisection |
| Discovery cost | **3,560 repos for 0 API requests** | awesome-list README mining |
| Search latency | **2,135 ms → 60 ms** | connection reuse, single-pass faceting, stopwords, page-cache sizing |

Three findings generalise beyond this project:

1. **The scarce resource is rarely the one you optimise for by default.** Three
   separate times, a limit that looked binding was not: the REST quota (bypassed
   by codeload), the fetch cap (protecting quota that content fetches never
   consumed), and query cost (dominated by a 2 MB page cache, not by SQL).
2. **A ranking signal must be measured against the corpus, not guessed.** Every
   threshold here is calibrated from the live distribution; the one time a
   constant was picked by intuition it silently inverted the ranking.
3. **Ordering beats volume.** Crawling in predicted-quality order reached a
   100,000-skill target using 26% of the queue.

---

## 1. Problem and constraints

An agent skill is a Markdown file named `SKILL.md` with YAML frontmatter
declaring at minimum a `name` and a `description`. They live at conventional
paths — `skills/<name>/SKILL.md`, `.claude/skills/<name>/SKILL.md`,
`plugins/<p>/skills/<name>/SKILL.md`, repository root — across tens of thousands
of unrelated public repositories. There is no registry.

Three constraints shape everything:

- **GitHub's REST API is rate-limited** to 60 requests/hour unauthenticated and
  5,000/hour per personal access token.
- **Search endpoints cap at 1,000 retrievable results** per query regardless of
  pagination, and report a `total_count` far above it.
- **The corpus is mostly noise.** 20% of indexed skills are verbatim copies of
  another skill; many repositories are aggregator dumps.

The system is four stages — crawler, indexer, ranker, search — each covered
below with the alternatives that were rejected and why.

---

## 2. Architecture

```
 discovery ──▶ queue ──▶ harvest ──▶ parse/validate ──▶ store ──▶ rank ──▶ search
 (search API,           (codeload    (frontmatter,      (SQLite    (corpus-  (BM25 +
  gharchive,             or trees     two-tier          + FTS5)     relative  vectors
  awesome lists)         API)         validation)                   scoring)  + prior)
```

Every stage checkpoints in SQLite. The queue *is* the progress record, so any
stage can be killed and resumed with no loss — a property that mattered when a
65-minute unattended harvest ran overnight.

**Module map**

| Module | Responsibility |
|---|---|
| `github.py` | Rate-limited API client: token pool, ETags, backoff |
| `discover.py` | Five discovery sources; date-sharded search |
| `harvest.py` | REST harvest path (trees API + raw content) |
| `tarball.py` | Quota-free harvest path (codeload archives) |
| `metadata.py` | Normalises three differently-shaped GitHub responses |
| `parse.py` | Frontmatter parsing, two-tier validation, craft signals |
| `store.py` | Schema, migrations, FTS5 index, queue, connection tuning |
| `ranking.py` | Percentile normalisation, six scoring families, trust |
| `authors.py` | Author reputation, originality, circularity avoidance |
| `search.py` | Hybrid retrieval, RRF fusion, dedupe, diversity, facets |
| `serve.py` | JSON API and search UI |

---

## 3. The crawler

### 3.1 Discovery

No single source finds everything, so five run in parallel.

| Source | Cost | Measured yield |
|---|---|---|
| Repository search | search bucket only | 23,367 repos, fully populated |
| Awesome-list mining | **0 API requests** | 3,560 repos from 2 README fetches |
| GH Archive | **0 API requests** | near-real-time updates to known repos |
| Code search | 10 req/min, capped | repos whose name and topics reveal nothing |
| Owner expansion | 1–3 req/owner | authors who published once usually published more |

**Beating the 1,000-result cap.** GitHub reports `total_count` above 1,000 but
refuses to paginate past it. `search_repos` recursively bisects the `created:`
date range until every shard fits under the cap; their union covers everything
the query matches. One query — `topic:claude-skills` — yielded **7,427
repositories** against a nominal ceiling of 1,000.

**Search is free in the sense that matters.** Search has its own rate-limit
bucket, disjoint from the core bucket the harvester needs. A long discovery
sweep therefore costs the harvest nothing. Better still, search results carry
nearly the complete repository object — stars, forks, issues, size, language,
licence, topics, timestamps, archive and template flags — so bulk discovery
populates every ranking signal without spending a single core request. Coverage
after discovery: 100% on stars and timestamps, 77% licence, 71% language.

**GH Archive** publishes hourly dumps of every public GitHub event. For known
repositories a `PushEvent` means "re-crawl now" — freshness without polling, at
no API cost. For unknown repositories the event carries no file list, so it can
only shortlist by name; the tree call confirms cheaply.

### 3.2 The REST harvest path and its cost model

Per repository:

| Step | Requests | Note |
|---|---|---|
| Metadata | 1, or **0** | Batched 100-at-a-time over GraphQL, or a 304 |
| Full recursive file tree | 1, or **0** | One call lists every path; 304 if unchanged |
| Each unchanged `SKILL.md` | **0** | Blob SHA matches — no fetch needed |
| Each changed `SKILL.md` | **0** | `raw.githubusercontent.com` is off the REST limit |

Measured against `anthropics/skills` (20 skills):

```
PASS 1: 20 skills, 2 api requests
PASS 2: 20 skills, 4 api requests total, 2 were 304
```

The second pass consumed **zero rate-limit quota**: GitHub does not charge for
conditional requests that return 304.

Three mechanisms produce that:

1. **Conditional requests.** Every GET carries the ETag from last time. This is
   the highest-leverage optimisation available and the one most crawlers skip.
2. **Blob SHAs.** Git's own content hash arrives in the tree response, so an
   unchanged file is detected without being fetched.
3. **A token pool.** Each token's remaining quota is tracked *per resource
   bucket* (`core`, `search`, `code_search`, `graphql`) from response headers,
   and requests route to whichever token has the most headroom.

### 3.3 The codeload path — the decisive optimisation

`codeload.github.com` serves repository archives and **is not part of the REST
API**. Downloading three archives (551 skills) left core quota *higher* than
before. Since repository search had already supplied complete metadata including
the default branch, an archive harvest needs **no API requests at all** — one
download replaces the tree call *and* every per-file content fetch.

| Path | Throughput | Time to drain a 23k queue |
|---|---|---|
| REST trees, unauthenticated | ~30 repos/hr | ~32 days |
| REST trees, one token | ~2,500 repos/hr | ~9 hours |
| **codeload archives** | **~16,000 repos/hr** | **~1.5 hours** |

The trade is downloading a whole repository to read a few files, so the path is
chosen by size — `size_kb` is known before any fetch. The distribution is
favourable: median repository **0.1 MB**, mean 4.9 MB, p95 13.2 MB. A 25 MB cap
covers ~97% of the queue.

Deliberate restraint: concurrency 4, a pacing floor between request starts, a
streaming size cap that abandons oversized archives mid-download, and a 30-second
backoff on 403/429. This endpoint is a courtesy, not an entitlement.

Archives are read as a stream and never extracted to disk, so a hostile archive
cannot escape a directory it was never given.

### 3.4 Crawl ordering

Every repository costs the same single request. What comes back varies by more
than an order of magnitude:

| Crawl order | Yield |
|---|---|
| By predicted `repo_score` | **60.8 skills/repo** |
| Uniform random sample | **3.72 skills/repo** |

**16.3x per request.** This is the payoff for ranking *repositories* and not
just skills: the score exists before a repository is ever harvested, because
search already supplied its metadata. The ranker decides what to crawl; the
crawl then feeds the ranker better data. `crawl` defaults to `--strategy score`;
`--strategy fifo` restores discovery order for a completeness sweep.

The effect compounds. A 100,000-skill target was reached from **6,096
repositories — 26% of the queue** — at 16.9 skills/repo against a corpus median
of 2.

### 3.5 Rate-limit handling

Primary limits are visible in response headers. Secondary limits are not: they
arrive as 403 or 429 with a `retry-after` header even when primary quota is
healthy. The client parks *that token only* for exactly the stated duration and
routes the retry to a sibling, so one throttled token never stalls the crawl.
Rate-limit backoffs are counted separately from error retries — waiting out a
quota window is normal operation, not a failure — and both are bounded so a
request cannot loop forever.

### 3.6 Rejected alternatives

| Rejected | Why |
|---|---|
| **HTML scraping** | Rate-limited then IP-banned; unnecessary, the API has everything |
| **Code Search as the backbone** | 10 req/min, capped at 1,000 results, requires a search term. A seed source, not an engine |
| **A GitHub App** | Installation tokens are scoped to repositories where the app is installed; you cannot install one on strangers' repositories. Useful only for your own org |
| **Per-file `contents` API** | Costs core quota per file; `raw.githubusercontent.com` does not |

---

## 4. The indexer

### 4.1 Parsing and two-tier validation

`SKILL.md` is Markdown opening with a YAML frontmatter block. Validation is
split deliberately:

- **Hard problems** — no frontmatter, no `name`, no `description`, unparseable
  YAML, empty body — mean the file is not a skill. Excluded from search.
- **Soft warnings** — description over the spec's 1,024-character limit, a name
  that is not a clean slug — mean it is a real skill that bends the spec.
  Indexed, with a score penalty.

**This distinction is load-bearing.** Enforcing the spec strictly as an
admission test dropped `anthropics/skills`' own `claude-api` skill — 74 KB of
genuinely useful content — over a description 44 characters too long. A search
engine that cannot find real, working skills has failed at its only job. The
split moved corpus validity from 90% to **95.7%**.

Unknown frontmatter keys are preserved verbatim so the index survives spec
additions. A missing `name` falls back to the containing directory, which is how
runtimes address skills anyway.

Skills are classified by path into `skills-dir`, `claude-project`, `plugin`,
`cursor`, `agent-dir`, `root`, `other`. Longest-prefix matching is required:
`.claude/skills/` also ends in `skills/`, and the specific match is the
informative one.

Observed distribution:

| Location | Count |
|---|---|
| `skills/` | 63,861 |
| other | 17,570 |
| plugin | 8,588 |
| `.claude/skills/` | 4,128 |
| repository root | 1,166 |
| `.cursor/skills/` | 406 |

### 4.2 Storage

SQLite with FTS5 — a deliberate choice, not a placeholder. The corpus is 100k
documents; FTS5 ranks that with BM25 in tens of milliseconds, in one file, with
no server. The schema maps cleanly onto Postgres + `tsvector` if it outgrows
that.

Current shape: **2.49 GB** total, of which the FTS index is **1.09 GB**. Average
skill body is 7,809 characters.

Tables: `repos` (full metadata + score), `skills` (content + score),
`skills_fts` (external-content FTS5 over name/description/body/repo/path),
`authors`, `corpus_stats` (quantile boundaries), `queue`, `etags`, `vectors`.

Two schema decisions worth noting:

- **Column names in `skills` are load-bearing.** FTS5 external-content tables
  reference the base table's columns by name, which is why the repository column
  is `repo` rather than `repo_full_name`.
- **Migrations are additive and automatic.** `CREATE TABLE IF NOT EXISTS`
  silently skips an existing table, so new columns need explicit `ALTER`. A
  `_migrate` step adds any missing column on open; nothing there can lose data.

**Metadata writes are additive.** Different endpoints populate different subsets
— search omits `subscribers_count`, only the repo endpoint has it — so
`upsert_repo` uses `COALESCE(excluded, existing)` on every column. A cheap
refresh can never erase richer data an expensive one already fetched.

### 4.3 Index maintenance

Trigger-driven inserts create one FTS5 segment per commit. After 100k skills
across thousands of crawl batches the index held **211,656 segment rows**;
compaction merged them to 94,077 and cut query time ~17%. `skill-engine rank`
now runs `optimize` and `ANALYZE` automatically.

### 4.4 Deduplication

Every skill carries a SHA-256 of its file text. This is the backbone of three
separate features: duplicate collapsing at search time, the distinctiveness
penalty in ranking, and the originality signal in author scoring. **20,081 of
100,006 skills (20%) share a content hash with another skill.**

---

## 5. The ranking layer

`skill-engine rank` recomputes every score offline from stored data. It is a
separate pass, not part of the crawl, because percentile normalisation needs the
whole corpus — a score assigned mid-crawl would be measured against a
distribution that no longer exists when the crawl ends.

### 5.1 Percentile normalisation, not magic constants

Star counts are power-law distributed: the gap between 10 and 100 means far more
than between 10,000 and 10,100. A formula like `9 * log10(stars)` bakes in a
guess about corpus scale that rots as the corpus grows. Every heavy-tailed
metric is instead normalised against the corpus's own quantiles, so "top 5% by
stars" means the same thing at 500 repositories and at 500,000.

Percentiles use **mid-rank** — averaging the left and right insertion points.
Roughly half the corpus has zero stars, and a plain `bisect_left` would score
every one of them identically to the single least-popular repository.

### 5.2 Signal families

Repository score:

| Family | Weight | Signals |
|---|---|---|
| popularity | 0.22 | stars, forks, subscribers |
| momentum | 0.14 | stars/day since creation, push recency |
| maintenance | 0.16 | push and release recency, release count, issue load |
| authority | 0.13 | org-owned, licence, description, topics, homepage, contributors |

Skill score:

| Family | Weight | Signals |
|---|---|---|
| craft | 0.33 | validity, description fit, body depth, bundled resources, declared tools, spec cleanliness |
| repo standing | 0.32 | the repository score above |
| author standing | 0.16 | see §5.5 |
| distinctiveness | 0.19 | content uniqueness, name uniqueness, repository focus |

### 5.3 Two structural rules

**Missing data must not mean zero.** Search results carry no `subscribers`;
un-enriched repositories carry no `contributors`. Scoring those as 0 punishes a
repository for *our* crawl budget rather than its own quality. Absent signals are
dropped and their weight redistributed across the ones present — that is what
`blend()` does, and it is why coverage gaps degrade the ranking gracefully
instead of corrupting it.

**Multiplicative trust, additive quality.** Being archived or being a fork is
not "a few points worse", it is a different category of thing. Penalties apply
to the whole score, so a fork cannot climb past an original by accumulating
small additive wins elsewhere.

| Penalty | Factor | Corpus incidence |
|---|---|---|
| archived | ×0.55 | 57 |
| fork | ×0.45 | — |
| disabled | ×0.25 | — |
| aggregator dump (>500 skills) | ×0.70 | — |
| template | ×0.90 | 69 |
| unlicensed | ×0.93 | 2,488 |
| inorganic popularity | ×0.72 | 2 |

### 5.4 Calibration, and the inorganic-popularity guard

Stars are the cheapest signal to manufacture and the most expensive to ignore,
so they are cross-checked against signals that are hard to fake: forks (someone
took a copy) and contributors (someone did work).

The threshold was **measured, not guessed**. Across repositories with ≥500
stars the fork/star ratio runs:

| p1 | p5 | p10 | p25 | p50 |
|---|---|---|---|---|
| 0.009 | 0.048 | 0.060 | 0.083 | 0.104 |

A cutoff of 0.012 sits just above the 1st percentile and flags 1.7% — tight
enough to catch only the genuine tail, loose enough that ordinary variation
never trips it. The guard never fires on missing data, which would punish an
un-enriched repository for what we failed to fetch.

**Validation that it is not a popularity leaderboard:** the top-scoring
repository has **654 stars and outranks one with 37,671**. Skill scores spread
p5 = 43.9, p50 = 77.2, p95 = 87.8 — neither saturated nor collapsed.

### 5.5 Author standing, and avoiding circularity

A skill's file tells you how well-made it is. It cannot tell you whether the
author knows what they are doing, or merely copied someone else's work. With 20%
of the corpus being copies, that question carries real information.

Every author is profiled from data already in the corpus — no extra API calls:

| Signal | Weight | Captures |
|---|---|---|
| craft | 0.30 | median craft of everything they published |
| originality | 0.22 | share of their skills that are not copies |
| reach | 0.16 | stars and forks across the whole portfolio |
| body of work | 0.10 | skill count, damped logarithmically |
| consistency | 0.08 | do they licence and describe their repositories |
| followers | 0.08 | optional, via `enrich-authors` |
| longevity + upkeep | 0.06 | tenure, still maintained |

Originality separates authors sharply:

```
93.4  giuseppe-trisciuoglio   119 skills  100% original   craft 0.91
59.8  arjun988                188 skills    0% original   (all vendored)
```

More skills, 34 points lower. Body of work is damped deliberately — publishing
400 skills is not forty times the evidence of publishing ten, and rewarding it
linearly is how you promote bulk scrapers.

**The circularity problem.** Author standing feeds the skill score, so it must
not be built *from* the skill score — that is a feedback loop where popular
authors inflate their own skills, which inflate them further. It is built from
`craft_score` instead, which judges a `SKILL.md` on its own contents and knows
nothing about repositories or authors. The dependency graph stays acyclic:

```
craft ──▶ author ──▶ skill
```

A test asserts this structurally, by checking that `build_profiles` never
references `score_skill`.

Author scores spread p10 = 61.6, p50 = 71.4, p90 = 81.0 across 4,889 authors.

### 5.6 Explainability

Every score stores a JSON breakdown of which family contributed what.
`skill-engine explain <id|owner/repo>` prints it, and the UI renders it in the
detail drawer. Missing signals display as `—`, never as zero. A ranking you
cannot interrogate is a ranking you cannot debug — and in this project the
breakdown is what surfaced two of the three ranking bugs in §7.

Weights are overridable per run (`--weight popularity=0.35`), so the ranking can
be tuned and the effect observed immediately.

---

## 6. Search

### 6.1 Hybrid retrieval

Three signals, fused with **weighted Reciprocal Rank Fusion**:

1. **BM25** via FTS5 with per-column weights — name 10, description 6, repo 2,
   path 1.5, body 1. A query term matching a skill's name counts far more than
   the same term buried in its body.
2. **Vector cosine**, when embeddings are enabled, for queries phrased
   differently from the skill's own vocabulary.
3. **The quality prior** from the ranking layer.

Fusion weights: keyword 1.0, vector 0.9, quality 0.5, with `k = 20`.

**Quality enters the same fusion as a third ranked list**, rather than being
blended afterwards as a 0–1 number. This is not a stylistic choice — mixing the
two spaces silently inverts the ranking, as §7.2 describes.

`k = 20` rather than the literature's 60: that constant is tuned for fusing long
TREC-scale result lists, whereas this engine needs discrimination inside the top
ten, where a smaller `k` keeps real separation between the first few positions.

### 6.2 Query construction

FTS5 raises on unbalanced quotes and stray operators, so every term is quoted
and OR-ed with prefix matching — OR behaves far better than the implicit AND for
natural-language queries where not every word appears.

**Stopwords are dropped**, which is a relevance fix that is also a performance
fix. Under OR semantics *a* and *from* match nearly every document: "extract
tables from a pdf" matched 86,036 skills and took 1.6 s to rank. Removing
stopwords leaves the terms that discriminate — 23,959 matches, 323 ms. They are
dropped only when something survives, so a search for "the" still searches for
"the".

### 6.3 Duplicate collapsing

Forks and vendored copies put the same file in many repositories. Ranking them
against each other is useless and actively harmful: BM25's length normalisation
seated a **zero-star fork above the original it was copied from**, because the
fork's repo and path fields were shorter. Results collapse on content hash,
keeping the highest-quality copy and reporting a count of the rest.

### 6.4 Result diversity

A single well-made collection can hold hundreds of skills and will legitimately
win every top slot on a broad query, leaving a page that answers "which repo is
best" when the user asked "which skill do I want". Results are capped at 3 per
repository by default. Surplus hits are **demoted, not dropped**, so a query
only one repository can answer still returns everything it has.

### 6.5 Performance

Three fixes took the median query from 2,135 ms to under 100 ms, each found by
measurement:

| Fix | Before | After |
|---|---|---|
| Reuse the SQLite connection per thread | 2,135 ms | 590 ms |
| Compute all facets in one FTS match | 3,410 ms (facets alone) | 350 ms |
| Size the page cache to the index | 200 ms (facet query) | **8 ms** |
| Drop stopwords | 1,600 ms / 86,036 hits | 323 ms / 23,959 hits |
| FTS5 `optimize` | 211,656 segments | 94,077 segments (−17%) |

The first was self-inflicted: the HTTP handler opened a fresh `Store` per
request, re-running the schema script and migration check against a 2.5 GB file
every time.

The second is counter-intuitive: the first faceted implementation was *slower*
than the unfaceted one, because one grouped query per facet re-executed the same
full-text match three times. Matching once and tallying in Python is 10x faster.

The third was the largest and was pure configuration. SQLite defaults to a ~2 MB
page cache; the FTS index alone is over 1 GB, so nearly every query read from
disk. `cache_size = 256 MB` plus `mmap_size` turned a 200 ms facet query into an
8 ms one — **25x, same SQL**.

Final latencies over 100k skills:

| Query | Matches | Time |
|---|---|---|
| `pdf` | 4,995 | 60 ms |
| `terraform aws modules` | 9,779 | 79 ms |
| `extract tables from a pdf invoice` | 24,961 | 134 ms |
| `react accessibility review` | 43,798 | 209 ms |

### 6.6 Embeddings are optional

BM25 is a strong baseline here — skill descriptions are short, keyword-dense,
and written to be matched. Embeddings are opt-in: `hashing` (free, exercises the
vector path, not semantic), `local` (sentence-transformers on CPU, free),
`voyage` (per-token). Anthropic does not serve an embeddings endpoint, so
`voyage` or `local` are the real semantic options.

Vector search is an exact scan. At this corpus size that is correct: tens of
thousands of vectors compare in well under a second, and it avoids an ANN index
that would need rebuilding after every crawl.

### 6.7 The interface

`skill-engine serve` provides a JSON API and a dependency-free search UI: live
search, facets computed from the matched set with counts, quality/popularity/
freshness thresholds, and a detail drawer showing the full `SKILL.md`, the
author's reputation panel, and the score breakdown. Every search response
carries `total`, `took_ms` and optional `facets`, so a client can build its own
interface without a second round trip.

---

## 7. Failure modes found and fixed

These are documented because each was invisible until measured, and each changed
a design decision.

### 7.1 The fetch cap defeated the penalty meant to catch dumps

`max_skills_per_repo` truncated the file list *before* `skill_count` was
recorded. A repository holding **2,118** `SKILL.md` files stored 400, sailed
under the aggregator-dump threshold of 500, and scored **94.4** as though it
were a curated collection. The true count is now always recorded regardless of
the cap; that repository scores **65.7** with the penalty applied.

The cap itself was also miscalibrated — it protected REST quota that content
fetches never consumed. Raising it recovered **1,265 skills for 6 API requests**,
because blob-SHA diffing meant the 400 already held were not refetched.

### 7.2 Rank-space and score-space do not mix

The quality prior was originally blended into RRF results as a normalised 0–1
value. RRF scores are compressed: with `k = 60`, first place beats second by
1.6%, while the prior varied by 4.5%. The prior silently overruled both
retrievers — both ranked the correct answer first and fusion still inverted it.
Fixed by making quality a third *ranked list* inside the same fusion, so every
signal stays in rank space.

### 7.3 Repository standing swamped the skill itself

Deriving the repository's share of a skill's score by summing the four
repository families gave it **65%** of the total. Every skill in one strong
repository outranked every skill everywhere else regardless of its own quality.
`repo_standing` is now stated explicitly at 0.32 against craft's 0.33. After the
change, a 277-star repository's skills rank alongside a 37,681-star
repository's on craft alone.

### 7.4 An empty `params` dict silently deleted pagination cursors

`httpx` *replaces* a URL's query string when given a params mapping, so passing
`{}` while following a `Link` header stripped the page cursor and re-requested
page 1 forever. Passing `None` preserves it.

### 7.5 Strict spec conformance discarded real skills

Covered in §4.1. Validation strictness is an admission policy, and the cost of a
false negative in a search engine is total: the skill simply cannot be found.

---

## 8. Design choices, consolidated

| Choice | Alternative | Rationale |
|---|---|---|
| Git Trees API as REST backbone | Code Search | One request lists every path; code search caps at 1,000 results and 10 req/min |
| codeload archives as primary path | REST trees | Zero API quota, ~16,000 repos/hour |
| PAT pool | GitHub App | Installation tokens cannot read strangers' repositories |
| SQLite + FTS5 | Postgres, Elasticsearch | 100k docs rank in tens of ms, one file, no server; migrates cleanly if outgrown |
| Exact vector scan | ANN index | Correct at this size; no index to rebuild per crawl |
| Percentile normalisation | Log constants | Corpus-relative, survives growth |
| Scoring as a separate pass | Score during crawl | Percentiles need the whole corpus |
| Two-tier validation | Strict spec conformance | False negatives are fatal for a search engine |
| Weighted RRF | Normalised weighted sum | BM25 and cosine are on incomparable scales |
| Content-hash dedupe | Fork-flag heuristics | Catches vendored copies that are not forks |
| Author craft from `craft_score` | From final skill score | Breaks the feedback loop |
| Embeddings opt-in | On by default | BM25 is strong here; embeddings cost money or setup |

---

## 9. Limitations

**What GitHub does not expose.** Dependents ("Used by") is HTML-only; traffic
(views, clones) requires push access; package downloads exist only for registry
publishers. The engine uses proxies instead, and the derived ones carry more
signal than raw counts: `stars_per_day` separates 500 stars this month from 500
over three years, and `fork_ratio` separates what people use from what they
bookmark.

**Coverage.** 6,147 of 23,367 discovered repositories are harvested; 17,228
remain queued. A uniform random sample of the remainder shows 88% contain at
least one skill at 3.72 skills/repo (bootstrap 95% CI 2.28–5.40), implying
roughly 60,000 further skills at a much lower yield per gigabyte, since crawl
ordering has already taken the dense repositories.

**The wider ceiling is unmeasured.** `"SKILL.md" in:readme` reports 323,417
repositories, but that matches README *text*, not repositories containing the
file. A 40-repository probe returned ~85%, but GitHub caps any query at 1,000
retrievable results, so that sample necessarily came from the most-recently-
updated slice — exactly the repositories most likely to contain skills. Treat
"hundreds of thousands" as plausible, not measured.

**Originality is corpus-relative.** It means "first or only holder of this
content hash *in our index*". If the copy was indexed and the original was not,
the copier receives undeserved credit. Completing the crawl tightens this.

**Enrichment is incomplete.** `contributors`, `releases` and author `followers`
are at 0% coverage; their weights are currently redistributed. Each costs 1–2
API requests per subject.

**No relevance evaluation set.** Ranking is validated by property tests and
spot-checks, not by a labelled judgement set. That is the largest gap: there is
no measurement of whether a weight change improves or degrades relevance beyond
inspection.

---

## 10. Reproducing

```bash
pip install -e .
export GITHUB_TOKENS=ghp_a,ghp_b          # optional; codeload needs none

skill-engine mass-discover --target 25000  # search bucket only
skill-engine sweep --target 100000         # codeload, zero API quota
skill-engine rank                          # corpus-wide scoring + FTS compaction
skill-engine serve                         # UI and API on :8000
```

`pytest` runs 106 tests with no network access, covering rate-limit and ETag
behaviour against a mock transport, the full harvest pipeline including the
zero-cost re-crawl claim, archive corruption and oversize handling, ranking
properties (missing-data neutrality, bounded scores, penalty application,
anti-circularity), search semantics, and the HTTP layer.
