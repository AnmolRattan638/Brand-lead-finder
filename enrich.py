#!/usr/bin/env python3
"""
Lead enrichment: turns a lead in leads.csv into a company with a website,
legal entity name, public phone, email and social links.

Run AFTER lead_finder.py:
    pip install requests beautifulsoup4
    python enrich.py --limit 30

How it works (public data only):
  1. Find the company website
       - website column (Product Hunt), else
       - outbound links inside the funding article that match the company name
  2. Fetch homepage + contact/about pages (respects robots.txt, 2s delay)
  3. Extract: tel: links and +91 mobile numbers, emails, Instagram/LinkedIn,
     legal name from footer ("X Private Limited", "(c) 2026 X")
  4. Write results back into leads.csv with a confidence tag

Reality check: expect a phone for roughly 30-50% of leads. Many new startups
only publish an email or an Instagram DM. Never scrape personal numbers from
leaked or purchased databases. Cold-call and WhatsApp rules apply (TRAI DND,
DPDP Act), so prefer email/Instagram first and keep outreach relevant.
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None
    BeautifulSoup = None

LEADS = Path("leads.csv")
UA = "Mozilla/5.0 (compatible; LeadFinder/1.0)"
DELAY = 2.0
TIMEOUT = 15

SKIP_DOMAINS = (
    "entrackr.com", "inc42.com", "yourstory.com", "reddit.com", "producthunt.com", "google.", "facebook.com",
    "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "wikipedia.org", "medium.com",
    "t.co", "bit.ly", "apple.com", "play.google", "whatsapp.com", "github.com", "crunchbase.com",
    "tracxn.com", "bloomberg.com", "reuters.com", "economictimes", "livemint.com", "moneycontrol.com",
    "techcrunch.com", "gravatar.com", "wp.com", "wordpress.com", "cloudfront.net", "w3.org",
)

PHONE_RE = re.compile(r"(?<![\d])(?:\+?91[\s\-]?|0)?([6-9]\d{4}[\s\-]?\d{5})(?![\d])")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
JUNK_EMAIL = ("example.", "sentry", "wixpress", ".png", ".jpg", ".webp", "yourname", "domain.com", "email.com")
LEGAL_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.'\-]*(?:\s[A-Z0-9][A-Za-z0-9&.'\-]*){0,5}\s"
    r"(?:Private Limited|Pvt\.?\s?Ltd\.?|Limited|LLP|Ventures|Industries))")
COPY_RE = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:20\d{2}\s*)?[-–]?\s*(?:20\d{2}\s*)?([A-Z][^|.\n©]{2,50})",
                     re.I)
CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us"]

_robots = {}
_last_hit = [0.0]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def allowed(url):
    root = "{0.scheme}://{0.netloc}".format(urlparse(url))
    rp = _robots.get(root)
    if rp is None:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(root + "/robots.txt", headers={"User-Agent": UA}, timeout=8)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots[root] = rp
    return rp.can_fetch(UA, url)


def fetch(url):
    """Polite GET. Returns HTML text or ''."""
    try:
        if not allowed(url):
            return ""
        wait = DELAY - (time.time() - _last_hit[0])
        if wait > 0:
            time.sleep(wait)
        _last_hit[0] = time.time()
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", "text/html"):
            return r.text
    except Exception:
        pass
    return ""


# ---------- parsing ----------
def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)[-10:]
    return "+91" + digits if len(digits) == 10 and digits[0] in "6789" else ""


def parse_page(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    out = {"phones_hi": [], "phones_md": [], "emails": [], "instagram": "", "linkedin": "", "legal": "",
           "contact_links": []}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("tel:"):
            p = clean_phone(href[4:])
            if p:
                out["phones_hi"].append(p)
        elif low.startswith("mailto:"):
            out["emails"].append(href[7:].split("?")[0].strip())
        elif "wa.me/" in low or "api.whatsapp.com" in low:
            m = re.search(r"(?:wa\.me/|phone=)(\d{10,13})", low)
            if m and clean_phone(m.group(1)):
                out["phones_hi"].append(clean_phone(m.group(1)))
        elif "instagram.com/" in low and not out["instagram"]:
            m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", href)
            if m and m.group(1).lower() not in ("p", "reel", "explore", "accounts", "share"):
                out["instagram"] = "https://instagram.com/" + m.group(1)
        elif "linkedin.com/" in low and not out["linkedin"]:
            m = re.search(r"linkedin\.com/(company|in)/([A-Za-z0-9_\-%]+)", href)
            if m:
                out["linkedin"] = f"https://linkedin.com/{m.group(1)}/{m.group(2)}"
        if "contact" in low and urlparse(urljoin(base_url, href)).netloc == urlparse(base_url).netloc:
            out["contact_links"].append(urljoin(base_url, href))

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)

    for m in PHONE_RE.finditer(text):
        p = clean_phone(m.group(1))
        if p:
            out["phones_md"].append(p)
    out["emails"] += EMAIL_RE.findall(text)

    foot = text[-1500:]
    m = LEGAL_RE.search(foot) or LEGAL_RE.search(text)
    if m:
        out["legal"] = m.group(1).strip()
    else:
        m = COPY_RE.search(foot)
        if m:
            out["legal"] = m.group(1).strip(" .,-")
    return out


def pick_website(lead, fetch_fn):
    site = (lead.get("website") or "").strip()
    if site:
        return site if site.startswith("http") else "https://" + site
    company = norm(lead.get("company"))
    if not company or not lead.get("url"):
        return ""
    html = fetch_fn(lead["url"])
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    best = ""
    for a in soup.find_all("a", href=True):
        u = a["href"]
        if not u.startswith("http"):
            continue
        host = urlparse(u).netloc.lower().replace("www.", "")
        if any(s in host for s in SKIP_DOMAINS):
            continue
        if company[:8] in norm(host):
            return "{0.scheme}://{0.netloc}".format(urlparse(u))
        best = best or ""
    return best


def enrich_lead(lead, fetch_fn=fetch):
    """Returns dict of fields to update on the lead."""
    res = {"enrich_status": "", "legal_name": "", "phone": "", "email": "", "instagram": "", "linkedin": ""}
    if not lead.get("company") and not lead.get("website"):
        res["enrich_status"] = "no company name (manual)"
        return res

    site = pick_website(lead, fetch_fn)
    if not site:
        res["enrich_status"] = "website not found"
        return res
    res["website"] = site

    pages = [site]
    seen = {site}
    html = fetch_fn(site)
    agg = {"phones_hi": [], "phones_md": [], "emails": [], "instagram": "", "linkedin": "", "legal": ""}

    def merge(p):
        for k in ("phones_hi", "phones_md", "emails"):
            agg[k] += p[k]
        for k in ("instagram", "linkedin", "legal"):
            agg[k] = agg[k] or p[k]

    extra = []
    if html:
        p = parse_page(html, site)
        merge(p)
        extra = p["contact_links"][:2]
    for path in [urljoin(site, x) for x in CONTACT_PATHS]:
        extra.append(path)
    for u in extra:
        if u in seen or len(seen) >= 4:
            continue
        seen.add(u)
        h = fetch_fn(u)
        if h:
            merge(parse_page(h, u))

    phone, conf = "", ""
    if agg["phones_hi"]:
        phone, conf = agg["phones_hi"][0], "phone: high (tel/whatsapp link)"
    elif agg["phones_md"]:
        phone, conf = agg["phones_md"][0], "phone: medium (text match, verify)"

    domain = urlparse(site).netloc.replace("www.", "")
    emails = [e for e in dict.fromkeys(x.lower() for x in agg["emails"]) if not any(j in e for j in JUNK_EMAIL)]
    emails.sort(key=lambda e: (domain not in e, e.split("@")[0] in ("info", "hello", "support", "contact")))

    res.update(phone=phone, email=emails[0] if emails else "", instagram=agg["instagram"],
               linkedin=agg["linkedin"], legal_name=agg["legal"])
    got = [x for x in (phone, res["email"], res["instagram"]) if x]
    res["enrich_status"] = (conf or "no phone") if got else "site found, no contact info"
    if phone == "" and got:
        res["enrich_status"] = "no phone, other contact found"
    return res


# ---------- CSV ----------
def main():
    if requests is None:
        sys.exit("Run: pip install requests beautifulsoup4")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30, help="max leads to enrich per run")
    ap.add_argument("--min-score", type=int, default=5)
    args = ap.parse_args()

    if not LEADS.exists():
        sys.exit("leads.csv not found. Run lead_finder.py first.")
    with LEADS.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
    for col in ("legal_name", "phone", "email", "instagram", "linkedin", "enrich_status"):
        if col not in fields:
            fields.append(col)

    done = 0
    for r in rows:
        if done >= args.limit:
            break
        if r.get("enrich_status") or int(r.get("score") or 0) < args.min_score:
            continue
        upd = enrich_lead(r)
        for k, v in upd.items():
            if v or k == "enrich_status":
                r[k] = v
        done += 1
        print(f"[{r['score']}] {r.get('company') or r['title'][:40]} -> {r['enrich_status']} "
              f"{r.get('phone', '')} {r.get('email', '')}")
        with LEADS.open("w", newline="", encoding="utf-8") as f:  # save as we go
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    print(f"Enriched {done} leads.")


if __name__ == "__main__":
    main()
