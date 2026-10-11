# Brand Lead Finder

Free lead finder for brand identity design: finds startups and founders who need branding, scores them, and enriches each with website, legal name, phone, email and socials.

## Setup
```
pip install -r requirements.txt
```

## Run
```
python lead_finder.py          # collect + score -> leads.csv
python enrich.py --limit 30    # add website, legal name, phone, email, socials
```

## Sources
- Funding news RSS (Entrackr, Inc42, YourStory)
- Reddit hiring posts (RSS)
- Product Hunt (optional: set `PH_TOKEN`)
- `manual_leads.csv` for Behance jobs and X/LinkedIn finds (`title,url,notes`)

## Notes
- Public data only. Respect each site's terms and robots.txt.
- Phone numbers tagged "medium" need manual verification.
- Follow TRAI DND and DPDP rules. Email or Instagram first, phone for follow-up.
- `leads.csv` is gitignored so lead data stays off GitHub.
