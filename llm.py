"""The one place the model is called. Every reply is a JSON object; anything else is a ModelError.

ModelError: this reply was unusable (bad JSON, wrong shape). The company is handed to a human.
ModelUnavailable: the model could not be reached after retries (rate limit, outage, bad key). Not the company's
fault, so a job pauses and keeps the company queued instead of failing it.
"""
import json
import time
from pathlib import Path

import config

_CANNED = Path(__file__).parent / "fixtures" / "llm_responses.json"
TIMEOUT_MS = 60_000
RETRY_WAITS = (2, 5)  # seconds between attempts on a rate limit or server error
_client = None


class ModelError(Exception):
    pass


class ModelUnavailable(Exception):
    pass


def _get_client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=config.API_KEY, http_options=types.HttpOptions(timeout=TIMEOUT_MS))
    return _client


def _call(prompt):
    from google.genai import errors
    last = None
    for wait in (0, *RETRY_WAITS):
        time.sleep(wait)
        try:
            resp = _get_client().models.generate_content(
                model=config.MODEL, contents=prompt,
                config={"temperature": config.TEMPERATURE, "response_mime_type": "application/json"})
            return resp.text
        except errors.APIError as ex:
            last = ex
            if ex.code in (400, 401, 403, 404):  # a bad key or model name will not fix itself
                raise ModelUnavailable(f"the model refused the request (http {ex.code}); check GEMINI_API_KEY and GEMINI_MODEL") from ex
            if ex.code not in (408, 429, 500, 502, 503, 504):
                raise ModelError(f"model error (http {ex.code})") from ex
        except Exception as ex:  # timeouts and connection errors from the HTTP client
            last = ex
    code = getattr(last, "code", None)
    raise ModelUnavailable("the model is rate limited, try again in a minute" if code == 429
                           else f"the model did not answer ({type(last).__name__})")


def ask(step, domain, prompt, key=None):
    """Return the model's JSON reply as a dict. key picks the sub-reply (e.g. which page was read)."""
    if config.offline():
        try:
            reply = json.loads(_CANNED.read_text(encoding="utf-8"))[step][domain]
            reply = reply[key] if key else reply
        except KeyError:
            raise ModelError(f"no canned {step} reply for {domain}") from None
    else:
        text = _call(prompt)
        try:
            reply = json.loads(text or "")
        except ValueError:
            raise ModelError(f"the {step} reply was not JSON") from None
    if isinstance(reply, list) and len(reply) == 1 and isinstance(reply[0], dict):
        reply = reply[0]  # the model sometimes wraps the object in a list
    if not isinstance(reply, dict):
        raise ModelError(f"the {step} reply was not a JSON object")
    return reply


def number(v, lo, hi):
    """An int clamped to [lo, hi], or None. Accepts "88" and 88.0."""
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return None
