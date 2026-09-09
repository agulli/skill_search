# skill-engine: a search engine for AI agent skills on GitHub

**Design and implementation notes**

---

## Abstract

skill-engine discovers, harvests, validates, ranks and serves the public corpus
of AI agent skills — `SKILL.md` files — published on GitHub. The crawl has
reached **3.12M skills** (2.01M unique after content-hash dedupe) from **1.27M
harvested repositories**, drawn from a discovered pool of **2.6M**. The index
currently in production is a **100,006-skill** cut of it (87,033 unique) serving
in **25–130 ms**, and section 9 explains why the largest corpus is deliberately
not the one deployed.

Two scales are discussed throughout, and they are not interchangeable. The 100k
figures describe a corpus small enough that everything fits in cache and 87% of
documents are unique. The multi-million figures describe a different regime, in
which duplication, disk, and an upstream byte limit dominate — and in which
several decisions that were right at 100k became wrong.

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

Findings from scaling to millions, added after the original 100k build:

| Result | Figure | Mechanism |
|---|---|---|
| Harvest throughput unblocked | **2,055 → 9,256 repos/h** | batch size decoupled from concurrency |
| Round transition | **3.5 hours → 2 seconds** | inter-round reranking disabled during crawl |
| Crawl database | **66 GB → 14 GB** | body cap, stale FTS cleared, vacuum |
| Upstream ceiling identified | **bytes, not requests** | a big-repo sweep that lost more than it gained |
| Refusals per hour under load | **169 → 1–5, and zero 403s** | global AIMD backoff honouring `Retry-After` |

Five findings generalise beyond this project:

1. **The scarce resource is rarely the one you optimise for by default.** Three
   separate times, a limit that looked binding was not: the REST quota (bypassed
   by codeload), the fetch cap (protecting quota that content fetches never
   consumed), and query cost (dominated by a 2 MB page cache, not by SQL).
2. **A ranking signal must be measured against the corpus, not guessed.** Every
   threshold here is calibrated from the live distribution; the one time a
   constant was picked by intuition it silently inverted the ranking.
3. **Ordering beats volume.** Crawling in predicted-quality order reached a
   100,000-skill target using 26% of the queue.
4. **More corpus is not more product.** Going from 100k to 1.44M documents
   *improved* precision at rank 1 and *degraded* recall at rank 10, because
   near-identical copies crowd the candidate pool before deduplication runs.
   Volume without a retrieval layer that can exploit it makes search worse.
5. **A measurement at n=100 is a rumour.** An A/B that looked like a clear win
   at n=100 was flat at n=300 — and then turned out to have been run on a
   corpus where the variable under test barely varied. Both checks were cheap;
   neither was optional.

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
| Repository search | search bucket only | 2.6M repos discovered; still ~19% novel per probe at 1.17M known |
| Awesome-list mining | **0 API requests** | 3,560 repos from 2 README fetches |
| GH Archive | **0 API requests** | ~25 new repos per archive-hour, but *only* the trailing week — older windows yield zero |
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

**codeload has no quota header, so its limit must be measured.** After roughly
forty hours of continuous crawling it began returning 429s — 169 in one hour.
The original handler slept 30 seconds inside the single task that was refused
and marked that repository failed, which left the other 39 workers hitting the
endpoint at the unchanged rate. The refusals continued, the pause repeated, and
throughput sawtoothed instead of settling.

The response is now global, and is ordinary congestion control:

* A 429 doubles the delay between request *starts* for every worker, not just
  the one refused, and pauses them all.
* The pause honours `Retry-After` when the server sends one, bounded to 300s.
  Guessing a duration the server has stated is both worse behaved and worse
  engineering — too short walks back into the refusal, too long idles for
  nothing.
* Each success walks the delay back down. The step matters as much as the
  backoff: at 0.01 the delay recovered a doubling in about eight seconds and
  immediately earned another refusal — four in ten minutes, oscillating between
  the safe rate and the rejected one. At 0.001, recovery takes minutes.
