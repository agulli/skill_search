"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import config as config_mod
from . import discover as disco
from .embed import build as build_embedder, embed_text, pack
from .github import GitHubClient
from .harvest import run_crawl
from .search import get_skill, search
from .store import Store

SEEDS = Path(__file__).resolve().parent.parent / "seeds.txt"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _client(cfg) -> tuple[GitHubClient, Store]:
    store = Store(cfg.db_path)
    gh = GitHubClient(
        cfg.tokens,
        etag_store=store,
        concurrency=cfg.concurrency,
        raw_concurrency=cfg.raw_concurrency,
    )
    return gh, store


# ------------------------------------------------------------------ commands


async def cmd_discover(args, cfg) -> None:
    gh, store = _client(cfg)
    try:
        total = 0
        sources = args.sources or ["seeds", "topics", "awesome", "keyword"]

        if "seeds" in sources and SEEDS.exists():
            total += disco.load_seeds(store, str(SEEDS))
        if "topics" in sources:
            total += await disco.discover_by_topic(gh, store)
        if "keyword" in sources:
            total += await disco.discover_by_keyword(gh, store)
        if "code" in sources:
            total += await disco.discover_by_code_search(gh, store)
        if "awesome" in sources:
            lists = [
                line.strip()
                for line in (SEEDS.read_text().splitlines() if SEEDS.exists() else [])
                if line.strip() and not line.startswith("#") and "awesome" in line.lower()
            ]
            if lists:
                total += await disco.mine_awesome_lists(gh, store, lists)
        if "gharchive" in sources:
            await disco.poll_gharchive(store, hours_back=args.hours)
        if "owners" in sources:
            total += await disco.expand_productive_owners(gh, store)

        store.commit()
        print(f"\nqueued {total} new repositories; queue depth {store.queue_depth()}")
        print(gh.quota_summary())
    finally:
        await gh.aclose()
        store.close()


async def cmd_enrich_authors(args, cfg) -> None:
    """Fetch follower counts — the one author signal not already in the corpus."""
    from .authors import ensure_schema
    from .github import NotFound

    gh, store = _client(cfg)
    ensure_schema(store)
    try:
        rows = store.db.execute(
            "SELECT login FROM authors WHERE followers IS NULL AND skills > 0 "
            "ORDER BY author_score DESC LIMIT ?", (args.limit,)
        ).fetchall()
        print(f"enriching {len(rows)} authors (1 request each)")
        done = 0
        for r in rows:
            try:
                data = await gh.get_json(f"/users/{r['login']}",
                                         etag_key=f"user:{r['login']}")
            except NotFound:
                continue
            if not data:
                continue
            store.db.execute(
                "UPDATE authors SET followers = ?, public_repos = ?, bio = ? "
                "WHERE login = ?",
                (data.get("followers"), data.get("public_repos"),
                 (data.get("bio") or "")[:280], r["login"]),
            )
            done += 1
            if done % 25 == 0:
                store.commit()
                print(f"  {done}/{len(rows)}  {gh.quota_summary()}")
        store.commit()
        print(f"enriched {done} authors; run `rank` to fold followers into scores")
    finally:
        await gh.aclose()
        store.close()


def cmd_authors(args, cfg) -> None:
    """List the highest-standing authors in the corpus."""
    store = Store(cfg.db_path)
    rows = store.db.execute(
        "SELECT login, author_score, skills, original_skills, total_stars, "
        "median_craft, followers FROM authors WHERE skills >= ? "
        "ORDER BY author_score DESC LIMIT ?",
        (args.min_skills, args.limit),
    ).fetchall()
    if not rows:
        print("no authors scored yet — run `skill-engine rank`")
        store.close()
        return
    print(f"{'score':>6} {'skills':>7} {'orig':>6} {'stars':>9} {'craft':>6}  author")
    for r in rows:
        orig = (r["original_skills"] / r["skills"]) if r["skills"] else 0
        craft = r["median_craft"] or 0
        print(f"{r['author_score']:>6.1f} {r['skills']:>7,} {orig:>5.0%} "
              f"{r['total_stars']:>9,} {craft:>6.2f}  {r['login']}")
    store.close()


async def cmd_mass_discover(args, cfg) -> None:
    gh, store = _client(cfg)
    try:
        def progress(i, total, have):
            print(f"  [{i}/{total}] {have:,} repositories known", flush=True)

        have = await disco.mass_discover(
            gh, store, target=args.target, on_progress=progress
        )
        print(f"\n{have:,} repositories, {store.queue_depth():,} queued for harvest")
        print(f"search requests: {gh.stats['requests']} "
              "(core rate-limit bucket untouched)")
    finally:
        await gh.aclose()
        store.close()


