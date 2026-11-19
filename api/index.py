"""Outreach Agent web app (one Vercel Python function, no framework).

Read-only on data/snapshot.json until a database is connected (DATABASE_URL). With a database and a Gemini key it
can also run the pipeline from the browser and learn from the reviewer's edits and ratings. There is no sign-in, so
anyone with the address can review it; spend is capped instead (runs an hour, companies a run, emails a day, and
mail only ever goes to your own inbox). Runs are jobs advanced one company per request (jobs.py), so no request
nears the function time limit. The offline demo runs on the bundled fictional fixtures and never touches the network.
"""
import hmac
import json
import os
import re
import sys
import tempfile
import time
import traceback
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
import contacts  # noqa: E402
import crm  # noqa: E402
import db  # noqa: E402
import drafts as drafts_mod  # noqa: E402
import impact  # noqa: E402
import jobs  # noqa: E402
import llm  # noqa: E402
import qualify  # noqa: E402
import run  # noqa: E402
import send as mail_out  # noqa: E402
import settings  # noqa: E402
import shortlist  # noqa: E402

config.OFFLINE = not config.API_KEY  # live whenever a Gemini key is configured
DOMAIN = re.compile(r"^(?=.{4,100}$)([a-z0-9-]+\.)+[a-z]{2,}$")  # rejects IPs and localhost
MAX_PER_RUN = jobs.MAX_PER_RUN
MAX_SENDS_PER_DAY = 10  # sending is locked to your own address; this stops a loop from filling your inbox
MAX_RUNS_PER_HOUR = 3  # the site is open to reviewers, so this protects Gemini and Hunter credits
CRON_BUDGET = 240  # seconds of the 300 s limit the weekly run may use; the rest is paused for a resume

STATUS = {  # label, css class
    "drafted": ("Email drafted", "ok"), "not_qualified": ("Not qualified", "mute"),
    "disqualified": ("Disqualified", "mute"), "manual_review": ("Needs a human", "warn"),
    "failed": ("Needs a human", "warn"), "no_contact": ("Needs a human", "warn"), "no_hook": ("Needs a human", "warn"),
    "error": ("Needs a human", "warn"), "awaiting_confirm": ("Confirm fit", "warn"),
}
TODO = {  # what the human should do for each hand-over
    "manual_review": "Open the site and decide if it is a B2B SaaS company. If yes, add it to your list and re-run.",
    "failed": "The homepage could not be read. Check the site by hand, or try the domain again later.",
    "no_contact": "Hunter found nobody who owns security or engineering. Look the right person up in Apollo and add them on the My contacts page.",
    "no_hook": "Nothing specific to open with. Write the first line yourself.",
    "awaiting_confirm": "Check the fit and the contact below. Confirm to draft the email, or drop the company.",
    "error": "Something went wrong while researching it (the reason is above). Re-run it; if it fails again, check the site by hand.",
}
STEPS = [("Shortlist", "Funded SaaS from Google News", 0), ("Scrape", "One cached, robots-aware fetch", 0),
         ("Extract", "Named fields, quotes verified", 0), ("Qualify", "Evidence Map loop, 4 fetches max", 0),
         ("Find contact", "Hunter.io, ranked by role", 0), ("Pain points", "Two, each with a quote", 0),
         ("Draft", "By name, with a specific hook", 0), ("Review", "A human approves", 1), ("Send", "To your inbox only", 1)]

