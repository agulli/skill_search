# skill-engine

A search engine for AI agent skills published on GitHub. It discovers
repositories containing `SKILL.md` files, harvests and validates them, and
serves hybrid keyword + semantic search over the result.

```bash
pip install -e .
export GITHUB_TOKEN=ghp_...
skill-engine discover              # find candidate repositories
skill-engine crawl --limit 500     # harvest them
skill-engine search fill in a pdf form
skill-engine serve                 # JSON API + search page on :8000
```

📄 **[whitepaper.md](whitepaper.md)** — the full design: crawler, indexer,
ranking, search, every design choice with its rejected alternatives, the
measured results, and the failure modes that changed the design.

---

## Why not just scrape, or just use code search

Scraping GitHub's HTML gets you rate-limited and then IP-banned, and it is
unnecessary: everything needed is in the API. But the obvious API route — the
Code Search endpoint — is also the wrong backbone:

- It is capped at **1000 results per query**, no matter how you paginate.
- It is rate-limited to **10 requests/minute** for authenticated users.
- It requires authentication and rejects some qualifier-only queries.

So code search is a *seed* source here, not the engine. The backbone is the
**Git Trees API**, which returns a repository's entire file listing — every
path, with each file's blob SHA — in a single request.

## The cost model

This is the whole design, and it is measured, not estimated. Per repository:

| Step | Requests | Notes |
|---|---|---|
| Repository metadata | 1, or **0** | Batched 100-at-a-time over GraphQL, or a 304 |
| Full recursive file tree | 1, or **0** | One call lists every path; 304 if unchanged |
| Each unchanged `SKILL.md` | **0** | Blob SHA matches, so no fetch is needed |
| Each changed `SKILL.md` | 0 | `raw.githubusercontent.com` is off the REST limit |

Measured against `anthropics/skills` (20 skills):

```
PASS 1: 20 skills, 2 api requests
PASS 2: 20 skills, 4 api requests total, 2 were 304
```

The second pass consumed **zero rate-limit quota**, because GitHub does not
charge for conditional requests that return 304. A crawler without ETags
re-spends its entire budget on every pass; this one spends it only on what
actually changed.

Three things make that work:

1. **Conditional requests.** Every GET carries the ETag from last time. This is
   the single highest-leverage optimisation available and the one most crawlers
   skip.
2. **Blob SHAs.** Git's own content hash is in the tree response, so an
   unchanged file is detected without being fetched.
3. **A token pool.** Each PAT gets 5,000 core requests/hour. The client tracks
   every token's remaining quota *per resource bucket* (`core`, `search`,
   `code_search`, `graphql`) from response headers and routes to whichever has
   the most headroom.

### On GitHub Apps

A GitHub App is often recommended for the higher installation rate limit. It
does not help here: **installation tokens are scoped to repositories where the
app is installed**, and you cannot install an app on strangers' repositories.
For crawling arbitrary public repos, a pool of PATs plus ETags is both simpler
and more effective. Use a GitHub App only if you are crawling your own org.

### Secondary rate limits

GitHub enforces undocumented secondary limits that arrive as a `403` or `429`
with a `retry-after` header, even when your primary quota is healthy. The
client parks *that token only* for exactly the stated duration and routes the
retry to a sibling, so one throttled token does not stall the crawl.

## Discovery

No single source finds everything, so five run in parallel:

| Source | Cost | Yield |
|---|---|---|
| **Repository search** | search bucket only | **23,367 repos, fully populated** |
| **Awesome-list mining** | 0 API requests | 3,560 repos from 2 README fetches |
| GH Archive | 0 API requests | Near-real-time updates to known repos |
| Code search | 10 req/min, capped | Skills in repos with no revealing name |
| Owner expansion | 1–3 req/owner | Anyone who published one usually published more |

**Beating the 1000-result cap.** Repository search reports `total_count` above
1000 but refuses to paginate past it. `discover.search_repos` recursively
bisects the `created:` date range until every shard fits under the cap, so the
union covers everything the query matches rather than the first thousand. In
practice a *single* query — `topic:claude-skills` — yields over 7,000
repositories once sharded, against a nominal ceiling of 1,000.

**Search is free, in the sense that matters.** Search has its own rate-limit
bucket, separate from the core bucket the crawler needs. So a long discovery
sweep costs the harvest nothing, and because search items arrive with nearly
the complete repository shape, bulk discovery populates every ranking signal
without spending a single core request.

