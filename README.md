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

## Run it from GitHub (no local setup)
The workflow in `.github/workflows/lead-finder.yml` runs daily at 09:00 IST and commits `leads.csv` back to the repo.

- **Run now:** Actions tab > Lead Finder > Run workflow.
- **Add Behance/X/LinkedIn finds:** edit `manual_leads.csv` on github.com (`title,url,notes`). Picked up on the next run.
- **Product Hunt (optional):** Settings > Secrets and variables > Actions > New secret named `PH_TOKEN`.
- **Repo must be private.** The workflow refuses to run on a public repo because it commits lead data.
- Reddit sometimes blocks GitHub's servers. If Reddit returns nothing, funding feeds and manual leads still work.

## Intent scoring and discovery (v2)
Every lead gets a 0-100 score and a tier: **HOT** (70+), **WARM** (40-69), COLD. Filter `leads.csv` by `tier` and work HOT first.

- **Groq scorer:** `llm.py` uses `openai/gpt-oss-120b` to read each post/page and judge real buying intent, need, timeline and budget signal. Without a key it falls back to keyword rules. Capped at 80 calls per run (`LLM_MAX_CALLS`, `GROQ_RPM` to match your rate limit).
- **Discovery:** `discover.py` runs the queries in `sources.yaml` through search APIs and watches public pages. Add any public site there.
- **Secrets** (Settings > Secrets and variables > Actions): `GROQ_API_KEY` (required for LLM scoring), plus at least one of `TAVILY_API_KEY`, `EXA_API_KEY`, `BRAVE_API_KEY` for search. `PH_TOKEN` stays optional.
- **JavaScript pages:** set `render: true` in `sources.yaml` and `pip install playwright && playwright install chromium` (not installed in the workflow by default).
- **Limits by design:** no logins, captcha solving or stealth. robots.txt is respected. Sites that need those are out of reach: use their official alerts or `manual_leads.csv`.
- Scraped text is untrusted. It is fenced in the prompt, and model output is validated and clamped, so a malicious post cannot change behaviour, only its own score.
