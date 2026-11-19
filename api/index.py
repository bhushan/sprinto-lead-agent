"""Outreach Agent web app (one Vercel Python function, no framework).

Read-only on data/snapshot.json until a database is connected (DATABASE_URL). With a database, a Gemini key and
APP_PASSWORD it can also run the pipeline from the browser and learn from the reviewer's edits and ratings.
The offline demo always runs on the bundled fictional fixtures and never touches the network or sends mail.
"""
import hmac
import json
import os
import re
import sys
import tempfile
import time
from html import escape as e
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(tempfile.gettempdir())  # send.py writes outbox/ relative to cwd; only the temp dir is writable on Vercel

import requests  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import drafts as drafts_mod  # noqa: E402
import impact  # noqa: E402
import qualify  # noqa: E402
import run  # noqa: E402

config.OFFLINE = not config.API_KEY  # live whenever a Gemini key is configured
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
DOMAIN = re.compile(r"^(?=.{4,100}$)([a-z0-9-]+\.)+[a-z]{2,}$")  # rejects IPs and localhost
MAX_PER_RUN = 5  # ~30 s per company; stays inside the 300 s function limit

STATUS = {  # label, css class
    "drafted": ("Email drafted", "ok"), "not_qualified": ("Not qualified", "mute"),
    "disqualified": ("Disqualified", "mute"), "manual_review": ("Needs a human", "warn"),
    "failed": ("Needs a human", "warn"), "no_contact": ("Needs a human", "warn"), "no_hook": ("Needs a human", "warn"),
}
TODO = {  # what the human should do for each hand-over
    "manual_review": "Open the site and decide if it is a B2B SaaS company. If yes, add it to your list and re-run.",
    "failed": "The homepage could not be read. Check the site by hand, or try the domain again later.",
    "no_contact": "Hunter found nobody who owns security or engineering. Look the right person up in Apollo and add them to data/contacts.csv.",
    "no_hook": "Nothing specific to open with. Write the first line yourself.",
}
STEPS = [("Shortlist", "Funded SaaS from Google News", 0), ("Scrape", "One cached, robots-aware fetch", 0),
         ("Extract", "Named fields, quotes verified", 0), ("Qualify", "Evidence Map loop, 4 fetches max", 0),
         ("Find contact", "Hunter.io, ranked by role", 0), ("Pain points", "Two, each with a quote", 0),
         ("Draft", "By name, with a specific hook", 0), ("Review", "A human approves", 1), ("Send", "To your inbox only", 1)]