CSS = """
:root{color-scheme:light;--bg:#f5f6f3;--panel:#fff;--field:#fff;--soft:#f9faf8;--soft2:#eceee9;--hover:#f1f3ef;--line:#dfe3de;--text:#1c2b30;--mute:#5c6a6e;--accent:#0f766e;--accent-ink:#0b5c56;--accent-soft:#e3f1ee;--on-accent:#fff;--amber:#8a5a0f;--amber-soft:#f8efdd;--amber-line:#ecd3a4;--focus:#0f766e;--blue:var(--accent);--green:var(--accent);--green-d:var(--accent-ink);--navy:var(--text);--okbg:var(--accent-soft);--okline:#bfdcd6;--warnbg:var(--amber-soft);--warnline:var(--amber-line);--warntext:var(--amber)}
[data-theme=dark]{color-scheme:dark;--bg:#121719;--panel:#182023;--field:#141b1e;--soft:#1b2427;--soft2:#243034;--hover:#1e282b;--line:#2a3539;--text:#e3eae8;--mute:#98a6a6;--accent:#3bb5a6;--accent-ink:#5fcdbf;--accent-soft:#17312e;--on-accent:#0c1c1a;--amber:#e7b664;--amber-soft:#2c2416;--amber-line:#5a4520;--focus:#5fcdbf;--okline:#24524c}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#121719;--panel:#182023;--field:#141b1e;--soft:#1b2427;--soft2:#243034;--hover:#1e282b;--line:#2a3539;--text:#e3eae8;--mute:#98a6a6;--accent:#3bb5a6;--accent-ink:#5fcdbf;--accent-soft:#17312e;--on-accent:#0c1c1a;--amber:#e7b664;--amber-soft:#2c2416;--amber-line:#5a4520;--focus:#5fcdbf;--okline:#24524c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 "Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}main a:not(.btn):hover{color:var(--accent-ink)}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:6px}
.app{display:grid;grid-template-columns:232px 1fr;min-height:100vh}
aside{background:var(--panel);border-right:1px solid var(--line);color:var(--text);padding:22px 12px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column}
.brand{display:flex;gap:10px;align-items:center;font-weight:700;font-size:16px;margin:0 10px 24px;letter-spacing:-.01em}
.brand i{width:22px;height:22px;border-radius:6px;border:2px solid var(--accent);background:linear-gradient(var(--accent),var(--accent)) center/8px 8px no-repeat}
.brand small{display:block;font-weight:400;font-size:12px;color:var(--mute);letter-spacing:0}
aside h4{margin:20px 10px 6px;font-size:12px;color:var(--mute);font-weight:600}
aside a{display:flex;justify-content:space-between;align-items:center;padding:7px 10px 7px 12px;border-radius:6px;color:var(--mute);font-size:14px;border-left:3px solid transparent}
aside a:hover{background:var(--hover);color:var(--text)}aside a.on{background:var(--accent-soft);color:var(--accent-ink);border-left-color:var(--accent);font-weight:600;border-radius:0 6px 6px 0}
.badge{background:var(--amber-soft);color:var(--amber);border:1px solid var(--amber-line);border-radius:99px;font-size:11px;font-weight:700;padding:0 7px}
.note{margin-top:auto;font-size:12px;color:var(--mute);padding:10px;line-height:1.5}
.top{display:flex;gap:12px;align-items:center;padding:12px 28px;border-bottom:1px solid var(--line);background:var(--bg);position:sticky;top:0;z-index:4}
.top form{flex:1;max-width:400px}.top input{width:100%;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--field);color:var(--text);font:inherit}
.btn{display:inline-block;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:14px;cursor:pointer;font-family:inherit;line-height:1.4}
.btn:hover{border-color:var(--mute)}
.btn.p,.btn.g{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}.btn.p:hover,.btn.g:hover{background:var(--accent-ink);border-color:var(--accent-ink)}
.btn[disabled]{opacity:.45;cursor:not-allowed}
main{padding:32px 28px 48px;max-width:1080px}.kicker{color:var(--mute);font-size:13px}
h1{font-size:30px;font-weight:700;letter-spacing:-.02em;line-height:1.2;margin:2px 0 6px}h2{font-size:19px;font-weight:650;letter-spacing:-.01em;margin:0 0 4px}
.sub{color:var(--mute);max-width:64ch;margin:0 0 22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}.card small{color:var(--mute);font-size:13px}
.card b{display:block;font-size:30px;font-weight:650;letter-spacing:-.02em}.card span{color:var(--mute);font-size:13px}.card.hi b{color:var(--accent-ink)}
.box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-top:16px}.box h3{margin:0 0 10px;font-size:14px;color:var(--text);font-weight:650}
.legend{float:right;font-size:12px;color:var(--mute)}.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 4px 0 12px}
.bar{height:6px;border-radius:99px;background:var(--soft2);overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--mute);font-weight:600;font-size:13px;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:11px 10px;border-top:1px solid var(--line);vertical-align:top}tr:first-child td{border-top:0}tr.l{cursor:pointer}tr.l:hover td{background:var(--hover)}td small{color:var(--mute)}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid var(--line);white-space:nowrap;background:var(--soft);color:var(--mute)}
.pill.ok{color:var(--accent-ink);border-color:var(--okline);background:var(--accent-soft)}.pill.warn{color:var(--amber);border-color:var(--amber-line);background:var(--amber-soft)}.pill.mute{color:var(--mute)}
.chips a{display:inline-block;margin:0 6px 8px 0;padding:4px 12px;border:1px solid var(--line);border-radius:99px;font-size:13px;color:var(--mute);background:var(--panel)}.chips a:hover{color:var(--text)}
.chips a.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.q{border-left:2px solid var(--line);padding:2px 0 2px 12px;margin:10px 0;color:var(--mute);font-size:14px}.q b{color:var(--text);font-weight:600}
pre{white-space:pre-wrap;font:14px/1.65 "Public Sans",system-ui,sans-serif;margin:0}.back{color:var(--mute);font-size:14px}.back:hover{color:var(--accent-ink)}
input.t,textarea.t{width:100%;border:1px solid var(--line);border-radius:8px;padding:9px 12px;font:inherit;background:var(--field);color:var(--text);margin:4px 0 8px}
input.t:focus,textarea.t:focus,.top input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
textarea.t{min-height:190px;line-height:1.65}textarea.dom{min-height:110px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.ok-t{color:var(--accent-ink);font-weight:600;font-size:13px}
.box>label{display:block;font-size:13px;color:var(--mute);margin-top:10px}
input[type=checkbox]{accent-color:var(--accent)}
.chip{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--line);border-radius:99px;padding:5px 12px;font-size:13px;background:var(--panel);color:var(--mute);white-space:nowrap}
.chip i{width:7px;height:7px;border-radius:50%;background:var(--accent)}
.statgrid{display:grid;grid-template-columns:repeat(3,1fr);background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.statgrid>div{padding:18px 20px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.statgrid>div:nth-child(3n){border-right:0}.statgrid>div:nth-last-child(-n+3){border-bottom:0}
.statgrid small{color:var(--mute);font-size:13px}.statgrid b{display:block;font-size:30px;font-weight:650;letter-spacing:-.02em;margin:2px 0}.statgrid span{color:var(--mute);font-size:13px}
.statgrid b em{font-style:normal;font-size:16px;color:var(--mute);font-weight:500}
.nodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:0;margin-top:16px;text-align:center;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.node{padding:14px 6px;border-right:1px solid var(--line);background:var(--panel)}.node:last-child{border-right:0}
.node .c{width:26px;height:26px;border-radius:50%;margin:0 auto 8px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent-ink);font-weight:700;font-size:12px}
.node.h{background:var(--amber-soft)}.node.h .c{background:var(--panel);color:var(--amber);border:1px solid var(--amber-line)}
.node b{display:block;font-size:13px;font-weight:600}.node small{display:block;color:var(--mute);font-size:12px;line-height:1.35;margin-top:2px}.node em{display:block;font-style:normal;font-size:22px;font-weight:650;margin-top:6px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}.seg button{border:0;border-right:1px solid var(--line);background:var(--panel);padding:7px 18px;font:inherit;cursor:pointer;color:var(--text)}.seg button:last-child{border-right:0}
.seg button.on{background:var(--accent);color:var(--on-accent);font-weight:600}
.num{display:inline-grid;place-items:center;flex:none;width:22px;height:22px;border-radius:50%;background:var(--accent-soft);color:var(--accent-ink);font-size:12px;font-weight:700;margin-right:10px}
.runsel{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font:inherit}
.drop{display:grid;grid-template-columns:1fr 40px;gap:10px;align-items:center;margin:10px 0;font-size:14px}
.tier{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:5px;font-weight:700;font-size:12px}
.tA{background:var(--accent);color:var(--on-accent)}.tB{background:var(--accent-soft);color:var(--accent-ink);border:1px solid var(--okline)}.tC{background:var(--soft2);color:var(--mute)}
.warnbox{background:var(--amber-soft);border:1px solid var(--amber-line);color:var(--amber);border-radius:8px;padding:12px 14px;margin:14px 0;font-size:14px}
.warnbox b{color:inherit}
@media(max-width:860px){.app{grid-template-columns:minmax(0,1fr)}aside{position:sticky;top:0;z-index:6;height:auto;flex-direction:row;align-items:center;gap:2px;overflow-x:auto;padding:8px 12px;border-right:0;border-bottom:1px solid var(--line)}aside h4,aside .note,.brand small{display:none}.brand{flex:none;margin:0 8px 0 0}.brand>div{white-space:nowrap}aside a{flex:none;gap:6px;border-left:0;border-radius:6px;padding:6px 10px;white-space:nowrap}aside a.on{border-radius:6px}.top{position:static}.top form{max-width:none;flex-basis:100%}.chip{display:none}main{min-width:0}.grid2{grid-template-columns:1fr}main{padding:20px 16px 40px}.top{padding:10px 16px;flex-wrap:wrap}.statgrid{grid-template-columns:repeat(2,1fr)}.statgrid>div{border-right:1px solid var(--line)!important;border-bottom:1px solid var(--line)!important}table{display:block;overflow-x:auto}}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

JS = """
const S=(k,v)=>{try{v===undefined?localStorage.removeItem(k):localStorage.setItem(k,v)}catch(e){}};
const G=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
const post=(p,data)=>fetch('/?p='+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})})
  .then(r=>r.text().then(t=>{let j;try{j=JSON.parse(t)}catch(e){throw new Error(r.status===504?'The server took too long on this step.':'Server error ('+r.status+').')}
    if(!r.ok)throw new Error(j.error||'Failed.');return j}),()=>{throw new Error('Network error. Check your connection.')});
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const progress=j=>j.discover?'Searching the news for new companies, up to three minutes. Keep this tab open.':
  'Researching '+j.queue[0]+' ('+(j.done.length+1)+' of '+(j.done.length+j.queue.length)+'), '+j.qualified+' qualified so far. Keep this tab open.';
