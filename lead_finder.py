#!/usr/bin/env python3
"""
Brand Identity Lead Finder (free MVP)

Sources:
  1. Funding news RSS (Entrackr, Inc42, YourStory)
  2. Product Hunt API (optional, set PH_TOKEN env var)
  3. Reddit RSS searches (people looking for brand designers)
  4. manual_leads.csv (paste Behance jobs / X / LinkedIn finds here)

Output: leads.csv (open in Google Sheets). Re-running appends only NEW leads.

Setup:
  pip install feedparser requests
  python lead_finder.py

Optional Product Hunt token (free): https://www.producthunt.com/v2/oauth/applications
  export PH_TOKEN="your_token"      (Windows: set PH_TOKEN=your_token)

Use public data only. Respect each site's terms before running outreach off it.
"""

import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

try:
    import feedparser
    import requests
except ImportError:
    feedparser = None
    requests = None

OUT_FILE = Path("leads.csv")
MANUAL_FILE = Path("manual_leads.csv")  # columns: title,url,notes
MIN_SCORE = 3  # leads below this are dropped
USER_AGENT = "Mozilla/5.0 (compatible; LeadFinder/1.0)"

FUNDING_FEEDS = {
    "Entrackr": "https://entrackr.com/feed",
    "Inc42": "https://inc42.com/feed/",
    "YourStory": "https://yourstory.com/feed",
}

REDDIT_SEARCHES = [
    ("indianstartups", "brand designer OR logo OR branding"),
    ("startups", "need brand designer OR looking for branding"),
    ("forhire", "[HIRING] brand identity OR logo designer"),
    ("DesignJobs", "brand identity freelance"),
]

# ---------- scoring keywords ----------
FUNDING_EARLY = ["pre-seed", "preseed", "seed round", "seed funding", "angel", "raises", "bags", "secures", "lands"]
FUNDING_LATE = ["series b", "series c", "series d", "series e", "ipo", "unicorn", "acquires", "acquisition"]
PHYSICAL = ["d2c", "skincare", "beauty", "cosmetic", "food", "beverage", "snack", "coffee", "tea ", "apparel",
            "fashion", "wellness", "supplement", "packaged", "fmcg", "jewellery", "jewelry", "personal care",
            "pet ", "baby", "organic", "brand", "consumer"]
LAUNCH = ["launch", "launches", "unveils", "debuts", "introduces", "new brand", "stealth", "comes out of stealth"]
INTENT = ["looking for a brand", "need a brand", "need a logo", "looking for a logo", "brand designer",
          "brand identity", "branding help", "need branding", "rebrand", "hiring designer", "logo designer",
          "visual identity", "packaging design"]
REBRAND = ["rebrand", "rebranding", "new identity", "refresh"]
NEGATIVE = ["for hire", "[for hire]", "hire me", "my portfolio", "i offer", "we offer", "our agency", "available for"]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def guess_company(title):
    m = re.match(r"^(.{2,60}?)\s+(raises|bags|secures|lands|closes|gets|picks up|launches|unveils|debuts|enters|raised)\b",
                 title, re.I)
    return m.group(1).strip(" '\"‘’") if m else ""


def score_lead(text, source):
    t = " " + text.lower() + " "
    score, signals = 0, []

    def hit(words):
        return [w for w in words if w in t]

    if any(w in t for w in NEGATIVE):
        return -10, ["designer self-promo"]

    if h := hit(INTENT):
        score += 5
        signals.append("intent:" + h[0])
    if hit(REBRAND):
        score += 3
        signals.append("rebrand")
    if hit(FUNDING_EARLY):
        score += 3
        signals.append("early funding")
    if hit(FUNDING_LATE):
        score -= 3
        signals.append("late-stage (likely has agency)")
    if h := hit(PHYSICAL):
        score += 3
        signals.append("physical/consumer:" + h[0].strip())
    if hit(LAUNCH):
        score += 2
        signals.append("launch")
    if source == "ProductHunt":
        score += 2
        signals.append("fresh launch")
    if source.startswith("Reddit") and not hit(INTENT):
        score -= 2
    if source == "Manual":
        score += 4
        signals.append("manual find")
    return score, signals


