"""
Job monitor: polls company ATS APIs for entry-level chemical/process engineering roles
and sends new postings to Discord and/or ntfy.sh.

Usage:
    python monitor.py                # run once
    python monitor.py --notify-all   # notify on first run too (for testing)
    python monitor.py --debug        # print every job seen, matched or not
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import requests

from companies import COMPANIES, DEFAULT_SEARCH_TERMS

SEEN_FILE = Path(__file__).parent / "seen_jobs.json"
TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


# ──────────────────────────────────────────────────────────────────────────────
# Job model + filtering
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    company: str
    title: str
    location: str
    url: str
    posted: str
    job_id: str

    def key(self) -> str:
        return f"{self.company}::{self.job_id}"


# ──────────────────────────────────────────────────────────────────────────────
# Date parsing — each ATS returns posted-dates in a different format.
# parse_posted_date() tries them all. Returns a UTC datetime or None.
# ──────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone, timedelta


def parse_posted_date(raw: str) -> datetime | None:
    """Best-effort parsing of the heterogeneous 'posted' strings the various
    ATSs return. Returns a timezone-aware UTC datetime, or None if unparseable.

    Handles:
      - ISO 8601 ("2026-04-21T15:30:00Z", "2026-04-21")
      - Epoch seconds / milliseconds (as int or string of digits)
      - Workday's "Posted X Days Ago" / "Posted Today" / "Posted Yesterday"
      - SF RMK's "Mar 17, 2026"
      - Avature's "21-04-2026" (DD-MM-YYYY)
      - Phenom's "2026-04-21" or epoch ms
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    now = datetime.now(timezone.utc)

    # Workday's relative phrasing
    low = s.lower()
    if "posted today" in low or low == "today":
        return now
    if "posted yesterday" in low or low == "yesterday":
        return now - timedelta(days=1)
    m = re.search(r"posted\s+(\d+)\+?\s+days?\s+ago", low)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"posted\s+(\d+)\+?\s+months?\s+ago", low)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)

    # Pure digits → epoch (seconds if 10 digits, milliseconds if 13)
    if s.isdigit():
        n = int(s)
        if n > 10**12:        # ms
            return datetime.fromtimestamp(n / 1000, tz=timezone.utc)
        if n > 10**9:         # seconds
            return datetime.fromtimestamp(n, tz=timezone.utc)

    # ISO 8601 with optional Z
    iso_candidates = [s, s.replace("Z", "+00:00")]
    for cand in iso_candidates:
        try:
            dt = datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    # Common explicit formats
    formats = [
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",            # Avature: 21-04-2026
        "%b %d, %Y",           # SF RMK: Mar 17, 2026
        "%B %d, %Y",           # March 17, 2026
        "%Y/%m/%d",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_age_filter(spec: str) -> timedelta | None:
    """Parse user-friendly age strings: '24h', '1d', '7d', '2w', '30'.
    Plain integers are interpreted as days. Returns None if invalid.
    """
    if not spec:
        return None
    s = spec.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([hdwm]?)", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "d"   # default: days
    return {
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
        "m": timedelta(days=n * 30),
    }[unit]


# ──────────────────────────────────────────────────────────────────────────────
# US-location filtering. Each ATS returns location strings in different
# formats — some include country codes ("Houston, TX, US"), some don't
# ("Houston, TX"), some are non-US ("Bengaluru, India"). is_us_location() does
# the best it can with what's there: explicit US tokens are accepted, explicit
# non-US tokens are rejected, ambiguous strings are kept (better to over-notify).
# ──────────────────────────────────────────────────────────────────────────────

# 50 states + DC + PR — enough that "Houston, TX" matches even without a "US" suffix.
US_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR",
}

US_COUNTRY_TOKENS = {
    "US", "USA", "U.S.", "U.S.A.",
    "UNITED STATES", "UNITED STATES OF AMERICA",
}

# Non-US country names commonly seen in postings on your target companies.
# Not exhaustive — just used to confidently REJECT obvious non-US locations.
NON_US_COUNTRY_TOKENS = {
    "CANADA", "MEXICO", "UK", "UNITED KINGDOM", "ENGLAND", "SCOTLAND", "WALES",
    "IRELAND", "FRANCE", "GERMANY", "SPAIN", "ITALY", "NETHERLANDS", "BELGIUM",
    "SWITZERLAND", "AUSTRIA", "DENMARK", "SWEDEN", "NORWAY", "FINLAND",
    "POLAND", "CZECH REPUBLIC", "HUNGARY", "ROMANIA", "PORTUGAL", "GREECE",
    "INDIA", "CHINA", "JAPAN", "KOREA", "SOUTH KOREA", "TAIWAN", "SINGAPORE",
    "MALAYSIA", "INDONESIA", "PHILIPPINES", "THAILAND", "VIETNAM", "HONG KONG",
    "AUSTRALIA", "NEW ZEALAND",
    "BRAZIL", "ARGENTINA", "CHILE", "COLOMBIA", "PERU", "VENEZUELA",
    "SOUTH AFRICA", "EGYPT", "NIGERIA", "MOROCCO",
    "UAE", "UNITED ARAB EMIRATES", "SAUDI ARABIA", "QATAR", "KUWAIT", "BAHRAIN",
    "OMAN", "ISRAEL", "TURKEY", "JORDAN", "SENEGAL",
    "RUSSIA", "UKRAINE",
}


def is_us_location(location: str) -> bool:
    """Best-effort: True if the location is in the US, False if confidently not,
    True if ambiguous (empty / unknown — better to over-notify).
    """
    if not location:
        return True   # unknown — let it through

    loc_upper = location.upper()

    # "Remote" or "Anywhere" → keep (often US-based with global title)
    if "REMOTE" in loc_upper or "VIRTUAL" in loc_upper or "ANYWHERE" in loc_upper:
        return True

    # Tokenize on commas, slashes, semicolons, parentheses, en-dashes
    tokens = re.split(r"[,/;()\-–—]", location)
    tokens = [t.strip().upper() for t in tokens if t.strip()]
    token_set = set(tokens)

    # Strong-positive override: explicit US country marker → always US.
    # This handles cases like "Indianapolis, IN, US" before the IN ambiguity
    # check kicks in.
    has_us_marker = any(t in US_COUNTRY_TOKENS for t in tokens)
    if has_us_marker:
        return True

    # Strong-negative: explicit non-US country (multi-word matched against
    # the whole string).
    for tok in tokens:
        if tok in NON_US_COUNTRY_TOKENS:
            return False
    for country in NON_US_COUNTRY_TOKENS:
        if " " in country and country in loc_upper:
            return False

    # IN ambiguity: "IN" alone could mean India or Indiana.
    # Heuristic: if the only US-state-looking token is "IN" AND there's another
    # token that's NOT a US state, it's probably India (e.g. "Bengaluru, KA, IN"
    # has KA which isn't a US state). If "IN" is the only or last token after a
    # US-recognizable city, we keep it.
    if "IN" in token_set:
        # Tokens that are US-state codes other than IN
        other_us_states = [t for t in tokens if t in US_STATE_ABBREVS and t != "IN"]
        # Tokens that look like they could be state/region codes (2-letter, all caps)
        # but aren't US states
        non_us_state_codes = [t for t in tokens
                              if len(t) == 2 and t.isalpha()
                              and t not in US_STATE_ABBREVS
                              and t not in US_COUNTRY_TOKENS]
        if non_us_state_codes and not other_us_states:
            # Pattern matches "City, FOREIGN_STATE, IN" → India
            return False

    # Strong-positive: any US state abbreviation
    for tok in tokens:
        if tok in US_STATE_ABBREVS:
            return True
        # Some Workday strings are like "Houston TX" with no comma
        for word in tok.split():
            if word in US_STATE_ABBREVS:
                return True

    # Ambiguous (e.g. "Houston" with no state) → keep
    return True