const drive=async(job,m)=>{try{while(job&&job.status==='running'){m.textContent=progress(job);const j=await post('step',{id:job.id});
    if(j.job.busy){await wait(5000)}job=j.job}
  m.textContent=(job&&job.note)||'Done.';setTimeout(()=>location.reload(),1500)}
  catch(x){m.textContent=x.message+' The run is saved: use Continue on the Run page.'}};
const startRun=(b,m,p,data)=>{b.disabled=true;m.textContent='Starting...';post(p,data).then(j=>drive(j.job,m)).catch(x=>{m.textContent=x.message;b.disabled=false})};
document.querySelectorAll('.draft').forEach(d=>{
  const id=d.dataset.d,sub=d.querySelector('.sub-in'),body=d.querySelector('.body-in'),msg=d.querySelector('.msg');
  const saved=G('draft:'+id);if(saved){try{const o=JSON.parse(saved);sub.value=o.s;body.value=o.b}catch(e){}}
  if(G('sent:'+id)){msg.textContent='Approved earlier'}
  const save=()=>S('draft:'+id,JSON.stringify({s:sub.value,b:body.value}));sub.oninput=body.oninput=()=>{save();msg.textContent='Edit saved in this browser'};
  d.querySelector('.send').onclick=()=>{save();const edit={domain:id,kind:'edit',data:{before:{subject:sub.defaultValue,body:body.defaultValue},after:{subject:sub.value,body:body.value}}};
    if(!CANSEND){S('sent:'+id,'1');msg.textContent='Approved and opened in your mail app';post('fb',edit).catch(()=>{});post('approved',{domain:id,subject:sub.value}).catch(()=>{});
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
  const n=c.querySelector('.cn'),t=c.querySelector('.ct');if(n)data.contact={name:n.value,title:t.value};startRun(b,m,'run',data)}});
const cont=document.querySelector('#cont');if(cont)cont.onclick=()=>{const m=document.querySelector('#status');cont.disabled=true;
  post('resume',{id:cont.dataset.id}).then(j=>drive(j.job,m)).catch(x=>{m.textContent=x.message;cont.disabled=false})};
document.querySelectorAll('.cfm').forEach(b=>{b.onclick=()=>{const m=b.closest('.box').querySelector('.amsg');b.disabled=true;
  m.textContent='Writing the email, up to a minute...';post('confirm',{domain:b.dataset.d}).then(j=>{m.textContent=j.note;setTimeout(()=>location.reload(),900)})
  .catch(x=>{m.textContent=x.message;b.disabled=false})}});
document.querySelectorAll('.dsm').forEach(b=>{b.onclick=()=>{b.disabled=true;post('dismiss',{domain:b.dataset.d}).then(()=>location.reload())
  .catch(x=>{b.closest('.box').querySelector('.amsg').textContent=x.message;b.disabled=false})}});
document.querySelectorAll('.fu').forEach(d=>{const id=d.dataset.d,sub=d.querySelector('.sub-in'),body=d.querySelector('.body-in'),msg=d.querySelector('.msg'),b=d.querySelector('.fsend');
  b.onclick=()=>{if(!CANSEND){post('approved',{domain:id,kind:'followup',subject:sub.value}).then(()=>{msg.textContent='Opened in your mail app and logged'}).catch(x=>msg.textContent=x.message);
      location.href='mailto:?subject='+encodeURIComponent(sub.value)+'&body='+encodeURIComponent(body.value);return}
    b.disabled=true;msg.textContent='Sending...';post('send',{domain:id,kind:'followup',subject:sub.value,body:body.value})
      .then(j=>{msg.textContent=j.note;setTimeout(()=>location.reload(),900)}).catch(x=>{msg.textContent=x.message;b.disabled=false})}});
document.querySelectorAll('.rep').forEach(b=>{b.onclick=()=>{b.disabled=true;post('replied',{domain:b.dataset.d}).then(()=>location.reload()).catch(x=>{alert(x.message);b.disabled=false})}});
go('#fudraft','followups',()=>({}),'Drafting the due follow-ups...');
const stopb=document.querySelector('#stop');if(stopb)stopb.onclick=()=>{stopb.disabled=true;post('stop',{id:stopb.dataset.id}).then(()=>location.reload()).catch(x=>{document.querySelector('#status').textContent=x.message;stopb.disabled=false})};
document.addEventListener('keydown',ev=>{if((ev.ctrlKey||ev.metaKey)&&ev.key==='k'){ev.preventDefault();const q=document.querySelector('#gq');if(q)q.focus()}});
const th=document.querySelector('#theme'),root=document.documentElement,sys=matchMedia('(prefers-color-scheme: dark)');
const isDark=()=>root.dataset.theme?root.dataset.theme==='dark':sys.matches;
const label=()=>{if(th)th.textContent=isDark()?'Light mode':'Dark mode'};label();sys.addEventListener('change',label);
if(th)th.onclick=()=>{const d=isDark()?'light':'dark';root.dataset.theme=d;S('theme',d);label()};
let target=3;document.querySelectorAll('.seg button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.seg button').forEach(x=>x.classList.remove('on'));b.classList.add('on');target=+b.dataset.n});
const runBtn=(sel,p,data)=>{const b=document.querySelector(sel);if(b)b.onclick=()=>startRun(b,document.querySelector('#status'),p,data())};
runBtn('#auto','auto',()=>({target}));
const chain=(sel,steps,wait)=>{const b=document.querySelector(sel);if(!b)return;b.onclick=async()=>{const m=document.querySelector('#status');b.disabled=true;m.textContent=wait;
  try{let j;for(const [p,d] of steps()){j=await post(p,d)}m.textContent=j.note||'Done';setTimeout(()=>location.reload(),700)}catch(x){m.textContent=x.message;b.disabled=false}}};