# ---------- sources ----------
def fetch_funding():
    out = []
    for name, url in FUNDING_FEEDS.items():
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            for e in feed.entries[:40]:
                title = clean(e.get("title"))
                summary = clean(e.get("summary"))[:400]
                out.append(dict(source=name, title=title, company=guess_company(title),
                                url=e.get("link", ""), text=f"{title}. {summary}"))
        except Exception as ex:
            print(f"[warn] {name} failed: {ex}", file=sys.stderr)
    return out


def fetch_reddit():
    out = []
    for sub, q in REDDIT_SEARCHES:
        url = f"https://www.reddit.com/r/{sub}/search.rss?q={quote_plus(q)}&restrict_sr=1&sort=new&t=month"
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            for e in feed.entries[:25]:
                title = clean(e.get("title"))
                summary = clean(e.get("summary"))[:500]
                out.append(dict(source=f"Reddit r/{sub}", title=title, company="",
                                url=e.get("link", ""), text=f"{title}. {summary}"))
        except Exception as ex:
            print(f"[warn] reddit {sub} failed: {ex}", file=sys.stderr)
    return out


def fetch_producthunt():
    token = os.environ.get("PH_TOKEN")
    if not token:
        print("[info] PH_TOKEN not set, skipping Product Hunt")
        return []
    query = """{ posts(order: NEWEST, first: 40) { edges { node {
        name tagline description url website makers { name twitterUsername } } } } }"""
    try:
        r = requests.post("https://api.producthunt.com/v2/api/graphql", json={"query": query},
                          headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        out = []
        for edge in r.json()["data"]["posts"]["edges"]:
            n = edge["node"]
            makers = ", ".join(
                m["name"] + (f" (@{m['twitterUsername']})" if m.get("twitterUsername") else "")
                for m in n.get("makers", []))
            out.append(dict(source="ProductHunt", title=f"{n['name']}: {n['tagline']}", company=n["name"],
                            url=n["url"], text=f"{n['name']}. {n['tagline']}. {n.get('description') or ''}",
                            website=n.get("website", ""), contact=makers))
        return out
    except Exception as ex:
        print(f"[warn] Product Hunt failed: {ex}", file=sys.stderr)
        return []


def fetch_manual():
    if not MANUAL_FILE.exists():
        MANUAL_FILE.write_text("title,url,notes\n", encoding="utf-8")
        return []
    out = []
    with MANUAL_FILE.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("url"):
                continue
            title = row.get("title", "")
            out.append(dict(source="Manual", title=title, company=title.split(" - ")[0],
                            url=row["url"], text=f"{title}. {row.get('notes', '')}"))
    return out


# ---------- pipeline ----------
FIELDS = ["date_found", "score", "source", "company", "title", "url", "signals",
          "website", "legal_name", "phone", "email", "instagram", "linkedin", "enrich_status",
          "founder_contact", "status", "notes"]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_existing():
    if not OUT_FILE.exists():
        return [], set(), set()
    with OUT_FILE.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows, {r["url"] for r in rows}, {norm(r["company"]) for r in rows if r["company"]}


def run():
    if feedparser is None:
        sys.exit("Run: pip install feedparser requests")

    raw = fetch_funding() + fetch_producthunt() + fetch_reddit() + fetch_manual()
    existing, seen_urls, seen_companies = load_existing()
    new = []

    for item in raw:
        if item["url"] in seen_urls:
            continue
        key = norm(item["company"])
        if key and key in seen_companies:
            continue
        score, signals = score_lead(item["text"], item["source"])
        if score < MIN_SCORE:
            continue
        seen_urls.add(item["url"])
        if key:
            seen_companies.add(key)
        new.append(dict(date_found=now(), score=score, source=item["source"], company=item["company"],
                        title=item["title"], url=item["url"], signals="; ".join(signals),
                        website=item.get("website", ""), founder_contact=item.get("contact", ""),
                        status="new", notes=""))

    rows = existing + new
    rows.sort(key=lambda r: int(r["score"]), reverse=True)
    with OUT_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"Scanned {len(raw)} items, {len(new)} new leads, {len(rows)} total in {OUT_FILE}")
    for r in new[:10]:
        print(f"  [{r['score']}] {r['company'] or r['title'][:50]} ({r['source']}) - {r['signals']}")


if __name__ == "__main__":
    run()
