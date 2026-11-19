import argparse
import json
import re
import time
from pathlib import Path

import contacts
import drafts
import impact
import llm
import qualify
import scrape
import send


def process(domain, contacts_path=contacts.CSV, approved=False, seed=None, lessons=None, contact=None):
    """Run the 7 steps for one domain. Always returns a row that records why it ended where it did.

    A step that breaks (an unusable model reply, an unexpected page) ends this company as "error" for a human.
    llm.ModelUnavailable is raised instead: the model being down says nothing about the company."""
    t0 = time.time()
    row = {"domain": domain, "needs_human": False, "flags": [], "fetches": 0}
    at = ["scrape"]

    def end(status, reason, human=False, **kw):
        row.update(status=status, reason=reason, seconds=round(time.time() - t0, 2), **kw)
        row["needs_human"] = row["needs_human"] or human
        return row

    try:
        return _steps(domain, row, end, at, contacts_path, approved, seed, lessons, contact)
    except llm.ModelUnavailable:
        raise
    except Exception as ex:  # noqa: BLE001 - every other failure becomes a hand-over with its reason
        detail = str(ex) if isinstance(ex, llm.ModelError) else type(ex).__name__
        return end("error", f"Stopped at {at[0]}: {detail}"[:300], human=True)


def _steps(domain, row, end, at, contacts_path, approved, seed, lessons, contact):
    home, note = scrape.fetch(domain, "home")
    if not home:  # degrade gracefully: log the reason, queue for a human, move on
        return end("failed", f"homepage not scraped: {note}", human=True)

    at[0] = "qualify"
    q = qualify.qualify(domain, home, seed)
    row.update(score=q["score"], tier=qualify.tier(q["score"]) if q["status"] in ("qualified", "not_qualified") else None, facts=q["facts"], evidence=q["evidence"], fetches=q["fetches"],
               flags=q["flags"], needs_human=q["needs_human"])
    if q["status"] != "qualified":
        return end(q["status"], q["reason"])

    facts = q["facts"]
    why = "added by a reviewer"
    at[0] = "contact"
    if not contact:
        contact, why = contacts.find_contact(domain, facts.get("headcount"), contacts_path)
    if not contact:
        return end("no_contact", why, human=True)
    row["contact"] = {**contact, "why": why}

    # best hook: a verified quote about audits or customer pressure; else the funding news (a recent round is a real reason to write)
    at[0] = "draft"
    hook = drafts.pick_hook(facts)
    if not hook:
        return end("no_hook", "no verified compliance signal and no funding or hiring news to open with", human=True)
    pains = drafts.pain_points(domain, facts, contact)
    if len(pains) != 2:
        return end("no_hook", f"expected 2 pain points, got {len(pains)}", human=True)
    mail = drafts.draft(domain, facts, contact, pains, hook, lessons)
    name = drafts.company(domain, facts)
    sources = " ".join([hook, json.dumps(pains), json.dumps(facts)]).lower()
    problems = drafts.check(mail, contact, name, sources)
    if problems:  # one rewrite with the problems named; anything still wrong goes to a human
        mail = drafts.draft(domain, facts, contact, pains, hook, lessons, fixes=problems)
        problems = drafts.check(mail, contact, name, sources)
    if problems:
        row["flags"].append("Check before sending: " + "; ".join(problems))
        row["needs_human"] = True
    row.update(pain_points=pains, hook=hook, subject=mail["subject"], body=mail["body"],
               send=send.send(domain, mail["subject"], mail["body"], approved))
    return end("drafted", q["reason"])


def public(row):
    """Drop the contact's email and the names of people passed over; the site shows titles and reasons only."""
    c = row.get("contact")
    if c:
        c.pop("email", None)
        c["why"] = re.sub(r"\. Passed over: (.*)$", lambda m: f". Passed over {m.group(1).count('),') + 1} others.", c["why"])
    return row


def run(domains, contacts_path=contacts.CSV, approved=False):
    rows = []
    sl = Path("data/shortlist.json")
    seeds = json.loads(sl.read_text(encoding="utf-8")) if sl.exists() else {}
    for d in domains:
        r = process(d, contacts_path, approved, seeds.get(d))
        rows.append(r)
        who = r.get("contact", {}).get("name", "-")
        print(f"{d:20} {r['status']:13} score={r.get('score')!s:5} {who:14} "
              f"{'HUMAN ' if r['needs_human'] else ''}{r['reason']}")
    Path("runs").mkdir(exist_ok=True)
    with open("runs/run_log.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(impact.run_stats(rows))
    print(impact.monthly_summary())
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", default="data/domains.txt")
    ap.add_argument("--contacts", default=contacts.CSV)
    ap.add_argument("--send", action="store_true", help="really send via Gmail (needs OFFLINE=0); default only drafts")
    a = ap.parse_args()
    run(Path(a.domains).read_text().split(), a.contacts, a.send)