const pf=()=>({sources:['funding','hiring'].filter(c=>document.querySelector('#p_s_'+c).checked),keywords:document.querySelector('#p_kw').value,regions:['IN','US','GB','SG'].filter(c=>document.querySelector('#p_r_'+c).checked)});
chain('#psearch',()=>[['prospects',pf()],['shortlist',{}]],'Saving, then searching Google News and checking homepages. About a minute, keep this tab open.');
chain('#psave',()=>[['prospects',pf()]],'Saving...');
chain('#padd',()=>[['adddomains',{domains:document.querySelector('#p_dom').value.split(/[\\s,]+/).filter(Boolean)}]],'Adding...');
chain('#pplAdd',()=>[['addpeople',{lines:document.querySelector('#pl_lines').value}]],'Adding...');
document.querySelectorAll('.rmp').forEach(b=>b.onclick=()=>{b.disabled=true;post('removeperson',{domain:b.dataset.d,name:b.dataset.n}).then(()=>location.reload()).catch(x=>{alert(x.message);b.disabled=false})});
go('#find','shortlist',()=>({}),'Searching Google News and checking homepages, about a minute...');
runBtn('#go','run',()=>({domains:document.querySelector('#doms').value.split(/[\\s,]+/).filter(Boolean)}));
runBtn('#short','run',()=>({shortlist:true}));
const form=()=>{const v=id=>document.querySelector('#'+id).value,csv=id=>v(id);
  return {threshold:v('s_threshold'),stage_penalty:v('s_penalty'),stages:['A','B','C'].filter(x=>document.querySelector('#s_stage_'+x).checked),
   exclude:csv('s_exclude'),limits:{certified:v('s_l_certified'),saas:v('s_l_saas'),stage:v('s_l_stage')},
   roles:{security:csv('s_r_security'),engineering:csv('s_r_engineering'),founder:csv('s_r_founder')},sender:v('s_sender'),target:v('s_target'),tier_a:v('s_tier_a'),confirm:document.querySelector('#s_confirm').checked}};
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
    if config.HUNTER_API_KEY and not config.offline() and time.time() - _hl["t"] > 60:
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
                "runs": db.runs(20), "due": sum(x["due"] for x in crm.rows()), "unseen": sum(d not in {r["domain"] for r in rows} for d in db.shortlist())}
    except Exception as ex:  # a broken database must not take the read-only pages down
        return {**snap, "db_error": str(ex)[:160]}


def live_missing():
    """What is still needed before the browser can run the pipeline."""
    return [name for name, ok in (("DATABASE_URL (the Neon connection string)", db.enabled),
                                  ("GEMINI_API_KEY or OPENROUTER_API_KEY", bool(config.API_KEY))) if not ok]


def tier_of(r):
    """Tier from the current settings, so changing the thresholds re-tiers every company."""
    return None if r["status"] in ("disqualified", "failed", "manual_review") else qualify.tier(r.get("score"))


def tier_badge(r):
    t = tier_of(r)
    return f'<span class="tier t{t}" title="Tier {t}">{t}</span>' if t else "-"


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
            f'<title>Outreach Agent</title><link rel=preconnect href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;650;700&display=swap" rel=stylesheet><style>{CSS}</style><script>try{{const t=localStorage.getItem("theme");if(t)document.documentElement.dataset.theme=t}}catch(e){{}}const CANSEND={"true" if mail_out.can_send() else "false"};</script><div class=app><aside>'
            '<div class=brand><i></i><div>Outreach Agent<small>Compliance prospecting</small></div></div>'
            f'<h4>Pipeline</h4>{link("overview", "Overview")}{link("run", "Run pipeline")}{link("companies", "Companies")}{link("prospects", "Prospects")}{link("data", "My contacts")}{link("method", "Method")}{link("settings", "ICP settings")}'
            f'<h4>Your decisions</h4>{link("review", "Review queue", human)}{link("drafts", "Drafts &amp; sends", drafts)}{link("crm", "CRM log", snap.get("due") or None)}'
            f'<h4>Learning</h4>{link("feedback", "Feedback loop")}'
            '<div class=note>The model reads and writes. Python rules decide. Every draft waits for you.<br><br>Case study build, not affiliated with Sprinto.</div></aside>'
            '<div><div class=top><form action="/"><input type=hidden name=p value=companies><input id=gq name=q placeholder="Search companies   Ctrl K" value="'
            f'{e(q)}"></form>{chip}<button class=btn id=theme title="Switch between light and dark" style="margin-left:auto">Dark mode</button>'
            '<a class="btn p" href="/?p=run">Run pipeline</a></div>'
            f'<main>{body}</main></div></div><script>{JS}</script>')


