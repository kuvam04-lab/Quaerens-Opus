# Job Monitor — entry-level chemE / process engineering

Polls ~40 company career sites directly via their ATS APIs (Workday, Greenhouse,
Lever, Ashby), filters for entry-level chemical / process / process-development /
refining / midstream engineering roles, and pushes new postings to your phone
(via ntfy.sh) and/or Discord. No LinkedIn, no Indeed — straight from the source.

You can run it on your laptop on demand, or set it up to run hourly for free on
GitHub Actions.

---

## Quick start (laptop, on demand)

```bash
# 1. Install
pip install -r requirements.txt

# 2. Pick a notification channel (do at least one)

# Option A — ntfy.sh (free push to your phone, no signup)
#   - Install the "ntfy" app on iOS or Android.
#   - In the app, subscribe to a topic name only you know,
#     e.g.  jeremy-chemE-jobs-7x9k2  (random suffix matters: topics are public).
export NTFY_TOPIC=jeremy-chemE-jobs-7x9k2

# Option B — Discord (rich embeds with clickable links)
#   - In any Discord server you own → Server Settings → Integrations → Webhooks
#     → New Webhook → copy URL.
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# 3. First run — quietly populates the cache so you don't get spammed
python monitor.py

# 4. Subsequent runs — only new postings get pushed
python monitor.py
```

That's it. Run `python monitor.py` whenever you want to check.

---

## Health check (run this BEFORE deploying to GitHub Actions)

After unzipping or after editing `companies.py`, run:

```bash
python monitor.py --validate
```

This hits every company once and prints a pass/fail table. **Takes ~3-5 min.**
Use it to catch broken Workday URLs, wrong Phenom `ref_num` values, or
mistyped BrassRing IDs before you rely on the hourly cron.

```
Company                         ATS           Result                Detail
----------------------------------------------------------------------------
ExxonMobil                      rmk           ✓   178 jobs
Chevron                         workday       ✓   412 jobs
BAE Systems                     phenom        ⚠   0 jobs returned   check URL/ref_num
Pfizer                          workday       ✓   263 jobs
...
```

**Three result types:**
- ✓ — fetcher works, returned jobs
- ⚠ 0 jobs — URL might be wrong, OR the company truly has no listings right
  now. Open the careers page in a browser to confirm.
- ✗ ERROR — config is malformed or the site is down

Run `python monitor.py --debug --only "<company name>"` on any failing entry
to see the raw response.

**Important:** A broken company will NOT crash the regular run. The other 54
companies finish normally. The error gets logged but jobs from that company
just stop appearing. That's why `--validate` exists — silent failures are easy
to miss.



```bash
python monitor.py --posted-within 24h    # only jobs posted in the last 24 hours
python monitor.py --posted-within 1d     # same thing
python monitor.py --posted-within 7d     # last week
python monitor.py --posted-within 2w     # last 2 weeks
python monitor.py --posted-within 30     # last 30 days (plain int = days)
```

Accepted suffixes: `h` (hours), `d` (days), `w` (weeks), `m` (months ≈ 30 days).

You can also set the env var `JOB_POSTED_WITHIN` instead of passing the flag —
useful for the GitHub Actions cron. To enable on the cloud run, add a repo
secret named `JOB_POSTED_WITHIN` with value like `7d`.

**How it works:** each ATS returns dates in a different format (ISO, epoch
ms, "Mar 17 2026", "21-04-2026", "Posted 3 Days Ago", etc.). The script
parses them all. **If a date can't be parsed, the job is kept** — better to
over-notify than to miss something.

---

## US-only filter

By default, the script filters out non-US jobs. Locations like
`Bengaluru, KA, IN`, `Singapore, 01, SG`, `France`, `United Kingdom` are
dropped; `Houston, TX`, `Indianapolis, IN`, `Foster City, California`,
`Remote` are kept.

```bash
python monitor.py --all-locations    # disable filter, include international
```

**How it works:** the filter accepts US state abbreviations (TX, CA, IN,
all 50 + DC + PR), explicit US country tokens (`US`, `USA`, `United States`),
and "Remote/Anywhere/Virtual". It rejects ~50 known non-US country names. It
disambiguates `IN` (India vs Indiana) by looking at adjacent tokens. **If a
location is empty or ambiguous (e.g. just "Houston" with no state), the job
is kept** — better to over-notify than to miss something.

---

## Hourly cloud run (GitHub Actions, free)