* Recovery accelerates only after 10 and 30 minutes without a refusal, capped
  at 4x. A long quiet period is evidence the limit has lifted; the first
  success after a refusal is evidence of nothing. The cap is deliberate: this
  is a taper, not a search for the boundary.

Observed converging on first deployment: 0.25 → 0.50 → 1.00 → 1.81 → 3.00s,
then quiet. Across the following day: 1–5 refusals an hour, hours at a time held
at maximum pacing, **zero 403s over 30+ hours and 3.1M skills**.

**403 is not 429, and treating them alike hid a real signal.** Both were
originally handled by the same branch, which made a block and a throttle
indistinguishable in the logs — and backing off, the correct response to a 429,
does nothing about a 403. They are now separate: five 403s within ten minutes
trips a breaker that stops the sweep outright rather than finishing the batch,
because every further request while forbidden makes a temporary block likelier
to become permanent. A single 403 does not halt a multi-day crawl, since a few
are per-repository — takedowns, disabled repositories — rather than about us.
The queue is the checkpoint, so stopping costs nothing but time.

**On rate limits as an adversary.** It is worth stating what this system does
not do. All traffic originates from one address, one User-Agent, one token;
nothing is hidden and nothing could be without changing identity. Backing off
on a 429 and recovering slowly is what a well-behaved client is *supposed* to
do — `Retry-After` exists precisely because servers expect clients to return.
That is a different activity from timing requests to stay under a detector,
which presumes the operator would object if they understood. The practical case
matches the principled one: visible compliance is what has kept this crawl at
zero blocks, whereas an address that appears to be gaming a limit gets stopped
at the account level, which costs the token and the project, not just the IP.

### 3.6 Rejected alternatives

| Rejected | Why |
|---|---|
| **HTML scraping** | Rate-limited then IP-banned; unnecessary, the API has everything |
| **Code Search as the backbone** | 10 req/min, capped at 1,000 results, requires a search term. A seed source, not an engine |
| **A GitHub App** | Installation tokens are scoped to repositories where the app is installed; you cannot install one on strangers' repositories. Useful only for your own org |
| **Per-file `contents` API** | Costs core quota per file; `raw.githubusercontent.com` does not |
| **BigQuery's public GitHub dataset** | Frozen at **2022-11-26**. 228 files across 2.3 billion match `SKILL.md` or `.claude/skills`, because agent skills are a 2024–25 convention. Recommended three times on the assumption that a bulk source is a fresher one; a $0.81 count query settled it. Check a dataset's modified date before designing around it |
| **Sharding the sweep across processes** | Tested to determine whether codeload's limit was per-connection. Four processes over disjoint queue slices gave **6,400 repos/h against one process's 9,256** — the modulo predicate defeats the `repos_score` index, and the limit is per-IP anyway |
| **Deep GH Archive mining** | Windows older than about a week yield **zero** new candidates; the earlier crawl already covered them. Only the trailing week is worth mining, at ~25 new repositories per archive-hour |
| **The REST harvest running beside the codeload sweep** | Uses a different quota bucket, so it looked additive. Measured **210 repos/h against the sweep's 8,000** while contending for the write lock. Net negative |
| **A dedicated large-repository sweep** *(later reversed — see below)* | Measured while a productive small-repository queue existed: **+4,524 skills/h gained, −6,129 lost** to the bandwidth it took from the main sweep. Correct at the time and wrong later |

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

SQLite with FTS5 — a deliberate choice, not a placeholder. At 100k documents
FTS5 ranks with BM25 in tens of milliseconds, in one file, with no server. The
schema maps cleanly onto Postgres + `tsvector` if it outgrows that.

