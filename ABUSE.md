# Resisting scraping and abuse

## What is actually being defended

The corpus is public GitHub content. Every skill in it came from a public
repository, and anyone with a token and a weekend can rebuild it — that is
precisely how it was built. **The data cannot be made secret, and treating
that as the goal leads to defences that cost real users something and buy
nothing.**

Two things are worth defending, and both are achievable:

1. **Availability and cost.** A search runs a full-text query over a 0.6 GB
   index on a single 1 GB machine. A loop can saturate it, and the people it
   degrades are the ones using the site normally.
2. **The derived work.** The ranking, the author reputation model, and the
   taxonomy are the product. They took the measurement and iteration recorded
   in `whitepaper.md`. Bulk extraction lifts that for free.

## The defences, and what each one is worth

| Layer | Stops | Does not stop |
|---|---|---|
| Page-size cap (50) | Cheap bulk extraction | Patient extraction |
| Offset cap (1,000) | Walking a category end to end | Enumeration via varied queries |
| Cost-weighted limiter | Sustained load from one address | A distributed scraper |
| `robots.txt` | Well-behaved crawlers | Anyone who ignores it |
| Cloudflare rules | Most of the above, at the edge | A determined, funded actor |

### Cost weighting

The limiter originally charged one token per request, so the ceiling was set by
the cheapest endpoint: loose enough to be polite to a browser loading a page,
and therefore far too loose for the endpoint that runs a query over the index.
Requests are now priced by what they cost to serve, scaled by page size and
offset depth.

Burst and rate defend different populations and are tuned separately. The
search box debounces at 110 ms, so a person typing and correcting fires several
searches within a second or two — **burst** is what keeps them from being
throttled mid-sentence. **Rate** is what bounds a scraper, since extraction is a
marathon and one burst barely moves it.

### Measured effect

Walking the full 100,006-skill corpus:

| | requests | time |
|---|---|---|
| Before | 500 | ~2 minutes |
| After | 2,000 | ~133 minutes |

A human still gets eight searches back to back and ten page loads a second.

## What is not defended, honestly

**A distributed scraper defeats all of this.** Every limit here is per-address.
Rotating through a few hundred addresses restores the original throughput, and
no per-IP scheme can prevent that. Cloudflare's bot management is the only layer
positioned to see that pattern, which is why the edge rules matter more than
anything in this repository.

**Rate limiting cannot distinguish a scraper from an enthusiast.** The limits
are set where normal use is comfortable, which necessarily leaves room for
patient extraction. Tightening until scraping is impossible would break the
site for people using it properly.

## Visibility

Until this change the server recorded **nothing** about requests — `log_message`
sat at `DEBUG` beneath an `INFO` root — so a report of scraping could be neither
confirmed nor refuted. Throttling events now log at `WARNING` with a salted,
truncated per-process handle for the client instead of an address: enough to
separate "one client made 9,000 requests" from "9,000 clients made one", which
is the whole question when judging whether traffic is abuse, without putting IP
addresses in logs.

    fly logs -a searchskills | grep throttled

## Recommended edge configuration

In the Cloudflare dashboard for `searchskills.ai`:

1. **Security → Bots → Bot Fight Mode: on.** Free, and handles the
   unsophisticated majority.
2. **Security → WAF → Rate limiting rules.** One rule:
   `URI Path contains /api/` → 60 requests per minute per IP → Managed
   Challenge. This runs at the edge, so throttled traffic never reaches or
   costs the origin.
3. **Leave the landing page cached.** `cf-cache-status: HIT` means most
   traffic never touches the machine at all — the cheapest defence available.
