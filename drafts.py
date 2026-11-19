"""Steps 5 and 6: two likely pain points for this person, then a personalised email."""
import config
import llm

PAIN_PROMPT = """{name} is {title} at {company} ({product}). Facts we know: {facts}
Name the two most likely pain points for someone in this role right now. Each must be backed by a
fact above. Return JSON: pain_points (list of exactly 2 objects with keys pain, evidence)."""

DRAFT_PROMPT = """Write a cold email from {sender} at Sprinto to {name}, {title} at {company}.
Address them by first name. The first sentence must be about something specific to {company},
built on this hook (paraphrase it, do not quote it back flatly): {hook}
Speak to these pain points: {pains}
Use only these claims about Sprinto, and pick one: it automates evidence collection through
integrations; it maps one set of controls across SOC 2, ISO 27001 and GDPR; it helps a team get
audit-ready faster than a spreadsheet process; it removes the security questionnaire block on enterprise deals.
Under 90 words, one soft ask for a short call. No greeting filler, no hype, no invented numbers or customers.
{lessons}Return JSON: subject, body."""


def company(domain, facts):
    return facts.get("company_name") or domain.split(".")[0].title()


def pain_points(domain, facts, contact):
    p = PAIN_PROMPT.format(name=contact["name"], title=contact["title"], company=company(domain, facts),
                           product=facts.get("product"), facts=facts)
    return llm.ask("pain_points", domain, p)["pain_points"]


def draft(domain, facts, contact, pains, hook, lessons=None):
    p = DRAFT_PROMPT.format(sender=config.SENDER_NAME, name=contact["name"], title=contact["title"],
                            company=company(domain, facts), hook=hook, pains=[x["pain"] for x in pains],
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
