"""HTTP layer: a JSON API and a search UI, on the standard library only.

Two things matter at 100k skills that did not at 100.

**Connection reuse.** The first version opened a fresh `Store` per request,
which re-ran the schema script and the migration check against a 2.5GB database
every time — that alone was most of a 2-second response. Connections are now
cached per thread (SQLite objects are not safe to share across threads, and
`ThreadingHTTPServer` runs handlers on many), which drops a query to well under
a tenth of a second.

**Filtering.** Twenty results out of a corpus this size is a lucky dip. The UI
is built around facets computed from the matched set — kind, licence, language —
plus quality, popularity and freshness thresholds, so the corpus is navigable
rather than merely searchable.

If you later want auth, CORS policy, or OpenAPI, swap this module for FastAPI
without touching anything else: `search.search()` is the seam.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .search import (browse, category_counts, count_matches, facet_counts,
                     get_skill, search)
from .taxonomy import category_tree
from .store import Store

log = logging.getLogger("skill_engine.serve")

_local = threading.local()


def get_store(db_path, read_only: bool = False) -> Store:
    """One SQLite connection per handler thread, opened once and kept.

    The cache is sized *per connection*, and `ThreadingHTTPServer` opens a
    thread per connection — so the configured cache is multiplied by however
    many clients are connected. At a 192MB cache and eight concurrent requests
    that reached 1.5GB resident and would have killed a 512MB machine.

    `SKILL_ENGINE_CACHE_MB` is therefore treated as a *total* budget, divided
    across the threads the server is willing to run. The loss is smaller than
    it sounds: for a read-only index the operating system's page cache does
    most of the work, and it is shared rather than duplicated per connection.
    """
    store = getattr(_local, "store", None)
    if store is None:
        budget = int(os.getenv("SKILL_ENGINE_CACHE_MB", "192"))
        workers = max(1, int(os.getenv("SKILL_ENGINE_MAX_WORKERS", "8")))
        per_thread = max(16, budget // workers)
        prev = os.environ.get("SKILL_ENGINE_CACHE_MB")
        os.environ["SKILL_ENGINE_CACHE_MB"] = str(per_thread)
        try:
            store = Store(db_path, read_only=read_only)
        finally:
            if prev is None:
                os.environ.pop("SKILL_ENGINE_CACHE_MB", None)
            else:
                os.environ["SKILL_ENGINE_CACHE_MB"] = prev
        _local.store = store
    return store


class RateLimiter:
    """Token bucket per client, refilled continuously.

    A public search endpoint runs full-text queries over a multi-gigabyte index,
    so an unthrottled client can consume the whole box with a loop. A CDN in
    front absorbs most abuse, but it caches by URL — a stream of *distinct*
    queries passes straight through to the origin, so the application needs its
    own limit regardless.

    Deliberately in-memory and per-process: no Redis, no coordination. It bounds
    what a single machine will do, which is the property that matters here.
    """

    def __init__(self, rate: float = 4.0, burst: int = 40, max_clients: int = 20_000):
        self.rate = rate            # sustained requests/second
        self.burst = burst          # bucket depth
        self.max_clients = max_clients
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str) -> tuple[bool, float]:
        """Returns (allowed, seconds to wait if not)."""
        now = time.monotonic()
        with self._lock:
            # Cheap bound on memory: a flood of unique IPs cannot grow this
            # without limit. Dropping the table costs at most one free burst.
            if len(self._buckets) > self.max_clients:
                self._buckets.clear()

            tokens, last = self._buckets.get(client, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[client] = (tokens, now)
                return False, (1.0 - tokens) / self.rate
            self._buckets[client] = (tokens - 1.0, now)
            return True, 0.0


def client_ip(handler, trust_proxy: bool) -> str:
    """The caller's address, taking proxy headers only when told to.

    `CF-Connecting-IP` and `X-Forwarded-For` are trivially forged by anyone
    talking to the origin directly, so honouring them unconditionally would let
    an attacker rotate their apparent identity and bypass the limiter entirely.
    They are trusted only when the deployment says it really is behind a proxy.
    """
    if trust_proxy:
        for header in ("CF-Connecting-IP", "X-Forwarded-For"):
            value = handler.headers.get(header)
            if value:
                return value.split(",")[0].strip()
    return handler.client_address[0]


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Agent Skills Search by AGI</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --bg:#fcfcfa; --panel:#fff; --fg:#1b1b19; --muted:#6d6d64; --faint:#93938a;
  --line:#e6e6df; --accent:#9a4f1b; --accent-soft:#f4e9e0; --ring:#c98a52;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#141417; --panel:#1b1b1f; --fg:#eaeae5; --muted:#9d9d94; --faint:#75756d;
  --line:#2b2b31; --accent:#e39a62; --accent-soft:#2a211a; --ring:#a3673a;
}}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:inherit}
header{position:sticky;top:0;z-index:20;background:var(--bg);
  border-bottom:1px solid var(--line)}
.bar{max-width:1120px;margin:0 auto;padding:14px 20px;display:flex;gap:14px;align-items:center}
.brand{font-weight:650;font-size:15px;letter-spacing:-.01em;white-space:nowrap}
.brand span{color:var(--muted);font-weight:400}
/* The corpus count is the first thing to go when the bar gets tight — the
   search box matters more than the statistic. */
@media(max-width:900px){.brand span{display:none}}
@media(max-width:560px){.brand{font-size:0;letter-spacing:0}
  .brand::before{content:"AGI";font-size:15px;letter-spacing:-.01em}}
.searchwrap{flex:1;position:relative}
#q{width:100%;padding:10px 40px 10px 14px;border:1px solid var(--line);border-radius:9px;
  background:var(--panel);color:var(--fg);font-size:15px}
#q:focus{outline:2px solid var(--ring);outline-offset:-1px;border-color:transparent}
.slash{position:absolute;right:10px;top:50%;transform:translateY(-50%);
  color:var(--faint);font-size:11px;border:1px solid var(--line);border-radius:4px;
  padding:1px 5px;pointer-events:none}
main{max-width:1120px;margin:0 auto;padding:20px;display:grid;
  grid-template-columns:230px 1fr;gap:26px;align-items:start}
@media(max-width:820px){main{grid-template-columns:1fr}aside{order:2}}
aside{position:sticky;top:74px;font-size:13px}
.fgroup{margin-bottom:18px}
.fgroup h3{margin:0 0 7px;font-size:11px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--faint);font-weight:600}
.fopt{display:flex;justify-content:space-between;gap:8px;padding:3px 6px;border-radius:5px;
  cursor:pointer;color:var(--muted)}
.fopt:hover{background:var(--accent-soft);color:var(--fg)}
.fopt.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
.fopt .n{color:var(--faint);font-variant-numeric:tabular-nums;font-size:12px}
select,input[type=number]{width:100%;padding:6px 8px;border:1px solid var(--line);
  border-radius:6px;background:var(--panel);color:var(--fg);font-size:13px}
.row{display:flex;gap:8px}
.meta{color:var(--muted);font-size:13px;margin:0 0 12px;
  display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.clear{color:var(--accent);cursor:pointer;font-size:12px}
.hit{padding:14px 0;border-top:1px solid var(--line);cursor:pointer}
.hit:first-child{border-top:0}
.hit:hover .title{color:var(--accent)}
.title{font-size:15px;font-weight:620;margin:0 0 3px;letter-spacing:-.005em}
.repo{font-size:12.5px;color:var(--muted);margin:0 0 6px}
.desc{margin:0 0 7px;font-size:13.5px;color:var(--fg)}
.snip{color:var(--muted);font-size:12.5px;margin:0 0 7px}
.snip b{color:var(--accent);font-weight:600}
.tags{display:flex;flex-wrap:wrap;gap:5px}
.tag{font-size:11.5px;color:var(--muted);border:1px solid var(--line);
  border-radius:999px;padding:1px 8px;white-space:nowrap}
.tag.q{color:var(--accent);border-color:var(--ring)}
.tag.a{color:var(--muted);border-style:dashed}
.authorbox{border:1px solid var(--line);border-radius:9px;padding:12px 14px;margin:0 0 16px;
  background:var(--bg)}
.authorbox h4{margin:0 0 8px;font-size:13px;display:flex;justify-content:space-between;
  align-items:baseline;gap:10px}
.authorbox h4 em{font-style:normal;color:var(--accent);font-variant-numeric:tabular-nums}
.afacts{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);margin:0 0 9px}
.afacts b{color:var(--fg);font-weight:600}
.more{margin:18px 0 0;padding:9px;width:100%;border:1px solid var(--line);
  border-radius:8px;background:var(--panel);color:var(--fg);cursor:pointer;font-size:13px}
.more:hover{border-color:var(--ring)}
.empty,.hint{color:var(--muted);padding:44px 0;text-align:center}
/* Directory: the landing view when nothing has been typed. */
.dirhead{margin:2px 0 18px}
.dirhead h1{font-size:21px;letter-spacing:-.015em;margin:0 0 4px}
.dirhead p{color:var(--muted);font-size:14px;margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(228px,1fr));gap:12px}
.cat{border:1px solid var(--line);border-radius:11px;padding:14px 15px;cursor:pointer;
  background:var(--panel);transition:border-color .12s,transform .12s}
.cat:hover{border-color:var(--ring);transform:translateY(-1px)}
.cat .top{display:flex;align-items:baseline;gap:8px;margin-bottom:3px}
.cat .ico{font-size:17px;line-height:1}
.cat h3{font-size:14.5px;margin:0;font-weight:640;flex:1}
.cat .n{font-size:12px;color:var(--accent);font-variant-numeric:tabular-nums}
.cat p{margin:0 0 9px;font-size:12.5px;color:var(--muted);line-height:1.45}
.subs{display:flex;flex-wrap:wrap;gap:4px}
.sub{font-size:11.5px;color:var(--muted);border:1px solid var(--line);
  border-radius:999px;padding:1px 7px}
.sub:hover{border-color:var(--ring);color:var(--accent)}
.crumb{display:flex;align-items:center;gap:8px;margin:0 0 14px;font-size:13px;flex-wrap:wrap}
.crumb a{color:var(--accent);cursor:pointer;text-decoration:none}
.crumb .sep{color:var(--faint)}
.crumb h2{font-size:17px;margin:0;font-weight:640}
.subnav{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px}
.subnav .sub{cursor:pointer;padding:3px 10px;font-size:12.5px}
.subnav .sub.on{background:var(--accent-soft);color:var(--accent);border-color:var(--ring)}
.sortbar{display:flex;gap:10px;font-size:12.5px;color:var(--muted);margin:0 0 10px}
.sortbar span{cursor:pointer}
.sortbar span.on{color:var(--accent);font-weight:600}
.hint b{color:var(--fg)}
.ex{display:inline-block;margin:4px;padding:3px 10px;border:1px solid var(--line);
  border-radius:999px;font-size:12.5px;cursor:pointer;color:var(--muted)}
.ex:hover{border-color:var(--ring);color:var(--accent)}
#drawer{position:fixed;inset:0 0 0 auto;width:min(720px,100%);background:var(--panel);
  border-left:1px solid var(--line);z-index:40;overflow-y:auto;padding:22px 26px;
  box-shadow:-14px 0 40px rgba(0,0,0,.13);transform:translateX(100%);
  transition:transform .16s ease}
#drawer.open{transform:none}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.32);z-index:39;display:none}
#scrim.open{display:block}
#drawer h2{margin:0 0 4px;font-size:19px;letter-spacing:-.01em}
#drawer .sub{color:var(--muted);font-size:13px;margin:0 0 14px}
#drawer pre{white-space:pre-wrap;word-wrap:break-word;font:12.5px/1.6 ui-monospace,
  SFMono-Regular,Menlo,monospace;background:var(--bg);border:1px solid var(--line);
  border-radius:8px;padding:14px;max-height:none;overflow-x:auto}
.close{position:absolute;top:16px;right:20px;cursor:pointer;color:var(--muted);
  font-size:22px;line-height:1}
.close:hover{color:var(--fg)}
.kv{display:grid;grid-template-columns:120px 1fr;gap:4px 12px;font-size:13px;margin:0 0 16px}
.kv dt{color:var(--faint)}
.kv dd{margin:0;word-break:break-word}
.bars{margin:0 0 16px}
.barrow{display:grid;grid-template-columns:110px 1fr 42px;gap:8px;align-items:center;
  font-size:12px;margin-bottom:4px}
.bartrack{height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.barfill{height:100%;background:var(--accent)}
.spin{display:inline-block;width:11px;height:11px;border:2px solid var(--line);
  border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>
<header><div class="bar">
  <div class="brand">Agent Skills Search by AGI <span id="corpus"></span></div>
  <div class="searchwrap">
    <input id="q" type="search" autocomplete="off" spellcheck="false"
           placeholder="Describe what you need — e.g. extract tables from a PDF invoice">
    <span class="slash">/</span>
  </div>
</div></header>
<main>
  <aside id="facets"></aside>
  <section>
    <div class="meta" id="meta"></div>
    <div id="results"></div>
  </section>
</main>
<div id="scrim"></div>
<div id="drawer"><span class="close" id="closeBtn">&times;</span><div id="detail"></div></div>
<script>
const $=s=>document.querySelector(s);
const EX=["extract tables from a pdf invoice","write terraform modules for aws",
  "review a react component for accessibility","summarise a research paper",
  "build a slide deck","analyse a spreadsheet and chart it"];
let state={q:"",limit:20,kind:"",license:"",language:"",min_stars:0,min_score:0,
           max_age_days:0,forks:0,cat:"",sub:"",sort:"quality",offset:0};
let CATS=null;
let inflight=null;

const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const hl=s=>esc(s).replace(/\[([^\]]{1,60})\]/g,"<b>$1</b>");
const num=n=>n>=1000?(n/1000).toFixed(n>=10000?0:1)+"k":String(n);

function qs(extra={}){
  const p=new URLSearchParams({q:state.q,limit:state.limit});
  for(const k of ["kind","license","language","min_stars","min_score","max_age_days","forks"])
    if(state[k]) p.set(k,state[k]);
  for(const [k,v] of Object.entries(extra)) p.set(k,v);
  return p.toString();
}

function syncURL(){
  const u=new URL(location);
  if(state.q) u.search=qs();
  else if(state.cat){ const p=new URLSearchParams({c:state.cat});
    if(state.sub) p.set("sub",state.sub); u.search=p.toString(); }
  else u.search="";
  history.replaceState({},"",u);
}

async function renderDirectory(){
  $("#facets").innerHTML=""; $("#meta").innerHTML="";
  if(!CATS){ CATS=await (await fetch("/api/categories")).json(); }
  const cards=CATS.categories.map(c=>`
    <div class="cat" data-cat="${esc(c.id)}">
      <div class="top"><span class="ico">${c.icon}</span>
        <h3>${esc(c.label)}</h3><span class="n">${num(c.count)}</span></div>
      <p>${esc(c.blurb)}</p>
      <div class="subs">${c.subs.slice(0,4).map(s=>
        `<span class="sub">${esc(s.label)} ${num(s.count)}</span>`).join("")}</div>
    </div>`).join("");
  $("#results").innerHTML=
    `<div class="dirhead"><h1>Browse the directory</h1>
       <p>${CATS.total.toLocaleString()} skills, sorted into ${CATS.categories.length} subjects.
          Or search above if you already know what you want.</p></div>
     <div class="grid">${cards}</div>`;
}

async function renderCategory(){
  const cat=CATS.categories.find(c=>c.id===state.cat);
  if(!cat) return renderDirectory();
  const p=new URLSearchParams({c:state.cat,limit:state.limit,offset:state.offset,sort:state.sort});
  if(state.sub) p.set("sub",state.sub);
  $("#meta").innerHTML='<span class="spin"></span>';
  const d=await (await fetch("/api/browse?"+p)).json();

  const subnav=cat.subs.map(s=>
    `<span class="sub ${state.sub===s.id?"on":""}" data-sub="${esc(s.id)}">${esc(s.label)} ${num(s.count)}</span>`).join("");
  const sorts=["quality","stars","recent","name"].map(k=>
    `<span class="${state.sort===k?"on":""}" data-sort="${k}">${k}</span>`).join("");

  $("#facets").innerHTML="";
  $("#meta").innerHTML=`<span><b>${d.total.toLocaleString()}</b> skills</span><span>${d.took_ms} ms</span>`;
  $("#results").innerHTML=
    `<div class="crumb"><a data-home="1">Directory</a><span class="sep">›</span>
       <h2>${cat.icon} ${esc(cat.label)}</h2></div>
     <div class="subnav"><span class="sub ${state.sub?"":"on"}" data-sub="">All ${num(cat.count)}</span>${subnav}</div>
     <div class="sortbar"><span style="cursor:default">sort:</span>${sorts}</div>
     ${d.results.map(hitCard).join("")}
     ${d.results.length>=state.limit?'<button class="more" id="more">Show more</button>':""}`;
}

function hitCard(r){
  const tags=[`<span class="tag q">q${Math.round(r.quality)}</span>`,
    r.author_score!=null?`<span class="tag a">author ${Math.round(r.author_score)}</span>`:"",
    `<span class="tag">${num(r.stars)}★</span>`,
    r.license?`<span class="tag">${esc(r.license)}</span>`:"",
    r.duplicates?`<span class="tag">${r.duplicates} copies</span>`:""].join("");
  return `<article class="hit" data-id="${r.id}">
    <p class="title">${esc(r.name)}</p>
    <p class="repo">${esc(r.repo)} · ${esc(r.path)}</p>
    <p class="desc">${esc((r.description||"").slice(0,240))}</p>
    <div class="tags">${tags}</div></article>`;
}

async function run(){
  if(!state.q.trim()){
    if(state.cat) return renderCategory();
    return renderDirectory();
  }
  if(false){
    return;
  }
  $("#meta").innerHTML='<span class="spin"></span>';
  if(inflight) inflight.abort();
  inflight=new AbortController();
  let d;
  try{ d=await (await fetch("/api/search?"+qs({facets:1}),{signal:inflight.signal})).json(); }
  catch(e){ if(e.name==="AbortError") return; $("#meta").textContent="search failed"; return; }
  render(d); syncURL();
}

function render(d){
  const active=["kind","license","language","min_stars","min_score","max_age_days"]
    .filter(k=>state[k]);
  $("#meta").innerHTML=
    `<span><b>${d.total.toLocaleString()}</b> matches · showing ${d.count}</span>`+
    `<span>${d.took_ms} ms</span>`+
    (active.length?`<span class="clear" id="clr">clear ${active.length} filter${active.length>1?"s":""}</span>`:"");

  $("#results").innerHTML = d.results.length
    ? d.results.map(r=>{
        const tags=[`<span class="tag q">q${Math.round(r.quality)}</span>`,
          r.author_score!=null?`<span class="tag a">author ${Math.round(r.author_score)}</span>`:"",
          `<span class="tag">${num(r.stars)}★</span>`,
          r.kind?`<span class="tag">${esc(r.kind)}</span>`:"",
          r.license?`<span class="tag">${esc(r.license)}</span>`:"",
          r.duplicates?`<span class="tag">${r.duplicates} copies</span>`:"",
          r.resources.length?`<span class="tag">${r.resources.length} files</span>`:""
        ].join("");
        return `<article class="hit" data-id="${r.id}">
          <p class="title">${esc(r.name)}</p>
          <p class="repo">${esc(r.repo)} · ${esc(r.path)}</p>
          <p class="desc">${esc((r.description||"").slice(0,240))}</p>
          ${r.snippet?`<p class="snip">${hl(r.snippet)}</p>`:""}
          <div class="tags">${tags}</div></article>`;
      }).join("") + (d.count>=state.limit && d.count<d.total
        ? `<button class="more" id="more">Show more (${d.count} of ${d.total.toLocaleString()})</button>`:"")
    : '<p class="empty">No skills matched. Try fewer words, or clear the filters.</p>';

  renderFacets(d.facets||{});
}

function renderFacets(f){
  const g=[];
  const group=(title,key,items)=>{
    if(!items||!items.length) return "";
    return `<div class="fgroup"><h3>${title}</h3>`+items.map(([v,n])=>
      `<div class="fopt ${state[key]===v?"on":""}" data-facet="${key}" data-val="${esc(v)}">
         <span>${esc(v)}</span><span class="n">${n}</span></div>`).join("")+`</div>`;
  };
  g.push(group("Location","kind",f.kind));
  g.push(group("Licence","license",f.license));
  g.push(group("Language","language",f.language));
  g.push(`<div class="fgroup"><h3>Thresholds</h3>
    <div class="row" style="margin-bottom:6px">
      <input type="number" id="min_stars" placeholder="min ★" value="${state.min_stars||""}">
      <input type="number" id="min_score" placeholder="min q" value="${state.min_score||""}">
    </div>
    <select id="max_age_days">
      <option value="">any age</option>
      <option value="30">pushed &lt; 30 days</option>
      <option value="90">pushed &lt; 90 days</option>
      <option value="365">pushed &lt; 1 year</option>
    </select></div>`);
  $("#facets").innerHTML=g.join("");
  const sel=$("#max_age_days"); if(sel) sel.value=state.max_age_days||"";
}

async function openSkill(id){
  $("#scrim").classList.add("open"); $("#drawer").classList.add("open");
  $("#detail").innerHTML='<p class="sub"><span class="spin"></span> loading…</p>';
  const s=await (await fetch("/api/skill/"+id)).json();
  const login=(s.repo||"").split("/")[0];
  let a=null; try{ const r=await fetch("/api/author/"+encodeURIComponent(login));
    if(r.ok) a=await r.json(); }catch(e){}
  const fam=(s.score_families||{});
  const bars=Object.entries(fam).map(([k,v])=>
    `<div class="barrow"><span>${esc(k)}</span>
      <span class="barrack bartrack"><span class="barfill" style="width:${Math.round((v||0)*100)}%"></span></span>
      <span>${v==null?"—":v.toFixed(2)}</span></div>`).join("");
  $("#detail").innerHTML=`
    <h2>${esc(s.name)}</h2>
    <p class="sub">${esc(s.repo)} · ${esc(s.path)}</p>
    <dl class="kv">
      <dt>Quality</dt><dd>${(s.score||0).toFixed(1)} / 100</dd>
      <dt>Stars</dt><dd>${(s.stars||0).toLocaleString()}</dd>
      <dt>Licence</dt><dd>${esc(s.license||"none")}</dd>
      <dt>Location</dt><dd>${esc(s.source_kind||"")}</dd>
      ${s.allowed_tools?.length?`<dt>Tools</dt><dd>${esc(s.allowed_tools.join(", "))}</dd>`:""}
      ${s.resources?.length?`<dt>Bundled</dt><dd>${esc(s.resources.join(", "))}</dd>`:""}
      <dt>Source</dt><dd><a href="${esc(s.url)}" target="_blank" rel="noopener">view on GitHub ↗</a></dd>
    </dl>
    ${a?authorPanel(a):""}
    ${bars?`<div class="bars">${bars}</div>`:""}
    <pre>${esc(s.body||"(no body)")}</pre>`;
}
function authorPanel(a){
  const f=(a.breakdown&&a.breakdown.facts)||{};
  const fam=(a.breakdown&&a.breakdown.families)||{};
  const pct=v=>v==null?"—":Math.round(v*100)+"%";
  const rows=[["craft",fam.craft],["originality",fam.originality],["reach",fam.reach]]
    .map(([k,v])=>`<div class="barrow"><span>${k}</span>
      <span class="bartrack"><span class="barfill" style="width:${Math.round((v||0)*100)}%"></span></span>
      <span>${v==null?"—":v.toFixed(2)}</span></div>`).join("");
  return `<div class="authorbox">
    <h4><span>Author · ${esc(a.login)}</span><em>${(a.author_score||0).toFixed(1)}</em></h4>
    <div class="afacts">
      <span><b>${(f.skills||0).toLocaleString()}</b> skills</span>
      <span><b>${pct(f.originality)}</b> original</span>
      <span><b>${(f.repos_with_skills||0)}</b> repos</span>
      <span><b>${(f.total_stars||0).toLocaleString()}</b>★ total</span>
      ${f.followers!=null?`<span><b>${f.followers.toLocaleString()}</b> followers</span>`:""}
    </div>${rows}</div>`;
}
function closeDrawer(){$("#scrim").classList.remove("open");$("#drawer").classList.remove("open");}

let t=null;
$("#q").addEventListener("input",e=>{
  state.q=e.target.value; state.limit=20; state.cat=""; state.sub="";
  // Server responds in 60-175ms, so a long debounce is now the
  // dominant delay rather than a protection against load.
  clearTimeout(t); t=setTimeout(run,110);
});
document.addEventListener("click",e=>{
  const ex=e.target.closest("[data-ex]");
  if(ex){$("#q").value=ex.dataset.ex; state.q=ex.dataset.ex; run(); return;}
  const f=e.target.closest("[data-facet]");
  if(f){const k=f.dataset.facet; state[k]=state[k]===f.dataset.val?"":f.dataset.val;
        state.limit=20; run(); return;}
  if(e.target.id==="more"){state.limit+=30; run(); return;}
  if(e.target.id==="clr"){["kind","license","language","min_stars","min_score","max_age_days"]
      .forEach(k=>state[k]=k.startsWith("min")||k.startsWith("max")?0:""); state.limit=20; run(); return;}
  const cat=e.target.closest("[data-cat]");
  if(cat){state.cat=cat.dataset.cat; state.sub=""; state.offset=0; state.limit=20; run(); return;}
  const sub=e.target.closest("[data-sub]");
  if(sub){state.sub=sub.dataset.sub; state.offset=0; state.limit=20; run(); return;}
  const so=e.target.closest("[data-sort]");
  if(so){state.sort=so.dataset.sort; state.limit=20; run(); return;}
  if(e.target.closest("[data-home]")){state.cat=""; state.sub=""; run(); return;}
  const hit=e.target.closest(".hit"); if(hit){openSkill(hit.dataset.id); return;}
  if(e.target.id==="scrim"||e.target.id==="closeBtn") closeDrawer();
});
document.addEventListener("change",e=>{
  if(["min_stars","min_score","max_age_days"].includes(e.target.id)){
    state[e.target.id]=e.target.value||0; state.limit=20; run();}
});
document.addEventListener("keydown",e=>{
  if(e.key==="/"&&document.activeElement!==$("#q")){e.preventDefault();$("#q").focus();}
  if(e.key==="Escape"){closeDrawer();}
});

(async()=>{
  const s=await (await fetch("/api/stats")).json();
  $("#corpus").textContent=`· ${s.valid_skills.toLocaleString()} skills from ${s.repos_with_skills.toLocaleString()} repos`;
  const cp=new URLSearchParams(location.search);
  if(cp.get("c")){ state.cat=cp.get("c"); state.sub=cp.get("sub")||""; }
  const p=new URLSearchParams(location.search);
  if(p.get("q")){ state.q=p.get("q"); $("#q").value=state.q;
    for(const k of ["kind","license","language","min_stars","min_score","max_age_days"])
      if(p.get(k)) state[k]=p.get(k);
  }
  run(); $("#q").focus();
})();
</script></body></html>"""