# Title must contain one of these (case-insensitive, regex-friendly).
INCLUDE_PATTERNS = [
    # Core titles
    r"\bprocess\s+engineer",
    r"\bprocess\s+development\s+engineer",
    r"\bchemical\s+engineer",
    r"\brefining\s+engineer",
    r"\bmidstream\s+engineer",
    r"\bprocess\s+development\b",         # "Process Development Scientist"
    r"\bmanufacturing\s+engineer",        # pharma/semi alt-naming
    # ChemE-adjacent engineering titles common at target companies
    r"\bproduction\s+engineer",
    r"\bplant\s+engineer",
    r"\boperations\s+engineer",
    # Reversed order: "Engineer - Operations", "Engineer, Process", etc.
    # Some companies (esp. oil/gas) lead with "Engineer" and put the discipline after.
    r"\bengineer\s*[-,\u2013\u2014]\s*(?:process|operations|production|plant|reliability|chemical|refining|midstream|manufacturing)",
    r"\breliability\s+engineer",
    r"\bprocess\s+(?:control|safety|design|systems|technology|automation)\s+engineer",
    r"\b(?:r&d|research\s+and\s+development|research\s+&\s+development)\s+engineer",
    r"\bresearch\s+engineer",             # often R&D at chemical companies
    r"\bproduct\s+(?:development|engineer)\b",  # specialty chems
    r"\bprocess\s+technologist\b",
    # Rotational / Early-career programs (broad match, then filtered by exclude)
    r"\brotational\b",                    # any title with 'rotational'
    r"\b(?:early\s+career|new\s+grad(?:uate)?|graduate|entry[\s-]?level)\b",
    r"\b(?:engineering|engineer)\s+(?:development|leadership|trainee)\s+program",
    r"\bdevelopment\s+program\b",         # "Engineering Development Program"
    r"\bltop\b",                          # Linde Technical Operations Program
    r"\b(?:technical|engineering)\s+(?:operations|career\s+path)\s+program",
    r"\bassociate\s+engineer",
    r"\bjunior\s+engineer",
    r"\b(?:engineer|engineering)\s+trainee\b",
    r"\btrainee\s+engineer\b",
    # Industry-specific titles (chemical / pharma / bioprocess specialties)
    r"\bcatalyst\s+engineer\b",
    r"\bpolymer\s+engineer\b",
    r"\bformulation(?:s)?\s+engineer\b",
    r"\bfermentation\s+engineer\b",
    r"\bdownstream\s+(?:processing\s+)?engineer\b",   # bioprocess
    # Functional / mission-area titles
    r"\bprocess\s+intensification\b",
    r"\bsustainability\s+engineer\b",
    r"\bdecarbonization\s+engineer\b",
]

# Exclude these (senior signals + advanced levels).
EXCLUDE_PATTERNS = [
    r"\bsenior\b", r"\bsr\.?\b", r"\bprincipal\b", r"\bstaff\b",
    r"\bmanager\b", r"\bdirector\b", r"\bvp\b", r"\bvice\s+president\b",
    r"\bhead\s+of\b", r"\bchief\b",
    r"\blead\b",                          # exclude any "lead X engineer" or "X lead"
    r"\bexpert\b",
    r"\b(ii|iii|iv|v)\b",                # Roman 2–5
    r"\b(level\s*[2-9])\b",
    r"\bengineer\s*[2-9]\b",             # "Engineer 2", "Engineer 3" ...
    r"\bphd\b", r"\bdoctoral\b", r"\bpostdoc",
    r"\bintern\b", r"\binternship\b", r"\bco-?op\b",
    r"\bsales\b", r"\bmarketing\b",
    r"\b(?:executive|exec)\b",
    # Don't match support/sales/admin roles even if they contain rotational keywords
    r"\bcustomer\s+(?:service|support)\b",
    r"\b(?:hr|human\s+resources)\b",
    r"\bfinance\b", r"\baccounting\b", r"\blegal\b",
]

INCLUDE_RE = re.compile("|".join(INCLUDE_PATTERNS), re.IGNORECASE)
EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)


def title_matches(title: str) -> bool:
    if not title:
        return False
    if EXCLUDE_RE.search(title):
        return False
    return bool(INCLUDE_RE.search(title))


# ──────────────────────────────────────────────────────────────────────────────
# Fetchers — one per ATS
# ──────────────────────────────────────────────────────────────────────────────

class GreenhouseFetcher:
    """Public Greenhouse job board API. Free, no auth."""

    def fetch(self, cfg: dict) -> list[Job]:
        slug = cfg["slug"]
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            jobs.append(Job(
                company=cfg["display_name"],
                title=j.get("title", ""),
                location=(j.get("location") or {}).get("name", ""),
                url=j.get("absolute_url", ""),
                posted=j.get("updated_at", ""),
                job_id=str(j.get("id", "")),
            ))
        return jobs


class LeverFetcher:
    """Public Lever postings API. Free, no auth."""

    def fetch(self, cfg: dict) -> list[Job]:
        slug = cfg["slug"]
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
        jobs = []
        for j in data:
            cats = j.get("categories", {}) or {}
            location = cats.get("location", "")
            if not location and isinstance(cats.get("allLocations"), list) and cats["allLocations"]:
                location = cats["allLocations"][0]
            jobs.append(Job(
                company=cfg["display_name"],
                title=j.get("text", ""),
                location=location,
                url=j.get("hostedUrl", ""),
                posted=str(j.get("createdAt", "")),
                job_id=j.get("id", ""),
            ))
        return jobs


class AshbyFetcher:
    """Public Ashby job board API. Free, no auth."""

    def fetch(self, cfg: dict) -> list[Job]:
        slug = cfg["slug"]
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            jobs.append(Job(
                company=cfg["display_name"],
                title=j.get("title", ""),
                location=j.get("location", ""),
                url=j.get("jobUrl", ""),
                posted=j.get("publishedAt", ""),
                job_id=j.get("id", ""),
            ))
        return jobs


class SuccessFactorsRMKFetcher:
    """
    Scrapes SAP SuccessFactors Recruiting Marketing (RMK) sites.
    These have vanity careers domains (e.g. jobs.exxonmobil.com) but are
    actually SF RMK underneath. Tells:
      - cookie panel mentions "SAP as service provider"
      - logos load from rmkcdn.successfactors.com
      - listing URLs follow /go/{Category}/{NumericId}/
      - paginate by inserting an offset: /go/{Category}/{ID}/{offset}/
      - job detail URLs follow /{Company}/job/{Slug}/{NumericId}/

    Config:
      ats            : "rmk"
      base_url       : "https://jobs.exxonmobil.com"  (no trailing slash)
      category_paths : ["/go/Engineering/3845600/", ...]   (one or more)
                       OR search paths like ["/search/?q=engineer"] —
                       the fetcher auto-detects which kind it is.
      country_filter : ["US"]   (optional; default keeps US + unknown locations)
    """

    PAGE_SIZE = 25
    MAX_PAGES = 10  # safety cap = up to 250 jobs per category

    def fetch(self, cfg: dict) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            print("  RMK fetcher needs beautifulsoup4: pip install beautifulsoup4")
            return []

        base = cfg["base_url"].rstrip("/")
        category_paths = cfg.get("category_paths") or []
        country_ok = [c.upper() for c in (cfg.get("country_filter") or ["US"])]
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for cat_path in category_paths:
            is_search = "/search" in cat_path.lower()
            if not is_search and not cat_path.endswith("/"):
                cat_path += "/"
            empty_streak = 0
            for page in range(self.MAX_PAGES):
                offset = page * self.PAGE_SIZE
                if is_search:
                    # /search/?...&startrow=N pagination
                    sep = "&" if "?" in cat_path else "?"
                    url = f"{base}{cat_path}{sep}startrow={offset}"
                else:
                    # /go/{Category}/{ID}/{offset}/ pagination
                    if offset == 0:
                        url = f"{base}{cat_path}?sortColumn=referencedate&sortDirection=desc"
                    else:
                        url = f"{base}{cat_path}{offset}/?sortColumn=referencedate&sortDirection=desc"
                try:
                    r = requests.get(url, timeout=TIMEOUT,
                                     headers={"User-Agent": USER_AGENT})
                    if r.status_code != 200:
                        break
                except requests.RequestException as e:
                    print(f"  RMK fetch failed for {cfg['display_name']}: {e}")
                    break

                soup = BeautifulSoup(r.text, "html.parser")
                page_jobs = self._parse_page(soup, cfg, base, country_ok)
                new_count = 0
                for j in page_jobs:
                    if j.url in seen_urls:
                        continue
                    seen_urls.add(j.url)
                    jobs.append(j)
                    new_count += 1
                if new_count == 0:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break  # likely past the end
                else:
                    empty_streak = 0
                time.sleep(0.5)
        return jobs

    def _parse_page(self, soup, cfg, base, country_ok) -> list[Job]:
        out: list[Job] = []
        seen = set()
        # SF RMK renders results in a <table>; each <tr> has the job link in
        # the first cell and metadata in the rest.
        for row in soup.find_all("tr"):
            link = row.find("a", href=lambda h: h and "/job/" in h)
            if not link:
                continue
            href = link.get("href", "").strip()
            title = link.get_text(strip=True)
            if not href or not title:
                continue
            full_url = href if href.startswith("http") else base + href
            # Strip query strings/anchors so the same job has one stable key
            full_url = full_url.split("?")[0].split("#")[0]
            if full_url in seen:
                continue
            seen.add(full_url)

            cells = row.find_all("td")
            location = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            posted = cells[4].get_text(strip=True) if len(cells) > 4 else ""

            # Country filter — locations look like "Houston, TX, US" or
            # "Bengaluru, KA, IN". Keep US (or whatever's allowed). Empty
            # location → keep (better to over-notify than miss).
            if location and country_ok:
                tokens = [t.strip().upper() for t in location.split(",")]
                if not any(tok in country_ok for tok in tokens):
                    continue

            # Job ID = trailing numeric segment of the URL
            parts = [p for p in full_url.rstrip("/").split("/") if p]
            job_id = parts[-1] if parts and parts[-1].isdigit() else full_url

            out.append(Job(
                company=cfg["display_name"],
                title=title,
                location=location,
                url=full_url,
                posted=posted,
                job_id=str(job_id),
            ))
        return out


