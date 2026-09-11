# skill-engine

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](tests/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A high-performance search engine and indexing pipeline for AI agent skills (`SKILL.md` specifications) across GitHub, GitLab, and Hugging Face.

Agent skills are markdown documents paired with YAML frontmatter that define specialized instructions, workflows, and tool bindings for autonomous AI agents. Because skills are distributed across hundreds of thousands of independent repositories without a centralized registry, `skill-engine` automatically discovers, harvests, validates, ranks, and serves them over full-text search, REST APIs, and the Model Context Protocol (MCP).

---

## Key Highlights

- **Scale & Coverage**: Crawled **3.36M skills** (2.17M unique after content-hash deduplication) across **371,465 repositories** and **212,343 authors**.
- **Ultra-Low Latency**: The production cut (100,006 skills) serves search queries in **25–130 ms** using SQLite FTS5 and optimized in-memory page caching.
- **Zero-Quota Discovery & Ingestion**: High-throughput archive streaming (~16,000 repositories/hour) bypasses GitHub REST API rate limits, augmented with GH Archive event mining and GitLab/Hugging Face ingestion.
- **Corpus-Calibrated Ranking**: BM25 relevance fused with a multi-signal quality model (craft, repository authority, author track record, and recency) normalized via corpus percentiles.
- **Native Gemini & MCP Integration**: Ready-to-use MCP server and a first-class Google GenAI SDK (`gemini-2.5-flash`) agent for autonomous skill discovery.

---

## Quickstart

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/agulli/skill-engine.git
cd skill-engine

# Create virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. End-to-End Pipeline

```bash
# 1. Discover repositories containing agent skills
skill-engine mass-discover --target 25000

# 2. Bulk harvest repository contents without consuming API quota
skill-engine sweep --target 100000

# 3. Compute corpus percentiles and quality scores
skill-engine rank

# 4. Launch the web UI and search API on http://127.0.0.1:8000
skill-engine serve
```

### 3. Querying via MCP and Gemini Agent

Expose the index to autonomous agents or query directly with Gemini:

```bash
# Serve the index via Model Context Protocol (MCP)
python mcp_server.py --db dist/skills.db

# Run the Gemini agent (requires GEMINI_API_KEY)
export GEMINI_API_KEY="your-gemini-api-key"
python gemini_agent.py "How do I extract tabular data from PDF files?"
```

---

## Architecture Overview

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
│    Discovery    │ ──▶ │ Queue System │ ──▶ │  Harvester   │
│  Search / GHA   │     │ (SQLite-WAL) │     │ Codeload/REST│
└─────────────────┘     └──────────────┘     └──────────────┘
                                                    │
                                                    ▼
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
│  Search & Serve │ ◀── │ Quality Rank │ ◀── │ Parser/Audit │
│  FTS5 / UI / MCP│     │ Signal Fusion│     │ YAML + Body  │
└─────────────────┘     └──────────────┘     └──────────────┘
```

### 1. Discovery
Discovers candidate repositories using five parallel mechanisms:
- **Date-Bisected Repository Search**: Recursively divides GitHub `created:` ranges to bypass the 1,000-result search limit.
- **GH Archive Stream Mining**: Ingests public GitHub event streams (PushEvent, CreateEvent) for zero-API-cost candidate extraction.
- **Curated Ecosystem Mining**: Scrapes awesome-lists and hub catalogs.
- **Multi-Forge Connectors**: Discovers open repositories on GitLab and Hugging Face.
- **Author Portfolio Expansion**: Queries other repositories by known skill creators.

### 2. Ingestion & Harvest
- **Archive Ingestion (Default)**: Streams `.tar.gz` archives directly from `codeload.github.com`, fetching complete repository trees in one request with **0 GitHub REST API quota consumed**.
- **REST Tree Fallback**: Uses GitHub GraphQL/REST with `If-None-Match` (ETag) caching and Git blob SHA diffing for delta updates.

### 3. Validation & Parsing
Two-tier validation pipeline:
- **Hard Validation**: Ensures valid YAML frontmatter, non-empty body, and required `name` and `description` fields.
- **Soft Validation**: Flags specification deviations (e.g. descriptions >1,024 chars, non-standard naming slugs) and applies proportional ranking penalties rather than discarding valid work.

### 4. Ranking & Quality Scoring
Offline scoring engine computes composite quality priors from four decoupled families:
- **Repository Authority (32%)**: Stars, forks, subscriber counts, issue activity, and maintenance cadence normalized via empirical percentiles.
- **Skill Craft (33%)**: Frontmatter completeness, documentation structure, code examples, resource links, and tool definitions.
- **Author Track Record (16%)**: Median craft score across portfolio, originality ratio (filtering mass-cloned repos), and consistency.
- **Distinctiveness & Freshness (19%)**: Content-hash uniqueness and half-life recency decay.

---

## CLI Reference

```bash
skill-engine discover          # Run discovery across configured sources
skill-engine mass-discover     # High-throughput repository discovery
skill-engine sweep             # Bulk harvest via codeload archives
skill-engine crawl             # Targeted REST API crawler with delta updates
skill-engine rank              # Compute percentile scores across corpus
skill-engine explain <id|repo> # Display detailed score breakdown for a skill
skill-engine categorize        # Assign taxonomy categories based on IDF weights
skill-engine search <query>    # Search the indexed skills from CLI
skill-engine serve             # Run local Web UI and JSON API server
```

---

## Agent Integration

### Model Context Protocol (MCP)

`mcp_server.py` provides read-only MCP tools for any MCP-compliant agent client (e.g., Google Antigravity, Claude Code, Cursor, OpenClaw):

| Tool | Purpose |
|---|---|
| `search_skills` | Keyword & semantic search over skill names, descriptions, and bodies |
| `get_skill` | Fetch full markdown content, frontmatter metadata, and resource paths |
| `browse_category` | Explore skills organized by taxonomy categories |
| `corpus_stats` | Inspect index size, repository counts, and category distributions |

### Autonomous Gemini Agent

`gemini_agent.py` demonstrates programmatic tool orchestration using the Google GenAI SDK (`google-genai`) and Gemini 2.5 Flash:

```python
from google import genai
from google.genai import types

client = genai.Client()
# Tool definitions are loaded directly from MCP server schema
```

---

## Production Deployment

The search engine ships as a self-contained SQLite artifact (`dist/skills.db`) and a lightweight Python web server:

```bash
# Build production artifact
python release.py data/scale.db dist/skills.db

# Deploy to Fly.io or Cloud Run
./deploy.sh app      # Deploy application container
./deploy.sh data     # Stream compacted database artifact
```

---

## Choosing Which Index to Serve

Larger is not better here, and the largest corpus is deliberately not the
deployed one. Measured on a label-free known-item benchmark, moving from the
100k cut to a 1.44M index **improved** precision at rank 1 and **degraded**
recall at rank 10 — sharply on short queries (MRR 0.441 → 0.313).

The mechanism is duplicate crowding. Search over-fetches five times the
requested results and collapses duplicates afterwards, so ten results are drawn
from fifty candidates. That multiplier was calibrated when 87% of the corpus was
unique; at 66% unique a query matching a widely-vendored skill fills most of
those fifty slots with copies of one file.

Raising the multiplier does not fix it — no multiplier survives a cluster of
1,998 copies. Build-time deduplication does. The larger index also requires
roughly five times the machine and is 12x slower at p50.

Until build-time deduplication and embeddings land, the 100k cut is the better
product. `release.py` can build a cut at any size.

## Operational Configuration

All settings are environment variables; defaults suit a laptop.

| Variable | Default | Purpose |
|---|---|---|
| `SKILL_ENGINE_DB` | `data/skills.db` | Database path |
| `GITHUB_TOKEN` / `GITHUB_TOKENS` | — | Comma-separated PATs; raises the API ceiling from 60 to 5,000 req/hr each |
| `SKILL_ENGINE_SWEEP_BATCH` | `400` | Candidates selected per sweep round — **must exceed concurrency**, see whitepaper §9.1 |
| `SKILL_ENGINE_SWEEP_CONCURRENCY` | `5` | Simultaneous archive downloads |
| `SKILL_ENGINE_MIN_DELAY` | `0.15` | Floor between request starts; the adaptive backoff raises it on refusal |
| `SKILL_ENGINE_MAX_DELAY` | `2.0` | Ceiling the backoff will not exceed |
| `SKILL_ENGINE_RECOVER_STEP` | `0.001` | Delay decrement per success; larger values reintroduce oscillation |
| `SKILL_ENGINE_FORBIDDEN_LIMIT` | `5` | HTTP 403 responses within ten minutes before the breaker halts the sweep |
| `SKILL_ENGINE_MAX_MB` | `10` | Archive size cap; raising to 50 admits large repositories (~17 skills each) |
| `SKILL_ENGINE_RERANK_EVERY` | `2000` | Rows between mid-crawl reranks. Set very high to disable it entirely — `release.py` ranks once at the end, so mid-crawl ranking is wasted work (whitepaper §9.3) |
| `SKILL_ENGINE_CRAWL_BODY_CAP` | `4000` | Body characters retained on write; controls crawl database growth |
| `SKILL_ENGINE_PUBLIC` | — | Set for public deployments: read-only, rate limiting, proxy-header trust |
| `SKILL_ENGINE_RATE` | `3` | Token-bucket refill per second per client |
| `SKILL_ENGINE_CACHE_MB` | `256` (`192` when serving) | SQLite page cache — the dominant factor in query latency. `serve` treats it as a total budget divided across worker threads |

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q                   # 174 tests
skill-engine --db dist/skills.db evaluate    # retrieval quality benchmark
```

`evaluate` reports known-item recall and MRR, category coherence, robustness to
rephrasing, and latency percentiles. Run it before and after any ranking change:
several apparently-clear improvements have measured flat once sample size was
raised from 100 to 300.

Every stage checkpoints in SQLite. The queue *is* the progress record, so any
command can be interrupted and rerun to resume where it stopped.

## Documentation

- **[Technical Architecture & Design (whitepaper.md)](whitepaper.md)**: In-depth engineering specifications, mathematical formulations, ranking proofs, and retrieval benchmarks.
- **[Security & Abuse Mitigation (ABUSE.md)](ABUSE.md)**: Edge defense architecture, rate limiting policies, and scraping protections.

---

## License

This project is licensed under the MIT License.
