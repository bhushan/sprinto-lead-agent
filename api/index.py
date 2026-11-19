"""Outreach Agent web app (one Vercel Python function, no framework).

Shows data/snapshot.json (the last live run). "Run demo" executes the pipeline offline on the
bundled fictional fixtures: OFFLINE is forced, so it never touches the network or sends mail.
"""
import json
import os
import sys
import tempfile
from html import escape as e
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

os.environ["OFFLINE"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(tempfile.gettempdir())  # send.py writes outbox/ relative to cwd; only the temp dir is writable on Vercel

import impact  # noqa: E402
import qualify  # noqa: E402
import run  # noqa: E402

STATUS = {  # label, css class
    "drafted": ("Email drafted", "ok"), "not_qualified": ("Not qualified", "mute"),
    "disqualified": ("Disqualified", "mute"), "manual_review": ("Needs a human", "warn"),
    "failed": ("Needs a human", "warn"), "no_contact": ("Needs a human", "warn"), "no_hook": ("Needs a human", "warn"),
}
STEPS = [("Shortlist", "Funded SaaS from Google News, stage from the headline"), ("Scrape", "One cached, robots-aware fetch per page"), ("Extract", "Named fields; quotes must appear on the page"),
         ("Qualify", "Evidence Map loop, max 4 extra fetches"), ("Find contact", "Hunter.io, ranked by who owns security"),
         ("Pain points", "Two, each backed by a quote"), ("Draft", "By name, with a specific hook"),
         ("Send", "You approve, mail goes to you only")]

CSS = """
:root{--bg:#09090b;--panel:#111114;--line:#232328;--text:#f4f4f5;--mute:#9a9aa3;--accent:#7c83ff;--ok:#34d399;--warn:#fbbf24;--g:linear-gradient(90deg,#4f8cff,#a78bfa)}
@media(prefers-color-scheme:light){:root{--bg:#fafafa;--panel:#fff;--line:#e4e4e7;--text:#18181b;--mute:#6b6b75;--accent:#5b61e8;--ok:#059669;--warn:#b45309}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
a{color:inherit;text-decoration:none}.wrap{max-width:1040px;margin:0 auto;padding:0 20px}
header{border-bottom:1px solid var(--line);position:sticky;top:0;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(8px);z-index:5}
header .wrap{display:flex;align-items:center;justify-content:space-between;height:56px}
.brand{display:flex;gap:10px;align-items:center;font-weight:600}.brand i{width:20px;height:20px;border-radius:6px;background:var(--g)}
nav{display:flex;gap:4px}nav a{padding:6px 12px;border-radius:8px;color:var(--mute);font-size:14px}nav a.on,nav a:hover{background:var(--panel);color:var(--text)}
.hero{padding:72px 0 40px;text-align:center}.eyebrow{color:var(--mute);font-size:13px;letter-spacing:.04em}
h1{font-size:clamp(42px,8vw,84px);line-height:1;letter-spacing:-.04em;margin:18px 0}h1 em{font-style:normal;background:var(--g);-webkit-background-clip:text;background-clip:text;color:transparent}
.lead{max-width:560px;margin:0 auto 28px;color:var(--mute);font-size:17px}
.btn{display:inline-block;padding:9px 18px;border-radius:999px;border:1px solid var(--line);font-size:14px;margin:4px}.btn.p{background:var(--text);color:var(--bg);border-color:var(--text)}
h2{font-size:clamp(28px,4vw,40px);letter-spacing:-.03em;line-height:1.1;margin:8px 0 6px}.sec{padding:36px 0}.kicker{color:var(--mute);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}.card b{display:block;font-size:34px;letter-spacing:-.03em}.card span{color:var(--mute);font-size:13px}
.fun{display:grid;gap:10px;margin-top:18px}.fun div{display:grid;grid-template-columns:150px 1fr 40px;gap:12px;align-items:center;font-size:14px}
.bar{height:10px;border-radius:99px;background:var(--panel);border:1px solid var(--line);overflow:hidden}.bar i{display:block;height:100%;background:var(--g)}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:10px;margin-top:16px}.step{border:1px solid var(--line);border-radius:12px;padding:12px;background:var(--panel)}
.step small{color:var(--accent);font-weight:600}.step p{margin:4px 0 0;color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;margin-top:14px}th{text-align:left;color:var(--mute);font-weight:500;font-size:12px;padding:8px 10px}
td{padding:12px 10px;border-top:1px solid var(--line);vertical-align:top}tr.l:hover td{background:var(--panel)}td small{color:var(--mute)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;border:1px solid var(--line);white-space:nowrap}
.pill.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}.pill.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}.pill.mute{color:var(--mute)}
.chips a{display:inline-block;margin:0 6px 6px 0;padding:4px 12px;border:1px solid var(--line);border-radius:99px;font-size:13px;color:var(--mute)}.chips a.on{background:var(--text);color:var(--bg)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:760px){.grid2{grid-template-columns:1fr}.fun div{grid-template-columns:100px 1fr 34px}}
.box{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-top:14px}.box h3{margin:0 0 8px;font-size:13px;color:var(--mute);font-weight:500;text-transform:uppercase;letter-spacing:.05em}
.q{border-left:2px solid var(--accent);padding:2px 0 2px 10px;margin:8px 0;color:var(--mute);font-size:13px}
pre{white-space:pre-wrap;font:14px/1.6 ui-monospace,Consolas,monospace;margin:0}.back{color:var(--mute);font-size:14px}
code{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:13px}
footer{color:var(--mute);font-size:13px;padding:36px 0;border-top:1px solid var(--line);margin-top:30px}
"""


def load():
    f = ROOT / "data/snapshot.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"rows": [], "generated": None, "hunter_left": None}


def shell(tab, body):
    nav = "".join(f'<a href="/?p={k}" class="{"on" if tab == k else ""}">{v}</a>'
                  for k, v in (("overview", "Overview"), ("companies", "Companies"), ("method", "Method"), ("run", "Run")))
    return (f'<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
            f'<title>Outreach Agent</title><style>{CSS}</style><header><div class=wrap><a class=brand href="/"><i></i>Outreach Agent</a>'
            f'<nav>{nav}</nav></div></header><main class=wrap>{body}</main>'
            f'<div class=wrap><footer>Case study build for Sprinto-style compliance outreach. Not affiliated with Sprinto. '
            f'Every email is a draft: nothing is sent automatically.</footer></div>')


def count(rows, *st):
    return sum(r["status"] in st for r in rows)


def overview(snap):
    rows = snap["rows"]
    n = len(rows)
    drafted, human = count(rows, "drafted"), sum(r["needs_human"] for r in rows)
    gates = n - count(rows, "failed", "disqualified", "manual_review")
    fit = count(rows, "drafted", "no_contact", "no_hook")
    stages = [("Researched", n - count(rows, "failed")), ("Passed the gates", gates), ("Fit score 70+", fit),
              ("Contact found", count(rows, "drafted", "no_hook")), ("Email drafted", drafted)]
    top = max(n, 1)
    fun = "".join(f'<div><span>{l}</span><span class=bar><i style="width:{v * 100 // top}%"></i></span><b>{v}</b></div>' for l, v in stages)
    steps = "".join(f'<div class=step><small>Step {i}</small><br><b>{t}</b><p>{d}</p></div>' for i, (t, d) in enumerate(STEPS, 1))
    when = f' Last live run: {e(snap["generated"][:16].replace("T", " "))} UTC.' if snap["generated"] else ""
    hl = "" if snap["hunter_left"] is None else f'<div class=card><b>{snap["hunter_left"]}</b><span>Hunter searches left</span></div>'
    return (f'<section class=hero><div class=eyebrow>Outreach agent for compliance automation</div>'
            f'<h1>Every decision,<br><em>defended.</em></h1>'
            f'<p class=lead>It reads a company\'s site, decides if it needs SOC 2 help, finds the right person and drafts an email, '
            f'and shows the quote behind every claim.</p><a class="btn p" href="/?p=companies">See the companies</a>'
            f'<a class=btn href="/?p=run">Run the pipeline</a></section>'
            f'<section class=sec><div class=kicker>Latest run.{when}</div><div class=cards>'
            f'<div class=card><b>{n}</b><span>Companies researched</span></div><div class=card><b>{drafted}</b><span>Emails drafted</span></div>'
            f'<div class=card><b>{human}</b><span>Waiting on a human</span></div>'
            f'<div class=card><b>{count(rows, "disqualified", "not_qualified")}</b><span>Dropped by a rule</span></div>{hl}</div></section>'
            f'<section class=sec><div class=kicker>Funnel</div><h2>Where companies drop off.</h2>'
            f'<p class=lead style="margin:0">From a scraped homepage to an email ready to review.</p><div class=fun>{fun}</div></section>'
            f'<section class=sec><div class=kicker>The case pipeline</div><h2>Seven steps, one command.</h2><div class=steps>{steps}</div></section>')


def companies(snap, flt):
    rows = snap["rows"]
    groups = {"all": None, "drafted": ("drafted",), "human": ("manual_review", "failed", "no_contact", "no_hook"),
              "dropped": ("disqualified", "not_qualified")}
    chips = "".join(f'<a class="{"on" if flt == k else ""}" href="/?p=companies&f={k}">{k.title()}'
                    f' ({len(rows) if v is None else count(rows, *v)})</a>' for k, v in groups.items())
    keep = [r for r in rows if groups.get(flt) is None or r["status"] in groups[flt]]
    keep.sort(key=lambda r: (r["status"] != "drafted", -(r.get("score") or 0)))
    body = "".join(
        f'<tr class=l onclick="location=\'/?p=company&d={quote(r["domain"])}\'"><td><a href="/?p=company&d={quote(r["domain"])}"><b>{e(r["domain"])}</b></a>'
        f'<br><small>{e(r["reason"][:140])}</small></td><td><span class="pill {STATUS[r["status"]][1]}">{STATUS[r["status"]][0]}</span></td>'
        f'<td>{"-" if r.get("score") is None else r["score"]}</td><td>{e(r["contact"]["name"]) if r.get("contact") else "-"}</td></tr>'
        for r in keep) or '<tr><td colspan=4><small>No companies yet. Run the pipeline and snapshot it.</small></td></tr>'
    return (f'<section class=sec><div class=kicker>Companies</div><h2>Every company, with its reason.</h2><div class=chips style="margin-top:14px">{chips}</div>'
            f'<table><tr><th>Company</th><th>Outcome</th><th>Fit</th><th>Contact</th></tr>{body}</table></section>')


def company(snap, domain):
    r = next((x for x in snap["rows"] if x["domain"] == domain), None)
    if not r:
        return '<section class=sec><a class=back href="/?p=companies">Back</a><h2>Not found.</h2></section>'
    lab, cls = STATUS[r["status"]]
    ev = "".join(f'<div class=q><b>{e(x["question"])}</b> {"answered" if x["answered"] else "unanswered, so: " + e(x["if_unanswered"])}'
                 f'<br>{e(", ".join(x["fetched"]) or "the homepage was enough")}</div>' for x in r.get("evidence", [])) or "<small>No pages were read.</small>"
    f = r.get("facts", {})
    facts = "".join(f"<tr><td><small>{e(k.replace('_', ' '))}</small></td><td>{e(', '.join(map(str, v)) if isinstance(v, list) else str(v))}</td></tr>"
                    for k, v in f.items() if v not in (None, "", [], False))
    mail = ""
    if r.get("body"):
        c = r["contact"]
        pains = "".join(f'<div class=q><b>{e(p["pain"])}</b><br>{e(p["evidence"])}</div>' for p in r["pain_points"])
        link = f'mailto:?subject={quote(r["subject"])}&body={quote(r["body"])}'
        mail = (f'<div class=grid2><div class=box><h3>Why this person</h3><b>{e(c["name"])}</b>, {e(c["title"])}<div class=q>{e(c["why"])}</div></div>'
                f'<div class=box><h3>Two pain points</h3>{pains}</div></div>'
                f'<div class=box><h3>Email draft</h3><p><b>Hook:</b> {e(r["hook"])}<br><b>Subject:</b> {e(r["subject"])}</p><pre>{e(r["body"])}</pre>'
                f'<p><a class="btn p" href="{e(link)}">Review and send from my mail app</a></p>'
                f'<small>Opens your own mail app with the draft filled in. Nothing is sent from here.</small></div>')
    flags = "".join(f"<div class=q>{e(x)}</div>" for x in r.get("flags", []))
    return (f'<section class=sec><a class=back href="/?p=companies">Back to companies</a><h2>{e(r["domain"])}</h2>'
            f'<span class="pill {cls}">{lab}</span> <small>fit {"-" if r.get("score") is None else r["score"]}</small>'
            f'<p class=lead style="margin:10px 0 0;text-align:left;max-width:none">{e(r["reason"])}</p>'
            f'<div class=grid2><div class=box><h3>Evidence map</h3>{ev}{flags}</div><div class=box><h3>Facts extracted</h3><table>{facts or "<tr><td><small>none</small></td></tr>"}</table></div></div>'
            f'{mail}</section>')


NINE = [("Kill switches", "A no means do not build it", [
            ("Problem real?", "Yes. 15-20 minutes per company, 8-15 a week, quality drops by Friday."),
            ("Workflow repeated?", "Yes. The same research for every new prospect, every week."),
            ("Input available?", "Yes. Company sites are public; contacts come from Hunter.io.")]),
        ("Sizing", "A no means build something smaller", [
            ("Output valuable?", "Yes. A qualified lead with a drafted email is directly actionable."),
            ("Impact measurable?", "Yes. Seconds per company, rewrite rate, conversion."),
            ("ROI worth it?", "Yes. Even a small lift in outbound conversion pays for it.")]),
        ("Boundary", "A no means put a human exactly here", [
            ("Failure risk OK?", "Partly. Mail goes to your own inbox only, and sending is a human step."),
            ("Judgment protected?", "Yes. Every unsure case goes to a human instead of a guess."),
            ("Owner clear?", "Yes. The Growth Lead reviews and sends.")])]
DECISIONS = [("Outcome", "Cut prospect research from 15-20 minutes to under 2 minutes per company."),
             ("Baseline", "8-15 companies a week, 15-20 minutes each, quality dropping toward the end of the list."),
             ("Workflow change", "The Growth Lead moves from researcher and writer to reviewer and sender."),
             ("Measurement", "Seconds per company under 120; fewer than 20% of emails rewritten; conversion holds."),
             ("Kill criterion", "Stop if more than 50% of emails are rewritten, running it costs more time than it saves, or contact accuracy falls below 60%.")]


def method():
    ev = "".join(f"<tr><td><b>{e(q['ask'])}</b></td><td>{e(', '.join('/' + p for p in q['paths']))}</td><td>{q['limit']}</td>"
                 f"<td>{e(q['if_unanswered'])}</td></tr>" for q in qualify.QUESTIONS)
    nine = "".join(f"<div class=box><h3>{e(t)}</h3><small>{e(s)}</small>"
                   + "".join(f"<div class=q><b>{e(a)}</b> {e(b)}</div>" for a, b in rows) + "</div>" for t, s, rows in NINE)
    dec = "".join(f"<div class=q><b>{i}. {e(a)}</b> {e(b)}</div>" for i, (a, b) in enumerate(DECISIONS, 1))
    return ('<section class=sec><div class=kicker>Method</div><h2>Why it is built this way.</h2>'
            '<p class=lead style="text-align:left;margin:0">Code decides, the model reads. The model fills named fields; Python checks them, '
            'picks the next page, stops at the limit, and hands over to a human when unsure.</p>'
            f'<div class=box><h3>Evidence Map (the live rules)</h3><table><tr><th>Question</th><th>Look at</th><th>Fetch limit</th><th>If unanswered</th></tr>{ev}</table>'
            '<small>Worst case: 4 page fetches beyond the homepage. A hallucinated quote that is not on the page is dropped and counted.</small></div>'
            f'<h2 style="margin-top:36px">The nine checks.</h2><div class=grid2>{nine}</div>'
            f'<h2 style="margin-top:36px">Five pre-build decisions.</h2><div class=box>{dec}</div></section>')


def run_page(demo):
    out = ""
    if demo:
        rows = [run.process(d, str(ROOT / "data/contacts.csv")) for d in (ROOT / "data/domains.txt").read_text().split()]
        st = impact.run_stats(rows)
        out = ('<div class=box><h3>Demo run (fictional companies, canned model replies, nothing sent)</h3><table>'
               + "".join(f'<tr><td><b>{e(r["domain"])}</b></td><td><span class="pill {STATUS[r["status"]][1]}">{STATUS[r["status"]][0]}</span></td>'
                         f'<td><small>{e(r["reason"][:110])}</small></td></tr>' for r in rows)
               + f'</table><p><small>{st["companies"]} companies, {st["needs_human"]} for a human, '
                 f'{st["avg_fetches_beyond_homepage"]} extra page fetches each on average (cap 4).</small></p></div>')
    return (f'<section class=sec><div class=kicker>Run</div><h2>Run the pipeline.</h2>'
            f'<p class=lead style="text-align:left;margin:0">The live run needs your keys, so it runs on your machine. This page can run the offline demo.</p>'
            f'<p><a class="btn p" href="/?p=run&demo=1">Run the demo</a></p>{out}'
            f'<div class=box><h3>Live run</h3><pre>cp .env.example .env     # add GEMINI_API_KEY, HUNTER_API_KEY, TO_ADDRESS\n'
            f'python shortlist.py 20   # funded SaaS from Google News\n'
            f'python run.py --domains data/shortlist.txt\npython snapshot.py       # refresh this page with the results\ngit push</pre></div></section>')


def page(url):
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    tab, snap = q.get("p", "overview"), load()
    if tab == "companies":
        return shell("companies", companies(snap, q.get("f", "all")))
    if tab == "company":
        return shell("companies", company(snap, q.get("d", "")))
    if tab == "method":
        return shell("method", method())
    if tab == "run":
        return shell("run", run_page(q.get("demo")))
    return shell("overview", overview(snap))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body, code = page(self.path).encode(), 200
        except Exception as ex:  # show the failure instead of a blank 500
            body, code = f"error: {ex}".encode(), 500
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
