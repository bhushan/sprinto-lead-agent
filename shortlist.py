"""Stage 1: find recently funded SaaS companies from Google News, no LLM and no scraping of the targets.

python shortlist.py [N]  ->  data/shortlist.txt (top N by recency) and data/shortlist.json
(domain -> funding stage + headline, which run.py uses as evidence for the "right stage?" question).
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

import config

QUERIES = ['"Series A" SaaS funding', '"Series B" SaaS funding', '"Series C" SaaS funding',
           '"Series A" fintech raises', '"Series B" B2B software startup', '"Series A" healthtech raises']
REGIONS = [("en-IN", "IN", "IN:en"), ("en-US", "US", "US:en")]
STAGE = re.compile(r"\bseries ([abc])\b", re.I)
EXCLUDE = re.compile(r"hardware|agency|consult|d2c|restaurant|real estate|ev maker|electric vehicle|drone|semiconductor|"
                     r"manufactur|apparel|brewery|pre-series", re.I)
NAME = re.compile(r"(?:.*?\b(?:startup|firm|platform|company|unicorn)\s+)?([A-Z][\w.-]*(?:\s[A-Z][\w.-]*)?)\s+"
                  r"(?:raises|secures|bags|closes|lands|gets|announces|nabs|snags|picks up)\b")
TLDS = [".com", ".io", ".ai", ".co", ".in"]
MAX_AGE = timedelta(days=730)


def headlines():
    for q in QUERIES:
        for hl, gl, ceid in REGIONS:
            url = f"https://news.google.com/rss/search?q={quote(q)}+when:2y&hl={hl}&gl={gl}&ceid={ceid}"
            try:
                root = ET.fromstring(requests.get(url, timeout=15, headers={"User-Agent": config.USER_AGENT}).content)
            except (requests.RequestException, ET.ParseError):
                continue
            for it in root.iter("item"):
                yield it.findtext("title", ""), it.findtext("link", ""), parsedate_to_datetime(it.findtext("pubDate"))


GENERIC = {"raises", "series", "funding", "startup", "round", "million", "billion", "valuation", "led", "secures",
           "platform", "company", "expand", "announces", "software", "saasrise", "yourstory", "fintech", "global"}


def resolve(name, headline):
    """A domain counts only if the homepage title names the company AND the page shares a topic word with the
    funding headline, so same-name companies (a different "Neon") and parked pages are rejected."""
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    topic = {w for w in re.findall(r"[a-z]{5,}", headline.lower()) if w not in GENERIC and w not in name.lower()}
    for tld in TLDS:
        try:
            r = requests.get(f"https://{slug}{tld}", timeout=6, headers={"User-Agent": config.USER_AGENT})
        except requests.RequestException:
            continue
        title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
        if r.ok and title and name.lower() in title.group(1).lower() and any(w in r.text.lower() for w in topic):
            return f"{slug}{tld}"
    return None


def main(n):
    now, found = datetime.now(timezone.utc), {}
    for title, link, when in sorted(headlines(), key=lambda h: h[2], reverse=True):
        stage, m = STAGE.search(title), NAME.match(title)
        if not stage or not m or EXCLUDE.search(title) or now - when > MAX_AGE:
            continue
        found.setdefault(m.group(1), dict(stage=f"Series {stage.group(1).upper()}", headline=title, url=link, date=when.date().isoformat()))
    out = {}
    for name, info in found.items():
        domain = resolve(name, info["headline"])
        if domain and domain not in out:
            out[domain] = {"name": name, **info}
            print(f"{domain:28} {info['stage']}  {info['headline'][:70]}")
        if len(out) >= n:
            break
    Path("data").mkdir(exist_ok=True)
    Path("data/shortlist.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    Path("data/shortlist.json").write_text(json.dumps(
        {d: {"funding_stage": i["stage"], "funding_source": i["headline"], "funding_url": i["url"], "funding_date": i["date"]}
         for d, i in out.items()}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(out)} domains -> data/shortlist.txt")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
