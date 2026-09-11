# Technical Architecture & Design Specification: skill-engine

**High-Performance Search & Quality Ranking Engine for AI Agent Skills**

---

## Abstract

`skill-engine` is an open-source indexing pipeline and search engine for AI agent skills (`SKILL.md` specifications) distributed across public code repositories (GitHub, GitLab, and Hugging Face). The crawler has indexed over **3.36M skills** (2.17M unique skills post-deduplication) across **371,465 repositories** and **212,343 authors**. 

The production serving engine powers sub-100ms queries over a high-density **100,006-skill** curated index. This document outlines the system architecture, zero-quota crawler design, two-tier validation pipeline, percentile-normalized quality model, Reciprocal Rank Fusion (RRF) search mechanics, and integration with the Google GenAI SDK and Model Context Protocol (MCP).

---

## 1. System Architecture

The pipeline operates across decoupled stages, with state checkpointed in an ACID-compliant SQLite WAL database:

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
│  Multi-Source   │ ──▶ │ Queue Engine │ ──▶ │ Harvester    │
│  Discovery      │     │ (Prioritized)│     │ (Zero-Quota) │
└─────────────────┘     └──────────────┘     └──────────────┘
                                                    │
                                                    ▼
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
│ Retrieval & UI  │ ◀── │ Quality Rank │ ◀── │ Parser       │
│ FTS5 / MCP / API│     │ Scoring Model│     │ Validation   │
└─────────────────┘     └──────────────┘     └──────────────┘
```

### Module Responsibilities

| Module | Core Responsibility |
|---|---|
| `github.py` | Rate-limited API client: token pool rotation, conditional ETag requests, AIMD backoff |
| `discover.py` | Multi-source discovery: date-bisected search, topic sweep, author expansion |
| `tarball.py` | High-throughput archive streaming from `codeload.github.com` (0 REST quota) |
| `harvest.py` | Fallback REST/GraphQL tree harvest for oversized repositories |
| `parse.py` | YAML frontmatter parsing, two-tier validation, craft signal extraction |
| `ranking.py` | Percentile normalization, multi-signal scoring families, trust multipliers |
| `authors.py` | Author track record, originality ratio, portfolio aggregation |
| `taxonomy.py` | IDF-weighted categorization into 16 domain clusters |
| `search.py` | Hybrid BM25/vector retrieval, RRF fusion, diversity demotion, dynamic facets |
| `serve.py` | Embedded stdlib HTTP server, JSON REST API, and responsive web interface |
| `mcp_server.py` | Read-only Model Context Protocol (MCP) server |
| `gemini_agent.py` | Reference agent integration using Google GenAI SDK (`gemini-2.5-flash`) |

---

## 2. Ingestion & High-Throughput Crawling

### 2.1 The Rate Limit Problem
GitHub REST API imposes strict rate limits (60 requests/hour unauthenticated; 5,000/hour per personal access token). A standard recursive repository tree fetch consumes multiple REST requests per repository, capping crawler throughput at ~2,500 repos/hour even with multiple tokens.

### 2.2 Zero-Quota Archive Streaming
To bypass REST API bottlenecks, `skill-engine` streams compressed archives directly from `codeload.github.com`:
- **Protocol**: `GET https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}`
- **API Quota Consumed**: **0 requests**
- **Throughput**: **~16,000 repositories/hour** on standard broadband.
- **In-Memory Filtering**: The `.tar.gz` stream is filtered in memory; only `SKILL.md` files and associated resources (`scripts/`, `references/`) are extracted and parsed, avoiding disk I/O bottlenecks.

### 2.3 Date-Bisected Repository Search
GitHub Code and Repository Search limits results to a maximum of 1,000 items per query. `skill-engine` implements recursive date bisection on the `created:` range:

$$\text{Range}(t_{\text{start}}, t_{\text{end}}) \longrightarrow \begin{cases} \text{Execute Query} & \text{if } N_{\text{total}} \le 1000 \\ \text{Bisect}\left(t_{\text{start}}, \frac{t_{\text{start}}+t_{\text{end}}}{2}\right) \cup \text{Bisect}\left(\frac{t_{\text{start}}+t_{\text{end}}}{2}, t_{\text{end}}\right) & \text{otherwise} \end{cases}$$

This guarantees 100% recall over broad queries, yielding >7,000 repositories from queries that would otherwise truncate at 1,000.

### 2.4 Event Stream Mining (GH Archive)
In addition to search, `gharchive_mine.py` processes hourly public GitHub activity archives (`data.gharchive.org`), extracting candidate repositories from `PushEvent`, `CreateEvent`, and `ReleaseEvent` streams.

---

## 3. Two-Tier Parsing & Validation Pipeline

Skills are structured markdown files with YAML frontmatter:

```markdown
---
name: pdf-table-extractor
description: Extract tabular data and text structures from PDF documents.
license: Apache-2.0
allowed-tools: [Read, Bash]
---
# PDF Table Extractor
Instructions and execution workflow for processing PDF files...
```

### 3.1 Two-Tier Validation Strategy

1. **Hard Validation Failures (Excluded from Index)**:
   - Missing or unparseable YAML frontmatter.
   - Missing required fields (`name`, `description`).
   - Empty markdown body.
2. **Soft Validation Warnings (Indexed with Score Penalty)**:
   - Description exceeding 1,024 characters.
   - Non-slug naming format (e.g. spaces or uppercase).
   - Missing license declaration.
   - Minimal body content (<200 characters).

Preserving valid implementations that slightly deviate from specifications prevents false rejections of valuable community contributions.

---

## 4. Corpus-Calibrated Quality Ranking Model

### 4.1 Percentile Normalization
Raw metrics (stars, forks, subscribers, repo size) exhibit heavy-tailed power-law distributions. Fixed mathematical formulas (e.g., $\log(\text{stars})$) fail as the index scales.

Instead, all unbounded numeric metrics are normalized against the empirical cumulative distribution function (CDF) of the active corpus:

$$S_{\text{metric}}(x) = \frac{\text{Rank}(x \text{ in } C)}{|C|}$$

where $C$ is the sorted corpus array.

### 4.2 Decoupled Scoring Families

Quality prior calculation is broken down into four decoupled families:

$$\text{Final Score} = \left( 0.32 S_{\text{repo}} + 0.33 S_{\text{craft}} + 0.16 S_{\text{author}} + 0.19 S_{\text{distinct}} \right) \times \prod_{i} M_i$$

#### 1. Repository Standing ($S_{\text{repo}}$)
- **Popularity**: Percentile of stars, forks, and subscribers.
- **Momentum**: Star velocity ($\text{stars}/\text{day}$).
- **Maintenance**: Half-life recency decay based on latest push date ($t_{1/2} = 120\text{ days}$).
- **Inorganic Popularity Guard**: Detects synthetic star inflation when the $\text{forks}/\text{stars}$ ratio falls below the 1st percentile ($<0.012$).

#### 2. Skill Craft ($S_{\text{craft}}$)
- Frontmatter completeness (valid description, allowed tools, license).
- Structural quality (presence of headings, code snippets, execution scripts).
- Resource linking (references to bundled `./scripts/` and `./references/`).

#### 3. Author Track Record ($S_{\text{author}}$)
- Portfolio median craft score.
- **Originality Ratio**: Fraction of published skills that are unique vs. identical duplicates across GitHub.
- Account tenure and activity history.

#### 4. Multiplicative Trust Multipliers ($M_i$)
- Archived repository: $\times 0.55$
- Repository fork: $\times 0.45$
- Disabled repository: $\times 0.25$
- Aggregator dump ($>500\text{ skills}$ in repo): $\times 0.70$
- Unlicensed work: $\times 0.93$

---

## 5. Taxonomy & Content Classification

The taxonomy organizes skills into 16 primary categories and 80+ subcategories using deterministic, IDF-weighted pattern matching:

$$\text{Score}(c, s) = \sum_{p \in P_c} \text{Weight}(p) \times \text{IDF}(p) \times \text{FieldMultiplier}(\text{field})$$

### Why IDF Weighting Matters
In a raw keyword frequency approach, broad categories with many rules capture a disproportionate share of the corpus. Weighting pattern matches by $\text{IDF} = \log(N / n_p)$ ensures that specific domain terms (e.g. "model context protocol", "terraform") carry higher discriminative power than universal terms (e.g. "agent", "tool").

```
Distribution across Categories:
├── Business & Management:   15.7%
├── Developer Productivity:  12.6%
├── AI, ML & Data Science:    8.0%
├── Security & Compliance:    7.4%
├── Cloud & Infrastructure:   6.9%
└── Other / Uncategorized:    6.8%
```

---

## 6. Search Architecture & Signal Fusion

Search combines BM25 full-text indexing with optional dense semantic vector retrieval:

```
Query ──▶ [ SQLite FTS5 (BM25) ] ──┐
      ──▶ [ Dense Vector Cosine] ──┼──▶ [ Reciprocal Rank Fusion ] ──▶ Demotion & Deduplication ──▶ Results
      ──▶ [ Quality Score Prior] ──┘