CSS = """
:root{--navy:#0b2545;--navy2:#13315c;--blue:#2563eb;--green:#10b981;--green-d:#047857;--bg:#f3f7fb;--panel:#fff;--line:#dfe7f1;--text:#0f1d33;--mute:#62748e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
a{color:inherit;text-decoration:none}.app{display:grid;grid-template-columns:236px 1fr;min-height:100vh}
aside{background:linear-gradient(180deg,var(--navy),var(--navy2));color:#cfe0f5;padding:20px 14px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column}
.brand{display:flex;gap:10px;align-items:center;color:#fff;font-weight:700;letter-spacing:.02em;margin:0 8px 22px}.brand i{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--blue),var(--green))}
.brand small{display:block;font-weight:500;font-size:11px;color:#8fb0d8}
aside h4{margin:18px 10px 6px;font-size:11px;letter-spacing:.12em;color:#7f9fc9;font-weight:600}
aside a{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border-radius:9px;color:#cfe0f5;font-size:14px}
aside a:hover{background:rgba(255,255,255,.07)}aside a.on{background:linear-gradient(90deg,var(--blue),#1d7fd6);color:#fff}
.badge{background:var(--green);color:#04281f;border-radius:99px;font-size:11px;font-weight:700;padding:1px 8px}
.note{margin-top:auto;font-size:12px;color:#8fb0d8;padding:10px}
.top{display:flex;gap:12px;align-items:center;padding:14px 28px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:4}
.top form{flex:1;max-width:420px}.top input{width:100%;padding:9px 14px;border:1px solid var(--line);border-radius:12px;background:var(--bg);font:inherit}
.live{color:var(--mute);font-size:13px;margin-left:auto}.live b{color:var(--green-d)}
.btn{display:inline-block;padding:9px 16px;border-radius:12px;border:1px solid var(--line);background:var(--panel);font-size:14px;cursor:pointer;font-family:inherit}
.btn.p{background:linear-gradient(90deg,var(--blue),var(--green));color:#fff;border:0;font-weight:600}.btn.g{background:var(--green);color:#04281f;border:0;font-weight:600}.btn[disabled]{opacity:.5;cursor:not-allowed}
main{padding:28px;max-width:1080px}.kicker{color:var(--mute);font-size:12px;letter-spacing:.06em;text-transform:uppercase}
h1{font-size:34px;letter-spacing:-.03em;margin:6px 0 4px}h2{font-size:20px;letter-spacing:-.02em;margin:0 0 4px}.sub{color:var(--mute);max-width:620px;margin:0 0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px}.card small{color:var(--mute);font-size:11px;letter-spacing:.1em;text-transform:uppercase}
.card b{display:block;font-size:34px;letter-spacing:-.03em;color:var(--navy)}.card span{color:var(--mute);font-size:13px}.card.hi b{color:var(--green-d)}
.box{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:16px}.box h3{margin:0 0 8px;font-size:12px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.08em}
.flow{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:12px}
.step{border:1px solid var(--line);border-radius:14px;padding:12px;background:#fafcff}.step small{display:flex;align-items:center;gap:6px;color:var(--mute);font-size:11px;font-weight:600}
.step small i{width:9px;height:9px;border-radius:50%;background:var(--blue)}.step.h small i{background:var(--green)}.step p{margin:3px 0 0;color:var(--mute);font-size:13px}
.legend{float:right;font-size:12px;color:var(--mute)}.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 12px}
.fun{display:grid;gap:10px;margin-top:10px}.fun div{display:grid;grid-template-columns:150px 1fr 36px;gap:12px;align-items:center;font-size:14px}
.bar{height:10px;border-radius:99px;background:#e8eff8;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--green))}
table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--mute);font-weight:600;font-size:11px;letter-spacing:.08em;text-transform:uppercase;padding:8px 10px}
td{padding:12px 10px;border-top:1px solid var(--line);vertical-align:top}tr.l{cursor:pointer}tr.l:hover td{background:#f6faff}td small{color:var(--mute)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;border:1px solid var(--line);white-space:nowrap;background:#fff}
.pill.ok{color:var(--green-d);border-color:#a7e8cf;background:#ecfdf5}.pill.warn{color:#a15c07;border-color:#f6d49a;background:#fffaf0}.pill.mute{color:var(--mute)}
.chips a{display:inline-block;margin:0 6px 8px 0;padding:4px 12px;border:1px solid var(--line);border-radius:99px;font-size:13px;color:var(--mute);background:#fff}.chips a.on{background:var(--navy);color:#fff;border-color:var(--navy)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.q{border-left:3px solid var(--blue);padding:2px 0 2px 10px;margin:8px 0;color:var(--mute);font-size:13px}.q b{color:var(--text)}
pre{white-space:pre-wrap;font:14px/1.6 ui-monospace,Consolas,monospace;margin:0}.back{color:var(--mute);font-size:14px}
input.t,textarea.t{width:100%;border:1px solid var(--line);border-radius:10px;padding:9px 12px;font:inherit;background:#fff;margin:4px 0 8px}textarea.t{min-height:190px;font:14px/1.6 ui-monospace,Consolas,monospace}
textarea.dom{min-height:110px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.ok-t{color:var(--green-d);font-weight:600;font-size:13px}
.warnbox{background:#fffaf0;border:1px solid #f6d49a;color:#7a4a05;border-radius:12px;padding:12px 14px;margin:14px 0;font-size:14px}
@media(max-width:860px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.grid2{grid-template-columns:1fr}.fun div{grid-template-columns:100px 1fr 30px}main{padding:18px}.top{padding:12px 18px}}
"""

