# A Python/Rust split — measured, then scoped

Written in response to "can we think about a Rust implementation of the whole
repo". The short answer: **a full rewrite would be mostly wasted effort, and the one
narrow port worth doing is worth 3–24x rather than the 100x I first estimated.**
I built it to find out, and section 3.2 is where my own projection was wrong.

Everything here was measured on this machine against the real corpus: 3,000
actual skill documents and the actual 287 taxonomy patterns, not synthetic
benchmarks.

---

## 1. Where the time actually goes

Before deciding what to rewrite, it is worth knowing what is slow. Measured
over 20,000 real skills, extrapolated to the 4.18M corpus:

| Stage | Throughput | Full corpus |
|---|---|---|
| `parse_skill` (YAML + markdown) | 452,585/sec | **0.2 min** |
| `score_skill` (the ranking model) | 276,766/sec | **0.3 min** |
| `score_repo` | 79,284/sec | **0.6 min** |
| **`safety.inspect`** | **808/sec** | **86 min** |
| **`taxonomy.classify`** | **851/sec** | **82 min** |

And search, profiled per query over the 100k production index:

```
23 ms/query, of which 0.174s of 0.189s cumulative is
sqlite3.Connection.execute  —  92% SQLite, ~8% Python
```

Two things fall out immediately, and both contradict the intuition that a
Python codebase is uniformly slow:

**Parsing and ranking are not bottlenecks.** They are already fast enough that
the entire four-million-skill corpus passes through them in **under 90
seconds combined**. Porting them to Rust would save perhaps a minute per
release build. That is not a reason to rewrite anything.

**Search is not Python-bound.** 92% of a query is spent inside SQLite. A Rust
rewrite of the search layer would optimise the remaining 8% — about 2 ms of a
23 ms query — while inheriting all the risk of reimplementing ranking fusion,
duplicate collapsing and faceting that 189 tests currently pin down.

The crawler is not in the table because it is not CPU-bound at all: it is
limited by `codeload.github.com`'s per-IP **byte** ceiling, established by
experiment (see `whitepaper.md` §9.2). A faster crawler in any language would
wait exactly as long.

---

## 2. The two stages that are slow, and why

`safety.inspect` and `taxonomy.classify` consume **168 minutes of the ~170
minute** total. Both do the same thing: run several hundred regular expressions
against each document.

Taxonomy alone holds 287 category patterns and 361 subcategory patterns, each
compiled separately and searched against four fields — up to **2,592 regex
searches per skill**.

The obvious optimisation is to combine them into one pattern. It does not work
in Python, and the reason matters:

| Approach (287 patterns, 3,000 real documents) | docs/sec | |
|---|---|---|
| Each pattern searched separately (current) | 186 | baseline |
| One alternation, match/no-match only | 12,343 | **66x faster** |
| One alternation with named groups (tells you *which* matched) | 18 | **10x slower** |

The middle row is the trap. Python can answer "does anything match" quickly,
but the pipeline needs to know *which* patterns matched — IDF weighting is
per-pattern, and per-field scoring needs to know which field a match came from.
Asking Python's `re` for that via named groups triggers catastrophic
backtracking and ends up **ten times slower than the naive loop**.

A prefilter does not rescue it either. For safety inspection, **99.4% of
documents match no safety pattern at all**, which looks like a perfect case for
a cheap gate — and measured 1.0x. No improvement whatsoever, because the
combined alternation costs as much to evaluate as the nine groups it replaced.

This is not a Python-is-slow result; it is a **backtracking-engine** result.
`re` cannot simulate N patterns in one linear pass. Rust's `regex` crate, being
a finite-automaton engine with no backtracking, can — that is precisely what
`RegexSet` is for.

---

## 3. What Rust actually buys — built, then measured

I did not stop at a benchmark. `rust/` now contains a working PyO3 module
(`skill_engine_rs`, ~120 lines) wired into `safety.assess_corpus` behind a
fallback, and a test asserts the two paths produce identical verdicts. The
numbers below are from that, not from a projection.

### 3.1 The matching itself

Same 287 taxonomy patterns, same 3,000 real skill bodies:

| Strategy | docs/sec | vs Python | 4.18M corpus |
|---|---|---|---|
| Python, patterns separately | 187 | 1.0x | 372 min |
| Rust `matches()`, per call across PyO3 | 1,002 | **5.4x** | 70 min |
| Rust `matches_batch()`, all 10 cores | 4,452 | **23.8x** | 15.6 min |

