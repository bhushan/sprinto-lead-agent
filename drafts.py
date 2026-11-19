"""Steps 5 and 6: two likely pain points for this person, then a personalised email."""
import re

import config
import llm

PAIN_PROMPT = """{name} is {title} at {company} ({product}). Facts we know: {facts}
Name the two most likely pain points for someone in this role right now. Each must be backed by a
fact above. Never say or imply the company lacks a certification or has compliance gaps: we have not verified that. Return JSON: pain_points (list of exactly 2 objects with keys pain, evidence)."""

DRAFT_PROMPT = """Write a cold email from {sender} at Sprinto to {name}, {title} at {company}.
Address them by first name. The first sentence must be about something specific to {company},
built on this hook (paraphrase it, do not quote it back flatly): {hook}
Speak to these pain points: {pains}
Use only these claims about Sprinto, and pick one: it automates evidence collection through
integrations; it maps one set of controls across SOC 2, ISO 27001 and GDPR; it helps a team get
audit-ready faster than a spreadsheet process; it removes the security questionnaire block on enterprise deals.
Under 90 words, one soft ask for a short call. No greeting filler, no hype, no invented numbers or customers.
Never say or imply the company lacks a certification or has compliance gaps: we have not verified that.
{fixes}{lessons}Return JSON: subject, body."""


def company(domain, facts):
    return facts.get("company_name") or domain.split(".")[0].title()


HOOK_WORDS = ("customer", "enterprise", "procurement", "require", "ask", "audit", "soc 2", "iso 27001", "gdpr", "certif", "compliance")
BANNED = ("hope this finds you", "i hope this email", "game-changing", "game changer", "revolutioni", "synergy", "cutting-edge",
          "best-in-class", "world-class", "reach out", "touch base", "circle back", "quick question", "excited to")


UNVERIFIED = re.compile(r"\b(lack\w*|missing|without|no|not|absence)\b(\W+\w+){0,4}\W+(certif\w*|soc 2|iso 27001|compliance)", re.I)


def pick_hook(facts):
    """The page quote closest to audits or customer pressure; the funding news when the pages gave none."""
    sigs = facts.get("compliance_signals") or []
    if sigs:
        return max(sigs, key=lambda s: sum(w in s.lower() for w in HOOK_WORDS))
    return facts.get("funding_source")


def check(mail, contact, name, sources):
    """Code check of a draft. Returns a list of problems (empty = fine). sources: lowercase text the claims may come from."""
    body, subject = mail["body"], mail["subject"]
    low, problems = body.lower(), []
    first = contact["name"].split()[0]
    if first.lower() not in low.splitlines()[0].lower():
        problems.append(f"open the first line by addressing {first} by first name")
    if name.lower() not in low:
        problems.append(f"mention {name} by name")
    words = len(body.split())
    if words > 110:
        problems.append(f"it has {words} words; keep it under 90")
    elif words < 25:
        problems.append("it is too short to be specific")
    if "?" not in body:
        problems.append("end with one soft question asking for a short call")
    if "!" in body:
        problems.append("remove exclamation marks")
    if re.search(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>", body):
        problems.append("remove placeholder text")
    if [b for b in BANNED if b in low]:
        problems.append("remove stock phrases: " + ", ".join(b for b in BANNED if b in low))
    if UNVERIFIED.search(body):
        problems.append("remove any claim that the company lacks a certification or has compliance gaps; that is not verified")
    if len(subject) > 90:
        problems.append("shorten the subject line")
    for n in re.findall(r"[$£€]?\d[\d,.]*\s?%?", body):
        digits = re.sub(r"\D", "", n)
        n = n.strip(" .,").lower()
        if (len(digits) >= 3 or "%" in n or n[:1] in "$£€") and digits != "27001" and n not in sources:
            problems.append(f"remove the number {n}; it is not in the company facts")
    return problems


def pain_points(domain, facts, contact):
    p = PAIN_PROMPT.format(name=contact["name"], title=contact["title"], company=company(domain, facts),
                           product=facts.get("product"), facts=facts)
    return llm.ask("pain_points", domain, p)["pain_points"]


def draft(domain, facts, contact, pains, hook, lessons=None, fixes=None):
    p = DRAFT_PROMPT.format(sender=config.SENDER_NAME, name=contact["name"], title=contact["title"],
                            company=company(domain, facts), hook=hook, pains=[x["pain"] for x in pains],
                            fixes=("Your previous draft broke these rules. Fix every one:\n- " + "\n- ".join(fixes) + "\n") if fixes else "",
                            lessons=("Follow these preferences the reviewer showed in past edits:\n- " + "\n- ".join(lessons) + "\n") if lessons else "")
    mail = llm.ask("draft", domain, p)
    if not mail["body"].rstrip().endswith(config.SENDER_NAME):  # the model sometimes drops the sign-off
        mail["body"] = mail["body"].rstrip() + f"\n\n{config.SENDER_NAME}"
    return mail


LEARN_PROMPT = """A reviewer edited cold-email drafts before sending. Each item shows the draft and the edited version
(or a rating when nothing was changed). Write at most 5 short, general rules about style and content that would make
future drafts need fewer edits. Do not mention company names. Return JSON: rules (list of strings).

{items}"""


def learn(events):
    """events: edit/rating events from db.events(). Returns the new list of rules."""
    items = []
    for ev in events:
        d = ev["data"]
        if ev["kind"] == "edit" and d["before"] != d["after"]:
            items.append(f"DRAFT:\n{d['before']['subject']}\n{d['before']['body']}\nEDITED:\n{d['after']['subject']}\n{d['after']['body']}")
        elif ev["kind"] == "rating":
            items.append("RATING: " + ("good draft" if d["value"] > 0 else "poor draft") + f" (hook: {d.get('hook', '')})")
    if not items:
        return []
    return llm.ask("learn", "all", LEARN_PROMPT.format(items="\n\n".join(items[:20])))["rules"][:5]
