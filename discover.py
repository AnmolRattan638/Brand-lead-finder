"""
Discovery layer: finds high-intent leads across the public web.

Two mechanisms, both configured in sources.yaml (add any public site there):

  1. SEARCH   Queries run through search APIs (Tavily, Exa, Brave: use whichever
              keys you set). This is how we reach LinkedIn/Reddit/X/job-board posts
              without logging in to or automating those platforms.
  2. PAGES    Public pages we watch directly (accelerator cohorts, job boards,
              directories). Static pages use requests; JavaScript pages can use
              Playwright (optional). Always respects robots.txt.

Not supported on purpose: logging into platforms, captcha solving, stealth or
proxy rotation. Sites that require those are out of reach; use their official
alerts or manual_leads.csv for them.

Env keys (all optional, set at least one for search):
  TAVILY_API_KEY, EXA_API_KEY, BRAVE_API_KEY
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    import yaml
    from bs4 import BeautifulSoup
except ImportError:
    requests = yaml = BeautifulSoup = None

CONFIG = Path(__file__).with_name("sources.yaml")
UA = "Mozilla/5.0 (compatible; LeadFinder/1.0)"
NAV_WORDS = {"login", "log in", "sign in", "sign up", "signup", "register", "about", "about us", "contact",
             "contact us", "privacy", "terms", "home", "blog", "careers", "faq", "pricing", "menu", "search",
             "read more", "learn more", "apply", "next", "previous", "more", "skip to content"}


def _item(source, title, url, text, company="", pre=None, website=""):
    return dict(source=source, title=title.strip(), company=company.strip(), url=url, text=text.strip(),
                website=website, contact="", pre=pre)


def load_config(path=CONFIG):
    if yaml is None:
        sys.exit("Run: pip install pyyaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------- search providers ----------
def _recency(days):
    if days <= 1:
        return "day", "pd"
    if days <= 7:
        return "week", "pw"
    return "month", "pm"


def _tavily(q, domains, n, days):
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    body = {"query": q, "max_results": n, "search_depth": "basic", "time_range": _recency(days)[0]}
    if domains:
        body["include_domains"] = domains
    r = requests.post("https://api.tavily.com/search", json=body,
                      headers={"Authorization": f"Bearer {key}"}, timeout=40)
    r.raise_for_status()
    return [(x.get("title", ""), x.get("url", ""), x.get("content", "")) for x in r.json().get("results", [])]


def _exa(q, domains, n, days):
    key = os.environ.get("EXA_API_KEY")
    if not key:
        return []
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")
    body = {"query": q, "numResults": n, "type": "auto", "startPublishedDate": start,
            "contents": {"text": {"maxCharacters": 1200}}}
    if domains:
        body["includeDomains"] = domains
    r = requests.post("https://api.exa.ai/search", json=body, headers={"x-api-key": key}, timeout=40)
    r.raise_for_status()
    return [(x.get("title", "") or "", x.get("url", ""), x.get("text", "") or "") for x in r.json().get("results", [])]


def _brave(q, domains, n, days):
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    if domains:
        q = "(" + " OR ".join(f"site:{d}" for d in domains) + ") " + q
    r = requests.get("https://api.search.brave.com/res/v1/web/search",
                     params={"q": q, "count": min(n, 20), "freshness": _recency(days)[1]},
                     headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=40)
    r.raise_for_status()
    return [(x.get("title", ""), x.get("url", ""), x.get("description", ""))
            for x in r.json().get("web", {}).get("results", [])]


PROVIDERS = [("tavily", _tavily), ("exa", _exa), ("brave", _brave)]


def search_providers_available():
    return [n for n, _ in PROVIDERS if os.environ.get(f"{n.upper()}_API_KEY")]


def run_searches(cfg):
    out, seen = [], set()
    days = int(cfg.get("freshness_days", 14))
    per = int(cfg.get("results_per_query", 10))
    if not search_providers_available():
        print("[info] no search API key set (TAVILY_API_KEY / EXA_API_KEY / BRAVE_API_KEY), skipping search")
        return out
    for entry in cfg.get("searches", []) or []:
        if entry.get("enabled", True) is False:
            continue
        q, domains, label = entry["query"], entry.get("domains") or [], entry.get("name", "search")
        for pname, fn in PROVIDERS:
            try:
                for title, url, text in fn(q, domains, per, days):
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    out.append(_item(f"Search:{label}", title or url, url, f"{title}. {text}"))
            except Exception as ex:
                print(f"[warn] {pname} failed for '{label}': {ex}", file=sys.stderr)
    return out


# ---------- page watcher ----------
def _render(url):
    """Playwright fetch for JavaScript-heavy public pages. Returns HTML or ''."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"[info] Playwright not installed, skipping JS page {url} "
              "(pip install playwright && playwright install chromium)")
        return ""
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(user_agent=UA)
            page.goto(url, wait_until="networkidle", timeout=45000)
            html = page.content()
            b.close()
            return html
    except Exception as ex:
        print(f"[warn] render failed for {url}: {ex}", file=sys.stderr)
        return ""


