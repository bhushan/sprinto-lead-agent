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
from datetime import datetime, timedelta, timezone
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
import send as mail_out  # noqa: E402
import settings  # noqa: E402
import shortlist  # noqa: E402

config.OFFLINE = not config.API_KEY  # live whenever a Gemini key is configured
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
DOMAIN = re.compile(r"^(?=.{4,100}$)([a-z0-9-]+\.)+[a-z]{2,}$")  # rejects IPs and localhost
MAX_PER_RUN = 5  # ~30 s per company; stays inside the 300 s function limit
MAX_SENDS_PER_DAY = 10  # sending is locked to your own address; this stops a loop from filling your inbox
MAX_RUNS_PER_HOUR = 3  # protects Gemini and Hunter credits when APP_PASSWORD is not set

STATUS = {  # label, css class
    "drafted": ("Email drafted", "ok"), "not_qualified": ("Not qualified", "mute"),
    "disqualified": ("Disqualified", "mute"), "manual_review": ("Needs a human", "warn"),
    "failed": ("Needs a human", "warn"), "no_contact": ("Needs a human", "warn"), "no_hook": ("Needs a human", "warn"),
}
TODO = {  # what the human should do for each hand-over
    "manual_review": "Open the site and decide if it is a B2B SaaS company. If yes, add it to your list and re-run.",
    "failed": "The homepage could not be read. Check the site by hand, or try the domain again later.",
    "no_contact": "Hunter found nobody who owns security or engineering. Look the right person up in Apollo and add them on the My contacts page.",
    "no_hook": "Nothing specific to open with. Write the first line yourself.",
}
STEPS = [("Shortlist", "Funded SaaS from Google News", 0), ("Scrape", "One cached, robots-aware fetch", 0),
         ("Extract", "Named fields, quotes verified", 0), ("Qualify", "Evidence Map loop, 4 fetches max", 0),
         ("Find contact", "Hunter.io, ranked by role", 0), ("Pain points", "Two, each with a quote", 0),
         ("Draft", "By name, with a specific hook", 0), ("Review", "A human approves", 1), ("Send", "To your inbox only", 1)]

CSS = """
:root{--soft:#fafcff;--soft2:#e8eff8;--hover:#f6faff;--okbg:#ecfdf5;--navy:#0b2545;--navy2:#13315c;--blue:#2563eb;--green:#10b981;--green-d:#047857;--bg:#f3f7fb;--panel:#fff;--line:#dfe7f1;--text:#0f1d33;--mute:#62748e}
[data-theme=dark]{--bg:#0a1220;--panel:#111b2e;--line:#22314b;--text:#e7eefa;--mute:#8fa3c2;--soft:#0f182a;--soft2:#1b2a44;--hover:#15233a;--okbg:#0d2a22;--navy:#dbe7fb;--green-d:#34d399}
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
.step{border:1px solid var(--line);border-radius:14px;padding:12px;background:var(--soft)}.step small{display:flex;align-items:center;gap:6px;color:var(--mute);font-size:11px;font-weight:600}
.step small i{width:9px;height:9px;border-radius:50%;background:var(--blue)}.step.h small i{background:var(--green)}.step p{margin:3px 0 0;color:var(--mute);font-size:13px}
.legend{float:right;font-size:12px;color:var(--mute)}.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 12px}
.fun{display:grid;gap:10px;margin-top:10px}.fun div{display:grid;grid-template-columns:150px 1fr 36px;gap:12px;align-items:center;font-size:14px}
.bar{height:10px;border-radius:99px;background:var(--soft2);overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--green))}
table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--mute);font-weight:600;font-size:11px;letter-spacing:.08em;text-transform:uppercase;padding:8px 10px}
td{padding:12px 10px;border-top:1px solid var(--line);vertical-align:top}tr.l{cursor:pointer}tr.l:hover td{background:var(--hover)}td small{color:var(--mute)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;border:1px solid var(--line);white-space:nowrap;background:#fff}
.pill.ok{color:var(--green-d);border-color:#a7e8cf;background:var(--okbg)}.pill.warn{color:#a15c07;border-color:#f6d49a;background:#fffaf0}.pill.mute{color:var(--mute)}
.chips a{display:inline-block;margin:0 6px 8px 0;padding:4px 12px;border:1px solid var(--line);border-radius:99px;font-size:13px;color:var(--mute);background:#fff}.chips a.on{background:var(--navy);color:#fff;border-color:var(--navy)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.q{border-left:3px solid var(--blue);padding:2px 0 2px 10px;margin:8px 0;color:var(--mute);font-size:13px}.q b{color:var(--text)}
pre{white-space:pre-wrap;font:14px/1.6 ui-monospace,Consolas,monospace;margin:0}.back{color:var(--mute);font-size:14px}
input.t,textarea.t{width:100%;border:1px solid var(--line);border-radius:10px;padding:9px 12px;font:inherit;background:#fff;margin:4px 0 8px}textarea.t{min-height:190px;font:14px/1.6 ui-monospace,Consolas,monospace}
textarea.dom{min-height:110px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.ok-t{color:var(--green-d);font-weight:600;font-size:13px}
.box>label{display:block;font-size:13px;color:var(--mute);margin-top:8px}
.chip{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--line);border-radius:99px;padding:6px 12px;font-size:13px;background:var(--panel);color:var(--mute);white-space:nowrap}
.chip i{width:8px;height:8px;border-radius:50%;background:var(--green)}.kbd{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--mute)}
.statgrid{display:grid;grid-template-columns:repeat(3,1fr);background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden}
.statgrid>div{padding:18px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.statgrid>div:nth-child(3n){border-right:0}.statgrid>div:nth-last-child(-n+3){border-bottom:0}
.statgrid small{color:var(--mute);font-size:11px;letter-spacing:.1em;text-transform:uppercase}.statgrid b{display:block;font-size:34px;letter-spacing:-.03em;color:var(--navy)}.statgrid span{color:var(--mute);font-size:13px}
.statgrid b em{font-style:normal;font-size:16px;color:var(--mute);font-weight:500}
.nodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:8px;margin-top:14px;text-align:center}
.node{border:1px solid var(--line);border-radius:14px;padding:12px 6px;background:var(--soft)}.node .c{width:34px;height:34px;border-radius:50%;margin:0 auto 6px;display:grid;place-items:center;background:var(--blue);color:#fff;font-weight:700;font-size:13px}
.node.h .c{background:var(--green);color:#04281f}.node b{display:block;font-size:13px}.node small{display:block;color:var(--mute);font-size:11px}.node em{display:block;font-style:normal;font-size:22px;font-weight:700;color:var(--navy)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:12px;overflow:hidden}.seg button{border:0;background:var(--panel);padding:8px 18px;font:inherit;cursor:pointer;color:var(--text)}.seg button.on{background:var(--blue);color:#fff}
.num{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--soft2);color:var(--blue);font-size:12px;font-weight:700;margin-right:10px}
.runsel{padding:8px 12px;border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--text);font:inherit}
.drop{display:grid;grid-template-columns:1fr 40px;gap:10px;align-items:center;margin:8px 0;font-size:14px}
.warnbox{background:#fffaf0;border:1px solid #f6d49a;color:#7a4a05;border-radius:12px;padding:12px 14px;margin:14px 0;font-size:14px}
@media(max-width:860px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.grid2{grid-template-columns:1fr}.fun div{grid-template-columns:100px 1fr 30px}main{padding:18px}.top{padding:12px 18px}}
"""