def make_handler(db_path, embedder_name: str, *, read_only: bool = False,
                 limiter: RateLimiter | None = None, trust_proxy: bool = False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "skill-engine"
        sys_version = ""          # do not advertise the Python version

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload) -> None:
            self._send(code, json.dumps(payload, default=str).encode(),
                       "application/json")

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _filters(self, p: dict) -> dict:
            def one(key, default=None):
                v = p.get(key, [default])[0]
                return v if v not in ("", None) else default
            return {
                "min_stars": one("min_stars", 0),
                "min_score": one("min_score", 0),
                "license": one("license"),
                "kind": one("kind"),
                "language": one("language"),
                "owner_type": one("owner_type"),
                "max_age_days": one("max_age_days"),
                "include_forks": one("forks", "0") == "1",
            }

        def do_GET(self):  # noqa: N802 (stdlib naming)
            import time as _t

            parsed = urlparse(self.path)

            # Liveness must answer even when the limiter is angry, or a health
            # check failing under load would take the machine out exactly when
            # it is busiest.
            if parsed.path == "/health":
                # Liveness, not readiness. The container must stay up when the
                # corpus is absent, or deployment deadlocks: the machine cannot
                # start without the database, and the database cannot be
                # uploaded without a running machine. The body reports whether
                # an index is actually loaded.
                ready = Path(str(db_path)).exists()
                body = b'{"ok":true,"index":true}' if ready else \
                    b'{"ok":true,"index":false,"detail":"no corpus at this path yet"}'
                return self._send(200, body, "application/json")

            if limiter is not None:
                ok, wait = limiter.allow(client_ip(self, trust_proxy))
                if not ok:
                    self.send_response(429)
                    self.send_header("Retry-After", str(max(1, int(wait) + 1)))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

            if not Path(str(db_path)).exists():
                # Everything except liveness needs the corpus.
                return self._json(503, {
                    "error": "index not loaded",
                    "detail": f"no database at {db_path}; upload one with "
                              "./deploy.sh data",
                })

            p = parse_qs(parsed.query)
            query = (p.get("q", [""])[0]).strip()
            limit = max(1, min(int(p.get("limit", ["20"])[0] or 20), 200))
            store = get_store(db_path, read_only)

            if parsed.path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")

            if parsed.path == "/api/search":
                if not query:
                    return self._json(400, {"error": "missing ?q="})
                filters = self._filters(p)
                t0 = _t.perf_counter()
                hits = search(store, query, limit=limit, filters=filters,
                              embedder_name=embedder_name)
                total = count_matches(store, query, filters)
                facets = (facet_counts(store, query, filters)
                          if p.get("facets") else {})
                return self._json(200, {
                    "query": query,
                    "count": len(hits),
                    "total": total,
                    "took_ms": round((_t.perf_counter() - t0) * 1000),
                    "facets": facets,
                    "results": [h.to_dict() for h in hits],
                })

            if parsed.path.startswith("/api/skill/"):
                try:
                    skill_id = int(parsed.path.rsplit("/", 1)[-1])
                except ValueError:
                    return self._json(400, {"error": "bad id"})
                data = get_skill(store, skill_id)
                if data:
                    # Ship the family breakdown, never the raw JSON blob — it is
                    # large, internal, and useless to a client either way.
                    raw = data.pop("score_detail", None)
                    data["score_families"] = {}
                    if raw:
                        try:
                            data["score_families"] = json.loads(raw).get("families", {})
                        except (json.JSONDecodeError, TypeError):
                            pass
                return self._json(200 if data else 404,
                                  data or {"error": "not found"})

            if parsed.path.startswith("/api/author/"):
                from .authors import get_author

                login = parsed.path.rsplit("/", 1)[-1]
                data = get_author(store, login)
                return self._json(200 if data else 404,
                                  data or {"error": "not found"})

            if parsed.path == "/api/categories":
                counts = category_counts(store)
                tree = []
                for cat in category_tree():
                    entry = counts.get(cat["id"], {"total": 0, "subs": {}})
                    if not entry["total"]:
                        continue
                    cat["count"] = entry["total"]
                    for sub in cat["subs"]:
                        sub["count"] = entry["subs"].get(sub["id"], 0)
                    cat["subs"] = [s for s in cat["subs"] if s["count"]]
                    cat["subs"].sort(key=lambda s: -s["count"])
                    tree.append(cat)
                tree.sort(key=lambda c: -c["count"])
                return self._json(200, {"categories": tree,
                                        "total": sum(c["count"] for c in tree)})

            if parsed.path == "/api/browse":
                category = p.get("c", [""])[0]
                if not category:
                    return self._json(400, {"error": "missing ?c="})
                sub = p.get("sub", [None])[0] or None
                offset = max(0, int(p.get("offset", ["0"])[0] or 0))
                sort = p.get("sort", ["quality"])[0]
                t0 = _t.perf_counter()
                hits, total = browse(store, category, sub, limit=limit,
                                     offset=offset, sort=sort,
                                     filters=self._filters(p))
                return self._json(200, {
                    "category": category, "subcategory": sub,
                    "count": len(hits), "total": total, "offset": offset,
                    "took_ms": round((_t.perf_counter() - t0) * 1000),
                    "results": [h.to_dict() for h in hits],
                })

            if parsed.path == "/api/stats":
                return self._json(200, store.stats())

            return self._json(404, {"error": "not found"})

    return Handler


