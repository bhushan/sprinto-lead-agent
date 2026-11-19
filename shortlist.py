"""Stage 1: discover companies from Google News, no LLM and no scraping of the targets.

Two sources: funding news (a recent Series A-C round) and hiring news (a security or compliance hire).
python shortlist.py [N]  ->  data/shortlist.txt (up to N domains) and data/shortlist.json
(domain -> the headlines that surfaced it, which run.py uses as evidence and as the email hook).
"""
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import zip_longest
from pathlib import Path
from urllib.parse import quote

import requests

import config

QUERIES = ['"Series A" SaaS funding', '"Series B" SaaS funding', '"Series C" SaaS funding',
           '"Series A" fintech raises', '"Series B" B2B software startup', '"Series A" healthtech raises']
HIRE_QUERIES = ['startup names head of security', 'startup hires CISO', 'startup taps security chief',
                'fintech startup appoints chief compliance officer', 'SaaS company appoints head of compliance']
REGION_TABLE = {"IN": ("en-IN", "IN", "IN:en"), "US": ("en-US", "US", "US:en"),
                "GB": ("en-GB", "GB", "GB:en"), "SG": ("en-SG", "SG", "SG:en")}
KEYWORDS, REGION_CODES, STAGES, SOURCES = [], ["IN", "US"], "ABC", ["funding", "hiring"]  # set from settings
STAGE = re.compile(r"\bseries ([abc])\b", re.I)
EXCLUDE = re.compile(r"hardware|agency|consult|d2c|restaurant|real estate|ev maker|electric vehicle|drone|semiconductor|"
                     r"manufactur|apparel|brewery|pre-series", re.I)
NAME = re.compile(r"(?:.*?\b(?:startup|firm|platform|company|unicorn)\s+)?([A-Z][\w.-]*(?:\s[A-Z][\w.-]*)?)\s+"
                  r"(?:raises|secures|bags|closes|lands|gets|announces|nabs|snags|picks up)\b")
HIRE_NAME = re.compile(r"(?:.*?\b(?:startup|firm|platform|company|unicorn)\s+)?([A-Z][\w.-]*(?:\s[A-Z][\w.-]*)?)\s+"
                       r"(?:Acquires\s+[A-Z][\w.-]*\s+and\s+)?(?i:hires|appoints|names|taps|welcomes|onboards|adds|elevates|promotes|brings)\b")
HIRE_EXCLUDE = re.compile(r"\bbank\b|insurance|entertainment|university|college|hospital|government|ministry|airlines?|motors|"
                          r"\bCEO\b|\bCFO\b|\bCMO\b", re.I)
ROLE = re.compile(r"\b(ciso|chief (?:information )?security|chief (?:risk and )?compliance|head up security|head of (?:information )?(?:security|compliance)|vp (?:of )?security|"
                  r"security (?:lead|engineer|officer|head)|compliance (?:manager|lead|officer|head)|grc|dpo)\b", re.I)
TLDS = [".com", ".io", ".ai", ".co", ".in"]
MAX_AGE = timedelta(days=730)
HIRE_AGE = timedelta(days=180)  # a security hire is only a signal while it is recent


def queries():
    """The user's own keywords (one news query each, any chosen stage), else the built-in SaaS queries."""
    if not KEYWORDS:
        return QUERIES
    st = " OR ".join(f'"Series {c}"' for c in STAGES)
    return [f"({st}) {kw} raises" for kw in KEYWORDS]


def hiring_queries():
    if not KEYWORDS:
        return HIRE_QUERIES
    return [f'{kw} startup hires OR appoints CISO OR "head of security" OR "head of compliance"' for kw in KEYWORDS]


def parse_date(v):
    """An RSS pubDate as an aware datetime, or None when it is missing or malformed."""
    try:
        d = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _feed(url):
    try:
        root = ET.fromstring(requests.get(url, timeout=(5, 12), headers={"User-Agent": config.USER_AGENT}).content)
    except (requests.RequestException, ET.ParseError):
        return []
    return [(it.findtext("title", ""), it.findtext("link", ""), parse_date(it.findtext("pubDate"))) for it in root.iter("item")]


def headlines(qs, window="2y"):
    """Every (title, link, date) for the queries in every chosen region. Feeds are read in parallel; a feed that fails is skipped."""
    urls = [f"https://news.google.com/rss/search?q={quote(q)}+when:{window}&hl={hl}&gl={gl}&ceid={ceid}"
            for q in qs for hl, gl, ceid in [REGION_TABLE[c] for c in REGION_CODES]]
    with ThreadPoolExecutor(8) as pool:
        for items in pool.map(_feed, urls):
            yield from (h for h in items if h[2] is not None)


