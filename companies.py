"""
Company list and ATS endpoints.

Each entry needs:
  display_name : str         — for notifications
  ats          : str         — see fetcher list below
  + ATS-specific config (see docstrings on each fetcher class)

Supported fetchers:
  workday, greenhouse, lever, ashby, rmk, avature, eightfold, phenom,
  cornerstone, brassring, taleo, generic_html

How to identify which platform a company uses:
  - Workday        : URL contains "myworkdayjobs.com"
  - Greenhouse     : "boards.greenhouse.io/{slug}" or "{slug}.greenhouse.io"
  - Lever          : "jobs.lever.co/{slug}"
  - Ashby          : "jobs.ashbyhq.com/{slug}"
  - SF RMK         : cookie banner mentions "SAP", logo URL is rmkcdn.successfactors.com
  - Avature        : assets from "templates-static-assets.avacdn.net"
  - Eightfold AI   : "{slug}.eightfold.ai/careers" — note the /careers/ path
  - Phenom People  : assets from "cdn.phenompeople.com"
  - Cornerstone    : "{tenant}.csod.com/ux/ats/careersite/..."
  - BrassRing      : "sjobs.brassring.com/TGnewUI/...?partnerid=N&siteid=N"
  - Taleo          : "{zone}.taleo.net/careersection/{cs}/jobsearch.ftl"
  - generic_html   : last-resort scraper for fully custom sites

⚠ ENTRIES MARKED WITH ⚠ ARE BEST-EFFORT: After running --validate, fix any
  that return 0 jobs by viewing the actual careers page and checking:
   - Phenom: search the HTML source for `refNum` and copy the value
   - Workday: confirm the URL structure tenant/pod/site
   - generic_html: confirm the link_must_contain pattern matches actual jobs
"""

DEFAULT_SEARCH_TERMS = [
    "process engineer",
    "chemical engineer",
    "process development",
    "refining engineer",
    "midstream",
]


def _wd(tenant: str, pod: str, site: str) -> dict:
    """Helper to build Workday api_url + base_url from the three pieces."""
    return {
        "api_url": f"https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
        "base_url": f"https://{tenant}.{pod}.myworkdayjobs.com/en-US/{site}",
    }


