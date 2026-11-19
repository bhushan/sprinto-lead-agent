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
Return JSON: subject, body."""


def pain_points(domain, facts, contact):
    p = PAIN_PROMPT.format(name=contact["name"], title=contact["title"], company=domain,
                           product=facts.get("product"), facts=facts)
    return llm.ask("pain_points", domain, p)["pain_points"]


def draft(domain, facts, contact, pains, hook):
    p = DRAFT_PROMPT.format(sender=config.SENDER_NAME, name=contact["name"], title=contact["title"],
                            company=domain, hook=hook, pains=[x["pain"] for x in pains])
    mail = llm.ask("draft", domain, p)
    if not mail["body"].rstrip().endswith(config.SENDER_NAME):  # the model sometimes drops the sign-off
        mail["body"] = mail["body"].rstrip() + f"\n\n{config.SENDER_NAME}"
    return mail