class WorkdayFetcher:
    """
    Workday's public CXS endpoint. Most large pharma/oil/chemicals/defense use this.
    Config requires:
      - api_url:  e.g. "https://msd.wd5.myworkdayjobs.com/wday/cxs/msd/SearchJobs/jobs"
      - base_url: e.g. "https://msd.wd5.myworkdayjobs.com/en-US/SearchJobs"
                  (used to build clickable job links)

    Strategy: pull ALL jobs (empty searchText) and let title_matches do the
    filtering. This is more robust than per-term keyword searches because:
      (a) some Workday tenants reject keyword searches with HTTP 422
      (b) keyword matching in Workday's index doesn't always match obvious
          titles (e.g. "Process Engineer I" might not match the search
          term "process engineer" depending on how the tenant has indexed)
    Capped at 1000 jobs per company to keep run time reasonable.
    """

    PAGE_SIZE = 20
    MAX_PAGES = 50   # 50 * 20 = 1000 jobs cap per company

    def fetch(self, cfg: dict) -> list[Job]:
        api_url = cfg["api_url"]
        base_url = cfg["base_url"].rstrip("/")
        seen_paths: set[str] = set()
        jobs: list[Job] = []

        offset = 0
        for page in range(self.MAX_PAGES):
            body = {
                "limit": self.PAGE_SIZE,
                "offset": offset,
                "searchText": "",
                "appliedFacets": {},
            }
            try:
                r = requests.post(
                    api_url,
                    json=body,
                    timeout=TIMEOUT,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                )
            except requests.RequestException as e:
                print(f"  Workday request failed for {cfg['display_name']}: {e}")
                break
            if r.status_code != 200:
                # Some tenants reject empty searchText with 400/422 — try a
                # fallback with a single broad keyword that nearly all
                # Workday tenants accept.
                if page == 0 and r.status_code in (400, 422):
                    return self._fetch_with_keyword(cfg)
                print(f"  Workday {cfg['display_name']} returned {r.status_code}")
                break
            try:
                data = r.json()
            except ValueError:
                break
            postings = data.get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                ext = p.get("externalPath", "")
                if not ext or ext in seen_paths:
                    continue
                seen_paths.add(ext)
                job_url = base_url + ext if ext.startswith("/") else f"{base_url}/{ext}"
                jobs.append(Job(
                    company=cfg["display_name"],
                    title=p.get("title", ""),
                    location=p.get("locationsText", ""),
                    url=job_url,
                    posted=p.get("postedOn", ""),
                    job_id=ext.split("/")[-1] or ext,
                ))
            if len(postings) < self.PAGE_SIZE:
                break
            offset += self.PAGE_SIZE
            time.sleep(0.3)
        return jobs

    def _fetch_with_keyword(self, cfg: dict) -> list[Job]:
        """Fallback for Workday tenants that reject empty searchText.
        Tries a single broad keyword 'engineer'."""
        api_url = cfg["api_url"]
        base_url = cfg["base_url"].rstrip("/")
        seen_paths: set[str] = set()
        jobs: list[Job] = []

        for term in ["engineer", "engineering"]:
            offset = 0
            for page in range(20):  # 400 jobs cap per term
                body = {
                    "limit": self.PAGE_SIZE,
                    "offset": offset,
                    "searchText": term,
                    "appliedFacets": {},
                }
                try:
                    r = requests.post(api_url, json=body, timeout=TIMEOUT,
                                      headers={"Accept": "application/json",
                                               "Content-Type": "application/json",
                                               "User-Agent": USER_AGENT})
                except requests.RequestException:
                    break
                if r.status_code != 200:
                    if page == 0:
                        print(f"  Workday {cfg['display_name']} returned "
                              f"{r.status_code} on fallback for {term!r}")
                    break
                try:
                    data = r.json()
                except ValueError:
                    break
                postings = data.get("jobPostings", [])
                if not postings:
                    break
                for p in postings:
                    ext = p.get("externalPath", "")
                    if not ext or ext in seen_paths:
                        continue
                    seen_paths.add(ext)
                    job_url = base_url + ext if ext.startswith("/") else f"{base_url}/{ext}"
                    jobs.append(Job(
                        company=cfg["display_name"],
                        title=p.get("title", ""),
                        location=p.get("locationsText", ""),
                        url=job_url,
                        posted=p.get("postedOn", ""),
                        job_id=ext.split("/")[-1] or ext,
                    ))
                if len(postings) < self.PAGE_SIZE:
                    break
                offset += self.PAGE_SIZE
                time.sleep(0.3)
            time.sleep(0.3)
        return jobs



