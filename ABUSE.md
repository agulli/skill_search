# Security & Abuse Mitigation Architecture

## 1. Overview & Threat Model

`skill-engine` indexes publicly available agent skills published across open-source repositories (GitHub, GitLab, and Hugging Face). The primary objectives of the defense architecture are:

1. **Service Availability & Reliability**: Protect the search API and web frontend from high-concurrency denial-of-service or exhaustive enumeration that degrades performance for normal users.
2. **Compute & Resource Protection**: Prevent CPU and memory exhaustion on lean container deployments running full-text search queries (SQLite FTS5) over large indices.
3. **Data Integrity & Safe Agent Interaction**: Ensure read-only safety when exposing search endpoints and MCP servers to autonomous agents and automated callers.

---

## 2. Multi-Layered Defense Strategy

| Layer | Mechanism | Scope & Mitigation |
|---|---|---|
| **Edge / CDN** | Cloudflare WAF & Bot Management | Mitigates volumetric DDoS, IP rotation attacks, and unauthenticated scrapers before reaching the origin. |
| **Network & IP Limiting** | Dynamic Token Bucket Limiter | Enforces per-IP cost-weighted rate limiting with burst allowance. |
| **Query & Pagination Caps** | Enforced bounds on `limit` and `offset` | Restricts deep offset pagination (`offset <= 1000`) and limits page size (`limit <= 50`). |
| **Filesystem & DB Safety** | Immutable SQLite Connections | Exposes databases in strict read-only mode (`SQLITE_OPEN_READONLY`) for public serving. |

---

## 3. Cost-Weighted Rate Limiting

Rather than charging a uniform cost per request, the rate limiter assigns costs proportional to database computation and data transfer overhead:

```python
# Rate limiter cost allocation
COSTS = {
    "/api/search": 3.0,      # FTS5 query + BM25 ranking + dynamic faceting
    "/api/browse": 3.0,      # Category scan + score sorting
    "/api/skill": 1.0,       # Point lookup by primary key
    "/api/categories": 0.5,  # Cached taxonomy structure
    "/": 0.25,               # Static landing page
    "/health": 0.0,          # Zero-cost health check (unthrottled)
    "/robots.txt": 0.0,      # Standard crawler directive
}
```

### Depth & Pagination Penalties
To deter automated scrapers from systematically dumping categories, request cost scales with pagination depth:

$$\text{Total Cost} = \left( \text{Base Cost} + \frac{\min(\text{offset}, 1000)}{200} \right) \times \left(1 + \frac{\min(\text{limit}, 50)}{100}\right)$$

### Burst vs. Sustained Rate
- **Burst Capacity (e.g., 10 tokens)**: Accommodates interactive UI features (e.g., live search debounced at 110ms) without throttling responsive user keystrokes.
- **Sustained Refill Rate (e.g., 1.5 tokens/sec)**: Bounds sustained scraper throughput over time.

---

## 4. Privacy-Preserving Telemetry

When requests exceed rate limits, the engine records structured warning logs without logging raw user IP addresses. Client identifiers are pseudonymized via a keyed hash:

```python
client_handle = hashlib.sha256(f"{client_ip}:{salt}".encode()).hexdigest()[:12]
log.warning("Rate limit exceeded for client [%s] on endpoint %s", client_handle, path)
```

This provides visibility into single-actor vs. distributed traffic anomalies while respecting client privacy.

---

## 5. Recommended Edge Deployment Configuration

For production deployments using Cloudflare in front of the application:

1. **WAF Rate Limiting Rule**:
   - Condition: `(http.request.uri.path contains "/api/")`
   - Threshold: 60 requests per minute per IP.
   - Action: Managed Challenge.
2. **Bot Fight Mode**: Enabled to block automated headless browsers and known malicious user agents.
3. **Static Asset Caching**: Cache HTML landing pages and static assets (`s-maxage=3600`) at Cloudflare edge nodes.