**GH Archive** publishes hourly dumps of every public GitHub event. For repos
already indexed, a `PushEvent` means "re-crawl now" — freshness without
polling, at no API cost. For unknown repos the event carries no file list, so
it can only shortlist by name; the tree call confirms cheaply.

## What the crawl is worth, and in what order

Every repository costs the same single tree request. What comes back does not.
Measured on the live corpus:

| Crawl order | Yield |
|---|---|
| By `repo_score` (top 24 repos) | **60.8 skills/repo** |
| Uniform random sample (50 repos) | **3.72 skills/repo** |

A **16x difference in yield per request**. When core quota is the binding
constraint — and it always is — ordering is the whole game, so `crawl` defaults
to `--strategy score` and spends quota on the best-ranked repositories first.
`--strategy fifo` restores discovery order for a completeness sweep, where
finishing the corpus matters more than early yield.

This is also the payoff for ranking *repositories* and not just skills: the
score exists before a repo has ever been harvested, because search already gave
us its full metadata. The ranker decides what to crawl, then the crawl feeds
the ranker better data.

### The fetch cap is a time budget, not a quota budget

`max_skills_per_repo` limits how many `SKILL.md` files are fetched from one
repository. Content comes from `raw.githubusercontent.com`, which does not draw
on the REST quota at all — so this caps wall-clock time, not the scarce
resource. The original value of 400 was calibrated as though it protected
quota, and discarded thousands of real skills to save something that was never
at risk. Raising it to 1,500 recovered **1,265 skills for 6 API requests**,
because the blob-SHA check meant the 400 already held were not refetched.

Worse, the cap was applied *before* `skill_count` was recorded, so a repository
holding 2,118 skill files stored 400, sailed under the aggregator-dump
threshold of 500, and scored 94.4 as though it were a curated collection. The
true count is now always recorded regardless of the cap; that repository now
scores 65.7 with the dump penalty correctly applied. A cap that hides the size
signal from the ranker is worse than no cap at all.

### How many skills are out there

From a uniform random sample of 50 uncrawled repositories:

- **88%** contain at least one `SKILL.md`
- **3.72** skills per repository on average (median 1, max 25)
- Bootstrap 95% CI on the mean: **2.28 – 5.40**

Extrapolated over the 23,367 repositories already discovered, that is roughly
**87,000 skills (95% CI 53,000 – 126,000)**, or about **73,000 unique** after
content-hash dedupe. Harvesting all of it costs ~23,600 core requests: 4.7
hours on one token, under an hour on five.

The wider ceiling is larger but unmeasured. `"SKILL.md" in:readme` reports
323,417 repositories, though that matches README *text*, not repositories that
actually contain the file. A 40-repo probe of that pool also returned ~85%, but
GitHub caps any query at 1,000 retrievable results, so that sample necessarily
came from the most-recently-updated slice — exactly the repos most likely to
contain skills. Treat "hundreds of thousands" as a plausible ceiling, not a
measured one.

## Metadata

`skill-engine mass-discover` reached 23,367 repositories with this coverage:

| Field | Coverage | Source |
|---|---|---|
| stars, forks, open issues, size | 100% | search |
| created / updated / pushed | 100% | search |
| topics, archived, fork, template | 100% | search |
| licence | 78% | search |
| language | 69% | search |
| **subscribers** (true watch count) | crawl only | `/repos/{o}/{r}` |
| **contributors**, **releases** | `enrich` only | 2 extra requests each |

### What GitHub does not expose

Worth being blunt, because these are the metrics people ask for first:

- **Dependents / "Used by"** is rendered in HTML only. There is no API for it.
- **Traffic** (views, clones) requires *push* access — unavailable for repos
  you do not own.
- **Package downloads** exist only for repos publishing to a registry.

So the engine uses proxies, and the *derived* ones carry more signal than the
raw counts: `stars_per_day` separates a repo that earned 500 stars this month
from one that earned them over three years, and `fork_ratio` separates
something people actually use from something they merely bookmark.

## Parsing and validation

`SKILL.md` is Markdown with YAML frontmatter. Validation is deliberately
two-tier:

- **Hard problems** — no frontmatter, no `name`, no `description`, unparseable
  YAML, empty body — mean the file is not a skill. Excluded from search.
