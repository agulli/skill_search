#!/usr/bin/env python
"""Bulk-load skills from BigQuery's public GitHub dataset.

The crawler is bounded by codeload's per-IP limit — measured at roughly 8,000
repositories an hour, decaying to a few thousand once GitHub starts refusing.
With more than a million repositories still queued, that is weeks of work.

Google publishes GitHub file *contents* as a BigQuery public dataset, which
answers the same question in minutes and costs money instead of time. This
loads from there, through the same parser and the same store as the crawler, so
a skill is validated, hashed and deduplicated identically no matter which path
it arrived by.

    python bigquery_ingest.py --project my-gcp-project --sample   # cheap test
    python bigquery_ingest.py --project my-gcp-project            # the real run

DO NOT RUN THIS EXPECTING SKILLS. Measured 2026-09-06, and the reason this
file is kept rather than deleted:

    files      2,309,424,945 rows   last modified 2022-11-26
    contents     281,191,977 rows   last modified 2022-11-27
    sample_files  72,879,442 rows   last modified 2016-06-28

    files matching SKILL.md / .claude/skills across the whole dataset:  228

The public dataset is a snapshot frozen in **November 2022**. Agent skills are a
2024-2025 convention — SKILL.md, `.claude/skills/`, none of it existed when the
snapshot was taken. Two hundred and twenty-eight matches in 2.3 billion files is
not a corpus, it is noise, and the full extraction would have cost $10-20 to
retrieve almost nothing.

The lesson is about the method, not the dataset: "bulk source, therefore faster"
was an assumption about *freshness* that nobody checked, and it survived three
recommendations before a $0.81 count query settled it. Check the modified date
before designing around a dataset.

The code is left working because the dataset may be refreshed, and because the
ingestion path — same parser, same store, same content hash as the crawler — is
the right shape for any future bulk source.

Nothing runs before a dry run reports what it will scan and cost, and the real
run needs `--yes` on top of that. BigQuery bills on bytes scanned across every
column referenced, and `contents.content` is most of a multi-terabyte table —
this is exactly the shape of query that produces a surprising bill.

Two caveats worth knowing before spending anything:

* The dataset covers repositories with a detectable open-source licence, not
  all of GitHub. It is a large subset, not a superset of what we crawl.
* It is a periodic snapshot, so it lags. The crawler still earns its keep for
  freshness and for everything the snapshot misses.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_engine.parse import classify_path, parse_skill, skill_slug_from_path
from skill_engine.store import Store

log = logging.getLogger("bq")

# Anything that looks like a skill definition. Kept deliberately broad here and
# filtered properly by the parser afterwards: BigQuery charges for the scan
# either way, so a second pass over rows we already paid for is free, whereas
# a query too narrow to catch a naming variant means paying twice.
QUERY = """
SELECT f.repo_name, f.path, c.content
FROM `bigquery-public-data.github_repos.{files}` AS f
JOIN `bigquery-public-data.github_repos.{contents}` AS c
  ON f.id = c.id
WHERE c.binary = FALSE
  AND c.content IS NOT NULL
  AND (
        ENDS_WITH(f.path, 'SKILL.md')
     OR ENDS_WITH(f.path, 'skill.md')
     OR REGEXP_CONTAINS(f.path, r'\\.claude/skills/[^/]+/[^/]+\\.md$')
  )
"""


def build_query(sample: bool) -> str:
    # The sample_* tables are a ~10% slice at a fraction of the cost. Running
    # the pipeline against them first is the difference between discovering a
    # parsing bug for a few cents and discovering it for the full price.
    return QUERY.format(
        files="sample_files" if sample else "files",
        contents="sample_contents" if sample else "contents",
    )


def estimate(client, sql: str) -> int:
    """Bytes this query will scan, without running it."""
    from google.cloud import bigquery

    job = client.query(
        sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    )
    return job.total_bytes_processed


def ingest(store: Store, rows, source: str = "bigquery") -> dict:
    """Push BigQuery rows through the ordinary parser and store.

    Deliberately the same path the crawler uses. A second ingestion route that
    validated differently would put two populations in one index and quietly
    invalidate every corpus-relative statistic — percentile normalisation in
    particular is computed across the whole corpus.
    """
    import json

    seen = kept = invalid = failed = 0
    for row in rows:
        seen += 1
        repo, path = row["repo_name"], row["path"]
        try:
            parsed = parse_skill(row["content"] or "", path)
        except Exception as exc:                      # malformed file, not fatal
            log.debug("%s/%s: %s", repo, path, exc)
            failed += 1
            continue

        # The repository may be entirely new to us — BigQuery reaches
        # repositories the crawler never queued — so a stub must exist before
        # the skill, which has a foreign key to it.
        store.ensure_repo_stub(repo, discovered_via=source)
        store.upsert_skill({
            "repo": repo,
            "path": path,
            "name": parsed.name or skill_slug_from_path(path),
            "description": parsed.description,
            "body": parsed.body[:200_000],
            "heading": parsed.heading,
            "version": parsed.version,
            "license": parsed.license,
            "allowed_tools": json.dumps(parsed.allowed_tools),
            "metadata": json.dumps({**parsed.metadata, **parsed.extra}, default=str),
            "resources": json.dumps(parsed.resources),
            "source_kind": classify_path(path),
            "blob_sha": "",
            "content_hash": parsed.content_hash,
            "body_len": parsed.body_len,
            "score": 0.0,
            "valid": int(parsed.valid),
            "invalid_reason": parsed.invalid_reason,
            "warnings": parsed.notes,
        })
        kept += 1
        invalid += 0 if parsed.valid else 1
        if kept % 5000 == 0:
            store.commit()
            log.info("  %d stored / %d rows", kept, seen)
    store.commit()
    return {"rows": seen, "kept": kept, "invalid": invalid, "failed": failed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, help="billing GCP project")
    ap.add_argument("--db", default="data/scale.db")
    ap.add_argument("--sample", action="store_true",
                    help="use the ~10%% sample tables (far cheaper)")
    ap.add_argument("--yes", action="store_true",
                    help="actually run; without it, only the cost is reported")
    ap.add_argument("--price-per-tb", type=float, default=6.25)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        from google.cloud import bigquery
    except ImportError:
        print("pip install google-cloud-bigquery")
        return 1

    client = bigquery.Client(project=args.project)
    sql = build_query(args.sample)

    scanned = estimate(client, sql)
    tb = scanned / 1e12
    print(f"\n  table set : {'sample (~10%)' if args.sample else 'FULL'}")
    print(f"  will scan : {tb:.3f} TB")
    print(f"  est. cost : ${tb * args.price_per_tb:,.2f} "
          f"(first 1 TB per month is free)")
    if not args.yes:
        print("\n  dry run only — re-run with --yes to execute\n")
        return 0

    print("\n  running...\n")
    store = Store(Path(args.db))
    before = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    stats = ingest(store, client.query(sql).result())
    after = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    store.close()

    print(f"\n  rows returned : {stats['rows']:,}")
    print(f"  stored : {stats['kept']:,}  ({stats['invalid']:,} flagged invalid)")
    print(f"  unparseable : {stats['failed']:,}")
    print(f"  corpus : {before:,} -> {after:,}  (+{after - before:,} new)")
    print("\n  next: python release.py data/scale.db dist/skills-next.db\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
