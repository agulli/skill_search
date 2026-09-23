# Documentation

| file | what it covers |
|---|---|
| [SAFETY.md](SAFETY.md) | The blocking gate: how a skill is judged harmful, what each rule was measured at, what the gate provably cannot detect, and how to review or reverse a block. |
| [eval.md](eval.md) | What the gate actually scores — precision, recall, false positives, false negatives — each with the sample it was measured on and the interval it carries. |
| [whitepaper.md](whitepaper.md) | Architecture and design: ranking, retrieval, the crawl, and the measurements behind each. |
| [ABUSE.md](ABUSE.md) | Edge defence: rate limiting, scraping protection, bulk-extraction resistance. |
| [RUST.md](RUST.md) | The native extension: measured profile, what it buys per component, and what it does not. |

`README.md` stays in the repository root because GitHub renders it as the
landing page; everything else lives here.