It has since been pushed considerably further, and the limits found were not the
ones expected. A 3.1M-skill crawl database is entirely workable for writing and
aggregation; what degrades is *serving* — the 1.44M shipped index measures 87 ms
at p50 against the 100k index's 7 ms, because the working set no longer fits the
page cache of a small machine. The constraint is memory for the cache, not
SQLite. Two related lessons: the write-ahead log grows without bound if the FTS
triggers are left live during a bulk rewrite (categorising alone pushed it past
15 GB), and `VACUUM` needs room for a second copy, which is what made a 70 GB
crawl database undeployable on a 155 GB disk.

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

## 4b. What changed at three million

The 100k build and the multi-million build are different engineering problems.
Four decisions that were correct at the smaller scale became wrong at the
larger one, and each was found by measurement rather than review.

### 4b.1 Batch size must not equal concurrency

The sweep ran with `batch=24` against `concurrency=24`, so exactly one batch was
ever in flight and it could not finish until its slowest member did: one large
tarball stalled twenty-three completed downloads. The sweep held **0.6 repos/s
against a pacing floor permitting 6.7**.

Widening the batch to 400 while leaving concurrency at 40 keeps the semaphore
saturated — as each fetch finishes the next starts, so stragglers overlap with
useful work instead of blocking it. Throughput went from **2,055 to 9,256
repos/h**, a 4.5x improvement from one number.

### 4b.2 The upstream limit is bytes, not requests

Above ~9,000 repos/h, more concurrency made throughput *worse*: 100 connections
measured 8,704 repos/h against 40 connections' 9,256, at 30% CPU, 14 Mbps and no
failures. Nothing local was saturated, which pointed upstream but did not say
which resource.

A dedicated large-repository sweep settled it. If the limit were on *requests*,
fetching 40-skill repositories instead of 6-skill ones would be a large win. It
was not: the large sweep gained 4,524 skills/h and cost the main sweep 6,129.
Under a byte ceiling, small repositories are simply better value per byte, and
taking bandwidth from them is a net loss.

### 4b.3 Ranking during a crawl is pure waste

Each 100,000-repository round ended with a full `recompute`: corpus statistics
over 1.43M repositories, profiling 212,343 authors, then scoring everything.
That took **3.5 hours during which the crawler harvested nothing** — a third of
its throughput.

The work is redundant while crawling. `release.py` ranks once from the finished
corpus, and a mid-crawl ordering only decides which repositories are swept next.
Disabled, the same round transition takes **two seconds**.

### 4b.4 The same measurement, two opposite answers

The large-repository sweep was measured, found net negative, and disabled. Days
later the identical change was measured again and found strongly positive. Both
measurements were right; the *alternative* had changed.

The first test ran while the queue still held productive small repositories at
5.4 skills each, so bandwidth spent on a 25 MB archive was bandwidth taken from
something already paying well. By the second test that queue was drained — what
remained was a low-yield discovery source at **0.3 skills/repo**, while ~35,000
repositories from the good sources sat untouched behind a 10 MB size cap.

Against that alternative the large repositories are not marginally better but
transformative:

| | harvested | productive | skills/repo |
|---|---|---|---|
| under 10 MB | 607,141 | 48.1% | 4.33 |
| **10–50 MB** | 3,201 | **50.3%** | **17.21** |

Admitting them took the sweep from ~400 skills/hour to **~85,000**, with
`topic:claude-skills` repositories returning **54.3 skills each**.

The lesson is about what a throughput measurement actually measures. "Is X
worth doing?" is never answered in isolation — it is answered against whatever
X displaces, and that comparator moves as the system runs. A conclusion drawn
from a benchmark carries an unstated clause about the conditions it was taken
under, and the conclusion expires when they change.

### 4b.5 Storing full bodies made the corpus undeliverable

At 2.47M skills the crawl database reached **70.8 GB** — 28 KB per skill, nearly
all body text. That made 5M skills reachable and a release from them impossible:
`release.py` needs a snapshot plus a compacted copy, roughly 258 GB against 155
GB free.