def overview(snap, run_id="all"):
    all_rows, runs = snap["rows"], snap.get("runs") or []
    rows = [r for r in all_rows if run_id == "all" or r.get("run", "earlier") == run_id]
    n = len(rows)
    fit = count(rows, *jobs.QUALIFIED)
    human = sum(r["needs_human"] for r in rows)
    hl, target = snap["hunter_left"], current_settings()["target"]
    opts = '<option value="all">All companies</option>' + "".join(
        f'<option value="{e(x["ts"])}" {"selected" if run_id == x["ts"] else ""}>Run {len(runs) - i} · {e(x["ts"][:10])}</option>' for i, x in enumerate(runs)) \
        + f'<option value="earlier" {"selected" if run_id == "earlier" else ""}>Earlier (imported)</option>'
    stats = [("Pool", len(all_rows), f'{snap.get("unseen", 0)} still unseen'), ("Researched", n - count(rows, "failed"), "pages read"),
             ("Qualified", f'{fit}' + (f' <em>/ {target}</em>' if run_id != "all" else ""), f'{sum(tier_of(r) == "A" for r in rows)} tier A · {sum(tier_of(r) == "B" for r in rows)} tier B · {sum(tier_of(r) == "C" for r in rows)} near miss'), ("Flagged", human, "waiting on a human"),
             ("Dropped", count(rows, "disqualified", "not_qualified"), "a gate failed"),
             ("Hunter left", "-" if hl is None else hl, "searches this month")]
    stat_html = "".join(f"<div><small>{a}</small><b>{b}</b><span>{c}</span></div>" for a, b, c in stats)
    sent = db.sent_since("") if db.enabled and not snap.get("db_error") else 0
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
            f'<div class=box><span class=legend><i style="background:var(--accent)"></i>automatic<i style="background:var(--amber)"></i>a person decides</span>'
            f'<h3>The case pipeline</h3><h2>From funding news to an email in your inbox.</h2><div class=nodes>{nodes}</div></div>'
            f'<div class=grid2><div class=box><h3>Why companies dropped</h3>{drop_html}</div>'
            f'<div class=box><h3>Companies in this view</h3><table>{here}</table><p><a class=back href="/?p=companies">All companies</a></p></div></div>')


def companies(snap, flt, q):
    rows = snap["rows"]
    groups = {"all": None, "qualified": jobs.QUALIFIED, "drafted": ("drafted",),
              "flagged": ("manual_review", "failed", "no_contact", "no_hook", "error", "awaiting_confirm"), "dropped": ("disqualified", "not_qualified")}
    chips = "".join(f'<a class="{"on" if flt == k else ""}" href="/?p=companies&f={k}&q={quote(q)}">{k.title()}'
                    f' ({len(rows) if v is None else count(rows, *v)})</a>' for k, v in groups.items())
    tiers = {t: sum(tier_of(r) == t for r in rows) for t in "ABC"}
    chips += "".join(f'<a class="{"on" if flt == "tier-" + t else ""}" href="/?p=companies&f=tier-{t}&q={quote(q)}">Tier {t} ({n})</a>' for t, n in tiers.items())
    keep = [r for r in rows if (flt.startswith("tier-") and tier_of(r) == flt[-1])
            or (not flt.startswith("tier-") and (groups.get(flt) is None or r["status"] in groups[flt]))
            and (not q or q.lower() in (r["domain"] + r["reason"] + str(r.get("facts", {}).get("company_name", ""))).lower())]
    keep.sort(key=lambda r: (r["status"] != "drafted", -(r.get("score") or 0)))
    body = "".join(
        f'<tr class=l onclick="location=\'/?p=company&d={quote(r["domain"])}\'"><td><a href="/?p=company&d={quote(r["domain"])}"><b>{e(str(r.get("facts", {}).get("company_name") or r["domain"].split(".")[0].title()))}</b></a>'
        f'<br><small>{e(r["domain"])}</small></td><td>{e(str(r.get("facts", {}).get("funding_stage") or "-"))}</td>'
        f'<td><span class="pill {STATUS[r["status"]][1]}">{STATUS[r["status"]][0]}</span></td>'
        f'<td>{tier_badge(r)}</td><td>{"-" if r.get("score") is None else r["score"]}</td><td><small>{e(r["reason"][:120])}</small></td></tr>'
        for r in keep) or '<tr><td colspan=6><small>Nothing matches.</small></td></tr>'
    return ('<div class=kicker>Pipeline</div><h1>Companies</h1><p class=sub>Every company the pipeline has researched. Open one to see each decision with its evidence.</p>'
            f'<form action="/" class=row style="margin-bottom:10px"><input type=hidden name=p value=companies><input type=hidden name=f value="{e(flt)}">'
            f'<input class=t name=q placeholder="Search name or domain" value="{e(q)}" style="max-width:320px;margin:0"><div class=chips style="margin:0">{chips}</div></form>'
            f'<div class=box style="margin-top:0"><table><tr><th>Company</th><th>Stage</th><th>Outcome</th><th>Tier</th><th>Fit</th><th>Reason</th></tr>{body}</table></div>')


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
        c = r.get("contact")
        who = f'<div class=q><b>{e(c["name"])}</b>, {e(c["title"])}<br>{e(c.get("why", ""))}</div>' if c else ""
        extra = f'<div class=box><h3>What a human should do</h3>{e(TODO.get(r["status"], ""))}{who}</div>'
    return (f'<a class=back href="/?p=companies">Back to companies</a><h1>{e(r["domain"])}</h1>'
            f'<span class="pill {cls}">{lab}</span> {tier_badge(r) if tier_of(r) else ""} <small>fit {"-" if r.get("score") is None else r["score"]}</small>'
            f'<p class=sub style="margin-top:10px">{e(r["reason"])}</p>'
            f'<div class=grid2><div class=box style="margin-top:0"><h3>Evidence map</h3>{ev}{flags}</div>'
            f'<div class=box style="margin-top:0"><h3>Facts extracted</h3><table>{facts}</table></div></div>{extra}')