1. Create a **private** GitHub repo (private matters: `seen_jobs.json` will be
   committed back, and you don't want strangers seeing your job-search activity).
2. Push these files to it.
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**. Add either or both:
   - `NTFY_TOPIC`  →  your ntfy topic name
   - `DISCORD_WEBHOOK_URL`  →  your Discord webhook URL
4. The workflow at `.github/workflows/monitor.yml` runs every hour automatically.
   You can also trigger it manually from the **Actions** tab → **Job Monitor**
   → **Run workflow**.

GitHub gives you 2,000 free Action minutes/month on private repos. This script
finishes in well under 2 minutes per run, so 24 runs/day × 30 days × ~1.5 min ≈
1,080 min/month. Comfortably free.

---

## How filtering works

A job title is included if it matches **any** of:
`process engineer`, `chemical engineer`, `process development engineer`,
`refining engineer`, `midstream engineer`, `process development`,
`manufacturing engineer`.

It's excluded if it contains: `senior`, `sr.`, `principal`, `staff`, `lead`,
`manager`, `director`, `II/III/IV/V`, `engineer 2/3/...`, `phd`, `intern`,
`co-op`, `sales`.

This catches "Process Engineer I", "Chemical Engineer 1", "Process Development
Engineer" while filtering out senior roles. Edit `INCLUDE_PATTERNS` /
`EXCLUDE_PATTERNS` at the top of `monitor.py` to tune.

---

## Fixing a broken company (Workday URL is wrong)

Run with `--debug` and look for errors:

```bash
python monitor.py --debug --only "Pfizer"
```

If a company returns 404 or 0 jobs forever, its `api_url` is wrong. To fix:

1. Go to that company's "search jobs" page (e.g. careers.merck.com).
2. You'll be redirected to something like:
   `https://msd.wd5.myworkdayjobs.com/en-US/SearchJobs/...`
3. From that URL, three pieces:
   - **tenant** = `msd`  (the leftmost subdomain)
   - **pod** = `wd5`  (the `wdN` chunk after the dot)
   - **site** = `SearchJobs`  (the path segment right after `en-US/`)
4. Open `companies.py`, find the line for that company, replace with:
   ```python
   {"display_name": "Merck (MSD)", "ats": "workday", **_wd("msd", "wd5", "SearchJobs")},
   ```
5. Save and re-run.

---

## Adding new companies

**If they use Workday** (most large companies do — Pfizer, Merck, Chevron,
Lockheed, etc.): use the recipe above and add a new line to `companies.py`.

**If they use Greenhouse**:
- URL pattern: `boards.greenhouse.io/{slug}` or `{slug}.greenhouse.io`
- Add: `{"display_name": "Acme", "ats": "greenhouse", "slug": "acme"}`

**If they use Lever**:
- URL pattern: `jobs.lever.co/{slug}`
- Add: `{"display_name": "Acme", "ats": "lever", "slug": "acme"}`

**If they use Ashby**:
- URL pattern: `jobs.ashbyhq.com/{slug}`
- Add: `{"display_name": "Acme", "ats": "ashby", "slug": "acme"}`

**If they use Avature** (template assets load from `avacdn.net`, e.g. TotalEnergies):
- Find the `/SearchJobs` path on the careers site
- Add:
  ```python
  {"display_name": "Acme", "ats": "avature",
   "base_url": "https://jobs.acme.com",
   "search_path": "/en_US/careers/SearchJobs",
   "country_filter": ["US"]}
  ```

**If they use SAP SuccessFactors RMK** (cookie panel mentions "SAP", logo from `rmkcdn.successfactors.com`):
- Click an Engineering category and copy the `/go/Engineering/{ID}/` path
- Add:
  ```python
  {"display_name": "Acme", "ats": "rmk",
   "base_url": "https://jobs.acme.com",
   "category_paths": ["/go/Engineering/123456/"],
   "country_filter": ["US"]}
  ```

**If they use Cornerstone OnDemand, Phenom People, Eightfold AI, iCIMS, Taleo, or a custom site** — there's no fetcher built yet. Best options:
- Subscribe to the company's email talent network (you already do this).
- Set a Gmail filter that auto-labels those emails into one folder for fast
  scanning.
- Or write a Selenium/Playwright scraper (out of scope here — the time-to-value
  isn't worth it for most chemE candidates).

---

## Companies covered

All 56 companies on your target list are wired up across 12 ATS fetchers:
Workday (23), generic_html (10), SuccessFactors RMK (8), Phenom (7),
Eightfold AI (3), Taleo (2), and 1 each of Avature, BrassRing, Greenhouse.

**`generic_html`** is a fallback fetcher for fully custom careers sites
(Lockheed, RTX, L3Harris, J&J, Takeda, Dow, etc.) that don't use a
standard ATS. It scrapes server-rendered HTML and extracts links matching
configurable patterns. Less reliable than ATS-specific fetchers — date and
location extraction are best-effort — but covers companies that would
otherwise need a Selenium/Playwright scraper.

---

## Files

- `monitor.py` — main script (fetchers, filtering, notifiers, CLI)
- `companies.py` — company list and ATS endpoints (edit here to add/fix)
- `requirements.txt` — only `requests`
- `seen_jobs.json` — auto-generated cache of job IDs you've already seen
- `.github/workflows/monitor.yml` — hourly GitHub Actions cron

---

## Troubleshooting

**"No DISCORD_WEBHOOK_URL or NTFY_TOPIC set":** export at least one (see Quick
Start).

**Getting nothing on ntfy:** make sure you (a) installed the app, (b) subscribed
to your topic in the app, (c) used the same string in `NTFY_TOPIC`. Topics are
public globally — pick something unguessable.

**Workday returns 404 for a company:** the `api_url` is wrong. Follow "Fixing a
broken company" above.

**Workday returns 0 jobs but the site has jobs:** the `searchText` filter may be
too narrow for that company's title conventions. Try adding more terms in
`DEFAULT_SEARCH_TERMS` in `companies.py` (e.g. `"manufacturing"`,
`"plant engineer"`).

**Getting too many notifications:** tighten `INCLUDE_PATTERNS` in `monitor.py`,
or add to `EXCLUDE_PATTERNS`.

**First run pushed nothing:** that's by design — it caches everything as
"already seen" so you don't get blasted with hundreds of postings. Use
`python monitor.py --notify-all` once if you actually want the backlog..
