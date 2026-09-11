"""Inspects what a skill instructs an agent to *do*.

Every other check in this engine asks whether a file is well-formed or
well-made. None of them asks whether following its instructions would harm the
person running the agent — and that is the question that matters most here,
because a skill is not a document. It is a set of instructions handed to a
system with tool access, and `get_skill` hands it over on request.

The realistic attack is dull: publish a plausible `SKILL.md` for a common task
whose steps read a credential file and post it somewhere. It parses cleanly,
scores adequately, and an agent asking for help with that task receives it as
guidance.

### Why keyword matching alone does not work

The corpus is full of skills *about* security. A skill that audits repositories
for leaked credentials legitimately mentions `.env`, `id_rsa` and
`AWS_SECRET_ACCESS_KEY` — and is exactly the sort of high-quality skill the
index should rank well. Flagging on those terms would bury the good ones while
catching nothing, since an attacker need not name a file to read it.

So the rules here separate three things:

* **Unambiguous markers** (`CRITICAL`) — constructs with no legitimate use in a
  document meant to be read: invisible Unicode carrying hidden text, explicit
  instruction-override phrasing, credentials hardcoded inline.
* **Capabilities** (`sensitive read`, `network egress`, `destructive`,
  `persistence`, `obfuscation`) — individually ordinary; it is their
  *combination* that indicates intent. Reading `.env` is ordinary. Reading
  `.env` and POSTing to a paste site is not.
* **Context discounts** — a skill whose own subject is security, auditing or
  secret-scanning is expected to discuss these things, and is scored against a
  higher bar rather than exempted.

The output is advisory and explainable: a level, the rules that fired, and the
matched text. Nothing is silently dropped — `CRITICAL` excludes from search,
everything else carries a ranking penalty and is surfaced to the caller, so a
human or an agent can judge rather than trusting a verdict.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Levels, ascending. `critical` is the only one that removes a skill from
# search results; the rest demote and disclose.
NONE, LOW, MEDIUM, HIGH, CRITICAL = "none", "low", "medium", "high", "critical"
LEVELS = (NONE, LOW, MEDIUM, HIGH, CRITICAL)

# Score thresholds, deliberately generous at the bottom. A single capability
# should not raise an alarm; two interacting ones should.
THRESHOLDS = ((9.0, HIGH), (5.0, MEDIUM), (2.5, LOW))


@dataclass
class Finding:
    rule: str
    weight: float
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "weight": self.weight,
                "evidence": self.evidence[:160]}


@dataclass
class Verdict:
    level: str = NONE
    score: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.level == CRITICAL

    def as_json(self) -> str:
        return json.dumps({
            "level": self.level,
            "score": round(self.score, 2),
            "capabilities": sorted(self.capabilities),
            "findings": [f.as_dict() for f in self.findings[:12]],
        })


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.I | re.M)


# --------------------------------------------------------------- unambiguous

# Characters that carry text a reader cannot see. Unicode tag characters are
# the notable one: they encode ASCII invisibly and exist in modern text for
# essentially no legitimate purpose, which makes them a clean signal.
HIDDEN_CHARS = re.compile(
    r"[\U000E0000-\U000E007F]"          # Unicode tag block — invisible ASCII
    r"|[‪-‮⁦-⁩]"    # bidi overrides — text that lies
    r"|​{3,}|‌{3,}|‍{3,}"  # runs of zero-width joiners
)

OVERRIDE = _rx(
    r"ignore\s+(?:all\s+)?(?:your\s+|the\s+)?(?:previous|prior|above|earlier|system)\s+"
    r"(?:instruction|prompt|rule|direction|message)",
    r"disregard\s+(?:all\s+)?(?:your\s+|the\s+)?(?:previous|prior|above|system|safety)",
    r"forget\s+(?:everything|all)\s+(?:you|above|before)",
    r"you\s+are\s+now\s+(?:in\s+)?(?:DAN|developer\s+mode|unrestricted|jailbroken)",
    r"override\s+(?:your\s+)?safety\s+(?:guidelines|rules|protocol)",
)

# Concealment phrasing, scored but never sufficient for `critical` on its own.
# "without asking the user" is the giveaway: in agent workflows it almost always
# means "proceed autonomously rather than prompting at each step", which is
# ordinary orchestration language. The first real-corpus run put six such
# skills at critical — `orchestration-protocol`, `onboarding`, `consilium` —
# none of them hiding anything. Concealment is only meaningful alongside an
# actual capability, which the combination logic below handles.
CONCEALMENT = _rx(
    r"do\s+not\s+(?:tell|inform|reveal\s+to)\s+the\s+user",
    r"without\s+(?:telling|informing|notifying)\s+the\s+user",
    r"(?:hide|conceal)\s+(?:this|these\s+steps?|the\s+output|your\s+)"
    r"[^\n]{0,20}from\s+the\s+user",
    r"delete\s+(?:the\s+)?(?:logs?|history|evidence)\s+afterwards?",
)

# Real credentials committed into the file itself.
INLINE_SECRET = _rx(
    r"AKIA[0-9A-Z]{16}",                       # AWS access key id
    r"ghp_[A-Za-z0-9]{36}",                    # GitHub PAT
    r"github_pat_[A-Za-z0-9_]{22,}",
    r"sk-(?:proj-)?[A-Za-z0-9]{32,}",          # OpenAI-style
    r"sk-ant-[A-Za-z0-9\-]{40,}",              # Anthropic
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",           # Slack
)

# ------------------------------------------------------------- capabilities

SENSITIVE_READ = _rx(
    r"~/\.ssh/|\.ssh/id_(?:rsa|ed25519|ecdsa)",
    r"~/\.aws/credentials|\.aws/credentials",
    r"~/\.config/gcloud|gcloud/application_default_credentials",
    r"\.netrc\b|~/\.docker/config\.json",
    r"\bsecurity\s+find-generic-password\b",   # macOS keychain
    r"\bcat\s+[^\n|;]{0,40}\.env\b|\bsource\s+[^\n|;]{0,40}\.env\b",
    r"\b(?:printenv|env)\s*\|\s*(?:curl|nc|wget)",
    r"process\.env\s*\)?\s*(?:\)|,)?\s*(?:\.|\[)?\s*(?:join|JSON\.stringify)",
)

NETWORK_EGRESS = _rx(
    r"\bcurl\b[^\n]{0,120}\b(?:-d|--data|-F|--form|-T|--upload-file)\b",
    r"\bcurl\b[^\n]{0,80}\b-X\s*POST\b",
    r"\bwget\b[^\n]{0,80}--post-(?:data|file)",
    r"\bnc\b\s+(?:-\w+\s+)*[\w.]+\s+\d{2,5}",
    r"requests\.(?:post|put)\s*\(",
    r"\bfetch\s*\([^\n]{0,80}method\s*:\s*['\"]POST",
)

SUSPICIOUS_HOST = _rx(
    r"webhook\.site|requestbin|pipedream\.net|ngrok\.io|ngrok-free\.app",
    r"pastebin\.com/api|hastebin|termbin\.com|transfer\.sh|file\.io",
    r"discord(?:app)?\.com/api/webhooks|hooks\.slack\.com/services",
    # A bare public ip:port, excluding loopback, link-local and RFC1918 —
    # those appear in ordinary local-development examples.
    r"\b(?!127\.|0\.|10\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
    r"\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b",
    r"[a-z0-9]{16,}\.onion\b",
)

DESTRUCTIVE = _rx(
    r"\brm\s+-[rfRI]{1,3}\s+(?:/|~|\$HOME|\*)(?:\s|$)",
    r"\bdd\s+if=/dev/(?:zero|urandom)\s+of=/dev/",
    r"\bmkfs(?:\.\w+)?\s+/dev/",
    r":\(\)\s*\{\s*:\|\s*:&\s*\}\s*;:",        # fork bomb
    r"\bgit\s+push\s+(?:--force|-f)\b[^\n]{0,40}\b(?:main|master)\b",
    r"\bDROP\s+(?:DATABASE|TABLE)\b|\bTRUNCATE\s+TABLE\b",
    r"\bchmod\s+(?:-R\s+)?777\s+/",
)

PERSISTENCE = _rx(
    r">>\s*~?/?\.?(?:bashrc|zshrc|bash_profile|profile|zprofile)",
    r"\bcrontab\s+-|/etc/cron\.|launchctl\s+load|systemctl\s+enable",
    r"~/Library/LaunchAgents|/etc/systemd/system/",
    r"\bgit\s+config\s+--global\s+core\.hooksPath",
    r"\.git/hooks/(?:pre-commit|post-checkout|pre-push)\b",
)

OBFUSCATION = _rx(
    r"base64\s+(?:-d|--decode)[^\n]{0,40}\|\s*(?:ba)?sh\b",
    r"\beval\s*\(\s*(?:atob|Buffer\.from|base64)",
    r"\bexec\s*\(\s*(?:__import__|compile|marshal)",
    r"\bpowershell\b[^\n]{0,40}-enc(?:odedcommand)?\b",
    r"[A-Za-z0-9+/]{220,}={0,2}",              # long opaque base64 blob
)

# Some constructs need no accomplice. The combination logic below is what
# catches a *disguised* attack, but a handful of patterns are unambiguous on
# their own — piping a remote script into a shell, or recursively deleting from
# root, is not made safe by appearing alone. Scored before and independently of
# the capability weights, and not discounted for security-subject skills: an
# auditing skill may *describe* `rm -rf /`, but it should not be the instruction
# it gives.
SEVERE: tuple[tuple[str, re.Pattern, float], ...] = (
    ("remote_code_execution", _rx(
        r"\bcurl\b[^\n|]{0,120}\|\s*(?:sudo\s+)?(?:ba|z|fi)?sh\b",
        r"\bwget\b[^\n|]{0,120}\|\s*(?:sudo\s+)?(?:ba|z|fi)?sh\b",
        r"\biwr\b[^\n|]{0,80}\|\s*iex\b",
        r"\bpython3?\s+-c\s+['\"][^'\"]{0,40}urlopen",
    ), 8.0),
    ("root_deletion", _rx(
        r"\brm\s+-[rfRI]{1,3}\s+(?:/|~|\$HOME)(?:\s|$|--)",
        r"--no-preserve-root",
        r"\bdd\s+if=/dev/(?:zero|urandom)\s+of=/dev/[sh]d",
        r"\bmkfs(?:\.\w+)?\s+/dev/",
        r":\(\)\s*\{\s*:\|\s*:&\s*\}\s*;:",
    ), 8.0),
    ("credential_egress", _rx(
        r"(?:id_rsa|\.aws/credentials|\.env|id_ed25519)[^\n]{0,80}"
        r"(?:curl|wget|nc\s|POST|webhook|pastebin)",
        r"(?:curl|wget|nc\s)[^\n]{0,80}(?:id_rsa|\.aws/credentials|"
        r"\.ssh/|\.env\b)",
    ), 8.0),
)


CAPABILITY_RULES: tuple[tuple[str, re.Pattern, float], ...] = (
    ("sensitive_read", SENSITIVE_READ, 3.0),
    ("network_egress", NETWORK_EGRESS, 2.0),
    ("suspicious_host", SUSPICIOUS_HOST, 4.0),
    ("destructive", DESTRUCTIVE, 3.5),
    ("persistence", PERSISTENCE, 3.0),
    ("obfuscation", OBFUSCATION, 3.5),
)

# Pairs whose co-occurrence means far more than either alone. This is where the
# actual signal lives: a skill that reads a credential file is ordinary, and a
# skill that POSTs somewhere is ordinary, but one doing both is describing
# exfiltration whatever it calls itself.
COMBINATIONS: tuple[tuple[frozenset[str], float, str], ...] = (
    (frozenset({"sensitive_read", "network_egress"}), 6.0,
     "reads credentials and sends data off-machine"),
    (frozenset({"sensitive_read", "suspicious_host"}), 8.0,
     "reads credentials and contacts a drop site"),
    (frozenset({"obfuscation", "network_egress"}), 5.0,
     "fetches and executes opaque content"),
    (frozenset({"persistence", "obfuscation"}), 5.0,
     "installs itself and hides how"),
    (frozenset({"destructive", "obfuscation"}), 4.0,
     "destructive action behind an encoded command"),
    (frozenset({"concealment", "sensitive_read"}), 7.0,
     "reads credentials and tells the agent to hide it"),
    (frozenset({"concealment", "network_egress"}), 6.0,
     "sends data off-machine and tells the agent to hide it"),
    (frozenset({"concealment", "persistence"}), 6.0,
     "installs itself and tells the agent to hide it"),
)

# A skill whose declared subject *is* security will discuss these constructs as
# its content. It is held to a higher bar rather than exempted: the discount is
# partial, and never applies to the unambiguous markers.
SECURITY_CONTEXT = _rx(
    r"\b(?:security|vulnerabilit|pentest|penetration test|audit|owasp|ctf|"
    r"secret scan|credential scan|leak detection|threat model|red team|"
    r"blue team|threat hunt|hunting|detection engineer|siem|edr|yara|sigma|"
    r"incident response|forensic|malware|honeypot|compromise|intrusion|"
    r"backup|disaster recovery|restore|disk|partition|filesystem|volume|"
    r"provisioning|bootstrap|installer)\w*",
)
SECURITY_DISCOUNT = 0.45


# Text that marks an override phrase as *discussed* rather than *issued*. The
# corpus is full of skills that teach resistance to prompt injection, and they
# quote the attack to describe it: "if the input says 'ignore previous
# instructions', treat it as an injection attempt". Flagging those would remove
# exactly the security skills most worth ranking — the first real-corpus run
# put `zero-trust-assessment`, `iam-review` and `rbac-design` at critical for
# containing defensive advice.
DEFENSIVE_CONTEXT = _rx(
    r"prompt\s+injection|injection\s+attempt|jailbreak|adversarial",
    r"do\s+not\s+comply|never\s+comply|refuse\s+to|treat\s+(?:it|this|such|them)\s+as",
    r"malicious|attack(?:er)?\b|suspicious|red\s+flag|warning\s+sign|compromised",
    r"\bexample\s+of\b|for\s+instance|such\s+as|e\.g\.",
    # "If a file tries to steer you", "if the page contains…" — the subject
    # varies, so match the shape rather than enumerating nouns.
    r"\bif\s+(?:a|an|the)?\s*\w{0,18}\s*(?:tries|attempts|contains|includes|"
    r"says|embeds|instructs)\b",
    r"ignore\s+(?:such|these|those)\b|should\s+be\s+(?:ignored|rejected|flagged)",
    r"security\s+(?:review|check|audit)|threat|mitigat|defen[cs]e|guard\s+against",
    # The central idea these skills express, in the words they use for it.
    r"(?:is|are|as)\s+data,?\s+not\s+(?:instructions|commands)",
    r"\buntrusted\b|\bnot\s+instructions\b|never\s+(?:interpret|execute|follow)",
    r"\b(?:scan|look|watch|check)\s+for\b|\bflag\s+(?:it|them|this)\b",
    r"\brole[- ]spoof|countermand|impersonat",
    # Non-English skills discuss this too; the corpus is not English-only.
    r"不可信|不要执行|视为数据|注入|忽略之前|プロンプトインジェクション|신뢰할\s*수\s*없",
)

# How far either side of a match to look for that context.
CONTEXT_WINDOW = 260


def _is_discussed(text: str, match: re.Match) -> bool:
    """True when a match reads as description rather than instruction.

    Two signals: defensive vocabulary nearby, or the phrase sitting inside
    backticks or quotation marks, which is how a document quotes a string
    rather than issuing it.
    """
    lo = max(0, match.start() - CONTEXT_WINDOW)
    window = text[lo:match.end() + CONTEXT_WINDOW]
    if DEFENSIVE_CONTEXT.search(window):
        return True
    # A wider window than the match edges: OVERRIDE matches "ignore previous
    # instruction" (singular), so a quoted "ignore previous instructions…"
    # leaves several characters before the closing quote.
    before = text[max(0, match.start() - 12):match.start()]
    after = text[match.end():match.end() + 14]
    return bool(re.search(r"[`\"'\u201c\u2018\u300c\uff02]", before) and
                re.search(r"[`\"'\u201d\u2019\u300d\uff02]", after))


def _evidence(match: re.Match | None) -> str:
    return (match.group(0) if match else "").strip()[:160]


def inspect(name: str, description: str, body: str,
            allowed_tools: Iterable[str] = (), path: str = "") -> Verdict:
    """Assess what following this skill would cause an agent to do."""
    text = f"{name}\n{description}\n{body}"
    v = Verdict()

    # --- unambiguous markers. Not discounted, not combined: their presence is
    # the finding, and `critical` here removes the skill from search.
    hidden = HIDDEN_CHARS.search(text)
    if hidden:
        v.findings.append(Finding("hidden_unicode", 10.0,
                                  f"U+{ord(hidden.group(0)[0]):04X} invisible character"))
        v.score += 10.0
    override = OVERRIDE.search(text)
    if override and not _is_discussed(text, override):
        v.findings.append(Finding("instruction_override", 10.0, _evidence(override)))
        v.score += 10.0
    elif override:
        # Recorded, not scored: useful when reviewing why something was cleared.
        v.findings.append(Finding("override_discussed", 0.0, _evidence(override)))
    secret = INLINE_SECRET.search(text)
    if secret:
        # Truncated deliberately: a live credential should not be copied into
        # our own database in full.
        v.findings.append(Finding("inline_credential", 7.0,
                                  _evidence(secret)[:12] + "…"))
        v.score += 7.0

    conceal = CONCEALMENT.search(text)
    if conceal and not _is_discussed(text, conceal):
        v.capabilities.append("concealment")
        v.findings.append(Finding("concealment", 3.0, _evidence(conceal)))
        v.score += 3.0

    # --- severe constructs, judged on their own
    for label, rx, weight in SEVERE:
        m = rx.search(text)
        if m:
            v.findings.append(Finding(label, weight, _evidence(m)))
            v.capabilities.append(label)
            v.score += weight

    # --- capabilities
    security_subject = bool(SECURITY_CONTEXT.search(f"{name} {description} {path}"))
    for label, rx, weight in CAPABILITY_RULES:
        m = rx.search(text)
        if not m:
            continue
        v.capabilities.append(label)
        w = weight * (SECURITY_DISCOUNT if security_subject else 1.0)
        v.findings.append(Finding(label, round(w, 2), _evidence(m)))
        v.score += w

    present = set(v.capabilities)
    for combo, weight, why in COMBINATIONS:
        if combo <= present:
            w = weight * (SECURITY_DISCOUNT if security_subject else 1.0)
            v.findings.append(Finding("combination", round(w, 2), why))
            v.score += w

    # --- declared tools disproportionate to the stated purpose
    tools = {str(t).lower() for t in (allowed_tools or [])}
    if tools & {"bash", "shell", "execute", "run", "terminal", "computer"}:
        if not security_subject and not re.search(
            r"\b(?:script|command|shell|terminal|build|deploy|install|test|run|"
            r"compile|docker|git|ci\b|pipeline|automat)", text, re.I
        ):
            v.findings.append(Finding(
                "unjustified_shell", 2.5,
                "declares shell access with no stated need for it"))
            v.score += 2.5

    severe_hit = any(f.rule in {r[0] for r in SEVERE} for f in v.findings)
    if (hidden or override) and v.score >= 10.0:
        v.level = CRITICAL
    elif severe_hit and _corroborated(v):
        # Deliberately capped at HIGH rather than CRITICAL. Only the
        # unambiguous markers above — invisible Unicode, explicit instruction
        # override — remove a skill from the index, because only those have no
        # legitimate reading. A severe construct plus a capability is genuinely
        # risky and sinks to a 0.40 multiplier, but the last four such cases in
        # a 40,000-skill sample were a Coolify operator, a backup-recovery
        # runbook, a threat-hunting playbook and a block-storage guide: all
        # plausibly doing exactly what they say. Removing those to catch an
        # attacker who could rephrase anyway is a bad trade.
        v.level = HIGH
    else:
        v.level = next((lvl for cut, lvl in THRESHOLDS if v.score >= cut), NONE)
    return v


def _corroborated(v: "Verdict") -> bool:
    """True when something beyond the severe construct itself points at intent."""
    severe_names = {r[0] for r in SEVERE}
    others = {c for c in v.capabilities if c not in severe_names}
    return bool(others) or len({f.rule for f in v.findings} & severe_names) > 1


def penalty(level: str) -> float:
    """Multiplier applied to a skill's quality score.

    Multiplicative, like the other trust penalties: a risky skill should not be
    able to climb back past a safe one by being well-written. `critical` is
    excluded from search entirely rather than scored.
    """
    return {NONE: 1.0, LOW: 0.95, MEDIUM: 0.75, HIGH: 0.40, CRITICAL: 0.0}[level]


# Optional Rust acceleration for the prefilter. Absent, everything below runs
# in pure Python and produces identical verdicts — the Python path stays the
# reference implementation, and a test asserts the two agree.
try:                                            # pragma: no cover
    import skill_engine_rs as _rs
except ImportError:                             # pragma: no cover
    _rs = None

# Every pattern whose presence could raise a score above NONE. Used only as a
# gate: a document matching none of these cannot be flagged by any rule that
# needs text, so the expensive per-document inspection can skip it.
# Rust's `regex` rejects look-around — that is how it guarantees linear time —
# and one pattern here uses a negative lookahead to exclude private IP ranges.
#
# Stripping a look-around always *broadens* a pattern, which is precisely what
# a gate needs. The governing rule: the gate may be over-inclusive but never
# under-inclusive. Admitting a document that turns out clean costs one
# inspection; skipping one that was not clean is a miss, and a gate that can
# miss is worse than no gate because it looks like it works.
LOOKAROUND_OPEN = re.compile(r"\(\?[=!]|\(\?<[=!]")


def _strip_lookaround(pattern: str) -> str:
    """Remove look-around groups, yielding a strictly broader pattern."""
    out, i = [], 0
    while i < len(pattern):
        m = LOOKAROUND_OPEN.match(pattern, i)
        if not m:
            out.append(pattern[i])
            i += 1
            continue
        # Skip to the matching close paren, honouring nesting and escapes.
        depth, j = 1, m.end()
        while j < len(pattern) and depth:
            ch = pattern[j]
            if ch == "\\":
                j += 2
                continue
            depth += (ch == "(") - (ch == ")")
            j += 1
        i = j
    return "".join(out)


def _gate_patterns() -> list[str] | None:
    """Patterns for the accelerated gate, or None if it cannot be made safe.

    Returning None rather than a partial set is deliberate: a gate missing one
    rule silently stops detecting whatever that rule caught.
    """
    groups = [HIDDEN_CHARS, OVERRIDE, CONCEALMENT, INLINE_SECRET,
              SENSITIVE_READ, NETWORK_EGRESS, SUSPICIOUS_HOST, DESTRUCTIVE,
              PERSISTENCE, OBFUSCATION]
    raw = [g.pattern for g in groups] + [rx.pattern for _, rx, _ in SEVERE]
    return [_strip_lookaround(p) if LOOKAROUND_OPEN.search(p) else p
            for p in raw]


# The one finding that fires with no textual match at all: shell access
# declared by a skill whose text never justifies it. A text gate would skip
# exactly those documents, so they are admitted on their tools instead.
SHELL_TOOLS = {"bash", "shell", "execute", "run", "terminal", "computer"}


def _needs_inspection(rows: list[Any]) -> list[int] | None:
    """Indices worth inspecting in full, or None if no accelerator is present.

    Measured on the real corpus: 99.4% of skills match no safety pattern, and
    `RegexSet::is_match` short-circuits, so this gate ran at 1.19M docs/sec
    against 187/sec for the Python equivalent. The saving is not the matching
    itself but the 99.4% of documents that never reach it.
    """
    if _rs is None:
        return None
    patterns = _gate_patterns()
    if patterns is None:
        return None
    try:
        matcher = _rs.Matcher(patterns)
    except ValueError as exc:
        # Any pattern the accelerator cannot compile falls back rather than
        # silently narrowing what gets inspected.
        log.warning("gate unavailable (%s); running the unaccelerated path", exc)
        return None
    texts = [f"{r['name'] or ''}\n{r['description'] or ''}\n{r['body'] or ''}"
             for r in rows]
    candidates = set(matcher.interesting(texts))
    for i, row in enumerate(rows):
        if i in candidates:
            continue
        try:
            tools = {str(t).lower() for t in json.loads(row["allowed_tools"] or "[]")}
        except Exception:
            continue
        if tools & SHELL_TOOLS:
            candidates.add(i)
    return sorted(candidates)


def assess_corpus(store, *, batch: int = 5000) -> dict[str, Any]:
    """Inspect every skill and record its verdict.

    Run as a batch stage rather than at parse time: inspection costs about a
    millisecond per skill, which is immaterial for one file and an hour for
    four million. `release.py` runs it once over the finished corpus, where the
    cost is paid alongside ranking and categorisation.
    """
    import collections

    counts: collections.Counter = collections.Counter()
    pending: list[tuple[str, str, int]] = []
    flagged_examples: list[dict[str, Any]] = []

    all_rows = store.db.execute(
        "SELECT id, name, description, body, allowed_tools, path, repo FROM skills"
    ).fetchall()

    # With the accelerator present, only the gated candidates are inspected and
    # everything else is recorded clean without running the rules. Without it,
    # every row is inspected — same result, more time.
    gated = _needs_inspection(all_rows)
    if gated is not None:
        # Clean rows are *not* written. `risk_level` defaults to 'none', and
        # measured on 30,000 skills, writing that default to the 29,620 clean
        # rows cost 4.3 seconds — 40% of the whole stage — against 0.41s to
        # inspect the 321 that mattered. Once matching was accelerated, the
        # write became the bottleneck.
        #
        # The one case that still needs a write is a *re*-assessment: a skill
        # previously flagged and now clean must be reset, or a stale verdict
        # outlives the rule that produced it. That is a single statement over a
        # small exclusion list rather than tens of thousands of updates.
        interesting = set(gated)
        counts[NONE] += len(all_rows) - len(interesting)
        rows = [all_rows[i] for i in gated]
    else:
        rows = all_rows

    for row in rows:
        try:
            tools = json.loads(row["allowed_tools"] or "[]")
        except Exception:
            tools = []
        v = inspect(row["name"] or "", row["description"] or "",
                    row["body"] or "", tools, row["path"] or "")
        counts[v.level] += 1
        pending.append((v.level, v.as_json() if v.level != NONE else None,
                        row["id"]))
        if v.level in (HIGH, CRITICAL) and len(flagged_examples) < 50:
            flagged_examples.append({
                "repo": row["repo"], "name": row["name"], "level": v.level,
                "rules": [f.rule for f in v.findings],
            })
        if len(pending) >= batch:
            store.db.executemany(
                "UPDATE skills SET risk_level = ?, risk_detail = ? WHERE id = ?",
                pending)
            store.commit()
            pending.clear()

    if pending:
        store.db.executemany(
            "UPDATE skills SET risk_level = ?, risk_detail = ? WHERE id = ?",
            pending)
        store.commit()

    if gated is not None:
        # Clear verdicts left by an earlier run on skills now assessed clean.
        flagged_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(flagged_ids)) or "NULL"
        store.db.execute(
            f"UPDATE skills SET risk_level = 'none', risk_detail = NULL "
            f"WHERE risk_level != 'none' AND id NOT IN ({placeholders})",
            flagged_ids)
        store.commit()

    return {"counts": dict(counts), "flagged": flagged_examples}