- **Soft warnings** — description over the spec's 1024-char limit, a name that
  is not a clean slug — mean it is a real skill that bends the spec. Indexed,
  with a score penalty.

This distinction is load-bearing. Enforcing the spec strictly as an admission
test dropped `anthropics/skills`' own `claude-api` skill — 74KB of genuinely
useful content — over a description 44 characters too long. A search engine
that cannot find real, working skills has failed at its only job.

## Retrieval

Three signals, fused with **weighted Reciprocal Rank Fusion**:

1. **BM25** via SQLite FTS5, with per-column weights — a term matching a
   skill's `name` counts far more than the same term buried in its body.
2. **Vector cosine**, when embeddings are enabled, for queries phrased
   differently from the skill's own vocabulary.
3. **The quality prior** described below.

Quality enters the *same* fusion as a third ranked list rather than being
blended in afterwards as a 0–1 number. That detail matters: RRF scores are
compressed — with the literature's `k=60`, first place beats second by 1.6% —
so a prior on a full 0–1 scale silently overrules the retrievers, and you end
up ranking by popularity with a search box attached. Keeping every signal in
rank space is what makes the weights mean what they say. `k=20` here, because
we care about discrimination inside the top ten rather than deep-rank
robustness.

**Duplicate collapsing.** Forks and vendored copies put the same file in many
repositories. Ranking them against each other is useless and actively harmful:
BM25's length normalisation can seat a zero-star fork *above* the original,
because the fork's repo and path fields happen to be shorter. Results are
collapsed on content hash, keeping the highest-quality copy and a count of the
rest.

## The ranking layer

`skill-engine rank` recomputes every score offline from stored metadata. It is
a separate pass, not something done during the crawl, because percentile
normalisation needs the whole corpus — a score assigned mid-crawl would be
measured against a distribution that no longer exists by the time the crawl
ends.

### Signals

Everything GitHub actually exposes, grouped into six bounded families:

| Family | Weight | Signals |
|---|---|---|
| **popularity** | 0.22 | stars, forks, subscribers (true watch count) |
| **momentum** | 0.14 | stars/day since creation, push recency |
| **maintenance** | 0.16 | push & release recency, release count, issues enabled, open-issue load |
| **authority** | 0.13 | org-owned, licence, description, topic curation, homepage, contributor count |
| **craft** | 0.22 | frontmatter validity, description fit, body depth, bundled resources, declared tools, spec cleanliness |
| **distinctiveness** | 0.13 | content uniqueness, name uniqueness, repo focus |

Plus **multiplicative trust penalties**: archived ×0.55, fork ×0.45, disabled
×0.25, template ×0.90, unlicensed ×0.93, aggregator dump ×0.70, inorganic
popularity ×0.72.

### Design decisions

**Percentile, not magic constants.** Stars are power-law distributed: the gap
between 10 and 100 means far more than between 10,000 and 10,100. A formula
like `9 * log10(stars)` bakes in a guess about corpus scale that rots as the
corpus grows. Every heavy-tailed metric is instead normalised against the
corpus's own quantiles, so "top 5% by stars" means the same thing at 500
repositories and at 500,000. Percentiles use **mid-rank** (averaging the left
and right insertion points), because roughly half the corpus has zero stars and
a plain `bisect_left` would score every one of them identically to the single
least-popular repo.

**Missing data must not mean zero.** Search results carry no `subscribers`;
un-enriched repos carry no `contributors`. Scoring those as 0 would punish a
repository for *our* crawl budget rather than its own quality. Absent signals
are dropped and their weight redistributed across the ones we do have — that is
what `blend()` does, and it is why coverage gaps degrade the ranking gracefully
instead of corrupting it.

**Multiplicative trust, additive quality.** Being archived or being a fork is
not "a few points worse", it is a different category of thing. Penalties apply
to the whole score, so a fork cannot climb past an original by accumulating
small additive wins elsewhere.

**Inorganic popularity.** Stars are the cheapest signal to manufacture and the
most expensive to ignore, so they are cross-checked against signals that are
hard to fake: forks (someone took a copy) and contributors (someone did work).
The threshold is calibrated against the live corpus, not guessed — across repos
with ≥500 stars the fork/star ratio runs p50 = 0.104, p25 = 0.083, p10 = 0.060,
p1 = 0.009, so the 0.012 cutoff sits just above the 1st percentile and flags
~1.7% of them. The guard never fires on missing data, which would punish an
un-enriched repo for what we failed to fetch.

