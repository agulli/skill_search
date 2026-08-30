# skill-engine

A search engine for AI agent skills published on GitHub.

Agent skills are `SKILL.md` files — Markdown with YAML frontmatter — scattered
across tens of thousands of unrelated public repositories. There is no registry.
skill-engine finds them, validates them, ranks them by quality, and serves
search over the result.

Currently indexes **100,006 skills** (95,725 valid, 87,033 unique) from **6,147
repositories** by **4,889 authors**. Queries return in **60–210 ms**.

```bash
pip install -e .
skill-engine mass-discover --target 25000   # find repositories
skill-engine sweep --target 100000          # harvest them
skill-engine rank                           # score everything
skill-engine serve                          # UI + API on :8000
```

No GitHub token is required. 📄 **[whitepaper.md](whitepaper.md)** covers the
full design and the reasoning behind each choice.

---

## Why

Finding a good agent skill is hard for three reasons, and the engine is built
around them:

**They are unlisted.** Skills live at conventional paths — `skills/<name>/`,
`.claude/skills/<name>/`, `plugins/<p>/skills/<name>/`, repository root — inside
repositories that are mostly about something else. GitHub's own code search
caps at 1,000 results per query and 10 requests/minute, so it cannot enumerate
them.

**The corpus is mostly noise.** 20% of indexed skills are verbatim copies of
another skill. Many repositories are bulk dumps of other people's work. Raw
keyword search over that returns the same file fifteen times.

**Popularity is a poor proxy for quality.** A 40,000-star monorepo that happens
to contain a skill is not a better answer than a focused 300-star collection
written by someone who knows the domain.

## How

```
discovery ──▶ queue ──▶ harvest ──▶ parse ──▶ store ──▶ rank ──▶ search
```

### Discovery

Five sources run in parallel, because none finds everything:

| Source | Cost | Finds |
|---|---|---|
| Repository search | search rate-limit bucket only | the bulk, with full metadata attached |
| Awesome-list mining | no API requests | curated hubs, hundreds of repos per README |
| GH Archive | no API requests | pushes to repositories already indexed |
| Code search | 10 req/min, capped at 1,000 | repos whose name and topics reveal nothing |
| Owner expansion | 1–3 requests per owner | other work by authors who published once |

Repository search caps at 1,000 results per query, so `search_repos` recursively
bisects the `created:` date range until every shard fits under the cap. Their
union covers everything the query matches — one query can yield 7,000+
repositories.

Search uses a rate-limit bucket separate from the one the harvester needs, so
discovery never competes with harvesting. Search results also carry nearly the
complete repository object, so discovery populates every ranking signal without
spending a single core request.

### Harvest

Two paths, chosen per repository by size:

**Archives (default).** `codeload.github.com` is not part of the REST API and
does not consume API quota. One download returns every file in a repository,
so a harvest needs *no API requests at all* — roughly 16,000 repos/hour, bounded
only by bandwidth. Repositories above `--max-mb` fall back to:

**The REST path.** One request for repository metadata (batched 100-at-a-time
over GraphQL) and one for the entire recursive file tree. Every request carries
an ETag, and the tree response includes each file's git blob SHA — so a
repository that has not changed costs **zero rate-limit quota**, and only files
whose SHA moved are refetched.

Crawl order matters more than crawl speed: repositories are harvested in
predicted-quality order, which yields roughly 16x more skills per request than
arbitrary order. `--strategy fifo` switches to discovery order for a
completeness sweep.

### Parse and validate

Validation is two-tier:

- **Hard problems** — no frontmatter, no `name`, no `description`, unparseable
  YAML, empty body — mean the file is not a skill. Excluded from search.
- **Soft warnings** — a description over the spec's 1,024-character limit, a
  name that is not a clean slug — mean it is a real skill that bends the spec.
  Indexed, with a score penalty.

Unknown frontmatter keys are preserved so the index survives spec additions.
Every skill is hashed (SHA-256) so verbatim copies can be detected corpus-wide.

### Rank

`skill-engine rank` scores everything offline. Heavy-tailed metrics are
normalised against the corpus's own quantiles rather than hand-tuned constants,
so "top 5%" means the same thing as the corpus grows.

**Repositories** score on popularity, momentum, maintenance and authority.
**Skills** score on their own craft (0.33), their repository (0.32), their
author (0.16) and distinctiveness (0.19).

**Authors** get a reputation from the median craft of everything they publish,
what fraction of it is original rather than copied, portfolio reach, and
consistency. Author standing is derived from craft alone — never from the final
skill score — so the dependency graph stays acyclic: craft → author → skill.

Penalties apply multiplicatively, not additively: archived ×0.55, fork ×0.45,
disabled ×0.25, aggregator dump ×0.70, unlicensed ×0.93. A repository with many
stars but almost no forks or contributors is flagged as inorganic (×0.72),
against a threshold calibrated from the corpus's measured fork/star
distribution.