GENERIC = {"raises", "series", "funding", "startup", "round", "million", "billion", "valuation", "led", "secures",
           "platform", "company", "expand", "announces", "software", "saasrise", "yourstory", "fintech", "global",
           "appoints", "hires", "names", "head", "security", "compliance", "officer", "chief", "information"}


def resolve(name, headline):
    """A domain counts only if the homepage title names the company AND the page shares a topic word with the
    headline, so same-name companies (a different "Neon") and parked pages are rejected."""
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    topic = {w for w in re.findall(r"[a-z]{5,}", headline.lower()) if w not in GENERIC and w not in name.lower()}
    if not slug:
        return None
    for tld in TLDS:
        try:
            r = requests.get(f"https://{slug}{tld}", timeout=(4, 6), headers={"User-Agent": config.USER_AGENT}, stream=True)
            page = r.raw.read(300_000, decode_content=True).decode(r.encoding or "utf-8", errors="replace") if r.ok else ""
            r.close()
        except (requests.RequestException, OSError):
            continue
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        if title and name.lower() in title.group(1).lower() and any(w in page.lower() for w in topic):
            return f"{slug}{tld}"
    return None


def _funding(now):
    found = {}
    for title, link, when in sorted(headlines(queries()), key=lambda h: h[2], reverse=True):
        stage, m = STAGE.search(title), NAME.match(title)
        if not stage or not m or EXCLUDE.search(title) or now - when > MAX_AGE:
            continue
        found.setdefault(m.group(1), dict(stage=f"Series {stage.group(1).upper()}", headline=title, url=link, date=when.date().isoformat()))
    return found


def _hiring(now):
    found = {}
    for title, link, when in sorted(headlines(hiring_queries(), "1y"), key=lambda h: h[2], reverse=True):
        m = HIRE_NAME.match(title)
        if not m or not ROLE.search(title) or EXCLUDE.search(title) or HIRE_EXCLUDE.search(title) or now - when > HIRE_AGE:
            continue
        found.setdefault(m.group(1), dict(headline=title, url=link, date=when.date().isoformat()))
    return found


def find(n, budget=150):
    """Return {domain: {name, funding, hiring}} for up to n companies. funding/hiring hold the headline that surfaced it (or None).

    budget: seconds. Names are resolved in parallel and whatever is resolved when time runs out is returned,
    so a slow news day or slow websites can never run past the hosting time limit."""
    deadline = time.time() + budget
    now = datetime.now(timezone.utc)
    funding = _funding(now) if "funding" in SOURCES else {}
    hiring = _hiring(now) if "hiring" in SOURCES else {}
    names = list(dict.fromkeys(x for pair in zip_longest(funding, hiring) for x in pair if x))[:n * 3]  # alternate the sources
    infos = {name: {"name": name, "funding": funding.get(name), "hiring": hiring.get(name)} for name in names}
    found = {}
    pool = ThreadPoolExecutor(6)
    jobs = {pool.submit(resolve, name, (i["funding"] or i["hiring"])["headline"]): name for name, i in infos.items()}
    try:
        for fut in as_completed(jobs, timeout=max(1, deadline - time.time())):
            domain = fut.result()
            if domain and domain not in found.values():
                found[jobs[fut]] = domain
            if len(found) >= n:
                break
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {found[name]: infos[name] for name in names if name in found}  # keep the news order


def seeds(found):
    """The evidence run.py stores: the funding stage for the 'right stage?' question, and the headlines used as email hooks."""
    out = {}
    for d, i in found.items():
        s = {}
        if i.get("funding"):
            f = i["funding"]
            s.update(funding_stage=f["stage"], funding_source=f["headline"], funding_url=f["url"], funding_date=f["date"])
        if i.get("hiring"):
            h = i["hiring"]
            s.update(hiring_source=h["headline"], hiring_url=h["url"], hiring_date=h["date"])
        out[d] = s
    return out


def main(n):
    out = find(n)
    for d, i in out.items():
        f, h = i["funding"], i["hiring"]
        print(f"{d:28} {('funding ' + f['stage']) if f else 'hiring':16} {((f or h)['headline'])[:70]}")
    Path("data").mkdir(exist_ok=True)
    Path("data/shortlist.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    Path("data/shortlist.json").write_text(json.dumps(seeds(out), indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(out)} domains -> data/shortlist.txt")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