### Author standing

A skill's file tells you how well-made it is. It cannot tell you whether the
person who wrote it knows what they are doing, or merely copied someone else's
work into a repository of their own. That second question matters a great deal
here: **20% of all indexed skills are verbatim copies of another skill.**

Every author gets a profile built entirely from data already in the corpus — no
extra API calls — and a 0–100 standing from six signals:

| Signal | Weight | What it captures |
|---|---|---|
| **craft** | 0.30 | median craft of everything they have published |
| **originality** | 0.22 | share of their skills that are not copies |
| **reach** | 0.16 | stars and forks across the whole portfolio |
| **body of work** | 0.10 | how many skills — damped logarithmically |
| **consistency** | 0.08 | do they license and describe their repos |
| **followers** | 0.08 | optional, via `enrich-authors` |
| longevity + upkeep | 0.06 | tenure, and whether it is still maintained |

Originality is the signal nothing else captures, and it separates authors
sharply. Measured on the live corpus:

```
93.4  giuseppe-trisciuoglio   119 skills  100% original   craft 0.91
59.8  arjun988                188 skills    0% original   (all vendored copies)
```

More skills, far lower standing. Body of work is damped on purpose —
publishing 400 skills is not forty times the evidence of publishing ten, and
rewarding it linearly is exactly how you promote bulk scrapers.

**Avoiding circularity.** Author standing feeds the skill score, so it must not
be built *from* the skill score — that is a feedback loop where popular authors
inflate their own skills, which inflate them further. It is built from
`ranking.craft_score` instead, which judges a `SKILL.md` on its own contents and
knows nothing about repositories or authors. The dependency graph stays acyclic:
craft → author → skill. A test asserts this directly, by checking that
`build_profiles` never references `score_skill`.

Author standing then enters the skill score as its own family (weight 0.16,
against repo standing 0.32 and craft 0.33), and appears in the UI both as a
per-result chip and as a panel in the detail drawer showing the author's skill
count, originality, reach and craft.

```
skill-engine authors --min-skills 10      # rank authors by standing
skill-engine enrich-authors --limit 500   # add follower counts (1 request each)
```

### Result diversity

A single well-made collection can hold hundreds of skills, and on a broad query
it will legitimately win every top slot — leaving a result page that answers
"which repo is best" when the user asked "which skill do I want". Results are
capped at 3 per repository by default (`--max-per-repo`, 0 to disable).
Surplus hits are **demoted, not dropped**, so a query only one repository can
answer still returns everything it has.

This also drove a weighting change. Deriving the repository's share of a
skill's score by summing the four repo families gave it 65% of the total, and
every skill in one strong repository outranked every skill everywhere else
regardless of its own quality. `repo_standing` is now stated explicitly at
0.40 against craft's 0.38: the repository is context, not the subject. After
the change a 277-star repo's skills rank alongside a 37,681-star repo's on
craft alone.

### Explainability

A ranking you cannot interrogate is a ranking you cannot debug. Every score
stores its full breakdown, and `skill-engine explain` prints it:

```
$ skill-engine explain 42
pdf (anthropics/skills)
  score 71.66  = 100 × base 0.7166 × trust 1.0000

  repo_standing    0.6479   ███████████████████
  craft            0.9245   ███████████████████████████
  distinctiveness  0.6667   ███████████████████
```

Weights are overridable per run — `skill-engine rank --weight popularity=0.35
--weight craft=0.15` — so you can tune for your own notion of quality and see
immediately what moves.

## Embeddings are optional

BM25 alone is a strong baseline here — skill descriptions are short,
keyword-dense, and written to be matched. Embeddings are opt-in:

| `--embedder` | Cost | Use |
|---|---|---|
| `none` (default) | free | BM25 only |
| `hashing` | free | Exercises the vector path; not semantic |
| `local` | free | `sentence-transformers` on CPU |
| `voyage` | per-token | Voyage AI, the provider Anthropic recommends |

Anthropic does not serve an embeddings endpoint, so `voyage` or `local` are the
real semantic options.

## Storage