Missing signals are never scored as zero — their weight is redistributed — so
incomplete metadata degrades ranking gracefully rather than corrupting it.

Every score stores a breakdown of which family contributed what:

```bash
skill-engine explain 42                      # a skill
skill-engine explain anthropics/skills       # a repository
```

### Browse

Search answers "I know what I want". A directory answers "show me what exists",
which for a corpus nobody has seen is often the better question — so the landing
view is a browsable catalogue of 16 subjects with subcategories and counts, and
typing switches to search.

Skills are sorted by weighted pattern matching over name, description, path and
repository topics. Rules rather than a model: deterministic, seconds over 100k
skills, free to re-run when the taxonomy changes, and explainable. The subjects
themselves were derived from term frequencies across the corpus rather than
invented.

Two details make the difference between a directory and a pile:

**Patterns are IDF-weighted.** Counting raw matches put 69.7% of the corpus in
one category — not because any single term dominated, but because a category
with twenty patterns accumulates more than one with eleven. Weighting each
pattern by how rare it is in the corpus makes specificity beat breadth.

**Corpus-universal terms identify nothing.** "agent", "claude" and "prompt"
describe what this whole corpus *is*, so an "AI & Agents" category built on them
swallowed 54% of it. Narrowed to genuine subjects — MCP, RAG, fine-tuning — the
distribution flattens to business 15.7%, productivity 12.6%, AI 8.0%, security
7.4%, with 6.8% honestly marked uncategorised.

```bash
skill-engine categorize      # sort the corpus, offline, re-runnable
```

### Search

Three retrievers fused with weighted Reciprocal Rank Fusion: BM25 over FTS5
(name weighted 10x above body), vector cosine when embeddings are enabled, and
the quality prior. Quality participates as a *ranked list* inside the fusion
rather than as a blended scalar, so it can reorder relevant results without
overruling relevance.

Results are collapsed on content hash, so a skill copied into fifteen
repositories appears once with a copy count. They are also capped at 3 per
repository by default, so one prolific collection cannot own the whole page;
surplus hits are demoted, never dropped.

Embeddings are optional. BM25 is a strong baseline for short, keyword-dense
descriptions:

| `--embedder` | Cost | Use |
|---|---|---|
| `none` (default) | free | BM25 only |
| `hashing` | free | exercises the vector path; not semantic |
| `local` | free | sentence-transformers on CPU |
| `voyage` | per-token | Voyage AI |

---

## Commands

```
skill-engine discover      [--sources seeds topics keyword code awesome gharchive owners]
skill-engine mass-discover [--target 25000]        # sweep search until N repos known
skill-engine sweep         [--target N] [--max-mb 25] [--concurrency 4]
                                                   # bulk harvest via archives, no API quota
skill-engine crawl         [--limit N] [--strategy score|fifo]
skill-engine enrich        [--limit N]             # contributor + release counts
skill-engine enrich-authors[--limit N]             # author follower counts
skill-engine run           [--limit N]             # discover + crawl, for cron
skill-engine rank          [--weight name=v] [--top N] [--no-optimize]
skill-engine explain       ID | owner/repo
skill-engine authors       [--min-skills 3]
skill-engine categorize                            # sort skills into the directory
skill-engine index         [--embedder local|voyage|hashing]
skill-engine search        QUERY [--min-stars N] [--min-score N] [--license MIT]
                                 [--kind ...] [--language ...] [--owner-type ...]
                                 [--max-age-days N] [--max-per-repo N] [--json]
skill-engine show          ID [--raw]
skill-engine stats                                 # totals + metadata coverage
skill-engine serve         [--host 127.0.0.1] [--port 8000]
```

`overnight.py` runs an unattended harvest, alternating archive sweeps with
discovery whenever the queue runs low:

```bash
python overnight.py 100000 data/big.db
```

Everything checkpoints in SQLite. The queue *is* the progress record, so any
command can be killed and rerun to resume exactly where it stopped.

### On a schedule

```bash
*/30 * * * *  skill-engine run --limit 400 --hours 1   # pick up pushes
0 3 * * 0     skill-engine discover --sources topics code owners
0 4 * * 0     skill-engine rank
```