JS = """
try{document.documentElement.dataset.theme=localStorage.getItem('theme')||''}catch(e){}
const S=(k,v)=>{try{v===undefined?localStorage.removeItem(k):localStorage.setItem(k,v)}catch(e){}};
const G=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
const tok=()=>{if(!NEEDPW)return '';let t=G('token');if(!t){t=prompt('Access password');if(t)S('token',t)}return t};
const post=(p,data)=>fetch('/?p='+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,token:tok()})})
  .then(r=>r.json().then(j=>{if(r.status===401)S('token');if(!r.ok)throw new Error(j.error||'failed');return j}));
document.querySelectorAll('.draft').forEach(d=>{
  const id=d.dataset.d,sub=d.querySelector('.sub-in'),body=d.querySelector('.body-in'),msg=d.querySelector('.msg');
  const saved=G('draft:'+id);if(saved){try{const o=JSON.parse(saved);sub.value=o.s;body.value=o.b}catch(e){}}
  if(G('sent:'+id)){msg.textContent='Approved earlier'}
  const save=()=>S('draft:'+id,JSON.stringify({s:sub.value,b:body.value}));sub.oninput=body.oninput=()=>{save();msg.textContent='Edit saved in this browser'};
  d.querySelector('.send').onclick=()=>{save();const edit={domain:id,kind:'edit',data:{before:{subject:sub.defaultValue,body:body.defaultValue},after:{subject:sub.value,body:body.value}}};
    if(!CANSEND){S('sent:'+id,'1');msg.textContent='Approved and opened in your mail app';post('fb',edit).catch(()=>{});
      location.href='mailto:?subject='+encodeURIComponent(sub.value)+'&body='+encodeURIComponent(body.value);return}
    const b=d.querySelector('.send');b.disabled=true;msg.textContent='Sending...';
    post('send',{domain:id,subject:sub.value,body:body.value}).then(j=>{S('sent:'+id,'1');msg.textContent=j.note;post('fb',edit).catch(()=>{})})
      .catch(x=>{msg.textContent=x.message}).finally(()=>{b.disabled=false})};
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
document.querySelectorAll('.act').forEach(b=>{b.onclick=()=>{const c=b.closest('.box'),m=c.querySelector('.amsg'),d=b.dataset.d,data={domains:[d]};
  const n=c.querySelector('.cn'),t=c.querySelector('.ct');if(n)data.contact={name:n.value,title:t.value};
  b.disabled=true;m.textContent='Running, up to a minute...';
  post('run',data).then(j=>{m.textContent=j.note;setTimeout(()=>location.reload(),700)}).catch(x=>{m.textContent=x.message;b.disabled=false})}});
document.addEventListener('keydown',ev=>{if((ev.ctrlKey||ev.metaKey)&&ev.key==='k'){ev.preventDefault();const q=document.querySelector('#gq');if(q)q.focus()}});
const th=document.querySelector('#theme');if(th)th.onclick=()=>{const d=document.documentElement.dataset.theme==='dark'?'':'dark';document.documentElement.dataset.theme=d;S('theme',d)};
let target=3;document.querySelectorAll('.seg button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.seg button').forEach(x=>x.classList.remove('on'));b.classList.add('on');target=+b.dataset.n});
go('#auto','auto',()=>({target}),'Finding companies, researching them and stopping when enough qualify. This takes up to 4 minutes, keep this tab open.');
const chain=(sel,steps,wait)=>{const b=document.querySelector(sel);if(!b)return;b.onclick=async()=>{const m=document.querySelector('#status');b.disabled=true;m.textContent=wait;
  try{let j;for(const [p,d] of steps()){j=await post(p,d)}m.textContent=j.note||'Done';setTimeout(()=>location.reload(),700)}catch(x){m.textContent=x.message;b.disabled=false}}};
const pf=()=>({keywords:document.querySelector('#p_kw').value,regions:['IN','US','GB','SG'].filter(c=>document.querySelector('#p_r_'+c).checked)});
chain('#psearch',()=>[['prospects',pf()],['shortlist',{}]],'Saving, then searching Google News and checking homepages. About a minute, keep this tab open.');
chain('#psave',()=>[['prospects',pf()]],'Saving...');
chain('#padd',()=>[['adddomains',{domains:document.querySelector('#p_dom').value.split(/[\\s,]+/).filter(Boolean)}]],'Adding...');
chain('#pplAdd',()=>[['addpeople',{lines:document.querySelector('#pl_lines').value}]],'Adding...');
document.querySelectorAll('.rmp').forEach(b=>b.onclick=()=>{b.disabled=true;post('removeperson',{domain:b.dataset.d,name:b.dataset.n}).then(()=>location.reload()).catch(x=>{alert(x.message);b.disabled=false})});
go('#find','shortlist',()=>({}),'Searching Google News and checking homepages, about a minute...');
go('#go','run',()=>({domains:document.querySelector('#doms').value.split(/[\\s,]+/).filter(Boolean)}),'Running, this takes up to 3 minutes. Keep this tab open.');
go('#short','run',()=>({shortlist:true}),'Running the next companies from the shortlist, up to 3 minutes. Keep this tab open.');
const form=()=>{const v=id=>document.querySelector('#'+id).value,csv=id=>v(id);
  return {threshold:v('s_threshold'),stage_penalty:v('s_penalty'),stages:['A','B','C'].filter(x=>document.querySelector('#s_stage_'+x).checked),
   exclude:csv('s_exclude'),limits:{certified:v('s_l_certified'),saas:v('s_l_saas'),stage:v('s_l_stage')},
   roles:{security:csv('s_r_security'),engineering:csv('s_r_engineering'),founder:csv('s_r_founder')},sender:v('s_sender'),target:v('s_target')}};
go('#savecfg','settings',()=>({settings:form()}),'Saving...');
go('#resetcfg','settings',()=>({reset:true}),'Resetting to defaults...');
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
        return {"rows": rows, "generated": db.last_update(), "hunter_left": hunter_left(snap["hunter_left"]), "db": True,
                "runs": db.runs(20), "unseen": sum(d not in {r["domain"] for r in rows} for d in db.shortlist())}
    except Exception as ex:  # a broken database must not take the read-only pages down
        return {**snap, "db_error": str(ex)[:160]}


def live_missing():
    """What is still needed before the browser can run the pipeline."""
    return [name for name, ok in (("DATABASE_URL (the Neon connection string)", db.enabled),
                                  ("GEMINI_API_KEY", bool(config.API_KEY))) if not ok]


def count(rows, *st):
    return sum(r["status"] in st for r in rows)


def ago(iso):
    try:
        mins = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() // 60)
    except (TypeError, ValueError):
        return ""
    return f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago" if mins < 2880 else f"{mins // 1440}d ago"


def shell(tab, body, snap, q=""):
    rows = snap["rows"]
    human, drafts = sum(r["needs_human"] for r in rows), count(rows, "drafted")

    def link(k, label, badge=None):
        b = f'<span class=badge>{badge}</span>' if badge else ""
        return f'<a href="/?p={k}" class="{"on" if tab == k else ""}">{label}{b}</a>'

    runs = snap.get("runs") or []
    chip = (f'<span class=chip><i></i>Run {len(runs)} <span>{runs[0]["drafted"]} drafted · {ago(runs[0]["ts"])}</span></span>' if runs else
            f'<span class=chip><i></i>{("Last run " + e(snap["generated"][:10])) if snap["generated"] else "No run yet"}</span>')
    return ('<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
            f'<title>Outreach Agent</title><style>{CSS}</style><script>const NEEDPW={"true" if APP_PASSWORD else "false"},CANSEND={"true" if mail_out.can_send() else "false"};</script><div class=app><aside>'
            '<div class=brand><i></i><div>OUTREACH<small>AGENT</small></div></div>'
            f'<h4>PIPELINE</h4>{link("overview", "Overview")}{link("run", "Run pipeline")}{link("companies", "Companies")}{link("prospects", "Prospects")}{link("data", "My contacts")}{link("method", "Method")}{link("settings", "ICP settings")}'
            f'<h4>HUMAN GATES</h4>{link("review", "Review queue", human)}{link("drafts", "Drafts &amp; sends", drafts)}'
            f'<h4>LEARNING</h4>{link("feedback", "Feedback loop")}'
            '<div class=note>The model reads and writes. Python rules decide. Every draft waits for you.<br><br>Case study build, not affiliated with Sprinto.</div></aside>'
            '<div><div class=top><form action="/"><input type=hidden name=p value=companies><input id=gq name=q placeholder="Search companies   Ctrl K" value="'
            f'{e(q)}"></form>{chip}<button class=btn id=theme title="Light or dark" style="margin-left:auto">Theme</button>'
            '<a class="btn p" href="/?p=run">Run pipeline</a></div>'
            f'<main>{body}</main></div></div><script>{JS}</script>')


def overview(snap, run_id="all"):
    all_rows, runs = snap["rows"], snap.get("runs") or []
    rows = [r for r in all_rows if run_id == "all" or r.get("run", "earlier") == run_id]
    n = len(rows)
    fit = count(rows, "drafted", "no_contact", "no_hook")
    human = sum(r["needs_human"] for r in rows)
    hl, target = snap["hunter_left"], current_settings()["target"]
    opts = '<option value="all">All companies</option>' + "".join(
        f'<option value="{e(x["ts"])}" {"selected" if run_id == x["ts"] else ""}>Run {len(runs) - i} · {e(x["ts"][:10])}</option>' for i, x in enumerate(runs)) \
        + f'<option value="earlier" {"selected" if run_id == "earlier" else ""}>Earlier (imported)</option>'
    stats = [("Pool", len(all_rows), f'{snap.get("unseen", 0)} still unseen'), ("Researched", n - count(rows, "failed"), "pages read"),
             ("Qualified", f'{fit}' + (f' <em>/ {target}</em>' if run_id != "all" else ""), "fit score at or above the threshold"), ("Flagged", human, "waiting on a human"),
             ("Dropped", count(rows, "disqualified", "not_qualified"), "a gate failed"),
             ("Hunter left", "-" if hl is None else hl, "searches this month")]
    stat_html = "".join(f"<div><small>{a}</small><b>{b}</b><span>{c}</span></div>" for a, b, c in stats)
    sent = sum(x["kind"] == "sent" for x in db.events(500)) if db.enabled and not snap.get("db_error") else 0
    counts = [n, n - count(rows, "failed"), n - count(rows, "failed"), n - count(rows, "failed", "disqualified", "manual_review"),
              count(rows, "drafted", "no_hook"), count(rows, "drafted"), count(rows, "drafted"), count(rows, "drafted"), sent]
    nodes = "".join(f'<div class="node {"h" if h else ""}"><div class=c>{i}</div><b>{t}</b><small>{d}</small><em>{c}</em></div>'
                    for i, ((t, d, h), c) in enumerate(zip(STEPS, counts), 1))
    dropped = {}
    for r in rows:
        if r["status"] in ("disqualified", "not_qualified"):
            why = re.sub(r"runs on \w+", "runs on a competitor", r["reason"]) if r["status"] == "disqualified" else "fit score below the threshold"
            dropped[why] = dropped.get(why, 0) + 1
    drop_html = "".join(f'<div class=drop><span>{e(w)}<span class=bar style="display:block;margin-top:4px"><i style="width:{c * 100 // max(dropped.values())}%"></i></span></span><b>{c}</b></div>'
                        for w, c in sorted(dropped.items(), key=lambda x: -x[1])) or "<small>Nothing dropped yet.</small>"
    here = "".join(f'<tr class=l onclick="location=\'/?p=company&d={quote(r["domain"])}\'"><td><b>{e(r["domain"])}</b></td>'
                   f'<td><span class="pill {STATUS[r["status"]][1]}">{STATUS[r["status"]][0]}</span></td><td>{"-" if r.get("score") is None else r["score"]}</td></tr>'
                   for r in sorted(rows, key=lambda r: (r["status"] != "drafted", -(r.get("score") or 0)))[:10])
    return (f'<div class=row><div><div class=kicker>Pipeline</div><h1>Pipeline overview</h1></div>'
            f'<select class=runsel style="margin-left:auto" onchange="location=\'/?p=overview&run=\'+encodeURIComponent(this.value)">{opts}</select></div>'
            '<p class=sub>Funded SaaS companies, shortlisted by rules, researched in batches, qualified in Python and drafted automatically when nothing needs a human.</p>'
            f'<div class=statgrid>{stat_html}</div>'
            f'<div class=box><span class=legend><i style="background:var(--blue)"></i>automatic<i style="background:var(--green)"></i>human gate</span>'
            f'<h3>The case pipeline</h3><h2>From funding news to an email in your inbox.</h2><div class=nodes>{nodes}</div></div>'
            f'<div class=grid2><div class=box><h3>Why companies dropped</h3>{drop_html}</div>'
            f'<div class=box><h3>Companies in this view</h3><table>{here}</table><p><a class=back href="/?p=companies">All companies</a></p></div></div>')


def companies(snap, flt, q):
    rows = snap["rows"]
    groups = {"all": None, "qualified": ("drafted", "no_contact", "no_hook"), "drafted": ("drafted",),
              "flagged": ("manual_review", "failed", "no_contact", "no_hook"), "dropped": ("disqualified", "not_qualified")}
    chips = "".join(f'<a class="{"on" if flt == k else ""}" href="/?p=companies&f={k}&q={quote(q)}">{k.title()}'
                    f' ({len(rows) if v is None else count(rows, *v)})</a>' for k, v in groups.items())
    keep = [r for r in rows if (groups.get(flt) is None or r["status"] in groups[flt])
            and (not q or q.lower() in (r["domain"] + r["reason"] + str(r.get("facts", {}).get("company_name", ""))).lower())]
    keep.sort(key=lambda r: (r["status"] != "drafted", -(r.get("score") or 0)))
    body = "".join(
        f'<tr class=l onclick="location=\'/?p=company&d={quote(r["domain"])}\'"><td><a href="/?p=company&d={quote(r["domain"])}"><b>{e(str(r.get("facts", {}).get("company_name") or r["domain"].split(".")[0].title()))}</b></a>'
        f'<br><small>{e(r["domain"])}</small></td><td>{e(str(r.get("facts", {}).get("funding_stage") or "-"))}</td>'
        f'<td><span class="pill {STATUS[r["status"]][1]}">{STATUS[r["status"]][0]}</span></td>'
        f'<td>{"-" if r.get("score") is None else r["score"]}</td><td><small>{e(r["reason"][:120])}</small></td></tr>'
        for r in keep) or '<tr><td colspan=5><small>Nothing matches.</small></td></tr>'
    return ('<div class=kicker>Pipeline</div><h1>Companies</h1><p class=sub>Every company the pipeline has researched. Open one to see each decision with its evidence.</p>'
            f'<form action="/" class=row style="margin-bottom:10px"><input type=hidden name=p value=companies><input type=hidden name=f value="{e(flt)}">'
            f'<input class=t name=q placeholder="Search name or domain" value="{e(q)}" style="max-width:320px;margin:0"><div class=chips style="margin:0">{chips}</div></form>'
            f'<div class=box style="margin-top:0"><table><tr><th>Company</th><th>Stage</th><th>Outcome</th><th>Fit</th><th>Reason</th></tr>{body}</table></div>')


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
    off = "disabled" if live_missing() else ""

    def action(r):
        d = e(r["domain"])
        if r["status"] == "no_contact":
            return (f'<div class=row style="margin-top:8px"><input class="t cn" style="max-width:200px;margin:0" placeholder="Contact name">'
                    f'<input class="t ct" style="max-width:220px;margin:0" placeholder="Title, e.g. CTO">'
                    f'<button class="btn g act" data-d="{d}" {off}>Add contact and draft</button><span class="ok-t amsg"></span></div>')
        return f'<div class=row style="margin-top:8px"><button class="btn act" data-d="{d}" {off}>Re-run this company</button><span class="ok-t amsg"></span></div>'

    cards = "".join(
        f'<div class=box><div class=row><b>{e(r["domain"])}</b><span class="pill warn">{e(r["status"].replace("_", " "))}</span>'
        f'<a class=btn style="margin-left:auto" href="/?p=company&d={quote(r["domain"])}">Open</a>'
        f'<button class="btn resolve" data-d="{e(r["domain"])}">Mark resolved</button></div>'
        f'<p style="margin:8px 0 4px">{e(r["reason"])}</p><div class=q><b>Do this:</b> {e(TODO.get(r["status"]) or "; ".join(r.get("flags", [])) or "Check this company.")}</div>{action(r)}</div>' for r in rows)
    return ('<div class=kicker>Human gate</div><h1>Review queue</h1><p class=sub>Companies the rules could not settle. '
            'Nothing here is guessed: each one waits for a person.</p>' + (cards or '<div class=box>Nothing is waiting on you.</div>'))


def drafts(snap):
    rows = [r for r in snap["rows"] if r.get("body")]
    warn = lambda r: "".join(f'<div class=warnbox style="margin:8px 0">{e(x)}</div>' for x in r.get("flags", []) if x.startswith("Check before sending"))
    cards = "".join(
        f'<div class="box draft" data-d="{e(r["domain"])}" data-hook="{e(r["hook"][:200])}"><div class=row><b>{e(r["contact"]["name"])}</b><small>{e(r["contact"]["title"])} at {e(r["domain"])}</small>'
        f'<a class=btn style="margin-left:auto" href="/?p=company&d={quote(r["domain"])}">Why this email</a></div>'
        f'<small>Hook: {e(r["hook"])}</small>{warn(r)}'
        f'<input class="t sub-in" value="{e(r["subject"])}"><textarea class="t body-in">{e(r["body"])}</textarea>'
        '<div class=row><button class="btn g send">Approve &amp; send</button><button class="btn copy">Copy</button>'
        '<button class="btn up" title="Good draft">Good</button><button class="btn down" title="Poor draft">Poor</button><span class="ok-t msg"></span></div></div>'
        for r in rows)
    to = config.TO_ADDRESS
    how = (f'"Approve &amp; send" emails the draft to your own inbox ({e(to[:1])}***@{e(to.split("@")[-1])}), never to the prospect, up to {MAX_SENDS_PER_DAY} a day. '
           if mail_out.can_send() else '"Approve &amp; send" opens your own mail app with the text filled in; nothing is sent from this site. ')
    return ('<div class=kicker>Human gate</div><h1>Drafts &amp; sends</h1><p class=sub>Edit a draft, then approve it. ' + how +
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
        if x["kind"] == "sent":
            return "sent to your inbox"
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
            f'<small>Worst case: {sum(q["limit"] for q in qualify.QUESTIONS)} page fetches beyond the homepage (change in ICP settings). A quote that is not on the page is dropped and counted.</small></div>'
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
    steps = [("Read funding news and shortlist Series A-C SaaS with rules, or take your domains", ""), ("Crawl the homepage, trust and careers pages", ""),
             ("Extract facts with verbatim quotes; Python checks every quote", ""), ("Qualify with rules; an agent loop fetches more pages for unknowns", ""),
             ("Find the decision-maker with Hunter and title rules", ""), ("Write two pain points and a personalised email; code validates it", ""),
             ("Drafts wait in Drafts and sends for your approval", "")]
    what = "".join(f'<div class=q style="border:0;padding:0;display:flex;margin:10px 0"><span class=num>{i}</span><span>{e(t)}</span></div>' for i, (t, _) in enumerate(steps, 1))
    if miss:
        live = ('<div class=warnbox><b>Live runs are switched off.</b> Still needed: ' + e(", ".join(miss)) + '. '
                'Add them in the Vercel project settings and redeploy.</div>')
        left = ""
    else:
        try:
            hist = "".join(f"<tr><td><small>{e(x['ts'][:16].replace('T', ' '))}</small></td><td>{x['n']} companies</td><td>{x['drafted']} drafted</td></tr>" for x in db.runs()) \
                or "<tr><td colspan=3><small>No runs yet.</small></td></tr>"
        except Exception as ex:
            hist = f"<tr><td colspan=3><small>Database error: {e(str(ex)[:120])}</small></td></tr>"
        t = current_settings()["target"]
        seg = "".join(f'<button class="{"on" if n == t and n <= 3 else ""}" data-n="{n}">{n}</button>' for n in (1, 2, 3))
        live = ""
        left = (f'<div class=box style="margin-top:0"><h2>Find companies for me</h2><p class=sub style="margin-bottom:12px">No input needed. It pulls fresh funding news, shortlists companies '
                'that fit your ICP, and takes each one through research, contact and email until enough qualify. It also runs by itself every Monday.</p>'
                f'<small class=kicker>Stop after this many qualify</small><div style="margin:6px 0 10px"><span class=seg>{seg}</span></div>'
                f'<p><button class="btn p" id=auto>Find companies for me</button> <span class=ok-t id=status></span></p>'
                f'<small>Each qualified company uses one Hunter search. Dropped companies cost nothing in Hunter. Up to {MAX_PER_RUN} companies are looked at per click.</small></div>'
                '<div class=box><h3>Or run on specific domains</h3><textarea class="t dom" id=doms placeholder="One domain per line, e.g. example.com"></textarea>'
                f'<div class=row><button class="btn g" id=go>Run these domains</button><button class=btn id=find>Refresh the shortlist</button>'
                f'<button class=btn id=short>Run the next from the shortlist ({snap.get("unseen", 0)} unseen)</button></div></div>'
                f'<div class=box><h3>Recent runs</h3><table>{hist}</table></div>')
    return ('<div class=kicker>Pipeline</div><h1>Run the pipeline</h1>'
            '<p class=sub>It finds recently funded companies on its own, researches and qualifies them, finds the right person and drafts a personalised email. '
            'Only what the rules cannot settle comes back to you. Nothing is emailed from here.</p>'
            f'{live}<div class=grid2 style="grid-template-columns:3fr 2fr;align-items:start"><div>{left}</div>'
            f'<div class=box style="margin-top:0"><h3>What happens to each company</h3>{what}</div></div>'
            f'<p><a class="btn" href="/?p=run&demo=1">Run the offline demo</a></p>{out}')


def current_settings():
    """Stored settings (or defaults), applied to the pipeline modules for this request."""
    stored = None
    if db.enabled:
        try:
            stored = db.get_settings()
        except Exception:
            pass
    s = settings.effective(stored)
    settings.apply(s)
    return s


def settings_page(snap):  # noqa: C901
    s = current_settings()
    editable = db.enabled and not snap.get("db_error")
    dis = "" if editable else "disabled"
    num = lambda i, v, lo, hi: f'<input class=t id={i} type=number min={lo} max={hi} value="{v}" style="max-width:120px" {dis}>'
    txt = lambda i, v, rows=2: f'<textarea class="t" id={i} style="min-height:{rows * 30}px;font:14px/1.5 inherit" {dis}>{e(", ".join(v))}</textarea>'
    stages = "".join(f'<label style="margin-right:14px"><input type=checkbox id=s_stage_{x} {"checked" if x in s["stages"] else ""} {dis}> Series {x}</label>' for x in "ABC")
    lim = s["limits"]
    note = "" if editable else '<div class=warnbox>Settings can only be changed once a database is connected. Showing the defaults.</div>'
    return ('<div class=kicker>Configure</div><h1>ICP settings</h1>'
            '<p class=sub>Who counts as a good fit, how hard the pipeline looks, and who it writes to. Changes apply to the next run and to Find new companies.</p>'
            f'{note}<div class=grid2><div class=box style="margin-top:0"><h3>Qualifying</h3>'
            f'<label>Score needed to draft an email (0 to 100)</label>{num("s_threshold", s["threshold"], 0, 100)}'
            f'<label>Points taken off when the funding stage is unknown</label>{num("s_penalty", s["stage_penalty"], 0, 30)}'
            f'<label>Funding stages in the ICP</label><div style="margin:6px 0 4px">{stages}</div>'
            f'<label>How many qualified companies a run aims for (1 to 5)</label>{num("s_target", s["target"], 1, 5)}</div>'
            f'<div class=box style="margin-top:0"><h3>How hard it looks (page fetches per question)</h3>'
            f'<label>Already certified? (trust and security pages, max 2)</label>{num("s_l_certified", lim["certified"], 0, 2)}'
            f'<label>B2B SaaS? (product and about pages, max 2)</label>{num("s_l_saas", lim["saas"], 0, 2)}'
            f'<label>Right stage? (careers, pricing, customers, max 3)</label>{num("s_l_stage", lim["stage"], 0, 3)}</div></div>'
            f'<div class=box><h3>Sectors to leave out of Find new companies</h3>{txt("s_exclude", s["exclude"], 3)}'
            '<small>Comma separated. A headline containing any of these is skipped.</small></div>'
            f'<div class=box><h3>Who to write to, best match first</h3>'
            f'<label>1. Security and compliance owners</label>{txt("s_r_security", s["roles"]["security"])}'
            f'<label>2. Engineering leaders</label>{txt("s_r_engineering", s["roles"]["engineering"])}'
            f'<label>3. Founders (used first at companies under 50 people)</label>{txt("s_r_founder", s["roles"]["founder"])}'
            '<small>Job-title words separated by commas. "vp engineering" also matches "VP of Engineering". Titles that match none of these are never chosen.</small></div>'
            f'<div class=box><h3>Email sender name</h3><input class=t id=s_sender value="{e(s["sender"])}" style="max-width:260px" {dis}></div>'
            f'<p><button class="btn g" id=savecfg {dis}>Save settings</button> <button class=btn id=resetcfg {dis}>Reset to defaults</button> <span class=ok-t id=status></span></p>')


def prospects_page(snap):
    s = current_settings()
    editable = db.enabled and not snap.get("db_error")
    dis = "" if editable else "disabled"
    have = {r["domain"] for r in snap["rows"]}
    try:
        sl = db.shortlist() if db.enabled else {}
    except Exception:
        sl = {}
    regions = "".join(f'<label style="margin-right:14px"><input type=checkbox id=p_r_{c} {"checked" if c in s["regions"] else ""} {dis}> {n}</label>'
                      for c, n in (("IN", "India"), ("US", "United States"), ("GB", "United Kingdom"), ("SG", "Singapore")))
    rows = "".join(
        f'<tr><td><b>{e(d)}</b></td><td>{e(v.get("funding_stage") or "added by you")}</td><td><small>{e((v.get("funding_source") or "")[:90])}</small></td>'
        f'<td><span class="pill {"mute" if d in have else "ok"}">{"Researched" if d in have else "Waiting"}</span></td></tr>' for d, v in list(sl.items())[-60:]) \
        or "<tr><td colspan=4><small>Nothing here yet. Search, or add your own domains.</small></td></tr>"
    note = "" if editable else '<div class=warnbox>A database is needed to save prospects. Showing a read-only view.</div>'
    return ('<div class=kicker>Pipeline</div><h1>Prospects</h1>'
            '<p class=sub>Tell it where to look. It searches funding news for companies matching your keywords, checks each one has a real website, and queues them. '
            'Run the queue from the Run page, or let the Monday run pick them up.</p>' + note +
            f'<div class=grid2><div class=box style="margin-top:0"><h3>Search for prospects</h3>'
            f'<label>Industries or topics to look for (comma separated, up to 5)</label>'
            f'<textarea class=t id=p_kw style="min-height:70px" placeholder="e.g. cybersecurity, healthtech, HR tech, payments" {dis}>{e(", ".join(s["keywords"]))}</textarea>'
            f'<small>Leave empty to use the built-in SaaS search. Funding stages come from ICP settings ({e(", ".join("Series " + x for x in s["stages"]))}).</small>'
            f'<label>Regions</label><div style="margin:6px 0 10px">{regions}</div>'
            f'<div class=row><button class="btn g" id=psearch {dis}>Save and search now</button><button class=btn id=psave {dis}>Save only</button></div></div>'
            f'<div class=box style="margin-top:0"><h3>Add your own companies</h3>'
            f'<label>Websites you already have in mind, one per line</label>'
            f'<textarea class=t id=p_dom style="min-height:120px" placeholder="example.com" {dis}></textarea>'
            f'<div class=row><button class="btn g" id=padd {dis}>Add to queue</button></div></div></div>'
            f'<p><span class=ok-t id=status></span></p>'
            f'<div class=box><h3>Queue ({len(sl)})</h3><table><tr><th>Company</th><th>Stage</th><th>Found in</th><th>Status</th></tr>{rows}</table>'
            '<p><a class="btn p" href="/?p=run">Go to Run pipeline</a></p></div>')


def data_page(snap):
    editable = db.enabled and not snap.get("db_error")
    dis = "" if editable else "disabled"
    try:
        people = db.all_people() if db.enabled else []
    except Exception:
        people = []
    rows = "".join(
        f'<tr><td><b>{e(p["name"])}</b></td><td>{e(p["title"])}</td><td>{e(p["domain"])}</td><td>{"saved" if p["has_email"] else "-"}</td>'
        f'<td><button class="btn rmp" data-d="{e(p["domain"])}" data-n="{e(p["name"])}" {dis}>Remove</button></td></tr>' for p in people) \
        or "<tr><td colspan=5><small>No contacts yet.</small></td></tr>"
    note = "" if editable else '<div class=warnbox>A database is needed to save contacts. Showing a read-only view.</div>'
    return ('<div class=kicker>Pipeline</div><h1>My contacts</h1>'
            '<p class=sub>Add people you already know at a company. When the pipeline researches that company it writes to your contact, '
            'if their title fits, and spends no Hunter search. Companies without your contacts still go through Hunter.</p>' + note +
            f'<div class=box style="margin-top:0"><h3>Add contacts</h3><label>One per line: website, name, title, email (email is optional)</label>'
            f'<textarea class=t id=pl_lines style="min-height:120px" placeholder="example.com, Priya Nair, CTO, priya@example.com" {dis}></textarea>'
            f'<div class=row><button class="btn g" id=pplAdd {dis}>Add contacts</button><span class=ok-t id=status></span></div>'
            '<small>Emails are stored for your records but never shown on this site.</small></div>'
            f'<div class=box><h3>Your contacts ({len(people)})</h3><table><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th></th></tr>{rows}</table></div>')


def page(url):
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    tab, snap = q.get("p", "overview"), load()
    current_settings()
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
    if tab == "data":
        return shell("data", data_page(snap), snap)
    if tab == "prospects":
        return shell("prospects", prospects_page(snap), snap)
    if tab == "settings":
        return shell("settings", settings_page(snap), snap)
    if tab == "method":
        return shell("method", method(), snap)
    if tab == "run":
        return shell("run", run_page(snap, q.get("demo")), snap)
    return shell("overview", overview(snap, q.get("run", "all")), snap)


def _limited():
    """No password is set, so cap spend instead: MAX_RUNS_PER_HOUR runs an hour, for everyone."""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    if sum(x["ts"] > since for x in db.runs(20)) >= MAX_RUNS_PER_HOUR:
        return 429, {"error": f"Run limit reached ({MAX_RUNS_PER_HOUR} an hour). Try again later."}


def _seeds():
    sl = ROOT / "data/shortlist.json"
    return {**(json.loads(sl.read_text(encoding="utf-8")) if sl.exists() else {}), **db.shortlist()}


def _process(domains, seeds, given=None, stop_after=None):
    """Run the pipeline on domains as one run. stop_after: stop once that many companies qualified."""
    ts, lessons, rows = db.now(), db.learnings(), []
    for d in domains:
        r = run.process(d, str(ROOT / "data/contacts.csv"), False, seeds.get(d), lessons, given)
        r["run"] = ts
        r = run.public(r)
        db.save_company(r)
        rows.append(r)
        if stop_after and count(rows, "drafted", "no_contact", "no_hook") >= stop_after:
            break
    drafted = count(rows, "drafted")
    db.add_run(len(rows), drafted, ts)
    return 200, {"note": f"Done: {len(rows)} companies looked at, {drafted} drafted."}


def do_run(body):
    current_settings()
    if (err := _limited()):
        return err
    seeds = _seeds()
    if body.get("shortlist"):
        have = {r["domain"] for r in db.companies()}
        domains = [d for d in (db.shortlist() or seeds) if d not in have]
    else:
        domains = [str(d).strip().lower() for d in body.get("domains", [])]
    domains = list(dict.fromkeys(domains))[:MAX_PER_RUN]
    if not domains:
        return 400, {"error": "No new domains to run. Use Find new companies first."}
    bad = [d for d in domains if not DOMAIN.match(d)]
    if bad:
        return 400, {"error": "Not a domain name: " + bad[0]}
    given = None  # a reviewer-supplied contact for a single company whose contact Hunter could not find
    c = body.get("contact")
    if isinstance(c, dict) and len(domains) == 1:
        name, title = str(c.get("name", "")).strip()[:80], str(c.get("title", "")).strip()[:80]
        if not name or not title:
            return 400, {"error": "Enter both a name and a title."}
        given = {"name": name, "title": title}
    return _process(domains, seeds, given)


def do_auto(body):
    """Find companies for me: refresh the shortlist if nothing is waiting, then research until `target` qualify."""
    s = current_settings()
    if (err := _limited()):
        return err
    try:
        target = max(1, min(5, int(body.get("target") or s["target"])))
    except (TypeError, ValueError):
        target = s["target"]
    have = {r["domain"] for r in db.companies()}
    waiting = [d for d in db.shortlist() if d not in have]
    if not waiting:
        found = shortlist.find(15)
        if found:
            db.save_shortlist(shortlist.seeds(found))
        waiting = [d for d in found if d not in have]
    if not waiting:
        return 200, {"note": "No new funded companies found right now. Try again later or run specific domains."}
    return _process(waiting[:MAX_PER_RUN], _seeds(), stop_after=target)


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


def do_shortlist(body):
    current_settings()
    found = shortlist.find(15)
    if not found:
        return 200, {"note": "Google News returned no usable companies right now. Try again later."}
    db.save_shortlist(shortlist.seeds(found))
    return 200, {"note": f"Found {len(found)} companies. They are in the queue below."}


def do_settings(body):
    if body.get("reset"):
        db.set_settings(None)
        return 200, {"note": "Back to the defaults."}
    try:
        cfg = body.get("settings")
        if not isinstance(cfg, dict):
            return 400, {"error": "Bad settings."}
        stored = settings.effective(db.get_settings())  # the form does not carry the prospect search fields
        db.set_settings(settings.validate({"keywords": stored["keywords"], "regions": stored["regions"], **cfg}))
    except ValueError as ex:
        return 400, {"error": str(ex)}
    return 200, {"note": "Saved. The next run uses these settings."}


def do_prospects(body):
    try:
        db.set_settings(settings.validate({**settings.effective(db.get_settings()),
                                           "keywords": body.get("keywords", ""), "regions": body.get("regions") or []}))
    except ValueError as ex:
        return 400, {"error": str(ex)}
    return 200, {"note": "Saved. The next search and the Monday run use these."}


def _site(d):
    return str(d).strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]


EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def do_addpeople(body):
    lines = [x for x in str(body.get("lines") or "").splitlines() if x.strip()][:100]
    rows = []
    for n, line in enumerate(lines, 1):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            return 400, {"error": f"Line {n}: write website, name, title (and an optional email), separated by commas."}
        domain, name, title, email = _site(parts[0]), parts[1][:80], parts[2][:80], ",".join(parts[3:]).strip()
        if not DOMAIN.match(domain):
            return 400, {"error": f"Line {n}: not a website address ({domain[:40]})."}
        if not name or not title:
            return 400, {"error": f"Line {n}: both a name and a title are needed."}
        if email and not EMAIL.match(email):
            return 400, {"error": f"Line {n}: that email looks wrong."}
        rows.append((domain, name, title, email))
    if not rows:
        return 400, {"error": "Enter at least one contact."}
    db.add_people(rows)
    return 200, {"note": f"Saved {len(rows)} contact{'s' if len(rows) > 1 else ''}."}


def do_removeperson(body):
    db.remove_person(str(body.get("domain", ""))[:100], str(body.get("name", ""))[:100])
    return 200, {"note": "Removed."}


def do_send(body):
    if not mail_out.can_send():
        return 503, {"error": "Sending is not set up. Add TO_ADDRESS and GMAIL_APP_PASSWORD in Vercel, or use the mail app button."}
    subject = " ".join(str(body.get("subject", "")).split())[:200]
    text = str(body.get("body", "")).strip()[:5000]
    if not subject or len(text.split()) < 5:
        return 400, {"error": "The subject and body cannot be empty."}
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    if sum(x["kind"] == "sent" and x["ts"] > since for x in db.events(500)) >= MAX_SENDS_PER_DAY:
        return 429, {"error": f"Daily limit reached ({MAX_SENDS_PER_DAY} emails). Try again tomorrow."}
    try:
        mail_out.deliver(subject, text)
    except Exception as ex:  # the error text can name the account, so only the type is shown
        return 502, {"error": f"Gmail did not accept it ({type(ex).__name__}). Check the app password."}
    db.add_event(str(body.get("domain", ""))[:100], "sent", {"subject": subject})
    return 200, {"note": "Sent to your own inbox."}


def do_adddomains(body):
    raw = body.get("domains")
    if not isinstance(raw, list):
        return 400, {"error": "Enter at least one website."}
    cleaned = [_site(d) for d in raw]
    domains = list(dict.fromkeys(d for d in cleaned if d))[:50]
    if not domains:
        return 400, {"error": "Enter at least one website."}
    bad = [d for d in domains if not DOMAIN.match(d)]
    if bad:
        return 400, {"error": "Not a website address: " + bad[0][:60]}
    db.save_shortlist({d: {} for d in domains}, keep_existing=True)
    return 200, {"note": f"Added {len(domains)} to the queue. Run them from the Run page."}


ROUTES = {"send": do_send, "addpeople": do_addpeople, "removeperson": do_removeperson, "prospects": do_prospects, "adddomains": do_adddomains, "auto": do_auto, "settings": do_settings, "run": do_run, "fb": do_fb, "learn": do_learn, "shortlist": do_shortlist}


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        try:
            if parse_qs(urlparse(self.path).query).get("p") == ["cron"]:
                secret = os.getenv("CRON_SECRET", "")
                if not secret or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {secret}"):
                    return self._send(401, json.dumps({"error": "Not allowed."}), "application/json")
                if not (db.enabled and config.API_KEY):
                    return self._send(503, json.dumps({"error": "Database or Gemini key missing."}), "application/json")
                code, out = do_auto({})
                return self._send(code, json.dumps(out), "application/json")
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
            elif APP_PASSWORD and not hmac.compare_digest(str(body.get("token", "")), APP_PASSWORD):
                code, out = 401, {"error": "Wrong or missing access password."}
            elif not db.enabled:
                code, out = 503, {"error": "No database is connected."}
            elif route in (do_run, do_auto, do_learn) and not config.API_KEY:
                code, out = 503, {"error": "GEMINI_API_KEY is not set."}
            else:
                code, out = route(body)
        except Exception as ex:
            code, out = 500, {"error": str(ex)[:200]}
        self._send(code, json.dumps(out), "application/json")