def serve(db_path, host: str = "127.0.0.1", port: int = 8000,
          embedder_name: str = "none", *, read_only: bool | None = None,
          rate: float | None = None, trust_proxy: bool | None = None) -> None:
    """Run the UI and API.

    Public deployments should set `SKILL_ENGINE_PUBLIC=1`, which turns on the
    read-only database, the rate limiter, and trust of the proxy's client-IP
    header — the three things that differ between a laptop and the internet.
    """
    public = os.getenv("SKILL_ENGINE_PUBLIC", "").lower() in ("1", "true", "yes")
    read_only = public if read_only is None else read_only
    trust_proxy = public if trust_proxy is None else trust_proxy
    if rate is None:
        rate = float(os.getenv("SKILL_ENGINE_RATE", "4")) if public else 0.0
    limiter = RateLimiter(rate=rate) if rate > 0 else None

    # Thread count is deliberately *not* capped with a semaphore. A slot would
    # be held for a whole keep-alive connection rather than a single request,
    # so a handful of idle browsers would deadlock the server. The memory
    # ceiling is enforced by dividing the cache budget per connection instead,
    # which bounds the same thing without blocking anyone.
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(db_path, embedder_name, read_only=read_only,
                     limiter=limiter, trust_proxy=trust_proxy),
    )
    server.daemon_threads = True
    mode = "read-only" if read_only else "read-write"
    print(f"skill-engine serving on http://{host}:{port}  ({mode}"
          f"{f', {rate:g} req/s/client' if limiter else ''})")
    print(f"  UI    http://{host}:{port}/")
    print(f"  API   http://{host}:{port}/api/search?q=pdf&facets=1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