JS = """
const S=(k,v)=>{try{v===undefined?localStorage.removeItem(k):localStorage.setItem(k,v)}catch(e){}};
const G=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
const tok=()=>{let t=G('token');if(!t){t=prompt('Access password');if(t)S('token',t)}return t};
const post=(p,data)=>fetch('/?p='+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,token:tok()})})
  .then(r=>r.json().then(j=>{if(r.status===401)S('token');if(!r.ok)throw new Error(j.error||'failed');return j}));
document.querySelectorAll('.draft').forEach(d=>{
  const id=d.dataset.d,sub=d.querySelector('.sub-in'),body=d.querySelector('.body-in'),msg=d.querySelector('.msg');
  const saved=G('draft:'+id);if(saved){try{const o=JSON.parse(saved);sub.value=o.s;body.value=o.b}catch(e){}}
  if(G('sent:'+id)){msg.textContent='Approved and opened in your mail app'}
  const save=()=>S('draft:'+id,JSON.stringify({s:sub.value,b:body.value}));sub.oninput=body.oninput=()=>{save();msg.textContent='Edit saved in this browser'};
  d.querySelector('.send').onclick=()=>{save();S('sent:'+id,'1');msg.textContent='Approved and opened in your mail app';
    post('fb',{domain:id,kind:'edit',data:{before:{subject:sub.defaultValue,body:body.defaultValue},after:{subject:sub.value,body:body.value}}}).catch(()=>{});
    location.href='mailto:?subject='+encodeURIComponent(sub.value)+'&body='+encodeURIComponent(body.value)};
  d.querySelector('.copy').onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(sub.value+'\\n\\n'+body.value);msg.textContent='Copied'};
  [['.up',1],['.down',-1]].forEach(([c,v])=>d.querySelector(c).onclick=()=>post('fb',{domain:id,kind:'rating',data:{value:v,hook:d.dataset.hook}})
    .then(()=>msg.textContent='Thanks, saved to the feedback loop').catch(x=>msg.textContent=x.message));
});
document.querySelectorAll('.resolve').forEach(b=>{
  const id=b.dataset.d,card=b.closest('.box');if(G('done:'+id)){card.style.opacity='.45';b.textContent='Resolved'}
  b.onclick=()=>{const on=!G('done:'+id);S('done:'+id,on?'1':undefined);card.style.opacity=on?'.45':'1';b.textContent=on?'Resolved':'Mark resolved'}
});
const go=(sel,p,data,wait)=>{const b=document.querySelector(sel);if(!b)return;b.onclick=()=>{const m=document.querySelector('#status');b.disabled=true;m.textContent=wait;
  post(p,data()).then(j=>{m.textContent=j.note||'Done';setTimeout(()=>location.reload(),600)}).catch(x=>{m.textContent=x.message;b.disabled=false})}};
go('#go','run',()=>({domains:document.querySelector('#doms').value.split(/[\\s,]+/).filter(Boolean)}),'Running, this takes up to 3 minutes. Keep this tab open.');
go('#short','run',()=>({shortlist:true}),'Running the next companies from the shortlist, up to 3 minutes. Keep this tab open.');
go('#learn','learn',()=>({}),'Learning from your edits and ratings...');
"""


def _snapshot():
    f = ROOT / "data/snapshot.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"rows": [], "generated": None, "hunter_left": None}


_hl = {"t": 0, "v": None}


def hunter_left(fallback):
    """Live Hunter balance (a free call) when a key is set; the snapshot value otherwise. Cached for a minute."""
    if config.HUNTER_API_KEY and not config.OFFLINE and time.time() - _hl["t"] > 60:
        try:
            r = requests.get("https://api.hunter.io/v2/account", params={"api_key": config.HUNTER_API_KEY}, timeout=3).json()
            _hl.update(t=time.time(), v=r["data"]["requests"]["searches"]["remaining"])
        except (requests.RequestException, KeyError, ValueError):
            pass
    return _hl["v"] if _hl["v"] is not None else fallback


def load():
    snap = _snapshot()
    if not db.enabled:
        return snap
    try:
        rows = db.companies()
        if not rows:  # first visit after connecting the database: import the saved run
            for r in snap["rows"]:
                db.save_company(r)
            rows = db.companies()
        return {"rows": rows, "generated": db.last_update(), "hunter_left": hunter_left(snap["hunter_left"]), "db": True}
    except Exception as ex:  # a broken database must not take the read-only pages down
        return {**snap, "db_error": str(ex)[:160]}


