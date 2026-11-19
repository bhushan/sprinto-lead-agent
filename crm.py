"""CRM log and the day-14 follow-up (component map: Send row).

Built from the events table, so nothing new has to be kept in sync: "sent" (emailed from the site to your inbox),
"approved" (opened in your mail app), "followup_sent", "replied". A first email with no reply after FOLLOWUP_DAYS
is due a follow-up; the follow-up is drafted by the model, checked in code, stored on the company row and waits
for approval in the CRM log like every other email.
"""
from datetime import datetime, timedelta, timezone

import db
import drafts
import llm

FOLLOWUP_DAYS = 14
KINDS = ("sent", "approved", "followup_sent", "replied")


def _age_days(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return 0


def rows():
    """One row per company that was emailed: first contact, channel, follow-up state. Newest first."""
    companies = {c["domain"]: c for c in db.companies()}
    out = {}
    for ev in db.events_of(KINDS):
        d = ev["domain"]
        if ev["kind"] in ("sent", "approved"):
            out.setdefault(d, {"domain": d, "first": ev["ts"], "subject": ev["data"].get("subject", ""),
                               "channel": "inbox" if ev["kind"] == "sent" else "mail app",
                               "followup_sent": None, "replied": None})
        elif d in out and ev["kind"] == "followup_sent":
            out[d]["followup_sent"] = out[d]["followup_sent"] or ev["ts"]
        elif d in out and ev["kind"] == "replied":
            out[d]["replied"] = ev["ts"]
    for d, r in out.items():
        c = companies.get(d, {})
        r["contact"] = (c.get("contact") or {}).get("name", "")
        r["title"] = (c.get("contact") or {}).get("title", "")
        r["followup"] = c.get("followup")
        r["days"] = _age_days(r["first"])
        r["due_on"] = (datetime.fromisoformat(r["first"]) + timedelta(days=FOLLOWUP_DAYS)).date().isoformat()
        r["due"] = not r["replied"] and not r["followup_sent"] and r["days"] >= FOLLOWUP_DAYS
        r["status"] = ("replied" if r["replied"] else "followed up" if r["followup_sent"] else
                       "follow-up due" if r["due"] else f"waiting ({FOLLOWUP_DAYS - r['days']} days to follow-up)")
    return sorted(out.values(), key=lambda r: r["first"], reverse=True)


def rows_by_domain():
    return {r["domain"]: r for r in rows()}


def draft_followup(domain):
    """Draft and store the follow-up for one company. Raises LookupError when there is nothing to follow up on."""
    c = next((x for x in db.companies() if x["domain"] == domain), None)
    if not c or not c.get("body") or not c.get("contact"):
        raise LookupError("There is no first email for that company.")
    first = {"subject": c["subject"], "body": c["body"]}
    f = drafts.followup(domain, c.get("facts") or {}, c["contact"], first)
    if f["problems"]:  # one rewrite is not worth a second model call here: flag it for the reviewer
        f["flag"] = "Check before sending: " + "; ".join(f["problems"])
    c["followup"] = {"subject": f["subject"], "body": f["body"], "flag": f.get("flag", ""), "drafted": db.now()}
    db.save_company(c)
    return c["followup"]


def draft_due(limit=10):
    """Draft due follow-ups that have none yet, at most `limit`. Returns (drafted, skipped)."""
    drafted = skipped = 0
    for r in rows():
        if drafted >= limit:
            break
        if r["due"] and not r["followup"]:
            try:
                draft_followup(r["domain"])
                drafted += 1
            except (LookupError, llm.ModelError):
                skipped += 1
    return drafted, skipped