class AvatureFetcher:
    """
    Scrapes Avature-hosted career sites. Avature is the platform behind, among
    others, jobs.totalenergies.com. Tells:
      - Template assets load from templates-static-assets.avacdn.net
      - Pagination URLs use ?jobOffset=N&jobRecordsPerPage=20
      - Each job is in an <h3> with an <a href="…/JobDetail/{slug}/{id}"> and a
        following <ul> of metadata (date, country, position type, subsidiary).

    Config:
      ats             : "avature"
      base_url        : "https://jobs.totalenergies.com"
      search_path     : "/en_US/careers/SearchJobs"
      country_filter  : ["US"]   (optional; default keeps US + unknown)
                        Also accepts "United States" — the filter checks each
                        comma- or slash-separated token.
    """

    PAGE_SIZE = 20
    MAX_PAGES = 15  # safety cap = 300 jobs per company

    # Tokens that match the user's country filter. Add aliases here.
    COUNTRY_ALIASES = {
        "US": {"US", "USA", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA"},
    }

    def fetch(self, cfg: dict) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            print("  Avature fetcher needs beautifulsoup4: pip install beautifulsoup4")
            return []

        base = cfg["base_url"].rstrip("/")
        search_path = cfg.get("search_path") or "/en_US/careers/SearchJobs"
        country_ok_codes = [c.upper() for c in (cfg.get("country_filter") or ["US"])]
        country_ok = set()
        for code in country_ok_codes:
            country_ok.update(self.COUNTRY_ALIASES.get(code, {code}))

        jobs: list[Job] = []
        seen_urls: set[str] = set()
        empty_streak = 0

        for page in range(self.MAX_PAGES):
            offset = page * self.PAGE_SIZE
            sep = "&" if "?" in search_path else "?"
            url = f"{base}{search_path}{sep}jobRecordsPerPage={self.PAGE_SIZE}&jobOffset={offset}"
            try:
                r = requests.get(url, timeout=TIMEOUT,
                                 headers={"User-Agent": USER_AGENT})
                if r.status_code != 200:
                    print(f"  Avature {cfg['display_name']} returned {r.status_code}")
                    break
            except requests.RequestException as e:
                print(f"  Avature fetch failed for {cfg['display_name']}: {e}")
                break

            soup = BeautifulSoup(r.text, "html.parser")
            page_jobs = self._parse_page(soup, cfg, base, country_ok)
            new_count = 0
            for j in page_jobs:
                if j.url in seen_urls:
                    continue
                seen_urls.add(j.url)
                jobs.append(j)
                new_count += 1
            if new_count == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    break  # likely past the end (or all duplicates)
            else:
                empty_streak = 0
            time.sleep(0.5)
        return jobs

    def _parse_page(self, soup, cfg, base, country_ok) -> list[Job]:
        out: list[Job] = []
        # Each job lives in an <h3> with a link to /JobDetail/.
        for link in soup.find_all("a", href=lambda h: h and "/JobDetail/" in h):
            title = link.get_text(strip=True)
            href = link.get("href", "").strip()
            if not title or not href:
                continue
            full_url = href if href.startswith("http") else base + href
            full_url = full_url.split("?")[0].split("#")[0]

            # Walk up to find a wrapping container, then look for the
            # adjacent <ul> with metadata (date, country, type, subsidiary).
            container = link.find_parent(["h3", "h2", "li", "article", "div"])
            metadata: list[str] = []
            ul = None
            if container is not None:
                ul = container.find_next_sibling("ul")
                if ul is None:
                    parent = container.find_parent()
                    if parent is not None:
                        ul = parent.find("ul")
            if ul is not None:
                metadata = [li.get_text(strip=True) for li in ul.find_all("li")]

            posted = metadata[0] if len(metadata) > 0 else ""
            location = metadata[1] if len(metadata) > 1 else ""

            # Country filter — TotalEnergies-style locations can be
            # "United States / US", "France", "Senegal", "United Arab Emirates".
            if location and country_ok:
                tokens = re.split(r"[,/]", location)
                tokens = [t.strip().upper() for t in tokens if t.strip()]
                if not any(tok in country_ok for tok in tokens):
                    continue

            # Job ID = trailing numeric segment of the JobDetail URL.
            parts = [p for p in full_url.rstrip("/").split("/") if p]
            job_id = parts[-1] if parts and parts[-1].isdigit() else full_url

            out.append(Job(
                company=cfg["display_name"],
                title=title,
                location=location,
                url=full_url,
                posted=posted,
                job_id=str(job_id),
            ))
        return out


class EightfoldFetcher:
    """
    Eightfold AI's public SmartApply JSON endpoint.
    Used by: Northrop Grumman (jobs.northropgrumman.com → domain=ngc.com).

    Config:
      ats             : "eightfold"
      base_url        : "https://jobs.northropgrumman.com"   (no trailing slash)
      domain          : "ngc.com"      (passed to the API as ?domain=)
      country_filter  : ["US"]   (optional; default keeps US + Remote + unknown)
    """

    PAGE_SIZE = 10           # Eightfold's default
    MAX_PAGES = 30           # safety cap = 300 jobs per company

    def fetch(self, cfg: dict) -> list[Job]:
        base = cfg["base_url"].rstrip("/")
        domain = cfg["domain"]
        country_ok = [c.upper() for c in (cfg.get("country_filter") or ["US"])]
        jobs: list[Job] = []
        seen_ids: set[str] = set()

        # Try both URL patterns. Some tenants put the API at /api/apply/v2/jobs,
        # others at /careers/api/apply/v2/jobs. We probe both on first request.
        api_paths_to_try = [
            "/api/apply/v2/jobs",
            "/careers/api/apply/v2/jobs",
            "/api/v1/jobs",
        ]
        working_path = None

        for page in range(self.MAX_PAGES):
            start = page * self.PAGE_SIZE
            data = None

            if working_path:
                paths = [working_path]
            else:
                paths = api_paths_to_try

            for path in paths:
                url = (f"{base}{path}?"
                       f"domain={domain}&hl=en&start={start}&num={self.PAGE_SIZE}")
                try:
                    r = requests.get(url, timeout=TIMEOUT,
                                     headers={"Accept": "application/json",
                                              "User-Agent": USER_AGENT})
                    if r.status_code == 200:
                        try:
                            data = r.json()
                            working_path = path
                            break
                        except ValueError:
                            continue
                except requests.RequestException as e:
                    print(f"  Eightfold fetch failed for {cfg['display_name']}: {e}")
                    return jobs

            if data is None:
                if not working_path:
                    print(f"  Eightfold {cfg['display_name']}: both URL patterns "
                          f"failed (tried {api_paths_to_try})")
                break

            positions = data.get("positions") or data.get("data", {}).get("positions", [])
            if not positions:
                break

            for p in positions:
                job_id = str(p.get("id") or p.get("display_job_id") or "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                title = p.get("name") or p.get("title") or ""
                # Location can be string OR {city,state,country}; handle both.
                loc = p.get("location") or ""
                if isinstance(loc, dict):
                    parts = [loc.get("city"), loc.get("state"), loc.get("country")]
                    loc = ", ".join([x for x in parts if x])
                country = (p.get("country") or "").upper()
                # locations[] can contain dicts OR plain strings like
                # "Charlotte, NC, United States". Handle both.
                if not country and isinstance(p.get("locations"), list) and p["locations"]:
                    first = p["locations"][0]
                    if isinstance(first, dict):
                        country = (first.get("country") or "").upper()
                    elif isinstance(first, str):
                        if not loc:
                            loc = first
                        # try to extract country from end of string
                        parts = [t.strip() for t in first.split(",")]
                        if parts:
                            country = parts[-1].upper()

                # Country filter — match either explicit `country` field or
                # presence of the country code in the location string.
                if country_ok:
                    loc_upper = (loc or "").upper()
                    keep = (country in country_ok) or any(
                        c in loc_upper for c in country_ok + ["UNITED STATES", "USA"]
                    ) or loc_upper.strip() == "" or "REMOTE" in loc_upper
                    if not keep:
                        continue

                # Job URL
                href = p.get("canonicalPositionUrl") or ""
                if not href:
                    href = f"{base}/careers/job/{job_id}?domain={domain}"

                # Posted date
                posted = ""
                if p.get("t_create"):
                    # epoch seconds
                    try:
                        from datetime import datetime, timezone
                        posted = datetime.fromtimestamp(
                            int(p["t_create"]), tz=timezone.utc
                        ).strftime("%Y-%m-%d")
                    except (ValueError, TypeError):
                        posted = str(p.get("t_create"))

                jobs.append(Job(
                    company=cfg["display_name"],
                    title=title,
                    location=loc,
                    url=href,
                    posted=posted,
                    job_id=job_id,
                ))

            if len(positions) < self.PAGE_SIZE:
                break
            time.sleep(0.4)
        return jobs


class PhenomFetcher:
    """
    Phenom People's public 'widgets' POST endpoint.
    Used by: EMD/Merck KGaA (careers.emdgroup.com),
             BAE Systems (jobs.baesystems.com).

    The widgets endpoint requires a per-site refNum, which is a short code
    embedded on the careers site. For EMD it's MQAEGZUS (visible in their
    CDN URLs like cdn.phenompeople.com/CareerConnectResources/MQAEGZUS/).

    Config:
      ats             : "phenom"
      display_name    : str
      base_url        : "https://careers.emdgroup.com"   (no trailing slash)
      ref_num         : "MQAEGZUS"
      country_filter  : ["US"] | ["United States"]   (optional; default ["US"])
      page_id         : "page20"     (optional; site-specific, default "page20")
      lang            : "en_global"  (optional; default "en_global")
    """

    PAGE_SIZE = 20
    MAX_PAGES = 15  # safety cap = 300 jobs per company

    def fetch(self, cfg: dict) -> list[Job]:
        base = cfg["base_url"].rstrip("/")
        ref_num = cfg.get("ref_num", "")

        # Auto-discover refNum if it's missing or looks like a placeholder
        # (placeholders are uppercase company names: ABBVIE, ROCHE, etc.).
        # A real Phenom refNum is 6-10 chars of mixed case+digits, e.g. MQAEGZUS.
        if not ref_num or self._looks_like_placeholder(ref_num):
            discovered = self._discover_ref_num(base)
            if discovered:
                if ref_num:
                    print(f"  Phenom {cfg['display_name']}: auto-discovered refNum "
                          f"{discovered!r} (replaces placeholder {ref_num!r})")
                ref_num = discovered
            elif not ref_num:
                print(f"  Phenom {cfg['display_name']}: no refNum configured "
                      f"and auto-discovery failed")
                return []

        country_filter = cfg.get("country_filter") or ["US"]
        country_ok = []
        for c in country_filter:
            if c.upper() == "US":
                country_ok.append("United States")
            else:
                country_ok.append(c)
        country_ok_upper = [c.upper() for c in country_ok]

        page_id = cfg.get("page_id", "page20")
        lang = cfg.get("lang", "en_global")
        jobs: list[Job] = []
        seen_ids: set[str] = set()

        for page in range(self.MAX_PAGES):
            payload = {
                "lang": lang,
                "deviceType": "desktop",
                "country": "global",
                "pageName": "search-results",
                "size": self.PAGE_SIZE,
                "from": page * self.PAGE_SIZE,
                "jobs": True,
                "counts": True,
                "all_fields": ["category", "country", "city", "type"],
                "clearAll": False,
                "jdsource": "facets",
                "isSliderEnable": False,
                "pageId": page_id,
                "siteType": "external",
                "keywords": "",
                "global": True,
                "selected_fields": {},
                "sort": {"order": "desc", "field": "postedDate"},
                "locationData": {},
                "refNum": ref_num,
                "ddoKey": "refineSearch",
            }
            try:
                r = requests.post(f"{base}/widgets", json=payload, timeout=TIMEOUT,
                                  headers={"Content-Type": "application/json",
                                           "Accept": "application/json",
                                           "User-Agent": USER_AGENT})
                if r.status_code != 200:
                    print(f"  Phenom {cfg['display_name']} returned {r.status_code}")
                    break
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                print(f"  Phenom fetch failed for {cfg['display_name']}: {e}")
                break

            # Response shape varies a bit by Phenom version. Look for an
            # 'eagerLoadRefineSearch' or top-level 'refineSearch' or 'jobs'.
            container = (data.get("eagerLoadRefineSearch")
                         or data.get("refineSearch")
                         or data)
            jobs_list = container.get("data", {}).get("jobs", []) \
                if isinstance(container.get("data"), dict) else container.get("jobs", [])
            if not jobs_list:
                break

            for j in jobs_list:
                job_id = str(j.get("jobId") or j.get("jobSeqNo") or j.get("id") or "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                title = j.get("title") or j.get("jobTitle") or ""
                country = (j.get("country") or "").strip()
                city = (j.get("city") or "").strip()
                state = (j.get("state") or "").strip()
                location = ", ".join([p for p in [city, state, country] if p])

                # Country filter
                if country_ok and country:
                    if country.upper() not in country_ok_upper and \
                       not any(c.upper() in country.upper() for c in country_ok):
                        continue

                href = j.get("jobURL") or j.get("ml_job_url") or ""
                if href and not href.startswith("http"):
                    href = base + href if href.startswith("/") else f"{base}/{href}"
                if not href:
                    href = f"{base}/job/{job_id}"

                posted = j.get("postedDate") or j.get("postingDate") or ""

                jobs.append(Job(
                    company=cfg["display_name"],
                    title=title,
                    location=location,
                    url=href,
                    posted=posted,
                    job_id=job_id,
                ))

            if len(jobs_list) < self.PAGE_SIZE:
                break
            time.sleep(0.4)
        return jobs

    @staticmethod
    def _looks_like_placeholder(ref_num: str) -> bool:
        """Heuristic: real Phenom refNums are mixed case/digits (e.g. MQAEGZUS,
        EBKEGNUF, GAOGAYGLOBAL). Placeholders are all-uppercase common-English
        words (ABBVIE, ROCHE, REGENERON). Treat short all-letter all-uppercase
        strings as placeholders that need rediscovery."""
        if not ref_num or len(ref_num) > 20:
            return False
        # Real refNums almost always have at least one of: digit, lowercase
        # letter, or are 8+ chars of consonant-heavy mixed letters.
        if any(c.isdigit() or c.islower() for c in ref_num):
            return False
        # All uppercase, no digits — looks like a placeholder English word.
        # Whitelist a few known-real all-caps refNums that don't follow the
        # typical pattern, in case Phenom uses them.
        return True

    def _discover_ref_num(self, base_url: str) -> str:
        """Scrape the careers site's search-results page to extract the refNum
        from inline JS. Tries common URL patterns."""
        candidate_paths = [
            "/global/en/search-results",
            "/en/search-results",
            "/search-results",
            "/jobs",
            "/en/jobs",
            "/",
        ]
        for path in candidate_paths:
            url = base_url.rstrip("/") + path
            try:
                r = requests.get(url, timeout=TIMEOUT,
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "text/html"})
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            # Multiple patterns the refNum might appear in
            patterns = [
                r'"refNum"\s*:\s*"([A-Za-z0-9]+)"',
                r"refNum\s*=\s*['\"]([A-Za-z0-9]+)['\"]",
                r"cdn\.phenompeople\.com/CareerConnectResources/([A-Z0-9]+)/",
            ]
            for pat in patterns:
                m = re.search(pat, r.text)
                if m:
                    return m.group(1)
        return ""


class CornerstoneFetcher:
    """
    Cornerstone OnDemand (CSOD) careers. Used by: Linde (linde.csod.com).

    CSOD is a 2-step fetch:
      1. GET the careers homepage HTML to extract a JWT token + cloud endpoint
         (both are embedded in inline JS).
      2. POST {cloud_endpoint}/rec-job-search/external/jobs with the token in
         an Authorization header and a JSON payload of search filters.

    Config:
      ats             : "cornerstone"
      display_name    : str
      tenant          : "linde"          (the {tenant}.csod.com subdomain)
      site_id         : 20               (numeric career site ID from URL)
      country_filter  : ["US"]   (optional; default ["US"])

    The site_id can be found in the URL after /careersite/, e.g.
    https://linde.csod.com/ux/ats/careersite/20/home → site_id = 20.
    """

    PAGE_SIZE = 25
    MAX_PAGES = 12  # safety cap = 300 jobs per company

    # US country codes Cornerstone may use ("US", "USA", or numeric IDs vary).
    # We post countryCodes=[] (no filter) and post-filter by location string.
    US_LOCATION_TOKENS = {"US", "USA", "U.S.", "U.S.A.", "UNITED STATES",
                          "UNITED STATES OF AMERICA"}

    def fetch(self, cfg: dict) -> list[Job]:
        tenant = cfg["tenant"]
        site_id = int(cfg["site_id"])
        country_ok = set()
        for c in (cfg.get("country_filter") or ["US"]):
            if c.upper() == "US":
                country_ok.update(self.US_LOCATION_TOKENS)
            else:
                country_ok.add(c.upper())

        token, cloud_endpoint = self._fetch_token(tenant, site_id)
        if not token or not cloud_endpoint:
            print(f"  Cornerstone {cfg['display_name']}: couldn't extract token")
            return []

        api_url = f"{cloud_endpoint.rstrip('/')}/rec-job-search/external/jobs"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": f"https://{tenant}.csod.com",
            "Referer": f"https://{tenant}.csod.com/",
            "Csod-Accept-Language": "en-US",
            "User-Agent": USER_AGENT,
        }
        jobs: list[Job] = []
        seen_ids: set[str] = set()

        for page in range(1, self.MAX_PAGES + 1):
            payload = {
                "careerSiteId": site_id,
                "careerSitePageId": 1,
                "pageNumber": page,
                "pageSize": self.PAGE_SIZE,
                "cultureId": 1,
                "searchText": "",
                "cultureName": "en-US",
                "states": [],
                "countryCodes": [],
                "cities": [],
                "placeID": "",
                "radius": None,
                "postingsWithinDays": None,
                "customFieldCheckboxKeys": [],
                "customFieldDropdowns": [],
                "customFieldRadios": [],
            }
            try:
                r = requests.post(api_url, json=payload, headers=headers, timeout=TIMEOUT)
                if r.status_code != 200:
                    print(f"  Cornerstone {cfg['display_name']} returned {r.status_code}")
                    break
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                print(f"  Cornerstone fetch failed for {cfg['display_name']}: {e}")
                break

            requisitions = data.get("data", {}).get("requisitions", [])
            if not requisitions:
                break
            total = data.get("data", {}).get("totalCount", 0)

            for req in requisitions:
                req_id = str(req.get("requisitionId") or req.get("id") or "")
                if not req_id or req_id in seen_ids:
                    continue
                seen_ids.add(req_id)

                title = req.get("title") or req.get("displayJobTitle") or ""
                # Location: usually req["primaryLocation"] or req["locations"][0]
                location = req.get("primaryLocation") or ""
                if not location and isinstance(req.get("locations"), list) and req["locations"]:
                    loc0 = req["locations"][0]
                    if isinstance(loc0, dict):
                        bits = [loc0.get("city"), loc0.get("state"), loc0.get("country")]
                        location = ", ".join([x for x in bits if x])
                    else:
                        location = str(loc0)

                # Country filter on location string
                if country_ok and location:
                    tokens = re.split(r"[,/]", location)
                    tokens = [t.strip().upper() for t in tokens if t.strip()]
                    if not any(t in country_ok for t in tokens):
                        continue

                # Job URL — best-effort; CSOD uses requisition IDs in the URL
                href = (f"https://{tenant}.csod.com/ux/ats/careersite/{site_id}/"
                        f"home/requisition/{req_id}?c={tenant}")

                posted = req.get("postingStartDate") or ""

                jobs.append(Job(
                    company=cfg["display_name"],
                    title=title,
                    location=location,
                    url=href,
                    posted=posted,
                    job_id=req_id,
                ))

            if len(jobs) >= total or len(requisitions) < self.PAGE_SIZE:
                break
            time.sleep(0.6)
        return jobs

    def _fetch_token(self, tenant: str, site_id: int) -> tuple[str, str]:
        """Scrape the careers page to extract JWT token + cloud endpoint."""
        url = f"https://{tenant}.csod.com/ux/ats/careersite/{site_id}/home?c={tenant}"
        try:
            r = requests.get(url, timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
            if r.status_code != 200:
                return "", ""
            html = r.text
        except requests.RequestException:
            return "", ""

        token = ""
        endpoint = ""
        # Token is in inline JS as a JWT (eyJ... three base64 chunks separated by .)
        m = re.search(r'"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"', html)
        if m:
            token = m.group(1)
        # Cloud endpoint looks like https://something.api.csod.com or similar
        m = re.search(r'"(https://[a-z0-9.-]*api[a-z0-9.-]*\.csod\.com)"', html)
        if m:
            endpoint = m.group(1)
        if not endpoint:
            # Fallback: try a common default
            endpoint = f"https://{tenant}.csod.com"
        return token, endpoint


class TaleoFetcher:
    """
    Oracle Taleo career sites. URL structure:
      https://{zone}.taleo.net/careersection/{cs_code}/jobsearch.ftl?lang=en

    Strategy: use Taleo's built-in RSS feed when available
      https://{zone}.taleo.net/careersection/{cs_code}/rss.ftl?lang=en
    Falls back to HTML scraping of the search page if RSS isn't enabled.

    Config:
      ats             : "taleo"
      display_name    : str
      zone            : str       — subdomain, e.g. "valero" for valero.taleo.net
      cs_code         : str       — career section code, e.g. "2", "career", "ext"
      country_filter  : list[str] (optional)
    """

    MAX_PAGES = 10
    PAGE_SIZE = 25

    def fetch(self, cfg: dict) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            print("  Taleo fetcher needs beautifulsoup4: pip install beautifulsoup4")
            return []

        zone = cfg["zone"]
        cs_code = cfg["cs_code"]
        base = f"https://{zone}.taleo.net"

        # Try RSS feed first
        rss_url = f"{base}/careersection/{cs_code}/rss.ftl?lang=en"
        try:
            r = requests.get(rss_url, timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT,
                                      "Accept": "application/rss+xml,application/xml"})
            if r.status_code == 200 and ("<rss" in r.text or "<feed" in r.text):
                return self._parse_rss(r.text, cfg, base)
        except requests.RequestException as e:
            print(f"  Taleo RSS fetch failed for {cfg['display_name']}: {e}")

        # Fallback: HTML scrape of the search page
        return self._scrape_html(cfg, base, cs_code, BeautifulSoup)

    def _parse_rss(self, xml_text: str, cfg: dict, base: str) -> list[Job]:
        """Parse Taleo's RSS feed. Each <item> has title, link, pubDate, description."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []
        soup = BeautifulSoup(xml_text, "xml")
        jobs: list[Job] = []
        for item in soup.find_all("item"):
            title = (item.title.get_text(strip=True) if item.title else "")
            link = (item.link.get_text(strip=True) if item.link else "")
            pub_date = (item.pubDate.get_text(strip=True) if item.pubDate else "")
            description = (item.description.get_text(strip=True) if item.description else "")
            # Try to extract location from description (Taleo descriptions
            # often contain location text after a "-" or in parentheses).
            location = ""
            m = re.search(r"(?:Location|Lieu|Ubicación)[:\s]+([^|\n<]+)", description, re.I)
            if m:
                location = m.group(1).strip()
            else:
                # Heuristic: many feeds put "City, ST" at the end of the title or description
                m2 = re.search(r"([A-Za-z .]+,\s*[A-Z]{2}(?:,\s*US)?)", title + " " + description)
                if m2:
                    location = m2.group(1).strip()

            # Job ID: extract from URL (?job=12345 or /jobid/12345)
            job_id = ""
            m3 = re.search(r"job[=/](\d+)", link, re.I)
            if m3:
                job_id = m3.group(1)
            else:
                job_id = link[-50:]   # last chunk as fallback

            jobs.append(Job(
                company=cfg["display_name"],
                title=title,
                location=location,
                url=link,
                posted=pub_date,
                job_id=job_id,
            ))
        return jobs

    def _scrape_html(self, cfg, base, cs_code, BeautifulSoup) -> list[Job]:
        """Fallback when RSS isn't available — scrape the search results page.

        Most Taleo sites render results in a server-side table/list. We look
        for any <a href> matching the jobdetail.ftl pattern.
        """
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        for page in range(self.MAX_PAGES):
            offset = page * self.PAGE_SIZE + 1
            url = (f"{base}/careersection/{cs_code}/jobsearch.ftl?"
                   f"lang=en&radiusType=K&searchExpanded=true"
                   f"&pageNo={page + 1}&searchByLocation=false")
            try:
                r = requests.get(url, timeout=TIMEOUT,
                                 headers={"User-Agent": USER_AGENT})
                if r.status_code != 200:
                    break
            except requests.RequestException:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            page_count = 0
            for link in soup.find_all("a", href=lambda h: h and "jobdetail.ftl" in h):
                href = link.get("href", "")
                title = link.get_text(strip=True)
                if not href or not title:
                    continue
                full_url = href if href.startswith("http") else (base + href if href.startswith("/") else f"{base}/{href}")
                m = re.search(r"job=(\d+)", full_url)
                job_id = m.group(1) if m else full_url[-30:]
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Try to find location in nearby cells
                location = ""
                row = link.find_parent(["tr", "li", "div"])
                if row:
                    # Look for a sibling cell or span containing location-like text
                    for cell in row.find_all(["td", "span", "div"]):
                        txt = cell.get_text(strip=True)
                        if re.search(r"[A-Za-z .]+,\s*[A-Z]{2}", txt) and txt != title:
                            location = txt
                            break

                jobs.append(Job(
                    company=cfg["display_name"],
                    title=title,
                    location=location,
                    url=full_url,
                    posted="",
                    job_id=job_id,
                ))
                page_count += 1
            if page_count == 0:
                break
            time.sleep(0.5)
        return jobs


class GenericHtmlFetcher:
    """
    Last-resort fetcher for fully custom careers sites that don't use any
    standard ATS. Scrapes an HTML page and extracts any <a> links whose text
    looks like a job title and URL looks like a job detail page.

    This is a "best effort" approach — it works well for sites like
    careers.lockheedmartinjobs.com, careers.bms.com, careers.regeneron.com
    that render their job lists server-side. It will NOT work for sites that
    render the listing entirely in JavaScript.

    Config:
      ats              : "generic_html"
      display_name     : str
      url              : str       — search results page URL
      link_must_contain: str | list[str] — substring(s) job links must contain
                                     in their href to be considered a job link
                                     (e.g. "/job/", "/jobs/", "?pid=")
      max_pages        : int (optional) — for sites with ?page=N pagination
      page_param       : str (optional) — query param name for pagination
      country_filter   : list[str] (optional)

    NOTE: this fetcher cannot extract dates or locations reliably for every
    site, so the post-fetch filters may be less effective. Use sparingly.
    """

    DEFAULT_MAX_PAGES = 5

    def fetch(self, cfg: dict) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            print("  Generic HTML fetcher needs beautifulsoup4")
            return []

        url_template = cfg["url"]
        markers = cfg.get("link_must_contain")
        if isinstance(markers, str):
            markers = [markers]
        if not markers:
            print(f"  {cfg['display_name']}: generic_html requires 'link_must_contain'")
            return []

        max_pages = cfg.get("max_pages", self.DEFAULT_MAX_PAGES)
        page_param = cfg.get("page_param")
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for page in range(max_pages):
            if page_param and page > 0:
                sep = "&" if "?" in url_template else "?"
                url = f"{url_template}{sep}{page_param}={page + 1}"
            elif page > 0:
                break  # no pagination configured
            else:
                url = url_template
            try:
                r = requests.get(url, timeout=TIMEOUT,
                                 headers={"User-Agent": USER_AGENT})
                if r.status_code != 200:
                    print(f"  {cfg['display_name']} returned {r.status_code}")
                    break
            except requests.RequestException as e:
                print(f"  {cfg['display_name']} fetch failed: {e}")
                break

            soup = BeautifulSoup(r.text, "html.parser")
            page_count = 0
            # Get the base URL from the request URL for resolving relative hrefs
            from urllib.parse import urlparse, urljoin
            parsed = urlparse(url)
            url_base = f"{parsed.scheme}://{parsed.netloc}"

            for link in soup.find_all("a", href=True):
                href = link["href"]
                if not any(m in href for m in markers):
                    continue
                title = link.get_text(strip=True)
                if not title or len(title) < 5 or len(title) > 200:
                    continue
                # Skip "Apply Now" / "View Job" / nav-style links
                if title.lower() in {"apply", "apply now", "view job", "see more",
                                     "learn more", "details", "read more"}:
                    continue
                full_url = href if href.startswith("http") else urljoin(url_base, href)
                full_url = full_url.split("#")[0]
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # Best-effort location extraction from neighboring elements
                location = ""
                container = link.find_parent(["li", "article", "tr", "div"])
                if container:
                    text = container.get_text(" ", strip=True)
                    m = re.search(r"([A-Za-z .]+,\s*[A-Z]{2}(?:,\s*US)?)", text)
                    if m:
                        location = m.group(1).strip()

                # Job ID: best-effort from the URL
                job_id_m = re.search(r"(\d{4,})", full_url)
                job_id = job_id_m.group(1) if job_id_m else full_url[-40:]

                jobs.append(Job(
                    company=cfg["display_name"],
                    title=title,
                    location=location,
                    url=full_url,
                    posted="",
                    job_id=job_id,
                ))
                page_count += 1

            if page_count == 0:
                break
            time.sleep(0.5)
        return jobs


class BrassRingFetcher:
    """
    Kenexa BrassRing (TalentLink) — IBM/Infor's legacy ATS.
    Used by: General Atomics (partnerid=25539, siteid=5313), Lockheed Martin
    (different siteid), and many other large defense/industrial companies.

    BrassRing exposes a documented JSON search endpoint:
      POST https://sjobs.brassring.com/TGnewUI/Search/Ajax/SearchResults
    with {partnerId, siteId, keyword, sortBy, ...} parameters. The response is
    JSON with a 'Jobs' array.

    Config:
      ats             : "brassring"
      display_name    : str
      partner_id      : 25539     (e.g. General Atomics)
      site_id         : 5313      (matches the search-page URL)
      country_filter  : ["US"]    (optional; default ["US"])
    """

    PAGE_SIZE = 25
    MAX_PAGES = 12  # safety cap = 300 jobs per company

    def fetch(self, cfg: dict) -> list[Job]:
        partner_id = str(cfg["partner_id"])
        site_id = str(cfg["site_id"])
        api_url = "https://sjobs.brassring.com/TGnewUI/Search/Ajax/SearchResults"
        search_home = (f"https://sjobs.brassring.com/TGnewUI/Search/Home/Home"
                       f"?partnerid={partner_id}&siteid={site_id}")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": USER_AGENT,
            "Referer": search_home,
            "Origin": "https://sjobs.brassring.com",
        }

        # Establish a session so cookies (e.g. anti-CSRF) are persisted.
        session = requests.Session()
        try:
            session.get(search_home, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"  BrassRing {cfg['display_name']}: home page fetch failed: {e}")
            return []

        jobs: list[Job] = []
        seen_ids: set[str] = set()

        for page in range(1, self.MAX_PAGES + 1):
            payload = {
                "partnerId": partner_id,
                "siteId": site_id,
                "Keyword": "",
                "Latitude": "",
                "Longitude": "",
                "powersearchoption": "PowerSearch",
                "pageNumber": str(page),
                "FromIndex": str((page - 1) * self.PAGE_SIZE + 1),
                "ToIndex": str(page * self.PAGE_SIZE),
                "Facet": "",
                "Filter": "",
                "Sortby": "1",         # 1 = Posted Date desc
                "SortDirection": "DESC",
                "EnableLOVDescription": "0",
                "ProSearchOptions": "0",
                "isDistributedJobLink": "false",
                "AdditionalParameters": "",
            }
            try:
                r = session.post(api_url, data=payload, headers=headers, timeout=TIMEOUT)
                if r.status_code != 200:
                    print(f"  BrassRing {cfg['display_name']} returned {r.status_code}")
                    break
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                print(f"  BrassRing fetch failed for {cfg['display_name']}: {e}")
                break

            # Response structure: {"Jobs": [...]}.  Some installs nest under
            # 'JobsResults' or 'Records' instead.
            page_jobs = (data.get("Jobs") or data.get("JobsResults")
                         or data.get("Records") or [])
            if not page_jobs:
                break

            new_count = 0
            for j in page_jobs:
                # BrassRing's response uses Pascal-case keys but the exact
                # names vary by tenant. Try common variants.
                job_id = str(j.get("JobId") or j.get("Jobid") or j.get("AutoReqId") or "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                title = (j.get("Title") or j.get("JobTitle")
                         or j.get("PositionTitle") or "")
                location = (j.get("Location") or j.get("LocationDisplay")
                            or j.get("JobLocation") or "")
                # Posting date variants
                posted = (j.get("PostingDate") or j.get("PostedDate")
                          or j.get("PostingDateDisplay") or "")

                # Build a stable URL to the job detail page
                job_url = (f"https://sjobs.brassring.com/TGnewUI/Search/home/"
                           f"HomeWithPreLoad?partnerid={partner_id}"
                           f"&siteid={site_id}&PageType=JobDetails&jobid={job_id}")

                jobs.append(Job(
                    company=cfg["display_name"],
                    title=title,
                    location=location,
                    url=job_url,
                    posted=posted,
                    job_id=job_id,
                ))
                new_count += 1

            if new_count == 0 or len(page_jobs) < self.PAGE_SIZE:
                break
            time.sleep(0.5)

        return jobs


FETCHERS = {
    "greenhouse": GreenhouseFetcher(),
    "lever": LeverFetcher(),
    "ashby": AshbyFetcher(),
    "workday": WorkdayFetcher(),
    "rmk": SuccessFactorsRMKFetcher(),
    "avature": AvatureFetcher(),
    "eightfold": EightfoldFetcher(),
    "phenom": PhenomFetcher(),
    "cornerstone": CornerstoneFetcher(),
    "brassring": BrassRingFetcher(),
    "taleo": TaleoFetcher(),
    "generic_html": GenericHtmlFetcher(),
}


# ──────────────────────────────────────────────────────────────────────────────
# Notifiers
# ──────────────────────────────────────────────────────────────────────────────

def send_discord(jobs: list[Job], webhook_url: str) -> None:
    """Discord allows up to 10 embeds per message."""
    for i in range(0, len(jobs), 10):
        batch = jobs[i:i + 10]
        embeds = []
        for j in batch:
            embeds.append({
                "title": j.title[:256] or "(untitled)",
                "url": j.url,
                "description": f"**{j.company}** — {j.location or 'location TBD'}"[:2000],
                "footer": {"text": f"Posted: {j.posted}" if j.posted else "Posted recently"},
                "color": 0x2eb886,
            })
        payload = {
            "content": f"🔔 **{len(batch)} new role{'s' if len(batch) != 1 else ''}**",
            "embeds": embeds,
        }
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.status_code >= 300:
                print(f"  Discord error {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"  Discord send failed: {e}")
        time.sleep(0.5)


def send_ntfy(jobs: list[Job], topic: str) -> None:
    """ntfy.sh: free push to phone. No account. Just install the app + pick a topic name."""
    base = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    for j in jobs:
        body = f"{j.company} • {j.location or 'location TBD'}\n{j.url}"
        try:
            requests.post(
                f"{base}/{topic}",
                data=body.encode("utf-8"),
                headers={
                    "Title": (j.title or "New job")[:100].encode("utf-8"),
                    "Click": j.url,
                    "Priority": "default",
                    "Tags": "briefcase",
                },
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"  ntfy send failed: {e}")
        time.sleep(0.2)


def notify(jobs: list[Job]) -> None:
    if not jobs:
        return
    discord = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    ntfy = os.environ.get("NTFY_TOPIC", "").strip()
    if not discord and not ntfy:
        print("⚠  No DISCORD_WEBHOOK_URL or NTFY_TOPIC set — printing instead.")
        for j in jobs:
            print(f"  • [{j.company}] {j.title} — {j.location} — {j.url}")
        return
    if discord:
        send_discord(jobs, discord)
    if ntfy:
        send_ntfy(jobs, ntfy)


# ──────────────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0))


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def run_validate(args) -> int:
    """Health check: try fetching each company once, print pass/fail summary.

    Exit codes:
      0 — all companies returned at least one job
      1 — one or more companies returned zero jobs or errored
    """
    print("Validating fetchers — this hits every company once. Takes ~3-5 min.\n")
    print(f"{'Company':32s}  {'ATS':12s}  {'Result':24s}  Detail")
    print("-" * 100)

    failed: list[tuple[str, str, str]] = []
    zero_jobs: list[tuple[str, str]] = []
    ok_count = 0

    for cfg in COMPANIES:
        if args.only and args.only.lower() not in cfg["display_name"].lower():
            continue
        ats = cfg.get("ats")
        fetcher = FETCHERS.get(ats)
        name = cfg["display_name"]
        if not fetcher:
            print(f"{name:32s}  {ats or '?':12s}  ⚠  no fetcher        unknown ATS")
            failed.append((name, ats or "?", "unknown ATS"))
            continue
        try:
            jobs = fetcher.fetch(cfg)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:60]}"
            print(f"{name:32s}  {ats:12s}  ✗  ERROR              {err}")
            failed.append((name, ats, err))
            continue
        n = len(jobs)
        if n == 0:
            print(f"{name:32s}  {ats:12s}  ⚠  0 jobs returned    "
                  f"check URL/ref_num/site_id")
            zero_jobs.append((name, ats))
        else:
            kept = sum(1 for j in jobs if title_matches(j.title))
            print(f"{name:32s}  {ats:12s}  ✓  {n:4d} jobs ({kept} match title filter)")
            ok_count += 1

        # --debug: print every title with a ✓ (would notify) or · (filtered out)
        # marker. Useful for tuning INCLUDE_PATTERNS / EXCLUDE_PATTERNS.
        if args.debug and jobs:
            for j in jobs:
                if title_matches(j.title):
                    marker = "✓"
                else:
                    # Show WHY it was filtered: include miss vs exclude hit
                    if EXCLUDE_RE.search(j.title):
                        marker = "✗ excl"
                    else:
                        marker = "·"
                loc = j.location[:30] if j.location else ""
                print(f"      {marker:7s} {j.title[:70]:70s}  {loc}")

    print("-" * 100)
    print(f"\nSummary: {ok_count} OK, {len(zero_jobs)} returned zero, {len(failed)} errored.\n")

    if failed:
        print("Errored companies (config likely wrong):")
        for name, ats, err in failed:
            print(f"  ✗ {name} ({ats}): {err}")
        print()
    if zero_jobs:
        print("Zero-job companies (URL might be wrong, or the company truly has no")
        print("listings right now — check the careers page in a browser):")
        for name, ats in zero_jobs:
            print(f"  ⚠ {name} ({ats})")
        print()

    return 0 if not failed and not zero_jobs else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor company ATSs for new chemE/process roles.")
    parser.add_argument("--notify-all", action="store_true",
                        help="Send notifications for everything matched, even on first run.")
    parser.add_argument("--debug", action="store_true",
                        help="Print every job title seen (matched or not).")
    parser.add_argument("--only", default="", help="Only run for companies whose name contains this substring.")
    parser.add_argument("--posted-within", default="",
                        help=("Only keep jobs posted within this window. "
                              "Examples: '24h', '1d', '7d', '2w', '30d'. "
                              "Plain integers are treated as days. "
                              "Also reads JOB_POSTED_WITHIN env var if flag is empty. "
                              "Jobs with unparseable post dates are kept (better safe than missed)."))
    parser.add_argument("--all-locations", action="store_true",
                        help=("Disable the US-only location filter. By default, "
                              "non-US jobs are filtered out. Locations that are "
                              "ambiguous or empty are always kept."))
    parser.add_argument("--validate", action="store_true",
                        help=("Health-check mode: hit every configured company once, "
                              "print a pass/fail table showing which fetched any jobs, "
                              "and exit. Use this after editing companies.py to catch "
                              "broken URLs before relying on the cron."))
    args = parser.parse_args(argv)

    if args.validate:
        return run_validate(args)

    # Resolve age filter (CLI flag wins over env var)
    age_spec = args.posted_within or os.environ.get("JOB_POSTED_WITHIN", "").strip()
    max_age = parse_age_filter(age_spec) if age_spec else None
    if age_spec and max_age is None:
        print(f"⚠  Couldn't parse --posted-within={age_spec!r}. "
              f"Use formats like '24h', '7d', '2w'. Ignoring.")
    if max_age:
        print(f"Age filter: only keeping jobs posted in the last {age_spec} "
              f"({max_age}).")

    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    if first_run:
        print("First run — populating seen-jobs cache. Use --notify-all to push these.")

    matched_jobs: list[Job] = []
    fetched_total = 0
    age_filtered = 0
    location_filtered = 0
    errors: list[tuple[str, str]] = []
    cutoff = datetime.now(timezone.utc) - max_age if max_age else None
    us_only = not args.all_locations

    for cfg in COMPANIES:
        if args.only and args.only.lower() not in cfg["display_name"].lower():
            continue
        ats = cfg.get("ats")
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            continue
        name = cfg["display_name"]
        try:
            jobs = fetcher.fetch(cfg)
        except Exception as e:  # noqa: BLE001 — keep loop alive
            errors.append((name, f"{type(e).__name__}: {e}"))
            if args.debug:
                traceback.print_exc()
            continue
        fetched_total += len(jobs)
        new_for_company = 0
        for j in jobs:
            if args.debug:
                marker = "✓" if title_matches(j.title) else " "
                print(f"  {marker} [{name}] {j.title} ({j.location})")
            if not title_matches(j.title):
                continue

            # US-only location filter
            if us_only and not is_us_location(j.location):
                location_filtered += 1
                if args.debug:
                    print(f"      (skipped: non-US location: {j.location!r})")
                continue

            # Age filter
            if cutoff is not None:
                posted_dt = parse_posted_date(j.posted)
                if posted_dt is not None and posted_dt < cutoff:
                    age_filtered += 1
                    if args.debug:
                        print(f"      (skipped: posted {j.posted}, older than cutoff)")
                    continue
                # If unparseable, keep — better to over-notify than miss.

            if j.key() in seen:
                continue
            seen.add(j.key())
            matched_jobs.append(j)
            new_for_company += 1
        if new_for_company:
            print(f"  + {name}: {new_for_company} new match(es)")

    print(f"\nFetched {fetched_total} postings across {len(COMPANIES)} companies.")
    print(f"Matched {len(matched_jobs)} new role(s).")
    if location_filtered:
        print(f"Filtered out {location_filtered} non-US role(s). "
              f"Use --all-locations to include them.")
    if age_filtered:
        print(f"Filtered out {age_filtered} role(s) older than {age_spec}.")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for name, err in errors:
            print(f"  ✗ {name}: {err}")

    save_seen(seen)

    should_notify = bool(matched_jobs) and (args.notify_all or not first_run)
    if should_notify:
        print(f"\nSending {len(matched_jobs)} notification(s)…")
        notify(matched_jobs)
    elif first_run and matched_jobs:
        print(f"\n(Skipping notifications on first run — would have sent {len(matched_jobs)}.)")
        print("Re-run with --notify-all to push these.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