def live_missing():
    """What is still needed before the browser can run the pipeline."""
    return [name for name, ok in (("DATABASE_URL (add Neon from the Vercel Marketplace)", db.enabled),
                                  ("GEMINI_API_KEY", bool(config.API_KEY)), ("APP_PASSWORD", bool(APP_PASSWORD))) if not ok]


def count(rows, *st):
    return sum(r["status"] in st for r in rows)


def shell(tab, body, snap, q=""):
    rows = snap["rows"]
    human, drafts, ev = sum(r["needs_human"] for r in rows), count(rows, "drafted"), 0

    def link(k, label, badge=None):
        b = f'<span class=badge>{badge}</span>' if badge else ""
        return f'<a href="/?p={k}" class="{"on" if tab == k else ""}">{label}{b}</a>'

    when = f'Last run <b>{e(snap["generated"][:10])}</b>' if snap["generated"] else "No run yet"
    return ('<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
            f'<title>Outreach Agent</title><style>{CSS}</style><div class=app><aside>'
            '<div class=brand><i></i><div>OUTREACH<small>AGENT</small></div></div>'
            f'<h4>PIPELINE</h4>{link("overview", "Overview")}{link("run", "Run pipeline")}{link("companies", "Companies")}{link("method", "Method")}'
            f'<h4>HUMAN GATES</h4>{link("review", "Review queue", human)}{link("drafts", "Drafts &amp; sends", drafts)}'
            f'<h4>LEARNING</h4>{link("feedback", "Feedback loop")}'
            '<div class=note>The model reads and writes. Python rules decide. Every draft waits for you.<br><br>Case study build, not affiliated with Sprinto.</div></aside>'
            '<div><div class=top><form action="/"><input type=hidden name=p value=companies><input name=q placeholder="Search companies" value="'
            f'{e(q)}"></form><span class=live>{when}</span><a class="btn p" href="/?p=run">Run pipeline</a></div>'
            f'<main>{body}</main></div></div><script>{JS}</script>')


def overview(snap):
    rows = snap["rows"]
    n = len(rows)
    fit = count(rows, "drafted", "no_contact", "no_hook")
    human = sum(r["needs_human"] for r in rows)
    hl = snap["hunter_left"]
    cards = [("Pool", n, "companies on the list", ""), ("Researched", n - count(rows, "failed"), "pages read", ""),
             ("Qualified", fit, "fit score 70 or more", "hi"), ("Flagged", human, "waiting on a human", ""),
             ("Dropped", count(rows, "disqualified", "not_qualified"), "a gate failed", ""),
             ("Hunter left", "-" if hl is None else hl, "searches this month", "")]
    cards_html = "".join(f'<div class="card {c}"><small>{a}</small><b>{b}</b><span>{d}</span></div>' for a, b, d, c in cards)
    flow = "".join(f'<div class="step {"h" if h else ""}"><small><i></i>STEP {i}</small><b>{t}</b><p>{d}</p></div>' for i, (t, d, h) in enumerate(STEPS, 1))
    stages = [("Researched", n - count(rows, "failed")), ("Passed the gates", n - count(rows, "failed", "disqualified", "manual_review")),
              ("Fit score 70+", fit), ("Contact found", count(rows, "drafted", "no_hook")), ("Email drafted", count(rows, "drafted"))]
    top = max(n, 1)
    fun = "".join(f'<div><span>{l}</span><span class=bar><i style="width:{v * 100 // top}%"></i></span><b>{v}</b></div>' for l, v in stages)
    return ('<div class=kicker>Latest run</div><h1>Pipeline overview</h1>'
            '<p class=sub>Funded SaaS companies, shortlisted by rules, researched in batches, qualified in Python and drafted automatically when nothing needs a human.</p>'
            f'<div class=cards>{cards_html}</div>'
            f'<div class=box><span class=legend><i style="background:var(--blue)"></i>automatic<i style="background:var(--green)"></i>human gate</span>'
            f'<h3>The case pipeline</h3><h2>Seven steps to an email in your inbox.</h2><div class=flow>{flow}</div></div>'
            f'<div class=box><h3>Funnel</h3><h2>Where companies drop off.</h2><div class=fun>{fun}</div></div>')


