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


def _item(source, title, url, text, company="", pre=None):
    return dict(source=source, title=title.strip(), company=company.strip(), url=url, text=text.strip(),
                website="", contact="", pre=pre)


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


def extract_links(html, base_url, max_items=80):
    """Return [(anchor_text, absolute_url, context_text)] for content-like links."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        t.decompose()
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        href = a["href"].strip()
        if not (3 <= len(text) <= 120) or text.lower() in NAV_WORDS:
            continue
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if url in seen or urlparse(url).scheme not in ("http", "https"):
            continue
        seen.add(url)
        parent = a.find_parent(["li", "article", "div", "tr"]) or a
        ctx = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))[:500]
        out.append((text, url, ctx))
        if len(out) >= max_items:
            break
    return out


def run_pages(cfg):
    import enrich  # reuse the polite, robots-aware fetcher

    out = []
    for page in cfg.get("pages", []) or []:
        if page.get("enabled", True) is False:
            continue
        name, url, mode = page.get("name", "page"), page["url"], page.get("mode", "classify")
        if page.get("render"):
            html = _render(url) if enrich.allowed(url) else ""
        else:
            html = enrich.fetch(url)
        if not html:
            print(f"[info] {name}: nothing fetched (blocked, empty or robots.txt disallows)")
            continue
        links = extract_links(html, url, int(page.get("max_items", 80)))
        for text, link, ctx in links:
            if mode == "trigger":
                pre = {"score": int(page.get("base_score", 55)), "signal": page.get("signal", name)}
                out.append(_item(f"Page:{name}", text, link, f"{text}. {ctx}", company=text, pre=pre))
            else:
                out.append(_item(f"Page:{name}", text, link, f"{text}. {ctx}"))
        print(f"[info] {name}: {len(links)} items")
    return out


def run_discovery(cfg_path=CONFIG):
    if requests is None or BeautifulSoup is None:
        sys.exit("Run: pip install -r requirements.txt")
    cfg = load_config(cfg_path)
    return run_searches(cfg) + run_pages(cfg)
