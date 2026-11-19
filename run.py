import argparse
import json
import re
import time
from pathlib import Path

import contacts
import drafts
import impact
import qualify
import scrape
import send


def process(domain, contacts_path="data/contacts.csv", approved=False, seed=None, lessons=None, contact=None):
    """Run the 7 steps for one domain. Always returns a row that records why it ended where it did."""
    t0 = time.time()
    row = {"domain": domain, "needs_human": False, "flags": [], "fetches": 0}

    def end(status, reason, human=False, **kw):
        row.update(status=status, reason=reason, seconds=round(time.time() - t0, 2), **kw)
        row["needs_human"] = row["needs_human"] or human
        return row

    home, note = scrape.fetch(domain, "home")
    if not home:  # degrade gracefully: log the reason, queue for a human, move on
        return end("failed", f"homepage not scraped: {note}", human=True)

    q = qualify.qualify(domain, home, seed)
    row.update(score=q["score"], facts=q["facts"], evidence=q["evidence"], fetches=q["fetches"],
               flags=q["flags"], needs_human=q["needs_human"])
    if q["status"] != "qualified":
        return end(q["status"], q["reason"])

    facts = q["facts"]
    why = "added by a reviewer"
    if not contact:
        contact, why = contacts.find_contact(domain, facts.get("headcount"), contacts_path)
    if not contact:
        return end("no_contact", why, human=True)
    row["contact"] = {**contact, "why": why}

    # best hook: a verified quote about audits or customer pressure; else the funding news (a recent round is a real reason to write)
    hook = (facts.get("compliance_signals") or [None])[0] or facts.get("funding_source")
    if not hook:
        return end("no_hook", "no verified compliance signal and no funding news to open with", human=True)
    pains = drafts.pain_points(domain, facts, contact)
    if len(pains) != 2:
        return end("no_hook", f"expected 2 pain points, got {len(pains)}", human=True)
    mail = drafts.draft(domain, facts, contact, pains, hook, lessons)
    if contact["name"].split()[0] not in mail["body"]:
        row["flags"].append("email does not address the contact by first name")
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


def run(domains, contacts_path, approved=False):
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
    ap.add_argument("--contacts", default="data/contacts.csv")
    ap.add_argument("--send", action="store_true", help="really send via Gmail (needs OFFLINE=0); default only drafts")
    a = ap.parse_args()
    run(Path(a.domains).read_text().split(), a.contacts, a.send)