Bodies are now capped at 4,000 characters on write. Nothing the shipped index
keeps is lost, because `release.py` already truncated to 2,000 — and that
measured *better* on every retrieval metric, since truncation removes spurious
matches deep in long documents. `content_hash` and `body_len` are computed
upstream from the full text, so deduplication and recorded lengths are
unaffected.

With a one-time trim, clearing the stale full-text index, and a vacuum:
**66 GB → 14 GB**, all 2,491,950 skills intact.

---

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
| distinctiveness | 0.19 | adoption, sprawl, name uniqueness, repository focus |

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

### 5.3b Adoption, not "uniqueness" — a signal that was backwards

The distinctiveness family originally contained a signal computed as
`1 / (1 + log2(copies))`, on the reasoning that *one copy is unique, ten copies
is boilerplate*. Measured against the corpus, that is backwards for the dominant
case.

The most-copied skills sit in **different owners'** accounts:

| skill | copies | distinct owners | owners per copy |
|---|---|---|---|
| `skill-creator` | 1,998 | 1,820 | 0.91 |
| `webapp-testing` | 1,455 | 1,356 | 0.93 |
| `canvas-design` | 1,232 | 1,174 | 0.95 |
| `clone-website` | 1,514 | 502 | **0.33** |

At 0.9 owners per copy, those are ~1,800 independent people each choosing to
vendor a skill. That is adoption evidence — structurally the same as a citation
count — and the old signal demoted precisely the skills the community had most
clearly endorsed.

Raw copy count cannot distinguish that from one account holding three copies of
a file, which is why it was the wrong variable rather than the wrong sign.
Counting **distinct owners** can, so the original insight survives where it
applies: `clone-website` at 0.33 owners per copy is intra-account sprawl and is
still penalised. Adoption is floored at 0.5, so a rare skill is not taxed for
being rare — adoption is a bonus for the widely held, not a tax on the obscure.

The measurement discipline here is worth recording, because the first two
attempts to validate this change were both worthless. An A/B at n=100 showed a
clear improvement; the same A/B at n=300 was flat. And the corpus it ran on —
the deployed 100k index — turned out to have **95.8% single-owner skills**, so
the signal could not fire at all and the scores merely shifted uniformly, which
reorders nothing. A change can be well-justified, harmless, and still unproven;
this one is.

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

**Collapsing after retrieval stops working at scale.** Search over-fetches five
times the requested results and collapses afterwards, so ten results are chosen
from fifty candidates. That multiplier was tuned when 87% of the corpus was
unique. At 66% unique it fails: a query matching `skill-creator` — which has
1,998 copies — pulls copies of one file into most of those fifty slots, and
after collapsing there is almost nothing left.

This is the mechanism behind the most counter-intuitive measurement in the
project. Going from 100k to 1.44M documents *improved* precision at rank 1
(0.48 → 0.64 on 8-term queries) and *degraded* recall at rank 10 (0.93 → 0.80,
and 0.82 → 0.49 on 3-term queries). More candidates mean the genuinely best one
is more likely present; they also mean the rest of the page fills with clones of
it. Short, vague queries — what users actually type — suffer most.

The structural fix is to deduplicate at **build** time, keeping one row per
content hash and carrying the copy count onto the survivor. Raising the
over-fetch multiplier is the tempting cheap fix and is wrong: no multiplier
survives a 1,998-copy cluster.

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

## 6b. The catalogue

Search requires knowing what you want. For a corpus nobody has surveyed, the
more common need is the opposite — *show me what exists* — so the landing view
is a browsable directory of 16 subjects, each with three to five
subcategories, counts, and skills ordered by the quality prior.

**Rules, not a model.** Classifying 100,000 skills with an LLM would cost real
money, take hours, and need re-running after every crawl. Categories are instead
matched by weighted patterns over a skill's name, description, path and its
repository's topics: deterministic, a few minutes over the whole corpus, free,
and explainable — a directory that cannot say *why* something is filed
somewhere cannot be corrected.

