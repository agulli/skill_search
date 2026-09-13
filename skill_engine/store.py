"""SQLite persistence layer: manages repositories, skills, FTS5 index, ETags, and prioritized queues."""


from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("skill_engine.store")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repos (
    full_name       TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    owner_type      TEXT,                -- User | Organization
    default_branch  TEXT,
    description     TEXT,
    homepage        TEXT,
    language        TEXT,

    -- Social proof. Note `watchers` in the REST API is an alias of stars;
    -- `subscribers` is the real watch count and only appears on the full repo
    -- endpoint, so it is often NULL after search-only discovery.
    stars           INTEGER DEFAULT 0,
    forks           INTEGER DEFAULT 0,
    subscribers     INTEGER,
    open_issues     INTEGER DEFAULT 0,
    size_kb         INTEGER DEFAULT 0,

    license         TEXT,
    topics          TEXT,                -- JSON array
    created_at      TEXT,
    updated_at      TEXT,
    pushed_at       TEXT,

    is_fork         INTEGER DEFAULT 0,
    archived        INTEGER DEFAULT 0,
    disabled        INTEGER DEFAULT 0,
    is_template     INTEGER DEFAULT 0,
    has_issues      INTEGER DEFAULT 0,
    has_wiki        INTEGER DEFAULT 0,
    has_pages       INTEGER DEFAULT 0,
    has_discussions INTEGER DEFAULT 0,

    -- Enrichment that costs extra requests; NULL until `enrich` runs.
    contributors    INTEGER,
    releases        INTEGER,
    latest_release  TEXT,

    tree_sha        TEXT,                -- last tree we harvested
    skill_count     INTEGER DEFAULT 0,
    discovered_via  TEXT,
    repo_score      REAL DEFAULT 0,
    score_detail    TEXT,                -- JSON breakdown, for explainability
    first_seen      REAL,
    last_crawled    REAL,
    last_meta_at    REAL,
    last_error      TEXT
);

-- Quantile boundaries per metric, so ranking can normalise heavy-tailed counts
-- by percentile rather than by a hand-tuned constant.
CREATE TABLE IF NOT EXISTS corpus_stats (
    metric       TEXT PRIMARY KEY,
    quantiles    TEXT NOT NULL,          -- JSON array of 101 boundaries
    n            INTEGER,
    computed_at  REAL
);

-- Column names here are load-bearing: the FTS5 external-content index below
-- references them by name.
CREATE TABLE IF NOT EXISTS skills (
    id              INTEGER PRIMARY KEY,
    repo            TEXT NOT NULL REFERENCES repos(full_name) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    heading         TEXT DEFAULT '',
    version         TEXT,
    license         TEXT,
    allowed_tools   TEXT,                -- JSON array
    metadata        TEXT,                -- JSON object
    resources       TEXT,                -- JSON array
    source_kind     TEXT,
    blob_sha        TEXT,                -- git blob SHA: our change detector
    content_hash    TEXT,                -- sha256 of file text: our dedupe key
    body_len        INTEGER DEFAULT 0,
    score           REAL DEFAULT 0,
    valid           INTEGER DEFAULT 0,
    invalid_reason  TEXT,                -- hard problems: why it is not a skill
    warnings        TEXT,                -- soft problems: spec deviations
    first_seen      REAL,
    last_seen       REAL,
    UNIQUE(repo, path)
);

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description, body, repo, path,
    content='skills',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid, name, description, body, repo, path)
    VALUES (new.id, new.name, new.description, new.body, new.repo, new.path);
END;
CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, body, repo, path)
    VALUES ('delete', old.id, old.name, old.description, old.body, old.repo, old.path);
END;
CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, body, repo, path)
    VALUES ('delete', old.id, old.name, old.description, old.body, old.repo, old.path);
    INSERT INTO skills_fts(rowid, name, description, body, repo, path)
    VALUES (new.id, new.name, new.description, new.body, new.repo, new.path);
END;

CREATE TABLE IF NOT EXISTS etags (
    key         TEXT PRIMARY KEY,
    etag        TEXT NOT NULL,
    fetched_at  REAL
);

