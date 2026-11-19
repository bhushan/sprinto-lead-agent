import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

import config

PAGES = Path(__file__).parent / "fixtures" / "pages"
_vendors = {}  # domain -> trust-center vendor seen on any page of that site
VENDORS = re.compile(r"(vanta|drata|sprinto)\.com", re.I)
_cache = {}  # (domain, page) -> (time, result); a warm server reuses a page for CACHE_SECONDS, then fetches again
_robots = {}  # domain -> (time, parser or None)
CACHE_SECONDS = 600
MAX_BYTES = 1_500_000  # read at most this much of a page; the text is cut to 4000 characters anyway
SKIP = {"script", "style", "noscript", "nav", "svg", "form"}
BREAKS = {"br", "p", "div", "li", "h1", "h2", "h3", "tr", "section", "footer"}
# short lines are usually nav/buttons, but these short lines are signal
KEEP = re.compile(r"soc ?2|iso ?27001|gdpr|hipaa|series [a-e]\b|seed|certified", re.I)


def fetch(domain, path="home"):
    """Return (text, note). text is "" when nothing usable came back; note says why."""
    hit = _cached(domain, path)
    if hit is None:
        hit = _fetch(domain, path)
        _cache[(domain, path)] = (time.time(), hit)
    return hit


def _cached(domain, path):
    t, v = _cache.get((domain, path), (0, None))
    return v if time.time() - t < CACHE_SECONDS else None


def is_public_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global and not a.is_multicast


def public_host(host):
    """True when every address the name resolves to is on the public internet (no internal or metadata hosts)."""
    try:
        addrs = {x[4][0] for x in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except (socket.gaierror, UnicodeError, OSError):
        return False
    return bool(addrs) and all(is_public_ip(a) for a in addrs)


def read_body(r):
    """(html, note) from a streamed response: web pages only, at most MAX_BYTES."""
    ctype = (r.headers.get("Content-Type") or "text/html").lower()
    if "html" not in ctype and "text/plain" not in ctype:
        r.close()
        return "", f"not a web page ({ctype.split(';')[0]})"
    chunks, size = [], 0
    for c in r.iter_content(65536):
        chunks.append(c)
        size += len(c)
        if size >= MAX_BYTES:
            break
    r.close()
    return b"".join(chunks)[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace"), ""


def vendor(domain):
    """Vanta, Drata or Sprinto if the site's trust center runs on one (checked in raw HTML, not visible text)."""
    return _vendors.get(domain)


def _spot(domain, raw):
    m = VENDORS.search(raw)
    if m:
        _vendors[domain] = m.group(1).title()


def _fetch(domain, path):
    if config.offline():
        f = PAGES / f"{domain}_{path}.txt"
        if not f.exists():
            return "", "page not found"
        raw = f.read_text(encoding="utf-8")
        _spot(domain, raw)
        return raw, ""
    base = f"https://{domain}"
    url = base + ("/" if path == "home" else f"/{path}")
    if not public_host(domain):
        return "", "the address does not resolve to a public website"
    if not _allowed(domain, base, url):
        return "", "blocked by robots.txt"
    time.sleep(config.FETCH_DELAY)
    try:
        r = requests.get(url, timeout=(5, 10), headers={"User-Agent": config.USER_AGENT}, stream=True)
    except requests.RequestException as e:
        return "", f"request failed: {type(e).__name__}"
    final = requests.utils.urlparse(r.url).hostname or ""
    if final != domain and not public_host(final):
        r.close()
        return "", "redirected to a non-public address"
    if r.status_code in (401, 403, 429, 503):
        r.close()
        return "", f"blocked or login wall (http {r.status_code})"
    if r.status_code >= 400:
        r.close()
        return "", f"http {r.status_code}"
    try:
        html, note = read_body(r)
    except requests.RequestException as e:
        return "", f"request failed: {type(e).__name__}"
    if not html:
        return "", note
    _spot(domain, html)
    text = clean(html)
    note = "thin page: may be JavaScript-rendered" if len(text.split()) < config.THIN_WORDS else ""
    return text, note


def _allowed(domain, base, url):
    """robots.txt is fetched once per site per CACHE_SECONDS. No robots.txt, or one that cannot be read, forbids nothing."""
    from urllib.robotparser import RobotFileParser
    t, rp = _robots.get(domain, (0, None))
    if time.time() - t >= CACHE_SECONDS:
        rp = None
        try:
            r = requests.get(base + "/robots.txt", timeout=5, headers={"User-Agent": config.USER_AGENT})
            if r.status_code == 200 and len(r.content) < 500_000:
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            pass
        _robots[domain] = (time.time(), rp)
    return rp is None or rp.can_fetch(config.USER_AGENT, url)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip, self.out = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        if tag in BREAKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP and self.skip:
            self.skip -= 1
        if tag in BREAKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def clean(html):
    """HTML -> short plain text: drop scripts/nav/forms, short UI lines and duplicates."""
    p = _Text()
    p.feed(html)
    seen, out = set(), []
    for line in "".join(p.out).splitlines():
        line = " ".join(line.split())
        if (len(line.split()) >= 6 or KEEP.search(line)) and line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)[:4000]