async def cmd_sweep(args, cfg) -> None:
    """Quota-free bulk harvest over codeload tarballs."""
    from .tarball import run_tarball_crawl

    store = Store(cfg.db_path)
    start_skills = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    print(f"starting at {start_skills:,} skills; target {args.target:,}")
    print(f"concurrency {args.concurrency}, max archive {args.max_mb}MB, "
          "0 API requests\n", flush=True)

    def progress(totals, indexed, rate, dl):
        gb = dl["bytes"] / 1e9
        print(
            f"  {totals['repos']:>6,} repos | {indexed:>7,} skills "
            f"| {rate:>6,.0f} repos/h | {gb:>5.1f}GB "
            f"| skip {dl['too_big']} fail {dl['failed']} fallback {totals['fallback']}",
            flush=True,
        )

    try:
        totals = await run_tarball_crawl(
            store, cfg,
            limit=args.limit,
            target_skills=args.target,
            concurrency=args.concurrency,
            max_mb=args.max_mb,
            on_progress=progress,
        )
    except KeyboardInterrupt:
        print("\ninterrupted — queue state is preserved, rerun to resume")
        totals = {}

    end_skills = store.db.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    print(f"\n{end_skills:,} skills indexed (+{end_skills - start_skills:,})")
    if totals:
        print(f"repos: {totals['repos']:,} processed, {totals['empty']:,} empty, "
              f"{totals['fallback']:,} deferred to REST, {totals['errors']:,} errors")
        d = totals.get("download_stats", {})
        print(f"downloaded {d.get('downloads', 0):,} archives, "
              f"{d.get('bytes', 0)/1e9:.1f}GB, 0 API requests")
    store.close()


async def cmd_crawl(args, cfg) -> None:
    gh, store = _client(cfg)
    try:
        totals = await run_crawl(
            gh, store, cfg, limit=args.limit, batch=args.batch,
            strategy=args.strategy,
        )
        print(
            f"\ncrawled {totals['repos']} repos, found {totals['skills']} skills, "
            f"{totals['errors']} errors"
        )
        if totals["repos"]:
            print(f"yield: {totals['skills']/totals['repos']:.1f} skills per repo")
        print(
            f"api requests: {gh.stats['requests']} "
            f"({gh.stats['not_modified']} served from cache as 304, "
            f"{gh.stats['retries']} retries, {gh.stats['waited']:.0f}s waiting)"
        )
        print(gh.quota_summary())
    finally:
        await gh.aclose()
        store.close()


async def cmd_run(args, cfg) -> None:
    """Discover then crawl, as one continuous pass. The cron-friendly command."""
    await cmd_discover(
        argparse.Namespace(sources=["gharchive", "seeds", "topics", "awesome"], hours=args.hours),
        cfg,
    )
    await cmd_crawl(argparse.Namespace(limit=args.limit, batch=100), cfg)


def cmd_index(args, cfg) -> None:
    """Compute embeddings for skills that do not have one yet."""
    store = Store(cfg.db_path)
    name = args.embedder or cfg.embedder
    if name == "none":
        print("embedder is 'none'; BM25 index is maintained live by triggers.")
        print("enable vectors with: --embedder hashing|local|voyage")
        store.close()
        return

    embedder = build_embedder(name)
    rows = store.db.execute(
        "SELECT s.id, s.name, s.description, s.body, s.repo FROM skills s "
        "LEFT JOIN vectors v ON v.skill_id = s.id AND v.model = ? "
        "WHERE s.valid = 1 AND v.skill_id IS NULL",
        (embedder.model,),
    ).fetchall()

    print(f"embedding {len(rows)} skills with {embedder.model}")
    batch = 64
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        texts = [
            embed_text(r["name"], r["description"], r["body"], r["repo"]) for r in chunk
        ]
        vectors = embedder.encode(texts)
        store.db.executemany(
            "INSERT INTO vectors(skill_id, model, dim, vec) VALUES(?,?,?,?) "
            "ON CONFLICT(skill_id) DO UPDATE SET model=excluded.model, "
            "dim=excluded.dim, vec=excluded.vec",
            [
                (r["id"], embedder.model, len(v), pack(v))
                for r, v in zip(chunk, vectors)
            ],
        )
        store.commit()
        print(f"  {min(i + batch, len(rows))}/{len(rows)}", end="\r", flush=True)

    print(f"\nembedded {len(rows)} skills")
    store.close()