Results were **exactly identical** across 600 documents — same pattern indices,
every time. Batching matters: calling across the PyO3 boundary per document
costs most of the parallelism, so the API takes a list and returns a list.

### 3.2 A correction, and the most useful thing I learned

My first benchmark showed a `is_match` prefilter at **2,305x**, and I wrote that
safety inspection would therefore drop from 86 minutes to under one. **That was
wrong, and the reason is worth more than the number was.**

`is_match` short-circuits on the *first* match. When a document matches it
stops almost immediately; when it does not, it must scan the entire document to
prove absence. So a *low* hit rate is the expensive case, not the cheap one —
the opposite of the intuition that a filter is cheap when it rejects.

Measured on 10,000 real documents, 24 MB:

| Pattern set | hit rate | docs/sec | MB/sec |
|---|---|---|---|
| Taxonomy (99.4% of docs match) | 99.2% | 377,029 | **924** |
| Safety (98.9% of docs do not) | 1.3% | 4,127 | **10** |

A 92x difference in throughput from hit rate alone. My 2,305x figure was
measured against taxonomy patterns — where nearly everything matches — and then
misapplied to safety, where nearly nothing does. Exactly backwards.

### 3.3 End-to-end, in the real pipeline

30,000 real skills, both paths, on identical database copies:

```
python counts : {critical: 10, high: 6, low: 226, medium: 138, none: 29620}
rust   counts : {critical: 10, high: 6, low: 226, medium: 138, none: 29620}
verdicts differing: 0   (identical)

python    40.2s     747 skills/sec    4.18M -> 93.3 min
rust      12.9s   2,328 skills/sec    4.18M -> 29.9 min
speedup    3.1x
```

**3.1x, not 100x.** And the phase breakdown says why:

| Phase | seconds | share |
|---|---|---|
| Gate: Rust `is_match`, all cores | 5.98 | **55.5%** |
| Write verdicts back to SQLite | 4.30 | **39.9%** |
| Inspect the 1.07% admitted | 0.41 | 3.8% |
| Read rows, build strings, compile | 0.08 | 0.8% |

Two lessons. The gate is expensive *because* it rarely matches — it is proving
absence over 68 MB. And once matching is cheap, **40% of the remaining time is
SQLite writing 29,620 "clean" rows**, which no amount of Rust addresses.

### 3.4 A constraint worth knowing before committing

Rust's `regex` **rejects look-around entirely** — no look-ahead, no
look-behind. That is how it guarantees linear time, and it is not negotiable.

This project uses a negative lookahead in exactly one place, to exclude private
IP ranges from the drop-site rule (added to fix a false positive where every
skill mentioning `127.0.0.1:8080` was flagged).

The resolution is a design principle rather than a patch: **the gate may be
over-inclusive but never under-inclusive.** Stripping a look-around always
broadens a pattern, so the gate admits `127.0.0.1` and Python's real pattern
rejects it a moment later. A gate that can *miss* is worse than no gate,
because it looks like it works.

Any future port must check for this. Several other useful constructs —
backreferences among them — are likewise unavailable.

## 4. Recommended split

**Port to Rust — one PyO3 extension module. `rust/` holds it at ~120 lines:**

| Component | Why |
|---|---|
| Pattern matching for `safety.inspect` | **Done.** 3.1x end-to-end, verdicts identical. Limited by proving absence over 68 MB, and by SQLite writes |
| Pattern matching for `taxonomy.classify` | **Not done.** 23.8x measured on the matching; the integration is larger because scoring is per-field and per-pattern |
| Tarball scan (gzip + tar walk) | Not yet profiled, but it is per-repo CPU work and trivially parallel |

The Rust side should expose one narrow function — *given these patterns and
these documents, return which patterns matched each document* — and nothing
else. All scoring, weighting, thresholds and policy stay in Python, where
they are tested and where the false-positive calibration lives.

**Keep in Python:**

| Component | Why |
|---|---|
| The crawler | Bounded by an upstream byte ceiling; language is irrelevant |
| Parsing | 452k/sec already; the whole corpus in 12 seconds |
| Ranking and author scoring | Sub-minute for the full corpus |
| Search and serving | 92% SQLite; a rewrite optimises 2 ms of 23 ms |
| Taxonomy weights, safety rules, thresholds | Calibration, not computation — and the part that took four iterations of false-positive work to get right |
| CLI, release pipeline, MCP server | Glue; clarity matters more than speed |