COMPANIES = [
    # ─── Oil, Gas & Refining ─────────────────────────────────────────────────
    {"display_name": "ExxonMobil",         "ats": "rmk",
     "base_url": "https://jobs.exxonmobil.com",
     "category_paths": ["/go/Engineering/3845600/", "/go/Research-and-technology/3844500/"],
     "country_filter": ["US"]},
    {"display_name": "Chevron",            "ats": "workday", **_wd("chevron",       "wd5", "jobs")},
    {"display_name": "Shell",              "ats": "workday", **_wd("shell",         "wd3", "ShellCareers")},
    {"display_name": "BP",                 "ats": "workday", **_wd("bpinternational", "wd3", "bpCareers")},
    {"display_name": "TotalEnergies",      "ats": "avature",
     "base_url": "https://jobs.totalenergies.com",
     "search_path": "/en_US/careers/SearchJobs",
     "country_filter": ["US"]},
    {"display_name": "Marathon Petroleum", "ats": "workday", **_wd("mpc",           "wd1", "MPCCareers")},
    {"display_name": "Phillips 66",        "ats": "rmk",
     "base_url": "https://careers.phillips66.com",
     "category_paths": ["/search/?q=&locationsearch="],
     "country_filter": ["US"]},
    {"display_name": "Valero",             "ats": "taleo",
     "zone": "valero", "cs_code": "2",
     "country_filter": ["US"]},
    {"display_name": "ConocoPhillips",     "ats": "workday", **_wd("conocophillips", "wd1", "External")},
    {"display_name": "HF Sinclair",        "ats": "rmk",
     "base_url": "https://careers.hfsinclair.com",
     "category_paths": ["/search/?q=&locationsearch="],
     "country_filter": ["US"]},
    {"display_name": "PBF Energy",         "ats": "workday", **_wd("pbfenergy",     "wd1", "PBF")},
    {"display_name": "Citgo",              "ats": "rmk",
     "base_url": "https://careers.citgo.com",
     "category_paths": ["/go/Engineering/9606200/",
                        "/go/Refinery-Operations-&-LaboratoryTerminals-&-Pipelines/9606400/"],
     "country_filter": ["US"]},
   {"display_name": "Oneok",         "ats": "workday", **_wd("oneok",     "wd1", "ONEOK")},

    # ─── Chemicals & Specialty Materials ─────────────────────────────────────
    {"display_name": "Dow",                "ats": "workday", **_wd("dow",           "wd1", "ExternalCareers")},
    # DuPont careers.dupont.com — Phenom (verify ref_num) ⚠
    {"display_name": "DuPont",             "ats": "phenom",
     "base_url": "https://careers.dupont.com",
     "ref_num": "DUPONT",
     "country_filter": ["US"]},
    {"display_name": "LyondellBasell",     "ats": "rmk",
     "base_url": "https://careers.lyondellbasell.com",
     "category_paths": ["/search/?q=&locationsearch="],
     "country_filter": ["US"]},
    {"display_name": "Eastman Chemical",   "ats": "rmk",
     "base_url": "https://jobs.eastman.com",
     "category_paths": ["/search/?q=&locationsearch="],
     "country_filter": ["US"]},
    {"display_name": "Celanese",           "ats": "generic_html",
     "url": "https://career-celanese.icims.com/jobs/search?ss=1",
     "link_must_contain": ["/jobs/", "/job/"],
     "page_param": "pr",
     "max_pages": 5,
     "country_filter": ["US"]},
    {"display_name": "Air Products",       "ats": "workday", **_wd("airproducts",   "wd5", "AP0001")},
    {"display_name": "Westlake",           "ats": "workday", **_wd("westlake",      "wd1", "Westlake")},
    # Olin uses an older Taleo URL pattern (taleo.net/phe02/...) ⚠ may need adjustment
    {"display_name": "Olin",               "ats": "generic_html",
     "url": "https://phe.tbe.taleo.net/phe02/ats/careers/v2/searchResults?org=OLIN&cws=47",
     "link_must_contain": ["/jobs/", "rid="],
     "country_filter": ["US"]},
    {"display_name": "Huntsman",           "ats": "workday", **_wd("huntsman",      "wd1", "huntsman")},
    {"display_name": "Albemarle",          "ats": "eightfold",
     "base_url": "https://albemarle.eightfold.ai",
     "domain": "albemarle.com",
     "country_filter": ["US"]},
    {"display_name": "Air Liquide",        "ats": "workday", **_wd("airliquidehr",  "wd3", "AirLiquideExternalCareer")},
    {"display_name": "Linde",              "ats": "cornerstone",
     "tenant": "linde", "site_id": 23,
     "country_filter": ["US"]},
    {"display_name": "EMD Electronics",    "ats": "phenom",
     "base_url": "https://careers.emdgroup.com",
     "ref_num": "MQAEGZUS",
     "country_filter": ["US"]},
    {"display_name": "BASF",               "ats": "rmk",
     "base_url": "https://basf.jobs",
     "category_paths": ["/search/?q=&locationsearch=United+States"],
     "country_filter": ["US"]},
    # Kraton's jobs.kraton.com is a shell — actual SF SuccessFactors instance
    # is at career4.successfactors.com?company=azcprd. The latter only shows a
    # redirect button, but its underlying job data lives there. Try generic_html
    # against the shell's RMK pattern first; if that returns 0, fall back to
    # email talent network.
    {"display_name": "Kraton",             "ats": "generic_html",
     "url": "https://career4.successfactors.com/careers?company=azcprd",
     "link_must_contain": ["/job?", "/jobs/"],
     "country_filter": ["US"]},

    # ─── Pharma & Biopharma ──────────────────────────────────────────────────
    {"display_name": "Merck (MSD)",        "ats": "workday", **_wd("msd",           "wd5", "SearchJobs")},
    {"display_name": "Pfizer",             "ats": "workday", **_wd("pfizer",        "wd1", "PfizerCareers")},
    # J&J careers.jnj.com is custom — generic_html
    {"display_name": "Johnson & Johnson",  "ats": "generic_html",
     "url": "https://www.careers.jnj.com/en/jobs/?search=&country=United+States",
     "link_must_contain": ["/jobs/"],
     "page_param": "page",
     "max_pages": 5,
     "country_filter": ["US"]},
    {"display_name": "Eli Lilly",          "ats": "workday", **_wd("lilly",         "wd5", "LLY")},
    # BMS is Eightfold (URL has ?domain=bms.com&pid=...)
    {"display_name": "Bristol Myers Squibb", "ats": "workday", **_wd("bristolmyerssquibb", "wd5", "BMS")},
    # AbbVie careers.abbvie.com is Attrax (not Phenom — the CDN is
    # attraxcdnprod1.azurefd.net). Server-rendered HTML works with generic_html.
    {"display_name": "AbbVie",             "ats": "generic_html",
     "url": "https://careers.abbvie.com/en/jobs",
     "link_must_contain": ["/en/job/"],
     "page_param": "page",
     "max_pages": 5,
     "country_filter": ["US"]},
    {"display_name": "Amgen",              "ats": "workday", **_wd("amgen",         "wd1", "Careers")},
    {"display_name": "Gilead Sciences",    "ats": "workday", **_wd("gilead",        "wd1", "gileadcareers")},
    # Roche/Genentech — Phenom (verify ref_num) ⚠
    {"display_name": "Roche",              "ats": "phenom",
     "base_url": "https://careers.roche.com",
     "ref_num": "ROCHE",
     "country_filter": ["US"]},
    {"display_name": "Genentech",          "ats": "phenom",
     "base_url": "https://careers.gene.com",
     "ref_num": "GENENTECH",
     "country_filter": ["US"]},
    {"display_name": "Regeneron",          "ats": "workday", **_wd("regeneron",     "wd1", "Careers")},
    {"display_name": "Vertex Pharmaceuticals", "ats": "workday", **_wd("vrtx",      "wd501", "vertex_careers")},
    # Takeda jobs.takeda.com is custom
    {"display_name": "Takeda",             "ats": "generic_html",
     "url": "https://jobs.takeda.com/search-jobs/United%20States/1113/2/6252001/39x76/-98x5/50/2",
     "link_must_contain": ["/job/"],
     "country_filter": ["US"]},

    # ─── Defense & Aerospace ─────────────────────────────────────────────────
    # Lockheed lockheedmartinjobs.com — custom
    {"display_name": "Lockheed Martin",    "ats": "generic_html",
     "url": "https://www.lockheedmartinjobs.com/search-jobs",
     "link_must_contain": ["/job/"],
     "country_filter": ["US"]},
    # RTX uses 'globalhr' as tenant rather than 'rtx' — odd but verified
    {"display_name": "RTX (Raytheon)",     "ats": "workday", **_wd("globalhr",      "wd5", "REC_RTX_Ext_Gateway")},
    # L3Harris careers.l3harris.com — custom
    {"display_name": "L3Harris",           "ats": "generic_html",
     "url": "https://careers.l3harris.com/en/search-jobs/United%20States/4832/2/6252001/39x76/-98x5/50/2",
     "link_must_contain": ["/job/"],
     "country_filter": ["US"]},
    # GD gd.com/careers — custom
    {"display_name": "General Dynamics",   "ats": "generic_html",
     "url": "https://www.gd.com/careers/job-search",
     "link_must_contain": ["/careers/job/", "/job/"],
     "country_filter": ["US"]},
    {"display_name": "SpaceX",             "ats": "greenhouse", "slug": "spacex"},
    {"display_name": "Northrop Grumman",   "ats": "eightfold",
     "base_url": "https://ngc.eightfold.ai",
     "domain": "ngc.com",
     "country_filter": ["US"]},
    {"display_name": "BAE Systems",        "ats": "phenom",
     "base_url": "https://jobs.baesystems.com",
     "ref_num": "EBKEGNUF",
     "country_filter": ["US"]},
    {"display_name": "General Atomics",    "ats": "brassring",
     "partner_id": 25539, "site_id": 5313,
     "country_filter": ["US"]},

    # ─── Semiconductors & Specialty Gases ────────────────────────────────────
    {"display_name": "Intel",              "ats": "workday", **_wd("intel",         "wd1", "External")},
    {"display_name": "Applied Materials",  "ats": "workday", **_wd("amat",          "wd1", "External")},
    {"display_name": "Lam Research",       "ats": "eightfold",
     "base_url": "https://lamresearch.eightfold.ai",
     "domain": "lamresearch.com",
     "country_filter": ["US"]},

    # ─── Renewables ──────────────────────────────────────────────────────────
    {"display_name": "Bloom Energy",       "ats": "workday", **_wd("bloomenergy",   "wd1", "BloomEnergyCareers")},
    {"display_name": "POET",               "ats": "workday", **_wd("poet",          "wd1", "POET")},

    # ─── EPC firms (strong pivot for chemE new grads) ────────────────────────
    {"display_name": "Fluor",              "ats": "eightfold",
     "base_url": "https://careers.fluor.com",
     "domain": "fluor.com",
     "country_filter": ["US"]},
    {"display_name": "Jacobs",             "ats": "avature",
     "base_url": "https://jacobs.avature.net",
     "search_path": "/careers",
     "country_filter": ["US"]},
    {"display_name": "KBR",                "ats": "workday", **_wd("kbr",           "wd5", "KBR_Careers")},
]