def review(snap):
    rows = [r for r in snap["rows"] if r["needs_human"]]
    off = "disabled" if live_missing() else ""

    def action(r):
        d = e(r["domain"])
        if r["status"] == "awaiting_confirm":
            c = r.get("contact") or {}
            return (f'<div class=q><b>Contact:</b> {e(c.get("name", ""))}, {e(c.get("title", ""))}<br>{e(c.get("why", ""))}</div>'
                    f'<div class=row style="margin-top:8px"><button class="btn g cfm" data-d="{d}" {off}>Confirm and draft</button>'
                    f'<button class="btn dsm" data-d="{d}" {"" if db.enabled else "disabled"}>Drop it</button><span class="ok-t amsg"></span></div>')
        if r["status"] == "no_contact":
            return (f'<div class=row style="margin-top:8px"><input class="t cn" style="max-width:200px;margin:0" placeholder="Contact name">'
                    f'<input class="t ct" style="max-width:220px;margin:0" placeholder="Title, e.g. CTO">'
                    f'<button class="btn g act" data-d="{d}" {off}>Add contact and draft</button><span class="ok-t amsg"></span></div>')
        return f'<div class=row style="margin-top:8px"><button class="btn act" data-d="{d}" {off}>Re-run this company</button><span class="ok-t amsg"></span></div>'

    cards = "".join(
        f'<div class=box><div class=row><b>{e(r["domain"])}</b><span class="pill warn">{e({"awaiting_confirm": "Confirm fit", "manual_review": "Check if SaaS"}.get(r["status"], r["status"].replace("_", " ").capitalize()))}</span>'
        f'<a class=btn style="margin-left:auto" href="/?p=company&d={quote(r["domain"])}">Open</a>'
        f'<button class="btn resolve" data-d="{e(r["domain"])}">Mark resolved</button></div>'
        f'<p style="margin:8px 0 4px">{e(r["reason"])}</p><div class=q><b>Do this:</b> {e(TODO.get(r["status"]) or "; ".join(r.get("flags", [])) or "Check this company.")}</div>{action(r)}</div>' for r in rows)
    return ('<div class=kicker>Human gate</div><h1>Review queue</h1><p class=sub>Companies the rules could not settle. '
            'Nothing here is guessed: each one waits for a person.</p>' + (cards or '<div class=box>Nothing is waiting on you.</div>'))