The taxonomy was derived rather than invented: term frequencies across 95,725
skill names and descriptions surfaced the real clusters (review, design, api,
analysis, audit, content, product, planning, security, mcp, research,
architecture, testing), and the subjects follow them.

### Three corrections, each found by looking at the output

**Breadth beat relevance.** Counting raw pattern matches put **69.7% of the
corpus into one category**. No single term was responsible — the most common,
"agent", appears in under 10% of skills. The cause was structural: a category
with twenty patterns simply accumulates more than one with eleven. Weighting
each pattern by its inverse document frequency against the corpus makes
categories compete on the *specificity* of what they matched rather than on how
many patterns their author happened to write.

**Corpus-universal terms identify nothing.** Even IDF-weighted, "AI & Agents"
still took 54%. The words "agent", "claude", "prompt" and "eval" describe what
this entire corpus *is*; inside it they are stopwords, not subjects — an "AI"
category in a directory of AI skills is a "Websites" category in the Yahoo
directory. Narrowed to genuinely distinct topics (MCP servers, RAG, fine-tuning,
guardrails), the distribution flattened.

**A missing word boundary.** The pattern `auth` compiled to `\bauth` with no
trailing anchor and matched "Cypher **auth**oring", filing a Neo4j Spark
connector under Security. Short patterns now match whole words; stems of six
characters or more keep prefix matching, so "refactor" still catches
"refactoring". Separately, a description listing seven features let a passing
mention of JWT outvote the subject, so each field's contribution is capped.

The result is a directory rather than a pile:

| Subject | Share | | Subject | Share |
|---|---|---|---|---|
| Business & Marketing | 15.7% | | Product & Planning | 4.6% |
| Productivity & Workflow | 12.6% | | Writing & Content | 4.5% |
| AI & Agents | 8.0% | | Testing & Quality | 3.9% |
| Security | 7.4% | | Data & Analytics | 2.8% |
| *Uncategorised* | *6.8%* | | Mobile & Desktop | 2.8% |
| Web & Frontend | 6.2% | | Documents & Files | 2.0% |
| Research & Science | 6.0% | | Games & Simulation | 0.9% |
| Design & Creative | 5.9% | | | |
| DevOps & Infrastructure | 5.1% | | | |
| Software Engineering | 4.9% | | | |

Uncategorised is shown rather than hidden. A directory that silently drops what
it cannot place is lying about its own coverage.

Browsing reuses the search pipeline's duplicate collapsing and per-repository
cap. Without them a category page is one prolific repository fifteen times over.

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

### 7.6 A batch that could not outrun its slowest member

`batch=24` against `concurrency=24` meant one batch in flight, finishing only
when its slowest member did. One large tarball stalled twenty-three completed
downloads, holding the sweep at 0.6 repos/s against a floor permitting 6.7.
Decoupling the two — batch 400, concurrency 40 — was a 4.5x improvement from a
single number, and no profiler would have pointed at it: nothing was slow, the
work was simply serialised behind its worst case.

### 7.7 A local backoff that made refusals worse

Sleeping 30 seconds inside the one task that received a 429, while 39 others
continued at the unchanged rate, is not backing off — it is backing off one
thread. Refusals continued, the pause repeated, throughput sawtoothed. The fix
was to make the response global; the subtlety was that the *recovery* step
mattered as much as the backoff, since too fast a recovery reproduces the
oscillation with extra steps.

### 7.8 Three signals that were measured and discarded

Recorded because a negative result that is not written down gets re-attempted:
queue sharding (worse — the modulo predicate defeats the score index), the REST
harvest running beside the sweep (210 repos/h against 8,000, while contending
for the write lock), and a large-repository sweep (gained 4,524 skills/h, cost
6,129). The last of these is the useful one: it is what established that the
upstream limit is on bytes rather than requests, which no amount of tuning
concurrency had settled.

### 7.9 An assumption about freshness that survived three recommendations

