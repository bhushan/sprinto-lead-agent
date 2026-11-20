import os
from contextlib import contextmanager
from contextvars import ContextVar

from dotenv import load_dotenv

load_dotenv()

OFFLINE = os.getenv("OFFLINE", "1") == "1"  # process default; read it through offline()
_forced = ContextVar("offline", default=None)


def offline():
    """True when this request runs on fixtures. A request can force it (the demo) without changing other requests."""
    v = _forced.get()
    return OFFLINE if v is None else v


@contextmanager
def forced_offline(flag=True):
    token = _forced.set(flag)
    try:
        yield
    finally:
        _forced.reset(token)

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")  # an sk-or- key goes through OpenRouter
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")  # Option A for step 4; empty = use data/contacts.csv (Option B)
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
TEMPERATURE = 0.3

TIER_A = 85              # score at or above this is tier A; the qualifying score is tier B
QUALIFY_THRESHOLD = 70      # minimum ICP fit (0-100, same scale as the score) to email
STAGE_UNKNOWN_PENALTY = 10  # Evidence Map: unanswered stage = "qualify at lower rank, flagged"
TO_ADDRESS = os.getenv("TO_ADDRESS", "growth.lead@example.com")  # case rule: all mail goes to your own inbox
SENDER_NAME = os.getenv("SENDER_NAME", "Bhushan")

USER_AGENT = "sprinto-outreach-research-bot/1.0"
FETCH_DELAY = 1.0  # seconds between live requests
THIN_WORDS = 200   # a live page with fewer words may be JavaScript-rendered