CREATE TABLE IF NOT EXISTS queue (
    full_name    TEXT PRIMARY KEY,
    priority     INTEGER DEFAULT 100,
    reason       TEXT,
    enqueued_at  REAL,
    attempts     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vectors (
    skill_id  INTEGER PRIMARY KEY REFERENCES skills(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vec       BLOB NOT NULL
);

-- Small key/value store for things computed once at release time and read on
-- every request — the browsable catalogue above all. Recomputing it per page
-- load meant a GROUP BY over every skill for an answer that only changes when
-- the index is rebuilt.
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    computed_at REAL
);

CREATE TABLE IF NOT EXISTS crawl_log (
    id       INTEGER PRIMARY KEY,
    ts       REAL,
    kind     TEXT,
    detail   TEXT
);
"""


# Every index lives here rather than in the schema script, and each is applied
# individually after the migration. Two reasons: an index may reference a column
# that only `_migrate` adds, and — more importantly — an index is an
# optimisation, not a correctness requirement. One that cannot be built against
# an older database should cost some query speed, never the ability to open the
# file at all.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS repos_last_crawled ON repos(last_crawled)",
    "CREATE INDEX IF NOT EXISTS repos_skill_count ON repos(skill_count)",
    "CREATE INDEX IF NOT EXISTS repos_stars ON repos(stars DESC)",
    "CREATE INDEX IF NOT EXISTS repos_score ON repos(repo_score DESC)",
    "CREATE INDEX IF NOT EXISTS skills_hash ON skills(content_hash)",
    "CREATE INDEX IF NOT EXISTS skills_valid_score ON skills(valid, score DESC)",
    "CREATE INDEX IF NOT EXISTS skills_repo ON skills(repo)",
    "CREATE INDEX IF NOT EXISTS skills_category ON skills(category, subcategory)",
    "CREATE INDEX IF NOT EXISTS queue_priority ON queue(priority DESC, enqueued_at)",
)


# Characters of skill body retained by the crawler. 0 disables the cap.
# Release builds trim to 2,000; keeping a little more here leaves room to
# change that decision without recrawling.
CRAWL_BODY_CAP = int(os.getenv("SKILL_ENGINE_CRAWL_BODY_CAP", "4000"))

# A skill the rules flag keeps its whole body regardless of the cap, and is
# assessed *before* any truncation happens.
#
# The ordering is the point. Truncating first and assessing later means the
# safety rules examine a different document from the one that was published,
# and the discarded remainder is gone for good — a payload at character 8,000
# would be permanently invisible, not merely unread. `release.py` has the same
# hazard and solves it by inheriting decisions made upstream; this is upstream.
#
# Keeping the flagged bodies whole is what makes the cap affordable. The rules
# gate about 1% of the corpus, so at four million skills this is roughly 40,000
# full bodies — under a gigabyte — against the 20 GB the capped remainder
# costs. The model layer and `review_blocks.py show` both need the real text:
# an excerpt centred on a finding cannot be reconstructed from a head-truncated
# body, and a reviewer cannot judge a block from the first 4,000 characters.
ASSESS_ON_CRAWL = os.getenv("SKILL_ENGINE_ASSESS_ON_CRAWL", "1") != "0"


class Store:
    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        """Open the corpus. `read_only` is for serving.

        A public server never writes: the corpus is rebuilt offline and shipped
        as a file. Opening with `mode=ro` makes that structural rather than
        conventional — a bug in a request handler cannot corrupt the index, and
        the volume itself can be mounted read-only. It also skips the schema
        script and migration check, which a read-only connection could not run
        anyway.
        """
        self.path = Path(path)
        self.read_only = read_only

        # Batch writers hold long transactions — a full rerank rewrites every
        # skill row in one statement — so a 30s busy timeout is not enough when
        # discovery and harvesting run concurrently. Measured: 22 discovery
        # batches were lost to lock timeouts before this was raised.
        busy = float(os.getenv("SKILL_ENGINE_BUSY_TIMEOUT", "180"))

        if read_only:
            self.db = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=busy
            )
            self.db.row_factory = sqlite3.Row
            self._tune()
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=busy)
        self.db.row_factory = sqlite3.Row
        self._tune()
        self.db.executescript(SCHEMA)
        self._migrate()
        self._build_indexes()
        self.db.commit()

    def _tune(self) -> None:
        """Size the page cache to the workload rather than to SQLite's default.

        This is the single biggest lever on query latency here, and it is pure
        configuration. SQLite defaults to a ~2MB page cache; the FTS index alone
        is 847MB at 100k skills, so nearly every query was reading from disk.
        Measured on a facet query over a 45k-document match: **200ms with the
        default cache, 8ms with these settings.**

        `mmap_size` lets SQLite read pages straight from the page cache of the
        OS instead of copying them, which compounds the same effect. Both are
        advisory — SQLite silently uses less if the platform cannot provide it,
        so this is safe on small machines.
        """
        import os

        cache_mb = int(os.getenv("SKILL_ENGINE_CACHE_MB", "256"))
        mmap_mb = int(os.getenv("SKILL_ENGINE_MMAP_MB", "2048"))
        self.db.execute(f"PRAGMA cache_size = -{cache_mb * 1024}")
        self.db.execute(f"PRAGMA mmap_size = {mmap_mb * 1024 * 1024}")
        self.db.execute("PRAGMA temp_store = MEMORY")
        # Cap the write-ahead log. Without this the WAL is reused but never
        # shrunk, and a long-lived reader blocks checkpointing entirely — an
        # 8-hour discovery process and a concurrent sweep grew it to 33.7 GB,
        # larger than the 27 GB database, at which point every write began
        # failing with OperationalError. The limit is what a checkpoint
        # truncates back to.
        limit_mb = int(os.getenv("SKILL_ENGINE_WAL_LIMIT_MB", "512"))
        self.db.execute(f"PRAGMA journal_size_limit = {limit_mb * 1024 * 1024}")

    def optimize(self) -> dict:
        """Compact the FTS index and refresh planner statistics.

        Trigger-driven inserts leave FTS5 with one segment per batch; after
        100k skills that was 211,656 segment rows, and merging them to 94,077
        cut query time by roughly a sixth. Worth running after any large ingest,
        which is why `rank` calls it.
        """
        before = self.db.execute(
            "SELECT COUNT(*) c FROM skills_fts_data").fetchone()["c"]
        self.db.execute("INSERT INTO skills_fts(skills_fts) VALUES('optimize')")
        self.db.commit()
        self.db.execute("ANALYZE")
        self.db.commit()
        after = self.db.execute(
            "SELECT COUNT(*) c FROM skills_fts_data").fetchone()["c"]
        return {"segments_before": before, "segments_after": after}

    def _build_indexes(self) -> None:
        """Apply each index independently, tolerating ones that cannot build."""
        for statement in INDEXES:
            try:
                self.db.execute(statement)
            except sqlite3.OperationalError as exc:
                # An older file may lack the column entirely. Losing an index
                # costs query speed; refusing to open costs everything.
                log.debug("index skipped (%s): %s", exc, statement)

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        `CREATE TABLE IF NOT EXISTS` silently skips an existing table, so new
        columns need adding explicitly or an older crawl database breaks on the
        next run. Additive-only: nothing here can lose indexed data.
        """
        additions = {
            "skills": {
                "warnings": "TEXT",
                "score_detail": "TEXT",
                "dup_count": "INTEGER DEFAULT 0",
                "category": "TEXT",
                "subcategory": "TEXT",
                "categories": "TEXT",
                # What following this skill would instruct an agent to do.
                # Defaults to 'none' so an un-assessed corpus behaves exactly
                # as before rather than treating silence as suspicion.
                "risk_level": "TEXT DEFAULT 'none'",
                "risk_detail": "TEXT",
                # The fused decision. Migrated here rather than added on demand
                # by whichever tool needs them first, so that `upsert_skill`
                # can retire a decision the moment an edit invalidates it —
                # which it can only do inside the ON CONFLICT clause, where the
                # old and new content hashes are both in scope.
                "risk_confidence": "REAL",
                "risk_action": "TEXT",
                "risk_analysis": "TEXT",
            },
            "repos": {
                "discovered_via": "TEXT", "owner_type": "TEXT", "homepage": "TEXT",
                "language": "TEXT", "subscribers": "INTEGER", "open_issues": "INTEGER",
                "size_kb": "INTEGER", "created_at": "TEXT", "updated_at": "TEXT",
                "disabled": "INTEGER DEFAULT 0", "is_template": "INTEGER DEFAULT 0",
                "has_issues": "INTEGER DEFAULT 0", "has_wiki": "INTEGER DEFAULT 0",
                "has_pages": "INTEGER DEFAULT 0", "has_discussions": "INTEGER DEFAULT 0",
                "contributors": "INTEGER", "releases": "INTEGER",
                "latest_release": "TEXT", "repo_score": "REAL DEFAULT 0",
                "score_detail": "TEXT", "last_meta_at": "REAL",
                # Which forge a repository came from. Defaults to GitHub so
                # every existing row stays correct without a backfill; only
                # non-GitHub sources set it. Kept on `repos` rather than
                # `skills` because a repository lives on exactly one host.
                "host": "TEXT DEFAULT 'github.com'",
            },
        }
        for table, columns in additions.items():
            existing = {
                r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")
            }
            for column, decl in columns.items():
                if column not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.db.commit()
        self.close()

    # ---------------------------------------------------------------- etags

    def get_etag(self, key: str) -> str | None:
        row = self.db.execute("SELECT etag FROM etags WHERE key = ?", (key,)).fetchone()
        return row["etag"] if row else None

    def put_etag(self, key: str, etag: str) -> None:
        self.db.execute(
            "INSERT INTO etags(key, etag, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET etag=excluded.etag, fetched_at=excluded.fetched_at",
            (key, etag, time.time()),
        )

    # ---------------------------------------------------------------- queue

    def enqueue(self, full_name: str, reason: str, priority: int = 100) -> bool:
        """Add a repo to the crawl queue. Returns True if it was new."""
        if "/" not in full_name:
            return False
        cur = self.db.execute(
            "INSERT INTO queue(full_name, priority, reason, enqueued_at) VALUES(?,?,?,?) "
            "ON CONFLICT(full_name) DO UPDATE SET priority = MAX(queue.priority, excluded.priority)",
            (full_name, priority, reason, time.time()),
        )
        return cur.rowcount > 0 and not self.db.execute(
            "SELECT 1 FROM repos WHERE full_name = ?", (full_name,)
        ).fetchone()

    def ensure_repo_stub(self, full_name: str, discovered_via: str = "") -> bool:
        """Create a minimal `repos` row for a candidate we have no metadata for.

        Sources that yield only a name — GH Archive mining, awesome-list links —
        would otherwise queue a repository the harvester can never see: the
        sweep joins `queue` to `repos`, so an entry with no row on the other
        side is silently unharvestable. The stub carries no metadata; the
        tarball path needs none, falling back to HEAD for the branch, and a
        later metadata fetch fills the rest in via COALESCE.
        """
        if "/" not in full_name:
            return False
        owner, _, name = full_name.partition("/")
        cur = self.db.execute(
            "INSERT OR IGNORE INTO repos(full_name, owner, name, discovered_via, "
            "first_seen) VALUES(?,?,?,?,?)",
            (full_name, owner, name, discovered_via, time.time()),
        )
        return cur.rowcount > 0

    def enqueue_many(self, names: Iterable[tuple[str, str, int]]) -> int:
        added = 0
        for full_name, reason, priority in names:
            if self.enqueue(full_name, reason, priority):
                added += 1
        self.db.commit()
        return added

    def take(self, limit: int, *, refresh_after_hours: int = 72,
             strategy: str = "score") -> list[sqlite3.Row]:
        """Pop the repos most worth crawling next.

        Ordering is the difference between a crawl that pays for itself and one
        that does not. Every repository costs the same single tree request, but
        what comes back varies by more than an order of magnitude: crawling in
        `repo_score` order returned **60.8 skills per repo** against **3.72 for
        a uniform random sample** — a 16x difference in yield per request. When
        core quota is the binding constraint, that ratio is the whole game.

        `score` (the default) spends quota on the best-ranked repositories
        first, falling back to discovery priority for anything not yet ranked.
        `fifo` preserves the old discovery-order behaviour for a full sweep
        where completeness matters more than early yield.
        """
        cutoff = time.time() - refresh_after_hours * 3600
        order = (
            "COALESCE(r.repo_score, 0) DESC, q.priority DESC, q.enqueued_at"
            if strategy == "score"
            else "q.priority DESC, q.enqueued_at"
        )
        rows = self.db.execute(
            f"""
            SELECT q.full_name, q.reason, q.priority, q.attempts,
                   COALESCE(r.repo_score, 0) AS repo_score
            FROM queue q
            LEFT JOIN repos r ON r.full_name = q.full_name
            WHERE q.attempts < 4
              AND (r.last_crawled IS NULL OR r.last_crawled < ?)
            ORDER BY {order}
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        if rows:
            self.db.executemany(
                "UPDATE queue SET attempts = attempts + 1 WHERE full_name = ?",
                [(r["full_name"],) for r in rows],
            )
            self.db.commit()
        return rows

    def dequeue(self, full_name: str) -> None:
        self.db.execute("DELETE FROM queue WHERE full_name = ?", (full_name,))

    def queue_depth(self) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) c FROM queue WHERE attempts < 4"
        ).fetchone()
        return row["c"]

    # ---------------------------------------------------------------- repos

    def get_repo(self, full_name: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM repos WHERE full_name = ?", (full_name,)
        ).fetchone()

    # Columns that a metadata refresh overwrites. Crawl state (tree_sha,
    # skill_count, scores) is deliberately excluded so that re-reading metadata
    # never clobbers harvest results.
    META_COLUMNS = (
        "owner_type", "default_branch", "description", "homepage", "language",
        "stars", "forks", "subscribers", "open_issues", "size_kb", "license",
        "topics", "created_at", "updated_at", "pushed_at", "is_fork", "archived",
        "disabled", "is_template", "has_issues", "has_wiki", "has_pages",
        "has_discussions", "contributors", "releases", "latest_release",
    )

    def upsert_repo(self, meta: dict, *, touch_crawled: bool = True) -> None:
        """Insert or refresh a repository's metadata.

        `meta` uses the normalised keys produced by `metadata.from_api_repo`.
        Missing keys are left untouched on an existing row rather than being
        written as NULL — search results and the full repo endpoint expose
        different subsets, and a cheap refresh must not erase richer data an
        expensive one already fetched.
        """
        owner, _, name = meta["full_name"].partition("/")
        row = {
            "full_name": meta["full_name"],
            "owner": owner,
            "name": name,
            "topics": json.dumps(meta["topics"]) if "topics" in meta else None,
            "discovered_via": meta.get("discovered_via"),
            "now": time.time(),
            "last_crawled": meta.get("last_crawled"),
        }
        for col in self.META_COLUMNS:
            if col == "topics":
                continue
            value = meta.get(col)
            if isinstance(value, bool):
                value = int(value)
            row[col] = value

        cols = ", ".join(self.META_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in self.META_COLUMNS)
        # COALESCE(excluded, existing) makes every metadata write additive.
        updates = ", ".join(
            f"{c} = COALESCE(excluded.{c}, repos.{c})" for c in self.META_COLUMNS
        )
        self.db.execute(
            f"""
            INSERT INTO repos(full_name, owner, name, {cols}, discovered_via,
                              first_seen, last_meta_at, last_crawled)
            VALUES(:full_name, :owner, :name, {placeholders}, :discovered_via,
                   :now, :now, :last_crawled)
            ON CONFLICT(full_name) DO UPDATE SET
                {updates},
                discovered_via = COALESCE(repos.discovered_via, excluded.discovered_via),
                last_meta_at   = excluded.last_meta_at,
                last_crawled   = COALESCE(excluded.last_crawled, repos.last_crawled)
            """,
            {**row, "last_crawled": row["last_crawled"] or (time.time() if touch_crawled else None)},
        )

    def mark_repo(self, full_name: str, *, tree_sha: str | None = None,
                  skill_count: int | None = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE repos SET last_crawled = ?, "
            "tree_sha = COALESCE(?, tree_sha), "
            "skill_count = COALESCE(?, skill_count), "
            "last_error = ? WHERE full_name = ?",
            (time.time(), tree_sha, skill_count, error, full_name),
        )

    def known_blob_shas(self, repo: str) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT path, blob_sha FROM skills WHERE repo = ?", (repo,)
        ).fetchall()
        return {r["path"]: r["blob_sha"] for r in rows}

    # --------------------------------------------------------------- skills

    def _assess_full_body(self, rec: dict) -> Any:
        """Run the safety rules over the untruncated body.

        Imported lazily: the store is the lowest layer here and should not
        require the rule engine to be importable in order to open a database.
        A failure to assess never blocks a write — the skill is stored
        unassessed and the next batch pass will pick it up, which is a better
        outcome than losing the row.
        """
        try:
            from .safety import CRITICAL, HIGH, MEDIUM, NONE, inspect
        except Exception:            # pragma: no cover - safety always imports
            return None

        # Run unconditionally. A pre-filter was the obvious optimisation and it
        # does not work here, measured three ways on 3,000 real skills with a
        # mean body of 8.3 KB:
        #
        #   full inspect                     6.91 ms
        #   one combined 42-pattern regex    7.04 ms
        #   Rust gate, whole batch           1.31 ms
        #   Rust gate, one row at a time     7.84 ms
        #
        # The cost *is* the matching — Python cannot scan 8 KB against these
        # patterns faster than the rules already do, and the Rust gate's FFI
        # overhead exceeds its own saving when handed a single row. The batch
        # figure is the one worth having, and it is unavailable here because
        # truncation is a per-row decision.
        #
        # It does not need to be faster. At 7 ms this thread sustains ~140
        # skills/sec, and the crawler produces about 9 — fifteen times the
        # headroom, against a crawl that is network-bound for 175 hours at four
        # million skills. Roughly 8 hours of CPU spread across that, to avoid
        # permanently discarding the text the gate is meant to read.
        try:
            tools = json.loads(rec.get("allowed_tools") or "[]")
        except Exception:
            tools = []
        try:
            v = inspect(rec.get("name") or "", rec.get("description") or "",
                        rec.get("body") or "", tools, rec.get("path") or "",
                        rec.get("metadata") or "")
        except Exception as exc:
            log.warning("assessment failed for %s/%s: %s",
                        rec.get("repo"), rec.get("path"), exc)
            return None
        v.gated = v.level in (CRITICAL, HIGH, MEDIUM)
        v.record_level = v.level
        v.record_detail = v.as_json() if v.level != NONE else None
        return v

    def upsert_skill(self, rec: dict) -> None:
        # Store at most CRAWL_BODY_CAP characters of body. At 2.47M skills the
        # crawl database reached 70.8GB — 28KB per skill, nearly all of it body
        # text — which made reaching 5M possible but building a release from it
        # impossible: that needs a snapshot plus a compacted copy, about 258GB.
        #
        # Nothing is lost that the shipped index keeps *for retrieval*:
        # release.py already truncates to 2,000 characters, and doing so
        # measured better on every retrieval metric, because truncation removes
        # spurious matches deep in long documents. `content_hash` and
        # `body_len` arrive already computed from the full text, so
        # deduplication and the recorded true length are unaffected.
        #
        # Safety is the exception, and the reason the assessment below runs
        # first: retrieval can afford to forget the tail of a document, and a
        # gate cannot.
        verdict = None
        if rec.get("body") and ASSESS_ON_CRAWL:
            try:
                verdict = self._assess_full_body(rec)
            except Exception as exc:
                # Storing the skill matters more than assessing it on this
                # pass. An unassessed row is picked up by the next batch, and
                # `release.py` refuses to ship gated skills that were never
                # modelled — whereas a lost row is lost.
                log.warning("crawl-time assessment failed for %s/%s: %s",
                            rec.get("repo"), rec.get("path"), exc)
        if (CRAWL_BODY_CAP and rec.get("body")
                and len(rec["body"]) > CRAWL_BODY_CAP
                and not (verdict is not None and verdict.gated)):
            rec = {**rec, "body": rec["body"][:CRAWL_BODY_CAP]}
        self.db.execute(
            """
            INSERT INTO skills(repo, path, name, description, body, heading, version,
                license, allowed_tools, metadata, resources, source_kind, blob_sha,
                content_hash, body_len, score, valid, invalid_reason, warnings,
                first_seen, last_seen)
            VALUES(:repo,:path,:name,:description,:body,:heading,:version,:license,
                   :allowed_tools,:metadata,:resources,:source_kind,:blob_sha,
                   :content_hash,:body_len,:score,:valid,:invalid_reason,:warnings,
                   :now,:now)
            ON CONFLICT(repo, path) DO UPDATE SET
                name=excluded.name, description=excluded.description, body=excluded.body,
                heading=excluded.heading, version=excluded.version, license=excluded.license,
                allowed_tools=excluded.allowed_tools, metadata=excluded.metadata,
                resources=excluded.resources, source_kind=excluded.source_kind,
                blob_sha=excluded.blob_sha, content_hash=excluded.content_hash,
                body_len=excluded.body_len, score=excluded.score, valid=excluded.valid,
                invalid_reason=excluded.invalid_reason, warnings=excluded.warnings,
                last_seen=excluded.last_seen,
                -- A changed body is a different document, and the decision
                -- recorded for the old one says nothing about it. Without this,
                -- a skill that passed review and was later edited to add a
                -- payload would keep its `allow`. Cleared here because this is
                -- the only place the previous content hash is still in scope.
                risk_confidence = CASE WHEN skills.content_hash = excluded.content_hash
                    THEN skills.risk_confidence ELSE NULL END,
                risk_action = CASE WHEN skills.content_hash = excluded.content_hash
                    THEN skills.risk_action ELSE NULL END,
                risk_analysis = CASE WHEN skills.content_hash = excluded.content_hash
                    THEN skills.risk_analysis ELSE NULL END
            """,
            {"warnings": "", **rec, "now": time.time()},
        )
        if verdict is not None:
            self._record_verdict(rec, verdict)

    def _record_verdict(self, rec: dict, verdict: Any) -> None:
        """Store the rule verdict computed from the untruncated body.

        The fused decision is retired by the upsert's own ON CONFLICT clause
        rather than here, because that is the only place the *previous* content
        hash is still in scope. `overrides` are keyed on content hash for the
        same reason and need no such handling: a human decision about text
        still applies to that text, and stops applying to a replacement nobody
        reviewed.
        """
        try:
            self.db.execute(
                "UPDATE skills SET risk_level = ?, risk_detail = ? "
                "WHERE repo = ? AND path = ?",
                (verdict.record_level, verdict.record_detail,
                 rec.get("repo"), rec.get("path")))
        except sqlite3.OperationalError:      # pragma: no cover
            pass

    def touch_skill(self, repo: str, path: str) -> None:
        """Record that an unchanged skill is still present, without a refetch."""
        self.db.execute(
            "UPDATE skills SET last_seen = ? WHERE repo = ? AND path = ?",
            (time.time(), repo, path),
        )

    def delete_missing_skills(self, repo: str, keep_paths: set[str]) -> int:
        rows = self.db.execute(
            "SELECT path FROM skills WHERE repo = ?", (repo,)
        ).fetchall()
        gone = [r["path"] for r in rows if r["path"] not in keep_paths]
        for path in gone:
            self.db.execute(
                "DELETE FROM skills WHERE repo = ? AND path = ?", (repo, path)
            )
        return len(gone)

    def duplicate_counts(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT content_hash, COUNT(*) c FROM skills "
            "WHERE content_hash != '' GROUP BY content_hash HAVING c > 1"
        ).fetchall()
        return {r["content_hash"]: r["c"] for r in rows}

    def rescore_duplicates(self) -> int:
        """Demote verbatim copies once we can see the whole corpus."""
        dupes = self.duplicate_counts()
        if not dupes:
            return 0
        import math

        for chash, count in dupes.items():
            penalty = min(15.0, 3.0 * math.log2(count + 1))
            self.db.execute(
                "UPDATE skills SET score = MAX(0, score - ?) WHERE content_hash = ?",
                (penalty, chash),
            )
        self.db.commit()
        return len(dupes)

    def stats(self) -> dict[str, Any]:
        q = self.db.execute
        return {
            "repos": q("SELECT COUNT(*) c FROM repos").fetchone()["c"],
            "repos_with_skills": q(
                "SELECT COUNT(*) c FROM repos WHERE skill_count > 0"
            ).fetchone()["c"],
            "skills": q("SELECT COUNT(*) c FROM skills").fetchone()["c"],
            "valid_skills": q("SELECT COUNT(*) c FROM skills WHERE valid=1").fetchone()["c"],
            "unique_content": q(
                "SELECT COUNT(DISTINCT content_hash) c FROM skills"
            ).fetchone()["c"],
            "embedded": q("SELECT COUNT(*) c FROM vectors").fetchone()["c"],
            "queued": self.queue_depth(),
            "etags_cached": q("SELECT COUNT(*) c FROM etags").fetchone()["c"],
        }

    def put_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value, computed_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "computed_at=excluded.computed_at",
            (key, value, time.time()),
        )
        self.db.commit()

    def get_meta(self, key: str) -> str | None:
        try:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None          # older database without the table
        return row["value"] if row else None

    def log(self, kind: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO crawl_log(ts, kind, detail) VALUES(?,?,?)",
            (time.time(), kind, detail),
        )

    def commit(self) -> None:
        self.db.commit()