def companies(snap, flt, q):
    rows = snap["rows"]
    groups = {"all": None, "drafted": ("drafted",), "human": ("manual_review", "failed", "no_contact", "no_hook"),
              "dropped": ("disqualified", "not_qualified")}
    chips = "".join(f'<a class="{"on" if flt == k else ""}" href="/?p=companies&f={k}">{k.title()}'
                    f' ({len(rows) if v is None else count(rows, *v)})</a>' for k, v in groups.items())
    keep = [r for r in rows if (groups.get(flt) is None or r["status"] in groups[flt])
            and (not q or q.lower() in (r["domain"] + r["reason"]).lower())]
    keep.sort(key=lambda r: (r["status"] != "drafted", -(r.get("score") or 0)))
    body = "".join(
        f'<tr class=l onclick="location=\'/?p=company&d={quote(r["domain"])}\'"><td><a href="/?p=company&d={quote(r["domain"])}"><b>{e(r["domain"])}</b></a>'
        f'<br><small>{e(r["reason"][:140])}</small></td><td><span class="pill {STATUS[r["status"]][1]}">{STATUS[r["status"]][0]}</span></td>'
        f'<td>{"-" if r.get("score") is None else r["score"]}</td><td>{e(r["contact"]["name"]) if r.get("contact") else "-"}</td></tr>'
        for r in keep) or '<tr><td colspan=4><small>Nothing matches.</small></td></tr>'
    return (f'<div class=kicker>Companies</div><h1>Every company, with its reason.</h1><div class=chips style="margin-top:14px">{chips}</div>'
            f'<div class=box style="margin-top:0"><table><tr><th>Company</th><th>Outcome</th><th>Fit</th><th>Contact</th></tr>{body}</table></div>')


def company(snap, domain):
    r = next((x for x in snap["rows"] if x["domain"] == domain), None)
    if not r:
        return '<a class=back href="/?p=companies">Back</a><h1>Not found.</h1>'
    lab, cls = STATUS[r["status"]]
    ev = "".join(f'<div class=q><b>{e(x["question"])}</b> {"answered" if x["answered"] else "unanswered, so: " + e(x["if_unanswered"])}'
                 f'<br>{e(", ".join(x["fetched"]) or "the homepage was enough")}</div>' for x in r.get("evidence", [])) or "<small>No pages were read.</small>"
    facts = "".join("<tr><td><small>{}</small></td><td>{}</td></tr>".format(
        e(k.replace("_", " ")), e(", ".join(map(str, v)) if isinstance(v, list) else str(v)))
        for k, v in r.get("facts", {}).items() if v not in (None, "", [], False)) or "<tr><td><small>none</small></td></tr>"
    flags = "".join(f"<div class=q>{e(x)}</div>" for x in r.get("flags", []))
    extra = ""
    if r.get("body"):
        c = r["contact"]
        pains = "".join(f'<div class=q><b>{e(p["pain"])}</b><br>{e(p["evidence"])}</div>' for p in r["pain_points"])
        extra = (f'<div class=grid2><div class=box><h3>Why this person</h3><b>{e(c["name"])}</b>, {e(c["title"])}<div class=q>{e(c["why"])}</div></div>'
                 f'<div class=box><h3>Two pain points</h3>{pains}</div></div>'
                 f'<div class=box><h3>Email draft</h3><p><b>Hook:</b> {e(r["hook"])}<br><b>Subject:</b> {e(r["subject"])}</p><pre>{e(r["body"])}</pre>'
                 '<p><a class="btn g" href="/?p=drafts">Edit and send in Drafts &amp; sends</a></p></div>')
    elif r.get("needs_human"):
        extra = f'<div class=box><h3>What a human should do</h3>{e(TODO.get(r["status"], ""))}</div>'
    return (f'<a class=back href="/?p=companies">Back to companies</a><h1>{e(r["domain"])}</h1>'
            f'<span class="pill {cls}">{lab}</span> <small>fit {"-" if r.get("score") is None else r["score"]}</small>'
            f'<p class=sub style="margin-top:10px">{e(r["reason"])}</p>'
            f'<div class=grid2><div class=box style="margin-top:0"><h3>Evidence map</h3>{ev}{flags}</div>'
            f'<div class=box style="margin-top:0"><h3>Facts extracted</h3><table>{facts}</table></div></div>{extra}')