def drafts(snap):
    rows = [r for r in snap["rows"] if r.get("body")]
    warn = lambda r: "".join(f'<div class=warnbox style="margin:8px 0">{e(x)}</div>' for x in r.get("flags", []) if x.startswith("Check before sending"))
    cards = "".join(
        f'<div class="box draft" data-d="{e(r["domain"])}" data-hook="{e(r["hook"][:200])}"><div class=row><b>{e(r["contact"]["name"])}</b>{tier_badge(r) if tier_of(r) else ""}<small>{e(r["contact"]["title"])} at {e(r["domain"])}</small>'
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


def run_page(snap, demo):
    out = ""
    if demo:
        with config.forced_offline():  # this request only; a live run elsewhere is unaffected
            rows = [run.process(d, contacts.CSV) for d in (ROOT / "data/domains.txt").read_text().split()]
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
            live = job_panel()
        except Exception as ex:
            hist = f"<tr><td colspan=3><small>Database error: {e(str(ex)[:120])}</small></td></tr>"
            live = ""
        t = current_settings()["target"]
        seg = "".join(f'<button class="{"on" if n == t and n <= 3 else ""}" data-n="{n}">{n}</button>' for n in (1, 2, 3))
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


def job_panel():
    """The run in progress or paused, with Continue and Stop, so a closed tab or a timed-out step loses nothing."""
    job = jobs.active() or next((j for j in db.jobs_list(3) if j["status"] == "paused"), None)
    if not job:
        return ""
    left = len(job["queue"])
    state = ("Searching the news for companies" if job.get("discover") else
             f'{len(job["done"])} researched, {left} left, {job["qualified"]} qualified')
    head = "A run is in progress" if job["status"] == "running" else "A run is paused"
    return (f'<div class=warnbox><b>{head}</b> (started {e(ago(job["id"]))}). {e(state)}. {e(job.get("note") or "")}'
            f'<div class=row style="margin-top:8px"><button class="btn g" id=cont data-id="{e(job["id"])}">Continue it here</button>'
            f'<button class=btn id=stop data-id="{e(job["id"])}">Stop it</button></div></div>')


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
            f'<label>Score for tier A (at or above it is tier A; the score above is tier B; 15 below it is tier C, a near miss that is not emailed)</label>{num("s_tier_a", s["tier_a"], 0, 100)}'
            f'<label>Funding stages in the ICP</label><div style="margin:6px 0 4px">{stages}</div>'
            f'<label>How many qualified companies a run aims for (1 to 5)</label>{num("s_target", s["target"], 1, 5)}'
            f'<label><input type=checkbox id=s_confirm {"checked" if s["confirm"] else ""} {dis}> Confirm fit and contact in the Review queue before an email is drafted</label></div>'
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
    sources = "".join(f'<label style="margin-right:14px"><input type=checkbox id=p_s_{c} {"checked" if c in s["sources"] else ""} {dis}> {n}</label>'
                      for c, n in (("funding", "Funding news"), ("hiring", "Security and compliance hires")))
    rows = "".join(
        f'<tr><td><b>{e(d)}</b></td><td>{e(v.get("funding_stage") or ("hiring" if v.get("hiring_source") else "added by you"))}</td><td><small>{e((v.get("funding_source") or v.get("hiring_source") or "")[:90])}</small></td>'
        f'<td><span class="pill {"mute" if d in have else "ok"}">{"Researched" if d in have else "Waiting"}</span></td></tr>' for d, v in list(sl.items())[-60:]) \
        or "<tr><td colspan=4><small>Nothing here yet. Search, or add your own domains.</small></td></tr>"
    note = "" if editable else '<div class=warnbox>A database is needed to save prospects. Showing a read-only view.</div>'
    return ('<div class=kicker>Pipeline</div><h1>Prospects</h1>'
            '<p class=sub>Tell it where to look. It searches funding news and security or compliance hiring news for companies matching your keywords, checks each one has a real website, and queues them. '
            'Run the queue from the Run page, or let the Monday run pick them up.</p>' + note +
            f'<div class=grid2><div class=box style="margin-top:0"><h3>Search for prospects</h3>'
            f'<label>Industries or topics to look for (comma separated, up to 5)</label>'
            f'<textarea class=t id=p_kw style="min-height:70px" placeholder="e.g. cybersecurity, healthtech, HR tech, payments" {dis}>{e(", ".join(s["keywords"]))}</textarea>'
            f'<small>Leave empty to use the built-in SaaS search. Funding stages come from ICP settings ({e(", ".join("Series " + x for x in s["stages"]))}).</small>'
            f'<label>Regions</label><div style="margin:6px 0 10px">{regions}</div>'
            f'<label>Where to look</label><div style="margin:6px 0 10px">{sources}</div>'
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
    if tab == "crm":
        return shell("crm", crm_page(snap), snap)
    if tab == "method":
        return shell("method", method(), snap)
    if tab == "run":
        return shell("run", run_page(snap, q.get("demo")), snap)
    return shell("overview", overview(snap, q.get("run", "all")), snap)


def _limited():
    """The site has no sign-in, so spend is capped instead: MAX_RUNS_PER_HOUR new runs an hour, for everyone."""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    if db.jobs_since(since) >= MAX_RUNS_PER_HOUR:
        return 429, {"error": f"Run limit reached ({MAX_RUNS_PER_HOUR} an hour, to protect credits). Try again later."}


def _seeds():
    sl = ROOT / "data/shortlist.json"
    return {**(json.loads(sl.read_text(encoding="utf-8")) if sl.exists() else {}), **db.shortlist()}


def _started(job):
    return 200, {"job": job, "note": "Started."}


def do_run(body):
    current_settings()
    if (err := _limited()):
        return err
    seeds = _seeds()
    if body.get("shortlist"):
        have = {r["domain"] for r in db.companies()}
        domains = [d for d in seeds if d not in have]
    else:
        raw = body.get("domains")
        if not isinstance(raw, list):
            return 400, {"error": "Enter at least one domain."}
        domains = [_site(d) for d in raw if str(d).strip()]
    domains = list(dict.fromkeys(domains))[:MAX_PER_RUN]
    if not domains:
        return 400, {"error": "No new domains to run. Use Find new companies first."}
    bad = [d for d in domains if not DOMAIN.match(d)]
    if bad:
        return 400, {"error": "Not a domain name: " + bad[0][:60]}
    given = None  # a reviewer-supplied contact for a single company whose contact Hunter could not find
    c = body.get("contact")
    if isinstance(c, dict) and len(domains) == 1:
        name, title = str(c.get("name", "")).strip()[:80], str(c.get("title", "")).strip()[:80]
        if not name or not title:
            return 400, {"error": "Enter both a name and a title."}
        given = {"name": name, "title": title}
    return _started(jobs.start(domains, seeds, given=given))


def do_auto(body):
    """Find companies for me: research what is waiting in the queue (discovering new companies first if nothing is),
    stopping once `target` qualify."""
    s = current_settings()
    if (err := _limited()):
        return err
    try:
        target = max(1, min(5, int(body.get("target") or s["target"])))
    except (TypeError, ValueError):
        target = s["target"]
    return _started(jobs.start_auto(target, _seeds()))


def _job_id(body):
    job_id = str(body.get("id", ""))[:40]
    if not db.get_job(job_id):
        raise LookupError("That run no longer exists. Reload the page.")
    return job_id


def do_step(body):
    current_settings()
    return 200, {"job": jobs.step(_job_id(body))}


def do_resume(body):
    job = jobs.resume(_job_id(body))
    return 200, {"job": job}


def do_stop(body):
    return 200, {"job": jobs.stop(_job_id(body))}


def cron():
    """The weekly run: continue a paused or unfinished run, else start a new automatic one; work until the budget is spent."""
    s = current_settings()
    job = jobs.active() or next((j for j in db.jobs_list(3) if j["status"] == "paused"), None)
    if job and job["status"] == "paused":
        job = jobs.resume(job["id"])
    if not job:
        job = jobs.start_auto(s["target"], _seeds())
    job = jobs.drain(job["id"], CRON_BUDGET)
    try:
        followups = crm.draft_due(limit=3)[0]  # the run used most of the time limit
    except llm.ModelUnavailable as ex:  # the model went down: the follow-ups wait for the next run or the button
        followups = f"not drafted: {ex}"
    return 200, {"followups": followups, "job": {k: job.get(k) for k in ("id", "status", "note", "done", "qualified", "drafted")}}


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
        db.set_settings(settings.validate({"keywords": stored["keywords"], "regions": stored["regions"], "sources": stored["sources"], **cfg}))
    except ValueError as ex:
        return 400, {"error": str(ex)}
    return 200, {"note": "Saved. The next run uses these settings."}


def do_prospects(body):
    try:
        db.set_settings(settings.validate({**settings.effective(db.get_settings()),
                                           "keywords": body.get("keywords", ""), "regions": body.get("regions") or [],
                                           "sources": body.get("sources", settings.effective(db.get_settings())["sources"])}))
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
    if db.sent_since(since) >= MAX_SENDS_PER_DAY:
        return 429, {"error": f"Daily limit reached ({MAX_SENDS_PER_DAY} emails). Try again tomorrow."}
    try:
        mail_out.deliver(subject, text)
    except Exception as ex:  # the error text can name the account, so only the type is shown
        return 502, {"error": f"Gmail did not accept it ({type(ex).__name__}). Check the app password."}
    db.add_event(str(body.get("domain", ""))[:100], "followup_sent" if body.get("kind") == "followup" else "sent",
                 {"subject": subject, "channel": "inbox"})
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


def crm_page(snap):
    if not db.enabled or snap.get("db_error"):
        return ('<div class=kicker>Human gate</div><h1>CRM log</h1><div class=warnbox>A database is needed to log sends and follow-ups. '
                'Add Neon from the Vercel Marketplace (Storage tab) and redeploy.</div>')
    rows = crm.rows()
    due = [r for r in rows if r["due"]]
    table = "".join(
        f'<tr><td><a href="/?p=company&d={quote(r["domain"])}"><b>{e(r["domain"])}</b></a><br><small>{e(r["contact"])}{", " + e(r["title"]) if r["title"] else ""}</small></td>'
        f'<td><small>{e(r["first"][:10])} · {e(r["channel"])}</small><br>{e(r["subject"][:70])}</td>'
        f'<td><span class="pill {"warn" if r["due"] else "ok" if r["replied"] else "mute"}">{e(r["status"])}</span><br><small>follow-up on {e(r["due_on"])}</small></td>'
        f'<td>{"" if r["replied"] else f"<button class=\"btn rep\" data-d=\"{e(r["domain"])}\">Mark replied</button>"}</td></tr>' for r in rows) \
        or '<tr><td colspan=4><small>Nothing sent yet. Approved emails show up here.</small></td></tr>'
    cards = "".join(
        f'<div class="box fu" data-d="{e(r["domain"])}"><div class=row><b>Follow-up to {e(r["contact"] or r["domain"])}</b><small>{e(r["domain"])}, first email {r["days"]} days ago</small></div>'
        + (f'<div class=warnbox style="margin:8px 0">{e(r["followup"]["flag"])}</div>' if r["followup"].get("flag") else "")
        + f'<input class="t sub-in" value="{e(r["followup"]["subject"])}"><textarea class="t body-in">{e(r["followup"]["body"])}</textarea>'
        '<div class=row><button class="btn g fsend">Approve &amp; send follow-up</button><span class="ok-t msg"></span></div></div>'
        for r in due if r["followup"])
    waiting = sum(1 for r in due if not r["followup"])
    miss = live_missing()
    btn = (f'<p><button class="btn g" id=fudraft {"disabled" if miss or not waiting else ""}>Draft {waiting} due follow-up{"s" if waiting != 1 else ""}</button> '
           f'<span class=ok-t id=status></span></p>')
    return ('<div class=kicker>Human gate</div><h1>CRM log</h1><p class=sub>Every approved email, how it went out, and its follow-up. '
            f'A first email with no reply after {crm.FOLLOWUP_DAYS} days gets a short follow-up drafted with a new angle; it waits here for your approval. '
            'Mark a company replied to stop its follow-up. The Monday run drafts due follow-ups too.</p>'
            f'{btn}{cards}<div class=box><h3>Log ({len(rows)})</h3><table><tr><th>Company</th><th>First email</th><th>Status</th><th></th></tr>{table}</table></div>')


def _company(domain):
    domain = str(domain or "")[:100]
    c = next((x for x in db.companies() if x["domain"] == domain), None)
    if not c:
        raise LookupError("That company is not in the list. Reload the page.")
    return c


def do_confirm(body):
    """A person confirmed fit and contact: draft the email from the stored research."""
    current_settings()
    c = _company(body.get("domain"))
    if c["status"] != "awaiting_confirm":
        return 409, {"error": "That company is not waiting for confirmation."}
    row = run.public(run.draft_for(c, db.learnings()))
    row["confirmed"] = db.now()
    db.save_company(row)
    return 200, {"note": "Email drafted. It is in Drafts and sends." if row["status"] == "drafted" else row["reason"]}


def do_dismiss(body):
    c = _company(body.get("domain"))
    if c["status"] != "awaiting_confirm":
        return 409, {"error": "That company is not waiting for confirmation."}
    db.save_company({**c, "status": "not_qualified", "needs_human": False, "reason": "Dropped by the reviewer: fit or contact not right."})
    return 200, {"note": "Dropped."}


def do_approved(body):
    """An email approved and opened in the reviewer's own mail app (no send from the site): logged for the CRM."""
    c = _company(body.get("domain"))
    kind = "followup_sent" if body.get("kind") == "followup" else "approved"
    db.add_event(c["domain"], kind, {"subject": " ".join(str(body.get("subject", "")).split())[:200], "channel": "mail app"})
    return 200, {"note": "Logged."}


def do_replied(body):
    c = _company(body.get("domain"))
    db.add_event(c["domain"], "replied", {})
    return 200, {"note": "Marked replied. No follow-up will be drafted."}


def do_followups(body):
    current_settings()
    drafted, skipped = crm.draft_due()
    return 200, {"note": f"{drafted} follow-ups drafted" + (f", {skipped} skipped (no first email stored)." if skipped else ".")}


ROUTES = {"send": do_send, "addpeople": do_addpeople, "removeperson": do_removeperson, "prospects": do_prospects,
          "adddomains": do_adddomains, "auto": do_auto, "settings": do_settings, "run": do_run, "fb": do_fb,
          "learn": do_learn, "shortlist": do_shortlist, "step": do_step, "resume": do_resume, "stop": do_stop,
          "confirm": do_confirm, "dismiss": do_dismiss, "approved": do_approved, "replied": do_replied, "followups": do_followups}
NEEDS_MODEL = (do_run, do_auto, do_learn, do_step, do_resume, do_confirm, do_followups)


def _log(where):
    print(f"[outreach] {where} failed:\n{traceback.format_exc()}", file=sys.stderr)  # shows in the Vercel logs


def post(path, raw):
    """(status, JSON body) for an action. Errors are short messages for the page; details go to the logs."""
    route = ROUTES.get(parse_qs(urlparse(path).query).get("p", [""])[0])
    if not route:
        return 404, {"error": "Unknown action."}
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return 400, {"error": "Bad request."}
    if not isinstance(body, dict):
        return 400, {"error": "Bad request."}
    if not db.enabled:
        return 503, {"error": "No database is connected."}
    if route in NEEDS_MODEL and not config.API_KEY:
        return 503, {"error": "No model key is set (GEMINI_API_KEY or OPENROUTER_API_KEY)."}
    try:
        return route(body)
    except jobs.Busy as ex:
        return 409, {"error": str(ex)}
    except llm.ModelUnavailable as ex:
        return 503, {"error": f"Not now: {ex}."}
    except LookupError as ex:
        return 404, {"error": str(ex)}
    except Exception as ex:  # noqa: BLE001
        _log(f"POST {path}")
        return 500, {"error": f"Something went wrong ({type(ex).__name__}). Try again in a moment."}


ERROR_PAGE = ('<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Outreach Agent</title>'
              '<body style="font:16px system-ui;margin:40px;max-width:560px"><h1>This page could not load</h1>'
              '<p>Something went wrong on the server. Reload in a moment, or go back to the <a href="/">overview</a>.</p>')


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")  # every page shows live data
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/favicon.ico", "/robots.txt", "/apple-touch-icon.png"):
            return self._send(204, b"", "text/plain")
        try:
            if parse_qs(urlparse(self.path).query).get("p") == ["cron"]:
                secret = os.getenv("CRON_SECRET", "")
                if not secret or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {secret}"):
                    return self._send(401, json.dumps({"error": "Not allowed."}), "application/json")
                if not (db.enabled and config.API_KEY):
                    return self._send(503, json.dumps({"error": "Database or model key missing."}), "application/json")
                code, out = cron()
                return self._send(code, json.dumps(out), "application/json")
            self._send(200, page(self.path))
        except Exception:  # noqa: BLE001
            _log(f"GET {self.path}")
            self._send(500, ERROR_PAGE)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 60_000:
            return self._send(413, json.dumps({"error": "Too much text in one request."}), "application/json")
        code, out = post(self.path, self.rfile.read(n) if n else b"")
        self._send(code, json.dumps(out), "application/json")
