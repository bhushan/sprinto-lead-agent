import llm

FIELDS = ("rejected_quotes", "company_name", "product", "buyer", "is_saas", "certifications", "funding_stage",
          "headcount", "open_roles", "enterprise_tier", "compliance_signals")

PROMPT = """Extract facts from this company web page as JSON with exactly these keys:
company_name (the company's own name as its page writes it, e.g. "Gridcog"; never the web address),
product (one line), buyer (who buys it, e.g. "logistics teams"), is_saas (bool: a software
product sold on subscription to businesses), certifications (security certifications the
company says it HOLDS, e.g. "SOC 2 Type II"; not ones it is working toward), funding_stage,
headcount (int), open_roles (int, number of open jobs listed), enterprise_tier (bool: pricing
page has an enterprise plan), compliance_signals (list of sentences COPIED EXACTLY from the page
that show audit preparation or customers demanding certification).
Use null (or [] for lists) when the page does not say. Do not infer.

PAGE:
{text}"""


def _norm(s):
    return " ".join(s.lower().split())


def _clean(raw):
    """Coerce each field to its type; a value of the wrong type is treated as unknown."""
    out = {}
    for k in FIELDS[1:]:
        v = raw.get(k)
        if k in ("is_saas", "enterprise_tier"):
            v = v if isinstance(v, bool) else None
        elif k in ("headcount", "open_roles"):
            v = llm.number(v, 0, 10_000_000)
        elif k in ("certifications", "compliance_signals"):
            v = [v] if isinstance(v, str) else v
            v = [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
        else:
            v = " ".join(str(v).split())[:300] if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip() else None
        out[k] = v
    return out


def extract(domain, page, text):
    out = _clean(llm.ask("extract", domain, PROMPT.format(text=text), key=page))
    # a quote the page does not contain is a hallucination: drop it, count it
    seen = _norm(text)
    claimed = out["compliance_signals"] or []
    out["compliance_signals"] = [s for s in claimed if _norm(s) in seen]
    out["rejected_quotes"] = len(claimed) - len(out["compliance_signals"])
    return out