def review(snap):
    rows = [r for r in snap["rows"] if r["needs_human"]]
    cards = "".join(
        f'<div class=box><div class=row><b>{e(r["domain"])}</b><span class="pill warn">{e(r["status"].replace("_", " "))}</span>'
        f'<a class=btn style="margin-left:auto" href="/?p=company&d={quote(r["domain"])}">Open</a>'
        f'<button class="btn resolve" data-d="{e(r["domain"])}">Mark resolved</button></div>'
        f'<p style="margin:8px 0 4px">{e(r["reason"])}</p><div class=q><b>Do this:</b> {e(TODO.get(r["status"], ""))}</div></div>' for r in rows)
    return ('<div class=kicker>Human gate</div><h1>Review queue</h1><p class=sub>Companies the rules could not settle. '
            'Nothing here is guessed: each one waits for a person.</p>' + (cards or '<div class=box>Nothing is waiting on you.</div>'))


def drafts(snap):
    rows = [r for r in snap["rows"] if r.get("body")]
    cards = "".join(
        f'<div class="box draft" data-d="{e(r["domain"])}" data-hook="{e(r["hook"][:200])}"><div class=row><b>{e(r["contact"]["name"])}</b><small>{e(r["contact"]["title"])} at {e(r["domain"])}</small>'
        f'<a class=btn style="margin-left:auto" href="/?p=company&d={quote(r["domain"])}">Why this email</a></div>'
        f'<small>Hook: {e(r["hook"])}</small>'
        f'<input class="t sub-in" value="{e(r["subject"])}"><textarea class="t body-in">{e(r["body"])}</textarea>'
        '<div class=row><button class="btn g send">Approve &amp; send</button><button class="btn copy">Copy</button>'
        '<button class="btn up" title="Good draft">Good</button><button class="btn down" title="Poor draft">Poor</button><span class="ok-t msg"></span></div></div>'
        for r in rows)
    return ('<div class=kicker>Human gate</div><h1>Drafts &amp; sends</h1><p class=sub>Edit a draft, then approve it. '
            '"Approve &amp; send" opens your own mail app with the text filled in, so mail goes out from your account and nothing is sent from this site. '
            'Your edits and ratings feed the Feedback loop and shape the next drafts.</p>' + (cards or '<div class=box>No drafts yet.</div>'))


def feedback(snap):
    if not db.enabled:
        return ('<div class=kicker>Learning</div><h1>Feedback loop</h1><div class=warnbox>No database is connected yet, so edits and ratings are not stored. '
                'Add Neon from the Vercel Marketplace (Storage tab) and redeploy.</div>')
    try:
        evs, rules = db.events(100), db.learnings()
    except Exception as ex:
        return f'<div class=kicker>Learning</div><h1>Feedback loop</h1><div class=warnbox>Database error: {e(str(ex)[:160])}</div>'
    edits = [x for x in evs if x["kind"] == "edit" and x["data"]["before"] != x["data"]["after"]]
    up = sum(x["kind"] == "rating" and x["data"]["value"] > 0 for x in evs)
    down = sum(x["kind"] == "rating" and x["data"]["value"] < 0 for x in evs)
    cards = "".join(f'<div class=card><small>{a}</small><b>{b}</b><span>{c}</span></div>' for a, b, c in (
        ("Events", len(evs), "edits and ratings"), ("Edited drafts", len(edits), "changed before sending"),
        ("Good / Poor", f"{up} / {down}", "ratings"), ("Rules learned", len(rules), "used in every new draft")))
    rule_html = "".join(f"<div class=q>{e(r)}</div>" for r in rules) or "<small>No rules yet. Edit or rate a few drafts, then click Learn.</small>"

    def summary(x):
        d = x["data"]
        if x["kind"] == "rating":
            return "rated " + ("good" if d["value"] > 0 else "poor")
        if d["before"] == d["after"]:
            return "approved without changes"
        return f"edited: {len(d['before']['body'].split())} to {len(d['after']['body'].split())} words"
    log = "".join(f"<tr><td><small>{e(x['ts'][:16].replace('T', ' '))}</small></td><td>{e(x['domain'])}</td><td>{e(summary(x))}</td></tr>" for x in evs[:20]) \
        or "<tr><td colspan=3><small>Nothing captured yet.</small></td></tr>"
    miss = live_missing()
    btn = ('<button class="btn g" id="learn">Learn from feedback</button>' if not miss else
           f'<button class=btn disabled>Learn from feedback</button> <small>needs {e(", ".join(miss))}</small>')
    return ('<div class=kicker>Learning</div><h1>Feedback loop</h1><p class=sub>Every edit and rating you make on a draft is stored. '
            'Learning turns them into a few rules that are added to the prompt of every new draft.</p>'
            f'<div class=cards>{cards}</div><div class=box><h3>Rules in use</h3>{rule_html}<p>{btn} <span class=ok-t id=status></span></p></div>'
            f'<div class=box><h3>Recent events</h3><table><tr><th>When</th><th>Company</th><th>What</th></tr>{log}</table></div>')


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
    nine = "".join(f"<div class=box style='margin-top:0'><h3>{e(t)}</h3><small>{e(s)}</small>"
                   + "".join(f"<div class=q><b>{e(a)}</b> {e(b)}</div>" for a, b in rows) + "</div>" for t, s, rows in NINE)
    dec = "".join(f"<div class=q><b>{i}. {e(a)}</b> {e(b)}</div>" for i, (a, b) in enumerate(DECISIONS, 1))
    return ('<div class=kicker>Method</div><h1>Why it is built this way.</h1>'
            '<p class=sub>Code decides, the model reads. The model fills named fields; Python checks them, picks the next page, '
            'stops at the limit, and hands over to a human when unsure.</p>'
            f'<div class=box><h3>Evidence Map (the live rules)</h3><table><tr><th>Question</th><th>Look at</th><th>Fetch limit</th><th>If unanswered</th></tr>{ev}</table>'
            '<small>Worst case: 4 page fetches beyond the homepage. A quote that is not on the page is dropped and counted.</small></div>'
            f'<h2 style="margin-top:28px">The nine checks</h2><div class=grid2 style="margin-top:10px">{nine}</div>'
            f'<h2 style="margin-top:28px">Five pre-build decisions</h2><div class=box style="margin-top:10px">{dec}</div>')