def cmd_search(args, cfg) -> None:
    store = Store(cfg.db_path)
    filters = {
        "min_stars": args.min_stars,
        "min_score": args.min_score,
        "license": args.license,
        "kind": args.kind,
        "repo": args.repo,
        "language": args.language,
        "owner_type": args.owner_type,
        "max_age_days": args.max_age_days,
        "include_forks": args.forks,
    }
    hits = search(
        store, " ".join(args.query), limit=args.limit, filters=filters,
        embedder_name=args.embedder or cfg.embedder,
        max_per_repo=None if args.max_per_repo == 0 else args.max_per_repo,
    )

    if args.json:
        print(json.dumps([h.to_dict() for h in hits], indent=2))
    elif not hits:
        print("no matches")
    else:
        for i, h in enumerate(hits, 1):
            print(f"\n{i}. \033[1m{h.name}\033[0m  ({h.repo}, {h.stars}★, q{h.score:.0f})")
            if h.description:
                print(f"   {h.description[:180]}")
            if h.snippet:
                print(f"   \033[2m{h.snippet[:160]}\033[0m")
            print(f"   \033[2m{h.url}\033[0m")
    store.close()


def cmd_show(args, cfg) -> None:
    store = Store(cfg.db_path)
    data = get_skill(store, args.id)
    if not data:
        print(f"no skill with id {args.id}", file=sys.stderr)
        sys.exit(1)
    if args.raw:
        print(data["body"])
    else:
        shown = {k: v for k, v in data.items() if k != "body"}
        print(json.dumps(shown, indent=2, default=str))
    store.close()


def cmd_rank(args, cfg) -> None:
    """Recompute every score from stored data. No network access."""
    from .ranking import Weights, recompute

    store = Store(cfg.db_path)
    overrides = {}
    for pair in args.weight or []:
        key, _, value = pair.partition("=")
        if not value:
            print(f"bad --weight {pair!r}, expected name=value", file=sys.stderr)
            sys.exit(1)
        overrides[key.strip()] = float(value)
    weights = Weights(**overrides)

    result = recompute(store, weights, keep_detail=not args.no_detail)
    print(
        f"scored {result['repos_scored']:,} repos, "
        f"{result.get('authors_scored', 0):,} authors and "
        f"{result['skills_scored']:,} skills against {result['metrics']} "
        f"corpus metrics (n={result['corpus_n']:,})"
    )
    if not args.no_optimize:
        opt = store.optimize()
        print(f"compacted FTS index: {opt['segments_before']:,} -> "
              f"{opt['segments_after']:,} segments")

    rows = store.db.execute(
        "SELECT s.name, s.repo, s.score, r.stars FROM skills s "
        "JOIN repos r ON r.full_name = s.repo WHERE s.valid = 1 "
        "ORDER BY s.score DESC LIMIT ?", (args.top,)
    ).fetchall()
    if rows:
        print(f"\ntop {len(rows)} skills:")
        for i, r in enumerate(rows, 1):
            print(f"  {i:>2}. {r['score']:>5.1f}  {r['name'][:32]:<34}"
                  f" {r['repo'][:38]:<40} {r['stars']:>7,}★")
    store.close()


def cmd_explain(args, cfg) -> None:
    """Print the full scoring breakdown for one skill or repository."""
    store = Store(cfg.db_path)
    if "/" in args.target:
        row = store.db.execute(
            "SELECT full_name, repo_score, score_detail FROM repos WHERE full_name = ?",
            (args.target,),
        ).fetchone()
        label, score, detail = (
            (row["full_name"], row["repo_score"], row["score_detail"]) if row
            else (None, None, None)
        )
    else:
        row = store.db.execute(
            "SELECT id, name, repo, score, score_detail FROM skills WHERE id = ?",
            (int(args.target),),
        ).fetchone()
        label, score, detail = (
            (f"{row['name']} ({row['repo']})", row["score"], row["score_detail"])
            if row else (None, None, None)
        )

    if not row:
        print(f"nothing found for {args.target!r}", file=sys.stderr)
        sys.exit(1)
    if not detail:
        print("no breakdown stored — run `skill-engine rank` first", file=sys.stderr)
        sys.exit(1)

    data = json.loads(detail)
    print(f"\n{label}\n  score {score:.2f}  = 100 × base {data['base']:.4f}"
          f" × trust {data['trust']:.4f}\n")
    for family, value in data["families"].items():
        bar = "█" * int((value or 0) * 30)
        print(f"  {family:<16} {value if value is None else round(value,4):<8} {bar}")
    for section in ("popularity", "momentum", "maintenance", "authority",
                    "craft", "distinctiveness"):
        if section in data and isinstance(data[section], dict):
            print(f"\n  {section}:")
            for k, v in data[section].items():
                print(f"    {k:<20} {'—' if v is None else round(v, 4)}")
    if data.get("penalties"):
        print("\n  penalties (multiplicative):")
        for k, v in data["penalties"].items():
            print(f"    {k:<20} ×{v}")
    if data.get("derived"):
        print("\n  derived:")
        for k, v in data["derived"].items():
            print(f"    {k:<20} {'—' if v is None else v}")
    store.close()