The shape is **Python orchestrates, Rust matches patterns.** That is a small,
well-defined surface with a measured payoff, rather than a rewrite that would
re-earn 189 tests' worth of behaviour for a minute of build time.

---

## 5. The case against a full rewrite

Stated plainly, because it is the question that was asked:

* **The measured upside is ~170 minutes of batch time per release**, of which
  a scoped port already recovers ~150. The remaining 20 minutes would cost the
  entire codebase.
* **Search would not get faster.** It is SQLite-bound. The Rust ecosystem has
  no drop-in FTS5 equivalent with BM25 and external-content tables; `tantivy`
  is excellent but a different index with different semantics, and migrating
  would invalidate every measured retrieval number in `whitepaper.md`.
* **The crawler would not get faster.** It is throttled upstream.
* **The risk is concentrated exactly where the value is.** The ranking model,
  the taxonomy IDF weighting, and the safety calibration are the parts worth
  the most and the parts hardest to verify. Each embeds decisions that were
  wrong on the first attempt and corrected by measurement — a reimplementation
  would reintroduce those bugs silently, because they are not the kind of bug a
  type system catches.

Two things would genuinely favour a full rewrite, and neither currently
applies: a single-binary deployment requirement, or memory pressure on the
serving box. The 100k index serves in 190 MB on a $5 machine.

---

## 6. What would make this worth doing anyway

If the goal is partly to *work in Rust* rather than purely to go faster — a
perfectly good reason — the scoped port is still the right first step, because
it is the piece where Rust is genuinely the better tool rather than merely a
different one. `RegexSet` has no Python equivalent.

A reasonable sequence, with where it actually stands:

1. **`skill_engine_rs` as a PyO3 module** — *done.* `rust/src/lib.rs` exposes a
   `Matcher` with `matches`, `is_match`, `matches_batch` and `interesting`,
   built with `maturin`. Python keeps every scoring decision.
2. **Wired behind a fallback** — *done.* If the module is absent, `safety.py`
   runs the pure-Python path and produces the same verdicts. There is no
   feature flag to remember and no configuration that can leave it half-on.
3. **Equivalence asserted in the test suite** — *done.*
   `test_rust_backend_agrees_with_python` builds identical fixtures, assesses
   them through both paths, and fails on any differing verdict. Separately
   verified on 30,000 real skills: 0 differences.
4. **Measure again before expanding** — *done, and it stopped the expansion.*
   The prefilter did not deliver what the microbenchmark promised (§3.2), so
   the honest gain is 3.1x rather than 100x. Taxonomy is the remaining
   candidate at a measured 23.8x on matching, but its integration is a larger
   change and should be justified separately.

Two further things the measurements suggest, in order of value:

* **The SQLite write looked like 40% of the stage — and removing it changed
  nothing.** The phase breakdown showed 4.3s of 10.8s spent writing
  `risk_level = 'none'` to 29,620 rows, against 0.41s inspecting the 321 that
  mattered. Since the column already defaults to `'none'`, those writes are
  avoidable, so I removed them: clean rows are no longer written, and a single
  statement resets any stale verdict from an earlier run.

  Measured after: **3.1x → 3.2x.** Nothing, within noise — the Python baseline
  itself moved 40.2s → 44.2s between runs. The change is kept because it is
  correct and because 4.1M redundant updates should matter more at full corpus
  scale than 29,620 do at this one, but that is an argument, not a
  measurement, and it should be labelled as such. A phase breakdown attributes
  time; it does not prove that removing the phase returns it.
* **The gate is slow because it proves absence.** 10 MB/s over patterns that
  mostly do not match. An Aho-Corasick literal prefilter ahead of the regex set
  would likely help, since most safety patterns contain distinctive literals
  (`id_rsa`, `webhook.site`, `--no-preserve-root`). Untested.

The deliberate property of that sequence: at no point is there a version of
this repository that does not work.

---

## 7. Open question for tomorrow

The one thing I have not measured is **tarball extraction**, which is the
crawler's per-repo CPU cost (gzip inflate plus a tar walk, currently in
`skill_engine/tarball.py`). It never mattered while the crawl was
network-bound, and the crawl is now finished — so it only matters if you intend
to re-crawl periodically at volume. Worth profiling before deciding.