class offline:
    """Run the pipeline on fixtures. ponytail: flips a module global, so a live run in the same instance could briefly see it; fine at this traffic."""
    def __enter__(self):
        self.prev, config.OFFLINE = config.OFFLINE, True

    def __exit__(self, *a):
        config.OFFLINE = self.prev


def run_page(snap, demo):
    out = ""
    if demo:
        with offline():
            rows = [run.process(d, str(ROOT / "data/contacts.csv")) for d in (ROOT / "data/domains.txt").read_text().split()]
        st = impact.run_stats(rows)
        out = ('<div class=box><h3>Demo run: fictional companies, canned model replies, nothing sent</h3><table>'
               + "".join(f'<tr><td><b>{e(r["domain"])}</b></td><td><span class="pill {STATUS[r["status"]][1]}">{STATUS[r["status"]][0]}</span></td>'
                         f'<td><small>{e(r["reason"][:110])}</small></td></tr>' for r in rows)
               + f'</table><p><small>{st["companies"]} companies, {st["needs_human"]} for a human, '
                 f'{st["avg_fetches_beyond_homepage"]} extra page fetches each on average (cap 4).</small></p></div>')
    miss = live_missing()
    if miss:
        live = ('<div class=warnbox><b>Live runs are switched off.</b> Still needed: ' + e(", ".join(miss)) + '. '
                'Add them in the Vercel project settings and redeploy.</div>')
    else:
        try:
            hist = "".join(f"<tr><td><small>{e(x['ts'][:16].replace('T', ' '))}</small></td><td>{x['n']} companies</td><td>{x['drafted']} drafted</td></tr>" for x in db.runs()) \
                or "<tr><td colspan=3><small>No runs yet.</small></td></tr>"
        except Exception as ex:
            hist = f"<tr><td colspan=3><small>Database error: {e(str(ex)[:120])}</small></td></tr>"
        live = (f'<div class=box><h3>Live run (real sites, Gemini and Hunter)</h3><p class=sub style="margin-bottom:8px">Up to {MAX_PER_RUN} companies per run, about 30 seconds each. '
                'Each one that reaches the contact step spends one Hunter search.</p>'
                '<textarea class="t dom" id=doms placeholder="One domain per line, e.g. example.com"></textarea>'
                '<div class=row><button class="btn g" id=go>Run these domains</button><button class=btn id=short>Run the next companies from the shortlist</button>'
                f'<span class=ok-t id=status></span></div></div><div class=box><h3>Recent runs</h3><table>{hist}</table></div>')
    return ('<div class=kicker>Run</div><h1>Run the pipeline</h1>'
            '<p class=sub>Results land in Companies, Review queue and Drafts &amp; sends. Nothing is emailed from here.</p>'
            f'{live}<p><a class="btn" href="/?p=run&demo=1">Run the offline demo</a></p>{out}')