SQLite with FTS5 — a deliberate choice, not a placeholder. The corpus of public
agent skills is currently in the tens of thousands of documents; FTS5 ranks
that with BM25 in single-digit milliseconds, in one file, with no server. The
schema maps cleanly onto Postgres + `tsvector` if it outgrows that. Vector
search is an exact scan, which is correct at this size and avoids an ANN index
that would need rebuilding after every crawl.

## Commands

```
skill-engine discover      [--sources seeds topics keyword code awesome gharchive owners]
skill-engine mass-discover [--target 10000]   # sweep search until N repos are known
skill-engine crawl         [--limit N] [--batch N]
skill-engine enrich        [--limit N]        # contributor + release counts
skill-engine run           [--limit N]        # discover + crawl, for cron
skill-engine rank          [--weight name=v] [--top N]   # recompute scores, offline
skill-engine explain       ID | owner/repo    # why it scored what it did
skill-engine index         [--embedder ...]   # build vectors
skill-engine search        QUERY [--min-stars N] [--min-score N] [--license MIT]
                                 [--kind ...] [--language ...] [--owner-type ...]
                                 [--max-age-days N] [--json]
skill-engine show          ID [--raw]
skill-engine stats                            # includes metadata coverage
skill-engine serve         [--port 8000]
```

A full pipeline from empty:

```bash
skill-engine mass-discover --target 10000   # search bucket; core untouched
skill-engine crawl --limit 2000             # 2 requests per repo, 0 if unchanged
skill-engine enrich --limit 500             # contributors + releases for the best
skill-engine rank                           # corpus-wide percentile scoring
```

### Running it continuously

```bash
# every 30 min: pick up pushes to known repos, crawl what is due
*/30 * * * * skill-engine run --limit 400 --hours 1

# weekly: re-run the expensive discovery sources
0 3 * * 0    skill-engine discover --sources topics code owners
```

`crawl` only revisits a repository once `SKILL_ENGINE_REFRESH_HOURS` (default
72) has elapsed, so re-running it frequently is cheap and safe.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_TOKENS` | – | Comma-separated PATs; the pool |
| `GITHUB_TOKEN` | – | Single-token fallback |
| `SKILL_ENGINE_DB` | `data/skills.db` | Database path |
| `SKILL_ENGINE_CONCURRENCY` | 6 | Concurrent API requests |
| `SKILL_ENGINE_REFRESH_HOURS` | 72 | Minimum age before re-crawling |
| `SKILL_ENGINE_EMBEDDER` | `none` | `none`, `hashing`, `local`, `voyage` |

Running without a token works but gives you 60 requests/hour and no code
search.

## The search UI

`skill-engine serve` puts a search interface on `http://127.0.0.1:8000` — one
dependency-free page: live search as you type, facets computed from the matched
set (location, licence, language) with counts, quality/popularity/freshness
thresholds, a result count, and a detail drawer showing the full `SKILL.md`
alongside the score breakdown that put it where it is. `/` focuses the box,
`Esc` closes the drawer, and the URL carries the query and filters so a search
is shareable.

Getting it usable at 100k skills took three fixes, each found by measuring
rather than guessing:

| Fix | Before | After |
|---|---|---|
| Reuse the SQLite connection per thread | 2,135 ms | 590 ms |
| Compute all facets in one FTS match | 3,410 ms (facets alone) | 350 ms |
| Drop stopwords from the match expression | 1,600 ms / 86,036 hits | 323 ms / 23,959 hits |

The first was self-inflicted: the handler opened a fresh `Store` per request,
re-running the schema script and migration check against a 2.5GB file every
time. The second was one grouped query per facet, each re-executing the same
full-text match — matching once and tallying in Python is 10x faster. The third
is a relevance fix that happens to be a performance fix: under OR semantics
*a* and *from* match nearly every document, so "extract tables from a pdf" was
ranking 86,036 skills to find the same answers that 23,959 gave.

## API

```
GET /                          # the search UI
GET /api/search?q=...&limit=20&facets=1
                               # &kind= &license= &language= &min_stars=
                               # &min_score= &max_age_days= &forks=1
GET /api/skill/{id}            # full body, resources, score families
GET /api/stats
```

Every search response carries `total` (matches before truncation), `took_ms`,
and optional `facets`, so a client can build its own interface without a second
round trip.

## Tests

```bash
pytest          # 106 tests, no network required
```

The GitHub client, rate-limit handling, and full harvest pipeline are tested
against a mock transport, including the assertions that unchanged repositories
cost nothing and that only changed files are refetched.