```

### 6.1 Weighted Reciprocal Rank Fusion (RRF)
BM25 scores and vector similarities inhabit different scales. Rather than arbitrary score normalization, `skill-engine` combines ranked lists via RRF ($k=20$):

$$\text{RRF}(d) = \sum_{r \in R} w_r \cdot \frac{1}{k + \text{Rank}_r(d)}$$

Where weights are assigned as:
- $w_{\text{BM25}} = 1.0$ (with column weights: name $\times 10$, description $\times 6$, body $\times 1$, repo $\times 2$, path $\times 1.5$)
- $w_{\text{Vector}} = 0.9$
- $w_{\text{Quality}} = 0.5$

### 6.2 Content Hash Deduplication & Repo Diversity
- **Deduplication**: Results sharing identical SHA-256 body hashes are grouped; the highest-ranking instance is served with an indicator of total vendored copies (`also_vendored_by`).
- **Diversity Cap**: A maximum of 3 results per repository are returned in the top tier; remaining matches from the same repository are demoted down the ranking list.

---

## 7. Storage, Memory Management & Performance

### 7.1 SQLite Engine Configuration
The storage layer utilizes SQLite 3 with FTS5:
- `PRAGMA journal_mode = WAL;`
- `PRAGMA synchronous = NORMAL;`
- `PRAGMA page_size = 4096;`
- In-memory cache allocated to SQLite page cache rather than MMAP to minimize RSS footprint on cloud containers (serving within 190MB RAM).

### 7.2 Release Truncation
Production releases (`release.py`) truncate body text beyond 2,000 characters for the search index. This reduces database size by 61% (2.49 GB $\to$ 1.47 GB), improves p95 query latency by 21x, and eliminates spurious keyword matches occurring deep in extensive documentation files.

---

## 8. Gemini & Model Context Protocol (MCP) Integration

### 8.1 Model Context Protocol (MCP) Server
`mcp_server.py` implements a read-only MCP server exposing four core primitives:
1. `search_skills(query, limit, min_stars)`
2. `get_skill(repo, path)`
3. `browse_category(category, limit)`
4. `corpus_stats()`

### 8.2 Autonomous Gemini Agent Integration
`gemini_agent.py` uses the official Google GenAI SDK (`google-genai`) and `gemini-2.5-flash`:
- Dynamically loads tool declarations from the MCP server at startup.
- Executes an explicit agent loop without automatic function calling, ensuring complete observability over tool invocations.
- Formats structured function responses with graceful error propagation.

---

## 9. Scaling Findings: What Changed Between 100k and 4M

Four decisions that were correct at 100,000 skills became incorrect at several
million. Each was identified by measurement rather than review, and each is
recorded because the failure mode is invisible from throughput alone.

### 9.1 Batch Size Must Not Equal Concurrency

The tarball sweep ran with `batch=24` against `concurrency=24`, so exactly one
batch was ever in flight and could not complete until its slowest member did.
A single large archive stalled twenty-three finished downloads. Throughput held
at 0.6 repos/sec against a pacing floor permitting 6.7.

Decoupling the two — batch 400, concurrency 40 — keeps the semaphore saturated:
as each fetch completes the next begins, so stragglers overlap with useful work
rather than blocking it.

| Configuration | Throughput |
|---|---|
| batch 24, concurrency 24 | 2,055 repos/hr |
| **batch 400, concurrency 40** | **9,256 repos/hr** |
| batch 1000, concurrency 100 | 8,704 repos/hr |

### 9.2 The Upstream Limit Is Bytes, Not Requests

Above roughly 9,000 repos/hr, additional concurrency measured *slower*, at 30%
CPU, 14 Mbps and zero failures. Nothing local was saturated, which indicated an
upstream constraint but not which resource.

A dedicated large-repository sweep resolved it. Under a *request* limit,
fetching 40-skill repositories instead of 6-skill ones would be a large gain.
It was not: the large sweep gained 4,524 skills/hr and cost the main sweep
6,129. Under a byte ceiling, small repositories are better value per byte.

### 9.3 Ranking During a Crawl Is Pure Waste

Each 100,000-repository round terminated with a full `recompute`: corpus
statistics over 1.43M repositories, profiling 212,343 authors, then scoring
every row. This consumed **3.5 hours during which nothing was harvested** —
approximately one third of available throughput.

The work is redundant while crawling. `release.py` ranks once from the finished
corpus, and mid-crawl ordering only determines which repositories are swept
next. With it disabled, the same round transition completes in two seconds.

### 9.4 Full Body Storage Made the Corpus Undeliverable

At 2.47M skills the crawl database reached 70.8 GB — 28 KB per skill, almost
entirely body text. This made 5M skills reachable and a release from them
impossible: `release.py` requires a snapshot plus a compacted copy,
approximately 258 GB against 155 GB of available disk.

Bodies are now capped at 4,000 characters on write. `content_hash` and
`body_len` are computed upstream from the full text, so deduplication and
recorded lengths are unaffected. Combined with clearing the stale full-text
index and vacuuming: **66 GB → 14 GB**, all 2,491,950 skills intact.

### 9.5 The Same Measurement, Two Opposite Answers

The large-repository sweep of §9.2 was measured, found net negative, and
disabled. Days later the identical change measured strongly positive. Both
results were correct; the *alternative* had changed.

The first test ran while the queue still held productive small repositories at
5.4 skills each, so bandwidth spent on a 25 MB archive was taken from something
already paying well. By the second test that queue was drained, leaving a
low-yield discovery source at 0.3 skills/repo while ~35,000 repositories from
productive sources sat untouched behind a 10 MB size cap.

| Size band | Harvested | Productive | Skills/repo |
|---|---|---|---|
| Under 10 MB | 607,141 | 48.1% | 4.33 |
| **10–50 MB** | 3,201 | **50.3%** | **17.21** |

Admitting them moved the sweep from ~400 skills/hr to ~85,000.

A throughput measurement never answers "is X worth doing?" — it answers "is X
worth more than what it displaces?", and that comparator moves as the system
runs. Conclusions drawn from a benchmark carry an unstated clause about the
conditions under which it was taken.

### 9.6 Quality Ranks Relevance Poorly

The sweep ordered candidates by `repo_score` before queue priority.
`repo_score` measures whether a repository is *good* — stars, forks, activity —
not whether it contains skills. Measured by discovery source, yield differs
tenfold while maximum scores are identical at ~98:

| Discovery source | Productive | Skills/repo |
|---|---|---|
| `topic:claude-skills` | 87.2% | 10.25 |
| `topic:agent-skills` | 90.6% | 8.60 |
| Date-sharded search | 56.8% | 4.36 |
| GH Archive | 24.0% | 3.92 |
| Broad keyword cross-products | 8.9% | 0.64 |

Score could not separate them, so the low-yield source — 93% of the queue —
monopolised the head of the ordering. One six-hour period harvested 11,848
repositories for five productive ones. Priority is now the primary sort, score
the tiebreak within a band; yield recovered from 0.0% to 35.8% within minutes.

---

## 10. Rejected Approaches

Recorded because a negative result that is not written down gets re-attempted.

| Approach | Measured outcome |
|---|---|
| **BigQuery public GitHub dataset** | Frozen at 2022-11-26. 228 files across 2.3 billion match `SKILL.md`, because agent skills are a 2024–25 convention. Verify a dataset's modified date before designing around it |
| **Queue sharding across processes** | Tested to determine whether the codeload limit was per-connection. Four processes over disjoint slices: 6,400 repos/hr against a single process's 9,256 — the modulo predicate defeats the `repos_score` index, and the limit is per-IP |
| **Deep GH Archive mining** | Windows older than approximately one week yield zero new candidates; prior crawls already covered them. Only the trailing week is productive, at ~25 new repositories per archive-hour |
| **REST harvest alongside the codeload sweep** | Uses a separate quota bucket, so it appeared additive. Measured 210 repos/hr against the sweep's 8,000 while contending for the write lock |
| **Broad `language:` / `stars:` query partitions** | Partition GitHub, not the skill corpus. Queued 1.3M repositories at 0.64 skills each |
| **HTML scraping** | Rate-limited then IP-banned; unnecessary given API coverage |
| **Code Search as the primary backbone** | 10 req/min, capped at 1,000 results. A seed source, not an engine |

---

## 11. Rate Limit Handling

`codeload.github.com` publishes no quota header, so its limit must be inferred
from refusals. After approximately forty hours of continuous crawling it began
returning HTTP 429 — 169 in a single hour.

The original handler slept 30 seconds inside the single task that was refused,
leaving the remaining workers at the unchanged rate. Refusals continued, the
pause repeated, and throughput oscillated rather than settling.

The current implementation applies standard congestion control:

1. A 429 doubles the inter-request delay for **every** worker and pauses them
   collectively.
2. The pause honours `Retry-After` when supplied, bounded to 300 seconds.
3. Each success decrements the delay. The recovery step matters as much as the
   backoff: at 0.01 the delay recovered a doubling in eight seconds and
   immediately earned another refusal. At 0.001, recovery takes minutes.
4. Recovery accelerates only after 10 and 30 minutes without refusal, capped at
   4x — a taper, not a search for the boundary.

Observed convergence: 0.25 → 0.50 → 1.00 → 1.81 → 3.00 sec, then quiet.
Sustained result: 1–5 refusals per hour and **zero HTTP 403 responses across
30+ hours and 3.1M skills**.

HTTP 403 is handled separately from 429. Both were originally handled by one
branch, which made a block indistinguishable from a throttle — and backing off,
the correct response to a 429, does nothing about a 403. Five 403 responses
within ten minutes trips a breaker that halts the sweep, because further
requests while forbidden make a temporary block likelier to become permanent.

---

## 12. Limitations

**Quality dilutes measurably with scale.** Comparing the 100k production cut
against the full crawl:

| Metric | 100k cut | 2.97M crawl |
|---|---|---|
| Valid | 95.7% | 93.0% |
| Unique | **87.0%** | **66.3%** |
| Licensed | **90.2%** | **50.5%** |

The redundancy is concentrated: 366,099 rows — 12% — are copies of just 2,901
skills, overwhelmingly canonical starter skills vendored into many projects.
Licence coverage halving matters independently: anyone needing to establish
reuse rights is served worse by the larger corpus.

**The largest corpus is deliberately not deployed.** The 1.44M index measured
*worse* than the 100k cut on short queries (MRR 0.313 against 0.441), requires
approximately five times the machine, and is 12x slower at p50.

The mechanism is duplicate crowding. Search over-fetches five times the
requested results and collapses duplicates afterwards, so ten results are drawn
from fifty candidates. That multiplier was calibrated when 87% of the corpus
was unique; at 66% unique, a query matching a widely-vendored skill fills most
of those fifty slots with copies of one file. Raising the multiplier does not
address it — no multiplier survives a cluster of 1,998 copies. Build-time
deduplication does.

**Adoption is counted by distinct owners, and remains unproven.** The
distinctiveness family originally penalised duplication as boilerplate.
Measured against the corpus this is inverted for the dominant case: the most-
copied skills sit in *different owners'* accounts at a ratio of 0.87–0.95 —
`skill-creator` has 1,998 copies across 1,820 distinct owners, which is
adoption evidence rather than noise. Raw copy count cannot distinguish that
from one account holding three copies, which is why it was the wrong variable
rather than the wrong sign; `clone-website`, at 0.33 owners per copy, is still
penalised as intra-account sprawl.

The change measured neutral, and the validation attempts are instructive: an
A/B at n=100 showed clear improvement and was flat at n=300, and the corpus it
ran against was 95.8% single-owner, so the signal could not fire at all. The
change is well-motivated, harmless, and unproven.

**Enrichment is incomplete.** `contributors`, `releases` and author `followers`
sit at 0% coverage; their weights are redistributed.

**No human relevance judgements.** Ranking is validated by property tests and a
label-free known-item benchmark. That benchmark measures whether a *specific*
skill is retrievable from its description — a genuine signal, but not the whole
of relevance, and it does not reward surfacing the canonical copy among
equivalents.

---

## 13. Verification & Empirical Benchmark Summary

| Benchmark Metric | Value |
|---|---|
| Ingestion Rate (Codeload) | 16,000 repos/hr |
| Mean Search Latency (p50) | 48 ms |
| 95th Percentile Latency (p95) | 118 ms |
| Total Indexed Skills | 3,360,000+ |
| Curated Production Cut | 100,006 skills |
| Unit & Integration Test Suite | 174 passing tests |

All algorithms, models, and retrieval mechanics are validated in the accompanying test suite under `tests/`.