def page(url):
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    tab, snap = q.get("p", "overview"), load()
    if tab == "companies":
        return shell("companies", companies(snap, q.get("f", "all"), q.get("q", "")), snap, q.get("q", ""))
    if tab == "company":
        return shell("companies", company(snap, q.get("d", "")), snap)
    if tab == "review":
        return shell("review", review(snap), snap)
    if tab == "drafts":
        return shell("drafts", drafts(snap), snap)
    if tab == "feedback":
        return shell("feedback", feedback(snap), snap)
    if tab == "method":
        return shell("method", method(), snap)
    if tab == "run":
        return shell("run", run_page(snap, q.get("demo")), snap)
    return shell("overview", overview(snap), snap)


def do_run(body):
    if body.get("shortlist"):
        have = {r["domain"] for r in db.companies()}
        domains = [d for d in (ROOT / "data/shortlist.txt").read_text().split() if d not in have]
    else:
        domains = [str(d).strip().lower() for d in body.get("domains", [])]
    domains = list(dict.fromkeys(domains))[:MAX_PER_RUN]
    if not domains:
        return 400, {"error": "No new domains to run."}
    bad = [d for d in domains if not DOMAIN.match(d)]
    if bad:
        return 400, {"error": "Not a domain name: " + bad[0]}
    sl = ROOT / "data/shortlist.json"
    seeds = json.loads(sl.read_text(encoding="utf-8")) if sl.exists() else {}
    lessons = db.learnings()
    rows = []
    for d in domains:
        r = run.public(run.process(d, str(ROOT / "data/contacts.csv"), False, seeds.get(d), lessons))
        db.save_company(r)
        rows.append(r)
    drafted = count(rows, "drafted")
    db.add_run(len(rows), drafted)
    return 200, {"note": f"Done: {len(rows)} companies, {drafted} drafted."}


def do_fb(body):
    kind, domain, data = body.get("kind"), str(body.get("domain", ""))[:100], body.get("data")
    if kind not in ("edit", "rating") or not isinstance(data, dict):
        return 400, {"error": "Bad feedback."}
    if kind == "edit" and not all(isinstance(data.get(k), dict) for k in ("before", "after")):
        return 400, {"error": "Bad feedback."}
    if kind == "rating" and data.get("value") not in (1, -1):
        return 400, {"error": "Bad feedback."}
    db.add_event(domain, kind, data)
    return 200, {"note": "Saved."}


def do_learn(body):
    rules = drafts_mod.learn(db.events(50))
    if not rules:
        return 200, {"note": "Nothing to learn yet. Edit or rate a few drafts first."}
    db.set_learnings(rules)
    return 200, {"note": f"Learned {len(rules)} rules."}


ROUTES = {"run": do_run, "fb": do_fb, "learn": do_learn}


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        try:
            self._send(200, page(self.path))
        except Exception as ex:  # show the failure instead of a blank 500
            self._send(500, f"error: {ex}")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 60_000)) or b"{}")
            route = ROUTES.get(parse_qs(urlparse(self.path).query).get("p", [""])[0])
            if not route:
                code, out = 404, {"error": "Unknown action."}
            elif not APP_PASSWORD or not hmac.compare_digest(str(body.get("token", "")), APP_PASSWORD):
                code, out = 401, {"error": "Wrong or missing access password."}
            elif not db.enabled:
                code, out = 503, {"error": "No database is connected."}
            elif route is not do_fb and not config.API_KEY:
                code, out = 503, {"error": "GEMINI_API_KEY is not set."}
            else:
                code, out = route(body)
        except Exception as ex:
            code, out = 500, {"error": str(ex)[:200]}
        self._send(code, json.dumps(out), "application/json")
