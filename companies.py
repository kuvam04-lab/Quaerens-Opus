"""
Company list and ATS endpoints.

Each entry needs:
  display_name : str         — for notifications
  ats          : str         — "workday" | "greenhouse" | "lever" | "ashby" |
                               "rmk" | "avature" | "eightfold" | "phenom" |
                               "cornerstone"
  slug         : str         — for greenhouse/lever/ashby (the company URL slug)

Workday entries also need:
  api_url      : str         — full POST endpoint, ends in /jobs
  base_url     : str         — career site base, used to build clickable links

RMK (SAP SuccessFactors Recruiting Marketing) entries need:
  base_url        : str       — e.g. "https://jobs.exxonmobil.com"
  category_paths  : list[str] — e.g. ["/go/Engineering/3845600/"]
  country_filter  : list[str] (optional) — defaults to ["US"]

Avature entries need:
  base_url        : str       — e.g. "https://jobs.totalenergies.com"
  search_path     : str       — e.g. "/en_US/careers/SearchJobs"
  country_filter  : list[str] (optional)

Eightfold AI entries need:
  base_url        : str       — e.g. "https://jobs.northropgrumman.com"
  domain          : str       — e.g. "ngc.com" (passed to ?domain= in the API)
  country_filter  : list[str] (optional)

Phenom People entries need:
  base_url        : str       — e.g. "https://careers.emdgroup.com"
  ref_num         : str       — short site code; find by inspecting CDN URLs on
                                the page (cdn.phenompeople.com/CareerConnect-
                                Resources/{REF_NUM}/) or page source
  country_filter  : list[str] (optional)

Cornerstone OnDemand (CSOD) entries need:
  tenant          : str       — the {tenant}.csod.com subdomain
  site_id         : int       — the numeric site id from the careers URL
                                (https://{tenant}.csod.com/ux/ats/careersite/{ID}/...)
  country_filter  : list[str] (optional)

How to find a Workday URL in 30 seconds:
  1. Open the company's "Search Jobs" page (e.g. https://careers.merck.com → it
     redirects to https://msd.wd5.myworkdayjobs.com/en-US/SearchJobs).
  2. From that URL: tenant=msd, pod=wd5, site=SearchJobs.
  3. Build:
        api_url  = https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
        base_url = https://{tenant}.{pod}.myworkdayjobs.com/en-US/{site}

How to identify which platform a company uses:
  - Workday        : URL contains "myworkdayjobs.com"
  - Greenhouse     : "boards.greenhouse.io/{slug}" or "{slug}.greenhouse.io"
  - Lever          : "jobs.lever.co/{slug}"
  - Ashby          : "jobs.ashbyhq.com/{slug}"
  - SF RMK         : cookie banner mentions "SAP", logo URL is rmkcdn.successfactors.com
  - Avature        : template assets load from "templates-static-assets.avacdn.net"
  - Eightfold AI   : URL is "{slug}.eightfold.ai" or page sources reference Eightfold
  - Phenom People  : assets load from "cdn.phenompeople.com" or HR mentions Phenom
  - Cornerstone    : URL is "{tenant}.csod.com/ux/ats/careersite/..."

⚠ Some Workday URLs below are best-effort. If a company returns 404 or 0 jobs,
  fix its api_url/base_url and re-run. The BAE Systems ref_num is also a guess
  — verify by viewing page source on jobs.baesystems.com if it returns no jobs.

Companies that need fetchers we haven't built yet (use email alerts):
  BASF              → SAP SuccessFactors RMK (basf.jobs); add by following
                      ExxonMobil's pattern. To enable: visit basf.jobs, click
                      Engineering, copy the /go/Engineering/{ID}/ path.
  General Atomics   → Kenexa BrassRing (sjobs.brassring.com). Legacy IBM ATS,
                      different scraping pattern. Use email alerts.
  Kraton            → iCIMS.
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
    {"display_name": "Marathon Petroleum", "ats": "workday", **_wd("marathonpetroleum", "wd1", "MPCCareers")},
    {"display_name": "Phillips 66",        "ats": "workday", **_wd("phillips66",    "wd5", "jobs")},
    {"display_name": "Valero",             "ats": "workday", **_wd("valero",        "wd1", "valero")},
    {"display_name": "ConocoPhillips",     "ats": "workday", **_wd("conocophillips", "wd1", "External")},
    {"display_name": "HF Sinclair",        "ats": "workday", **_wd("hfsinclair",    "wd1", "HFSinclair")},
    {"display_name": "PBF Energy",         "ats": "workday", **_wd("pbfenergy",     "wd1", "PBF")},
    {"display_name": "Citgo",              "ats": "rmk",
     "base_url": "https://careers.citgo.com",
     "category_paths": ["/go/Engineering/9606200/",
                        "/go/Refinery-Operations-&-LaboratoryTerminals-&-Pipelines/9606400/"],
     "country_filter": ["US"]},

    # ─── Chemicals & Specialty Materials ─────────────────────────────────────
    {"display_name": "Dow",                "ats": "workday", **_wd("dow",           "wd5", "Dow_Careers")},
    {"display_name": "DuPont",             "ats": "workday", **_wd("dupont",        "wd5", "DuPont_Careers")},
    {"display_name": "LyondellBasell",     "ats": "workday", **_wd("lyondellbasell", "wd1", "LyondellBasellCareers")},
    {"display_name": "Eastman Chemical",   "ats": "workday", **_wd("eastman",       "wd5", "Eastman_External_Career_Site")},
    {"display_name": "Celanese",           "ats": "workday", **_wd("celanese",      "wd1", "celanesecareers")},
    {"display_name": "Air Products",       "ats": "workday", **_wd("airproducts",   "wd1", "AirProductsExternal")},
    {"display_name": "Westlake",           "ats": "workday", **_wd("westlake",      "wd1", "Westlake")},
    {"display_name": "Olin",               "ats": "workday", **_wd("olin",          "wd1", "olincareers")},
    {"display_name": "Huntsman",           "ats": "workday", **_wd("huntsman",      "wd1", "huntsman")},
    {"display_name": "Albemarle",          "ats": "workday", **_wd("albemarle",     "wd1", "External_Careers")},
    {"display_name": "Air Liquide",        "ats": "workday", **_wd("airliquidehr",  "wd3", "AirLiquideExternalCareer")},
    {"display_name": "Linde",              "ats": "cornerstone",
     "tenant": "linde", "site_id": 20,
     "country_filter": ["US"]},
    {"display_name": "EMD Electronics",    "ats": "phenom",
     "base_url": "https://careers.emdgroup.com",
     "ref_num": "MQAEGZUS",
     "country_filter": ["US"]},
    {"display_name": "BASF",               "ats": "rmk",
     "base_url": "https://basf.jobs",
     "category_paths": ["/light_green_NA/"],
     "country_filter": ["US"]},
    {"display_name": "Kraton",             "ats": "rmk",
     "base_url": "https://jobs.kraton.com",
     "category_paths": ["/go/Operations-&-Supply-Chain/7910100/",
                        "/go/Research-and-Development/7910300/"],
     "country_filter": ["US"]},

    # ─── More chemicals notes ────────────────────────────────────────────────
    # (all confirmed companies wired up above)

    # ─── Pharma & Biopharma ──────────────────────────────────────────────────
    {"display_name": "Merck (MSD)",        "ats": "workday", **_wd("msd",           "wd5", "SearchJobs")},
    {"display_name": "Pfizer",             "ats": "workday", **_wd("pfizer",        "wd1", "PfizerCareers")},
    {"display_name": "Johnson & Johnson",  "ats": "workday", **_wd("jnjc",          "wd1", "jnjc")},
    {"display_name": "Eli Lilly",          "ats": "workday", **_wd("lilly",         "wd5", "LLY")},
    {"display_name": "Bristol Myers Squibb", "ats": "workday", **_wd("bms",         "wd5", "BMS")},
    {"display_name": "AbbVie",             "ats": "workday", **_wd("abbvie",        "wd1", "External")},
    {"display_name": "Amgen",              "ats": "workday", **_wd("amgen",         "wd1", "Careers")},
    {"display_name": "Gilead Sciences",    "ats": "workday", **_wd("gilead",        "wd1", "gileadcareers")},
    {"display_name": "Roche / Genentech",  "ats": "workday", **_wd("roche",         "wd3", "roche")},
    {"display_name": "Regeneron",          "ats": "workday", **_wd("regeneron",     "wd5", "Regeneron_Careers")},
    {"display_name": "Vertex Pharmaceuticals", "ats": "workday", **_wd("vrtx",      "wd5", "vertex_careers")},
    {"display_name": "Takeda",             "ats": "workday", **_wd("takeda",        "wd3", "takedajobs")},

    # ─── Defense & Aerospace ─────────────────────────────────────────────────
    {"display_name": "Lockheed Martin",    "ats": "workday", **_wd("lockheedmartin", "wd1", "LMCareers")},
    {"display_name": "RTX (Raytheon)",     "ats": "workday", **_wd("rtx",           "wd5", "REC_RTX_Ext_Gateway")},
    {"display_name": "L3Harris",           "ats": "workday", **_wd("l3harris",      "wd1", "External")},
    {"display_name": "General Dynamics OTS", "ats": "workday", **_wd("gdotssite",   "wd5", "GDOTS_External")},
    {"display_name": "SpaceX",             "ats": "greenhouse", "slug": "spacex"},
    {"display_name": "Northrop Grumman",   "ats": "eightfold",
     "base_url": "https://jobs.northropgrumman.com",
     "domain": "ngc.com",
     "country_filter": ["US"]},
    {"display_name": "BAE Systems",        "ats": "phenom",
     "base_url": "https://jobs.baesystems.com",
     "ref_num": "EBKEGNUF",   # ⚠ best-guess; verify by viewing page source for refNum
     "country_filter": ["US"]},
    {"display_name": "General Atomics",    "ats": "brassring",
     "partner_id": 25539, "site_id": 5313,
     "country_filter": ["US"]},
    # Aerojet Rocketdyne — merged into L3Harris, covered above.

    # ─── Semiconductors & Specialty Gases ────────────────────────────────────
    {"display_name": "Intel",              "ats": "workday", **_wd("intel",         "wd1", "External")},
    {"display_name": "Applied Materials",  "ats": "workday", **_wd("amat",          "wd1", "External")},
    {"display_name": "Lam Research",       "ats": "workday", **_wd("lamresearch",   "wd1", "External")},

    # ─── Renewables ──────────────────────────────────────────────────────────
    {"display_name": "Bloom Energy",       "ats": "greenhouse", "slug": "bloomenergy"},
    {"display_name": "POET",               "ats": "workday", **_wd("poet",          "wd1", "POET")},
    # General Atomics — Kenexa BrassRing, no fetcher built yet. Use email alerts.

    # ─── EPC firms (strong pivot for chemE new grads) ────────────────────────
    {"display_name": "Fluor",              "ats": "workday", **_wd("fluor",         "wd1", "Fluor")},
    {"display_name": "Jacobs",             "ats": "workday", **_wd("jacobs",        "wd1", "Professional")},
    {"display_name": "KBR",                "ats": "workday", **_wd("kbr",           "wd1", "KBR_Careers")},
]