async def cmd_enrich(args, cfg) -> None:
    """Fetch contributor and release counts for the highest-value repos."""
    from .harvest import enrich_repo

    gh, store = _client(cfg)
    try:
        rows = store.db.execute(
            "SELECT full_name FROM repos WHERE skill_count > 0 AND contributors IS NULL "
            "ORDER BY stars DESC LIMIT ?", (args.limit,)
        ).fetchall()
        print(f"enriching {len(rows)} repositories (2 requests each)")
        for i, r in enumerate(rows, 1):
            try:
                extra = await enrich_repo(gh, r["full_name"])
                store.upsert_repo(extra, touch_crawled=False)
            except Exception as exc:
                log = logging.getLogger("skill_engine.cli")
                log.warning("%s: %s", r["full_name"], exc)
            if i % 25 == 0:
                store.commit()
                print(f"  {i}/{len(rows)}  {gh.quota_summary()}")
        store.commit()
        print(f"done; {gh.stats['requests']} requests, "
              f"{gh.stats['not_modified']} were 304")
    finally:
        await gh.aclose()
        store.close()


def cmd_stats(args, cfg) -> None:
    store = Store(cfg.db_path)
    stats = store.stats()
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key.replace('_', ' '):<{width}}  {value:>8,}")

    cov = store.db.execute(
        """
        SELECT COUNT(*) n,
               SUM(stars IS NOT NULL)        AS stars,
               SUM(subscribers IS NOT NULL)  AS subscribers,
               SUM(created_at IS NOT NULL)   AS created,
               SUM(language IS NOT NULL)     AS language,
               SUM(license IS NOT NULL)      AS license,
               SUM(contributors IS NOT NULL) AS contributors,
               SUM(latest_release IS NOT NULL) AS releases,
               SUM(repo_score > 0)           AS scored
        FROM repos
        """
    ).fetchone()
    if cov and cov["n"]:
        print("\nmetadata coverage:")
        for field in ("stars", "created", "language", "license", "subscribers",
                      "contributors", "releases", "scored"):
            got = cov[field] or 0
            print(f"  {field:<14} {got:>7,} / {cov['n']:,}  ({100*got/cov['n']:>5.1f}%)")

    rows = store.db.execute(
        "SELECT source_kind, COUNT(*) c FROM skills WHERE valid=1 "
        "GROUP BY source_kind ORDER BY c DESC"
    ).fetchall()
    if rows:
        print("\nby location:")
        for r in rows:
            print(f"  {r['source_kind'] or 'unknown':<16} {r['c']:>6,}")

    bad = store.db.execute(
        "SELECT invalid_reason, COUNT(*) c FROM skills WHERE valid=0 "
        "GROUP BY invalid_reason ORDER BY c DESC LIMIT 8"
    ).fetchall()
    if bad:
        print("\nrejected candidates:")
        for r in bad:
            print(f"  {(r['invalid_reason'] or '?')[:52]:<54} {r['c']:>6,}")
    store.close()