`crawl` only revisits a repository once `SKILL_ENGINE_REFRESH_HOURS` has
elapsed, so running it often is cheap and safe.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_TOKENS` | – | Comma-separated PATs; used as a pool |
| `GITHUB_TOKEN` | – | Single-token fallback |
| `SKILL_ENGINE_DB` | `data/skills.db` | Database path |
| `SKILL_ENGINE_CONCURRENCY` | 6 | Concurrent API requests |
| `SKILL_ENGINE_RAW_CONCURRENCY` | 12 | Concurrent `raw.githubusercontent` fetches |
| `SKILL_ENGINE_REFRESH_HOURS` | 72 | Minimum age before re-crawling |
| `SKILL_ENGINE_MAX_SKILLS_PER_REPO` | 1500 | Content fetches per repository |
| `SKILL_ENGINE_CACHE_MB` | 256 | SQLite page cache |
| `SKILL_ENGINE_MMAP_MB` | 2048 | SQLite mmap window |
| `SKILL_ENGINE_EMBEDDER` | `none` | `none`, `hashing`, `local`, `voyage` |

A token raises the REST limit from 60 to 5,000 requests/hour and enables code
search and GraphQL batching. The archive harvest path needs none.

## Web UI and API

`skill-engine serve` provides a search page and a JSON API on one
dependency-free stdlib server. The UI has live search, facets computed from the
matched set with counts, quality/popularity/freshness thresholds, and a detail
drawer showing the full `SKILL.md`, the author's reputation, and the score
breakdown. `/` focuses the search box, `Esc` closes the drawer, and the URL
carries the query and filters so a search is shareable.

```
GET /                       the search UI
GET /api/search?q=...&limit=20&facets=1
                            &kind= &license= &language= &min_stars=
                            &min_score= &max_age_days= &forks=1
GET /api/categories         the directory tree with counts
GET /api/browse?c=...&sub=...&sort=quality|stars|recent|name
GET /api/skill/{id}         body, resources, score families
GET /api/author/{login}     reputation and its breakdown
GET /api/stats
```

Every search response carries `total`, `took_ms` and optional `facets`, so a
client can build its own interface without a second round trip.

> The server binds to `127.0.0.1` and has no authentication or rate limiting.
> It is not ready to face the open internet as-is.

## Deployment

The corpus is built on your machine and shipped as a file; the server only
reads it. So the container needs no GitHub credentials, no write access, and no
scheduled jobs — and a deploy is two independent steps.

```bash
fly launch --no-deploy --name searchskills   # once
./deploy.sh app                              # code only, fast
./deploy.sh data                             # upload the corpus
```

`deploy.sh data` runs `VACUUM INTO` before uploading, which repacks pages freed
by the crawl: **2.49 GB → 1.47 GB**, and ~0.60 GB gzipped over the wire.

Setting `SKILL_ENGINE_PUBLIC=1` switches on the three things that differ between
a laptop and the internet: the database opens **read-only**, a per-IP **rate
limiter** engages, and the proxy's client-IP header is trusted. That header is
ignored otherwise, since anyone reaching the origin directly can forge it.

`/health` answers outside the limiter, so a health check cannot fail precisely
when the machine is busiest.

**Memory.** Measured on the deployed corpus, for a broad query matching 43,798
skills:

| cache | mmap | query | peak RSS |
|---|---|---|---|
| 64 MB | 2048 MB | 95 ms | 517 MB |
| 64 MB | 0 | 128 ms | 86 MB |
| **192 MB** | **0** | **100 ms** | **189 MB** |

`mmap` maps the database into the address space and RSS counts those pages.
Giving the same memory to SQLite's page cache instead is the same speed at a
third of the footprint, so mmap is off and the service fits a 512 MB machine.

Put Cloudflare in front for TLS, caching and DDoS protection. A read-only search
index caches well, and the origin only sees queries the CDN has not answered.

## Storage

SQLite with FTS5 — one file, no server. 100k documents rank with BM25 in tens of
milliseconds, and the schema maps cleanly onto Postgres + `tsvector` if it ever
outgrows that. Vector search is an exact scan, which is correct at this size and
avoids an index that would need rebuilding after every crawl.

`rank` compacts the FTS index and refreshes planner statistics automatically.

## Development

```bash
pip install -e ".[dev]"
pytest                      # 124 tests, no network required
```

Tests cover rate-limit and ETag behaviour against a mock transport, the full
harvest pipeline including the zero-cost re-crawl guarantee, archive corruption
and oversize handling, ranking properties (missing-data neutrality, bounded
scores, penalty application, anti-circularity), search semantics, and the HTTP
layer.

| Module | Responsibility |
|---|---|
| `github.py` | Rate-limited API client: token pool, ETags, backoff |
| `discover.py` | Five discovery sources; date-sharded search |
| `harvest.py` | REST harvest path |
| `tarball.py` | Archive harvest path |
| `metadata.py` | Normalises GitHub's differently-shaped responses |
| `parse.py` | Frontmatter parsing, validation, craft signals |
| `store.py` | Schema, migrations, FTS5, queue, tuning |
| `ranking.py` | Percentile normalisation, scoring families, trust |
| `authors.py` | Author reputation and originality |
| `search.py` | Hybrid retrieval, fusion, dedupe, diversity, facets |
| `serve.py` | JSON API and search UI |