BigQuery's public GitHub dataset was proposed three times as the way past
codeload's ceiling, on the reasoning that a bulk source must be faster than a
crawler. It is — for 2022. The dataset is frozen at 2022-11-26 and contains 228
files matching `SKILL.md` across 2.3 billion, because agent skills postdate it
entirely. A $0.81 count query settled what three rounds of argument had not.
The lesson is not about BigQuery: *check a dataset's modified date before
designing around it*, and prefer the cheap query that could falsify a plan over
the expensive one that assumes it.

---

## 8. Design choices, consolidated

| Choice | Alternative | Rationale |
|---|---|---|
| Git Trees API as REST backbone | Code Search | One request lists every path; code search caps at 1,000 results and 10 req/min |
| codeload archives as primary path | REST trees | Zero API quota, ~16,000 repos/hour |
| PAT pool | GitHub App | Installation tokens cannot read strangers' repositories |
| SQLite + FTS5 | Postgres, Elasticsearch | 100k docs rank in tens of ms, one file, no server. Holds to millions for writing; serving degrades once the working set outgrows the page cache, which is a memory limit rather than a SQLite one |
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

**Coverage.** 1.27M of 2.6M discovered repositories are harvested; the rest
remain queued, and the backlog is *growing* — discovery runs roughly four times
faster than harvesting, so the crawl does not converge on its own. Under the
adaptive throttle the sweep sustains 1,000–10,000 repositories an hour, which
makes the remaining queue weeks of work rather than days.

**The population estimate is soft.** Three methods roughly agree on 4.5–6M raw
skills: GitHub's code search reports ~5.77M `SKILL.md` files (a heavily rounded
estimate — exactly 5.5 × 2²⁰ — not a count); draining the current queue at the
measured 4.23 skills/repo projects 5.3M; and fitting the decay in discovery
novelty (29.4% new at 711k repositories known, 19.0% at 1.17M) implies a
searchable population near 1.4–1.6M repositories. None of these is a measurement
of the thing itself, and yield decays as the crawl works down the queue, so the
lower end is likelier.

**Quality dilutes with scale, measurably.** Comparing the deployed 100k index
with the 2.97M crawl:

| | 100k | 2.97M |
|---|---|---|
| valid | 95.7% | 93.0% |
| unique | **87.0%** | **66.3%** |
| licensed | **90.2%** | **50.5%** |

A third of the large corpus is redundant, and the redundancy is concentrated:
**366,099 rows — 12% — are copies of just 2,901 skills**, overwhelmingly the
canonical starter skills that thousands of projects vendor. The halving of
licence coverage matters separately: anyone who needs to know they may legally
reuse a skill is served much worse by the larger corpus, which argues for
surfacing licence as a filter rather than hiding it.

**Originality is corpus-relative.** It means "first or only holder of this
content hash *in our index*". If the copy was indexed and the original was not,
the copier receives undeserved credit. Completing the crawl tightens this.

**Enrichment is incomplete.** `contributors`, `releases` and author `followers`
are at 0% coverage; their weights are currently redistributed. Each costs 1–2
API requests per subject.

**No relevance evaluation set.** Ranking is validated by property tests, a
label-free known-item benchmark, and spot-checks — not by human judgements.
The benchmark measures whether a *specific* skill can be found from its
description, which is a genuine signal but not the whole of relevance: it does
not reward surfacing the *canonical* copy among equivalents, which is precisely
what the adoption signal is for. A change can therefore be invisible to it and
still be right, or visible to it and still be noise. Both happened.

**The largest corpus is deliberately not deployed.** The 1.44M index measured
*worse* than the 100k on short queries (MRR 0.313 against 0.441), needs a
machine roughly five times larger, and is 12x slower at p50. Serving it would
mean paying more for a worse experience. The corpus is ahead of the retrieval
layer, and the work that would let volume pay off — build-time deduplication
and embeddings — is not done. Crawling further is currently the *least*
valuable thing that could be done to this project.

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