STATE_FILE = Path("seen_pages.json")  # remembers what each watched page already listed


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _origin(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"


def extract_links(html, base_url, max_items=100, link_regex=None, external_only=False):
    """Return [(name, absolute_url, context_text)] for content-like links."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        t.decompose()
    base_host = urlparse(base_url).netloc.replace("www.", "")
    rx = re.compile(link_regex) if link_regex else None
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        host = urlparse(url).netloc.replace("www.", "")
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        if rx and not rx.search(url):
            continue
        if external_only and (host == base_host or host.endswith("." + base_host)):
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if len(text) > 120:  # whole card wrapped in the link: prefer its heading, else the first words
            h = a.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
            htext = re.sub(r"\s+", " ", h.get_text(" ", strip=True)) if h else ""
            text = htext if 2 < len(htext) <= 80 else text[:80].rsplit(" ", 1)[0]
        if not (2 < len(text) <= 120) or text.lower() in NAV_WORDS:
            continue
        seen.add(url)
        parent = a.find_parent(["li", "article", "tr", "div"]) or a
        ctx = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))[:500]
        out.append((text, url, ctx))
        if len(out) >= max_items:
            break
    return out


def _fetch_page(page, url, enrich):
    if page.get("render"):
        return _render(url) if enrich.allowed(url) else ""
    return enrich.fetch(url)


def run_pages(cfg):
    import enrich  # reuse the polite, robots-aware fetcher

    state = _load_state()
    out = []
    for page in cfg.get("pages", []) or []:
        if page.get("enabled", True) is False:
            continue
        name, url, mode = page.get("name", "page"), page["url"], page.get("mode", "classify")

        urls = [url]
        if page.get("page_template"):
            base = url.rstrip("/")
            urls += [page["page_template"].format(url=base, n=i) for i in range(2, int(page.get("pages", 1)) + 1)]

        links, complete = [], True
        for i, u in enumerate(urls):
            html = _fetch_page(page, u, enrich)
            if not html:
                complete = False  # any failed page makes the baseline unreliable
                break
            links += extract_links(html, u, int(page.get("max_items", 100)), page.get("link_regex"),
                                   bool(page.get("external_only")))
        if not links:
            print(f"[info] {name}: nothing fetched (blocked, empty or robots.txt disallows)")
            continue
        uniq = {}
        for l in links:
            uniq.setdefault(l[1], l)
        links = list(uniq.values())

        first_time = name not in state
        known = set(state.get(name, []))
        if not complete:
            print(f"[info] {name}: partial crawl, skipping so the baseline stays accurate")
            continue
        if first_time:
            fresh = links if page.get("emit_initial") else []
        else:
            fresh = [l for l in links if l[1] not in known]
        state[name] = sorted(known | {l[1] for l in links})

        kws = page.get("require_keywords") or []
        if kws:  # whole-word match (plural ok) so "tea" does not match "team"
            rx = re.compile(r"\b(?:" + "|".join(re.escape(str(k).lower()) for k in kws) + r")s?\b")
            fresh = [l for l in fresh if rx.search((l[0] + " " + l[2]).lower())]

        for text, link, ctx in fresh:
            site = _origin(link) if page.get("external_only") else ""
            if mode == "trigger":
                pre = {"score": int(page.get("base_score", 55)), "signal": page.get("signal", name)}
                out.append(_item(f"Page:{name}", text, link, f"{text}. {ctx}", company=text, pre=pre, website=site))
            else:
                out.append(_item(f"Page:{name}", text, link, f"{text}. {ctx}", website=site))
        print(f"[info] {name}: {len(links)} listed, {len(fresh)} new"
              + (" (baseline recorded)" if first_time and not page.get("emit_initial") else ""))

    try:
        STATE_FILE.write_text(json.dumps(state, indent=0), encoding="utf-8")
    except OSError as ex:
        print(f"[warn] could not save {STATE_FILE}: {ex}", file=sys.stderr)
    return out


def run_discovery(cfg_path=CONFIG):
    if requests is None or BeautifulSoup is None:
        sys.exit("Run: pip install -r requirements.txt")
    cfg = load_config(cfg_path)
    return run_searches(cfg) + run_pages(cfg)