def cmd_serve(args, cfg) -> None:
    from .serve import serve

    serve(cfg.db_path, host=args.host, port=args.port,
          embedder_name=args.embedder or cfg.embedder)


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skill-engine",
        description="Search engine for AI agent skills published on GitHub",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--db", help="path to the SQLite database")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="find candidate repositories")
    d.add_argument(
        "--sources", nargs="+",
        choices=["seeds", "topics", "keyword", "code", "awesome", "gharchive", "owners"],
        help="default: seeds topics awesome keyword",
    )
    d.add_argument("--hours", type=int, default=2, help="GH Archive hours to replay")
    d.set_defaults(func=cmd_discover, is_async=True)

    c = sub.add_parser("crawl", help="harvest queued repositories")
    c.add_argument("--limit", type=int, default=200, help="max repos this run")
    c.add_argument("--batch", type=int, default=100, help="GraphQL metadata batch size")
    c.add_argument("--strategy", choices=["score", "fifo"], default="score",
                   help="score: best-ranked repos first (16x yield/request); "
                        "fifo: discovery order, for a completeness sweep")
    c.set_defaults(func=cmd_crawl, is_async=True)

    sw = sub.add_parser("sweep", help="bulk harvest via codeload tarballs (no API quota)")
    sw.add_argument("--target", type=int, default=100_000, help="stop at N skills")
    sw.add_argument("--limit", type=int, default=100_000, help="max repos to process")
    sw.add_argument("--concurrency", type=int, default=4)
    sw.add_argument("--max-mb", type=int, default=120,
                    help="skip archives larger than this")
    sw.set_defaults(func=cmd_sweep, is_async=True)

    r = sub.add_parser("run", help="discover then crawl (for cron)")
    r.add_argument("--limit", type=int, default=500)
    r.add_argument("--hours", type=int, default=2)
    r.set_defaults(func=cmd_run, is_async=True)

    i = sub.add_parser("index", help="build vector embeddings")
    i.add_argument("--embedder", choices=["hashing", "local", "voyage"])
    i.set_defaults(func=cmd_index, is_async=False)

    s = sub.add_parser("search", help="query the index")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--min-stars", type=int, default=0)
    s.add_argument("--min-score", type=float, default=0, help="minimum quality score")
    s.add_argument("--license")
    s.add_argument("--kind", help="root, skills-dir, claude-project, plugin, …")
    s.add_argument("--repo", help="restrict to repos matching this substring")
    s.add_argument("--language", help="repository primary language")
    s.add_argument("--owner-type", choices=["User", "Organization"])
    s.add_argument("--max-age-days", type=int,
                   help="only repos pushed within this many days")
    s.add_argument("--forks", action="store_true", help="include forks")
    s.add_argument("--max-per-repo", type=int, default=3,
                   help="cap results per repository (0 disables)")
    s.add_argument("--embedder", choices=["none", "hashing", "local", "voyage"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search, is_async=False)

    sh = sub.add_parser("show", help="print one skill by id")
    sh.add_argument("id", type=int)
    sh.add_argument("--raw", action="store_true", help="print the SKILL.md body")
    sh.set_defaults(func=cmd_show, is_async=False)

    rk = sub.add_parser("rank", help="recompute all quality scores (offline)")
    rk.add_argument("--weight", action="append", metavar="NAME=VALUE",
                    help="override a family weight, e.g. --weight popularity=0.3")
    rk.add_argument("--top", type=int, default=15, help="preview the top N skills")
    rk.add_argument("--no-detail", action="store_true",
                    help="skip storing the JSON breakdown (smaller database)")
    rk.add_argument("--no-optimize", action="store_true",
                    help="skip compacting the FTS index afterwards")
    rk.set_defaults(func=cmd_rank, is_async=False)

    ex = sub.add_parser("explain", help="show why something scored what it did")
    ex.add_argument("target", help="a skill id, or an owner/repo full name")
    ex.set_defaults(func=cmd_explain, is_async=False)

    en = sub.add_parser("enrich", help="fetch contributor and release counts")
    en.add_argument("--limit", type=int, default=200)
    en.set_defaults(func=cmd_enrich, is_async=True)

    ea = sub.add_parser("enrich-authors", help="fetch author follower counts")
    ea.add_argument("--limit", type=int, default=500)
    ea.set_defaults(func=cmd_enrich_authors, is_async=True)

    au = sub.add_parser("authors", help="rank authors by standing")
    au.add_argument("--limit", type=int, default=25)
    au.add_argument("--min-skills", type=int, default=3)
    au.set_defaults(func=cmd_authors, is_async=False)

    md = sub.add_parser("mass-discover", help="sweep search until N repos are known")
    md.add_argument("--target", type=int, default=10_000)
    md.set_defaults(func=cmd_mass_discover, is_async=True)

    st = sub.add_parser("stats", help="index statistics")
    st.set_defaults(func=cmd_stats, is_async=False)

    sv = sub.add_parser("serve", help="run the JSON API and search page")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--embedder", choices=["none", "hashing", "local", "voyage"])
    sv.set_defaults(func=cmd_serve, is_async=False)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    cfg = config_mod.load()
    if args.db:
        cfg.db_path = Path(args.db)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.cmd in ("discover", "crawl", "run") and not cfg.has_auth:
        print(
            "warning: no GITHUB_TOKEN / GITHUB_TOKENS set — you get 60 requests/hour\n"
            "         and code search is unavailable. Set at least one PAT.\n",
            file=sys.stderr,
        )

    if getattr(args, "is_async", False):
        asyncio.run(args.func(args, cfg))
    else:
        args.func(args, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
