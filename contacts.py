"""Step 4: find the right point of contact.

Option A: Hunter.io Domain Search (live, needs HUNTER_API_KEY). Option B: data/contacts.csv
(look people up in Apollo by hand). Either way the pick is ranked by who owns security work,
not by "the nearest person with manager in their title".
"""
import csv
import re
from pathlib import Path

import requests

import config
import db

CSV = str(Path(__file__).parent / "data" / "contacts.csv")


class LookupFailed(Exception):
    """The contact search itself failed (quota, bad key, outage): not the same as finding nobody."""


TIERS = [
    (re.compile(r"ciso|security|compliance|trust|grc", re.I), 100, "owns security and compliance"),
    (re.compile(r"\b(cto|chief technology)\b|(vp|vice president|head|director)\b.*\beng", re.I), 80,
     "engineering leader who signs off security work"),
    (re.compile(r"founder|\bceo\b|chief executive", re.I), 60, "founder-led company, decides directly"),
]


def _rank(title, headcount):
    for pat, score, why in TIERS:
        if pat.search(title):
            if score == 60 and headcount and headcount < 50:
                return 90, why + " (company under 50 people)"
            return score, why
    return 0, "no ownership of security or engineering"


def _csv(domain, path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return [r for r in csv.DictReader(f) if r.get("domain") == domain and r.get("name")]
    except FileNotFoundError:
        return []


def _hunter(domain):
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search", timeout=15,
                         params={"domain": domain, "limit": 10, "api_key": config.HUNTER_API_KEY})
    except requests.RequestException as ex:
        raise LookupFailed(f"Hunter search failed: {type(ex).__name__}") from None
    if r.status_code in (401, 403):
        raise LookupFailed("Hunter search failed: the API key was refused")
    if r.status_code == 429:
        raise LookupFailed("Hunter search failed: monthly quota used up or too many requests")
    if r.status_code >= 400:
        raise LookupFailed(f"Hunter search failed (http {r.status_code})")
    try:
        emails = r.json()["data"]["emails"] or []
    except (ValueError, KeyError, TypeError):
        raise LookupFailed("Hunter search failed: unexpected reply") from None
    return [{"name": f"{e['first_name']} {e.get('last_name') or ''}".strip(), "title": e.get("position") or "",
             "email": e.get("value") or ""}
            for e in emails if isinstance(e, dict) and e.get("first_name") and (e.get("confidence") or 0) >= 50]


def _known(domain):
    """Contacts added on the Data page. Using them costs no Hunter search."""
    try:
        return db.people(domain) if db.enabled else []
    except Exception:
        return []


def find_contact(domain, headcount=None, path=CSV):
    """Return (contact, why). contact is None when nobody on the list owns the problem, or the search failed."""
    known = _known(domain)
    try:
        people = known or (_hunter(domain) if (config.HUNTER_API_KEY and not config.offline()) else _csv(domain, path))
    except LookupFailed as ex:
        return None, str(ex)
    ranked = sorted(((*_rank(p.get("title") or "", headcount), p) for p in people), key=lambda x: -x[0])
    if not ranked or ranked[0][0] == 0:
        return None, f"{len(people)} candidates, none owns security or engineering"
    score, why, best = ranked[0]
    others = ", ".join(f"{p['name']} ({p['title']})" for s, _, p in ranked[1:])
    return best, f"{best['title']}: {why}" + (" (from your own data)" if known else "") + (f". Passed over: {others}" if others else "")
