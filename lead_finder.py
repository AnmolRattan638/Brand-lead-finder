#!/usr/bin/env python3
"""
Brand Identity Lead Finder (free MVP)

Sources:
  1. Funding news RSS (Entrackr, Inc42, YourStory)
  2. Product Hunt API (optional, set PH_TOKEN env var)
  3. Reddit RSS searches (people looking for brand designers)
  4. manual_leads.csv (paste Behance jobs / X / LinkedIn finds here)
  5. discover.py (search APIs + watched public pages, see sources.yaml)

If GROQ_API_KEY is set, Groq gpt-oss-120b scores buying intent (HOT/WARM/COLD).
Otherwise keyword rules are used.

Output: leads.csv (open in Google Sheets). Re-running appends only NEW leads.

Setup:
  pip install -r requirements.txt
  python lead_finder.py

Optional Product Hunt token (free): https://www.producthunt.com/v2/oauth/applications
  export PH_TOKEN="your_token"      (Windows: set PH_TOKEN=your_token)

Use public data only. Respect each site's terms before running outreach off it.
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import llm

try:
    import feedparser
    import requests
except ImportError:
    feedparser = None
    requests = None

OUT_FILE = Path("leads.csv")
MANUAL_FILE = Path("manual_leads.csv")  # columns: title,url,notes
MIN_SCORE = 30  # 0-100 scale; leads below this are dropped
RULE_SCALE = 8  # converts keyword-rule score (0-15) to the 0-100 scale
LLM_PREFILTER = 16  # funding/RSS items need this rule score before spending an LLM call
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
FIELDS = ["date_found", "score", "tier", "source", "company", "need", "timeline", "budget_signal",
          "title", "url", "signals", "reason", "location",
          "website", "legal_name", "phone", "email", "instagram", "linkedin", "enrich_status",
          "founder_contact", "status", "notes"]

LLM_SOURCES = ("Search:", "Page:", "Reddit", "Manual")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_existing():
    if not OUT_FILE.exists():
        return [], set(), set()
    with OUT_FILE.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows, {r["url"] for r in rows}, {norm(r["company"]) for r in rows if r["company"]}


def collect():
    raw = fetch_funding() + fetch_producthunt() + fetch_reddit() + fetch_manual()
    try:
        import discover
        raw += discover.run_discovery()
    except SystemExit:
        raise
    except Exception as ex:  # discovery must never break the core run
        print(f"[warn] discovery failed: {ex}", file=sys.stderr)
    return raw


def evaluate(item, llm_budget):
    """Return (score 0-100, tier, fields dict) for one item, or None to drop."""
    if item.get("pre"):  # trigger-mode page entries: fixed base score, no LLM cost
        score = item["pre"]["score"]
        return score, llm.tier_for(score), {"signals": item["pre"]["signal"]}

    rule_raw, signals = score_lead(item["text"], item["source"])
    if rule_raw <= -10:  # designer self-promo
        return None
    rule_score = max(0, min(100, rule_raw * RULE_SCALE))

    wants_llm = item["source"].startswith(LLM_SOURCES) or rule_score >= LLM_PREFILTER
    res = llm.classify(item["text"], item["source"], item["url"], max_calls=llm_budget) if wants_llm else None

    if res is None:  # no key, budget spent, or call failed: keyword rules
        return rule_score, llm.tier_for(rule_score), {"signals": "; ".join(signals)}
    if not res["is_buyer"] and item["source"] != "Manual":
        return None
    score = res["intent_score"]
    if item["source"] == "Manual":
        score = max(score, 60)  # you curated it by hand
    return score, llm.tier_for(score), {
        "signals": "; ".join(signals), "company": res["company"], "need": res["need"],
        "timeline": res["timeline"], "budget_signal": res["budget_signal"], "location": res["location"],
        "reason": res["reason"], "founder_contact": res["contact_hint"]}


def run():
    if feedparser is None:
        sys.exit("Run: pip install -r requirements.txt")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-llm", type=int, default=int(os.environ.get("LLM_MAX_CALLS", "80")),
                    help="max Groq calls per run (free-tier safety)")
    args = ap.parse_args()

    print("[info] intent scoring:", f"Groq {llm.MODEL}" if llm.available() else "keyword rules (set GROQ_API_KEY for LLM)")
    raw = collect()
    existing, seen_urls, seen_companies = load_existing()
    new = []

    for item in raw:
        if item["url"] in seen_urls:
            continue
        key = norm(item["company"])
        if key and key in seen_companies:
            continue
        ev = evaluate(item, args.max_llm)
        seen_urls.add(item["url"])
        if ev is None:
            continue
        score, tier, f = ev
        if score < MIN_SCORE:
            continue
        company = item["company"] or f.get("company", "")
        ckey = norm(company)
        if ckey and ckey in seen_companies:
            continue
        if ckey:
            seen_companies.add(ckey)
        new.append(dict(date_found=now(), score=score, tier=tier, source=item["source"], company=company,
                        need=f.get("need", ""), timeline=f.get("timeline", ""),
                        budget_signal=f.get("budget_signal", ""), title=item["title"], url=item["url"],
                        signals=f.get("signals", ""), reason=f.get("reason", ""), location=f.get("location", ""),
                        website=item.get("website", ""),
                        founder_contact=item.get("contact", "") or f.get("founder_contact", ""),
                        status="new", notes=""))

    rows = existing + new
    rows.sort(key=lambda r: int(r["score"]), reverse=True)
    with OUT_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    hot = sum(1 for r in new if r["tier"] == "HOT")
    warm = sum(1 for r in new if r["tier"] == "WARM")
    print(f"Scanned {len(raw)} items, {len(new)} new leads ({hot} HOT, {warm} WARM), {len(rows)} total in {OUT_FILE}")
    for r in new[:10]:
        print(f"  [{r['score']} {r['tier']}] {r['company'] or r['title'][:50]} ({r['source']}) - "
              f"{r['need'] or r['signals']}")


if __name__ == "__main__":
    run()
