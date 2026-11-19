"""ICP settings: the knobs a person can change without touching code.

validate() cleans what the web form sends, apply() pushes the values into the modules that use them.
Stored in the database when one is connected; DEFAULTS otherwise.
"""
import re

import config
import contacts
import qualify
import shortlist

QKEYS = ("certified", "saas", "stage")  # Evidence Map questions, in order
QMAX = (2, 2, 3)                        # a limit can never exceed the pages that exist for the question
ROLES = [("security", 100, "owns security and compliance"),
         ("engineering", 80, "engineering leader who signs off security work"),
         ("founder", 60, "founder-led company, decides directly")]

DEFAULTS = {
    "threshold": 70,
    "stage_penalty": 10,
    "stages": ["A", "B", "C"],
    "exclude": ["hardware", "agency", "consult", "d2c", "restaurant", "real estate", "ev maker", "electric vehicle",
                "drone", "semiconductor", "manufactur", "apparel", "brewery", "pre-series"],
    "limits": {"certified": 1, "saas": 1, "stage": 2},
    "roles": {"security": ["ciso", "security", "compliance", "trust", "grc"],
              "engineering": ["cto", "chief technology", "vp engineering", "vice president engineering",
                              "head of engineering", "director of engineering"],
              "founder": ["founder", "ceo", "chief executive"]},
    "sender": "Piyush",
    "target": 3,
    "keywords": [],
    "regions": ["IN", "US"],
    "tier_a": 85,
    "sources": ["funding", "hiring"],
}


def _int(v, lo, hi, label):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not lo <= n <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return n


def _words(v, most, label):
    items = [w.strip().lower() for w in (v if isinstance(v, list) else str(v or "").replace("\n", ",").split(","))]
    items = [w for w in items if w]
    if len(items) > most or any(len(w) > 40 or not re.fullmatch(r"[a-z0-9 .&'-]+", w) for w in items):
        raise ValueError(f"{label}: use plain words or phrases separated by commas (at most {most}).")
    return items


def validate(raw):
    """Return a clean settings dict or raise ValueError with a message for the form."""
    if not isinstance(raw, dict):
        raise ValueError("Bad settings.")
    s = {"threshold": _int(raw.get("threshold"), 0, 100, "Score threshold"),
         "stage_penalty": _int(raw.get("stage_penalty"), 0, 30, "Unknown-stage penalty")}
    stages = sorted({x for x in raw.get("stages") or [] if x in ("A", "B", "C")})
    if not stages:
        raise ValueError("Pick at least one funding stage.")
    s["stages"] = stages
    s["exclude"] = _words(raw.get("exclude"), 40, "Excluded sectors")
    lim = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    s["limits"] = {k: _int(lim.get(k), 0, m, f"Fetch limit ({k})") for k, m in zip(QKEYS, QMAX)}
    roles = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    s["roles"] = {k: _words(roles.get(k), 12, f"Roles ({k})") for k, _, _ in ROLES}
    if not any(s["roles"].values()):
        raise ValueError("Give at least one contact role to look for.")
    sender = str(raw.get("sender", "")).strip()
    if not re.fullmatch(r"[A-Za-z .'-]{1,40}", sender):
        raise ValueError("Sender name: letters only, up to 40.")
    s["sender"] = sender
    s["target"] = _int(raw.get("target", 3), 1, 5, "Qualified target")
    s["tier_a"] = _int(raw.get("tier_a", DEFAULTS["tier_a"]), 0, 100, "Tier A score")
    s["tier_a"] = max(s["tier_a"], s["threshold"])  # tier A can never sit below the qualifying score
    sources = sorted({x for x in (raw.get("sources", DEFAULTS["sources"]) or []) if x in ("funding", "hiring")})
    if not sources:
        raise ValueError("Pick at least one discovery source.")
    s["sources"] = sources
    s["keywords"] = _words(raw.get("keywords", DEFAULTS["keywords"]), 5, "Search keywords")
    regions = sorted({c for c in (raw.get("regions", DEFAULTS["regions"]) or []) if c in shortlist.REGION_TABLE})
    if not regions:
        raise ValueError("Pick at least one region.")
    s["regions"] = regions
    return s


def effective(stored):
    return {**DEFAULTS, **(stored or {})}


def _phrase(kw):
    """'vp engineering' also matches 'VP of Engineering' and 'VP, Engineering'."""
    return r"\b" + r"[\s,&/-]+(?:of[\s,&/-]+)?".join(re.escape(w) for w in kw.split()) + r"\b"


def apply(s):
    """Push settings into the pipeline modules. ponytail: module globals, so concurrent requests share the last applied settings."""
    s = effective(s)
    config.QUALIFY_THRESHOLD, config.STAGE_UNKNOWN_PENALTY, config.SENDER_NAME = s["threshold"], s["stage_penalty"], s["sender"]
    config.TIER_A, shortlist.SOURCES = s["tier_a"], s["sources"]
    for q, k in zip(qualify.QUESTIONS, QKEYS):
        q["limit"] = min(s["limits"][k], len(q["paths"]))
    qualify.STAGES_TEXT = ", ".join("Series " + x for x in s["stages"])
    contacts.TIERS = [(re.compile("|".join(_phrase(k) for k in s["roles"][name]), re.I), score, why)
                      for name, score, why in ROLES if s["roles"][name]]
    shortlist.KEYWORDS, shortlist.REGION_CODES, shortlist.STAGES = s["keywords"], s["regions"], "".join(s["stages"])
    shortlist.STAGE = re.compile(r"\bseries ([%s])\b" % "".join(s["stages"]), re.I)
    shortlist.EXCLUDE = re.compile("|".join(re.escape(w) for w in s["exclude"]) or r"(?!x)x", re.I)
