"""Checks for the cases that break a hosted run: bad model replies, rate limits, provider errors, concurrent
requests, oversized pages, step-by-step jobs and the sign-in gate. Run: python test_stability.py"""
import importlib.util
import os
import tempfile
import threading

os.environ["OFFLINE"] = "1"
os.environ["DATABASE_URL"] = os.environ["POSTGRES_URL"] = ""  # never touch a real database from a test
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("VERCEL_ENV", None)

import config  # noqa: E402
import contacts  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
import llm  # noqa: E402
import run  # noqa: E402
import scrape  # noqa: E402
import settings  # noqa: E402
import shortlist  # noqa: E402

settings.apply(settings.DEFAULTS)
real_ask = llm.ask


def patched_ask(replies):
    """Replace one step's model reply; the other steps keep the canned replies."""
    def ask(step, domain, prompt, key=None):
        if step in replies:
            r = replies[step]
            if isinstance(r, Exception):
                raise r
            return r
        return real_ask(step, domain, prompt, key)
    return ask


# 1. A malformed model reply ends that company as a hand-over with the reason, never a crash
llm.ask = patched_ask({"qualify": {"reason": "no score here"}})
r = run.process("finlytic.example")
assert r["status"] == "error" and r["needs_human"] and "qualify" in r["reason"], r
llm.ask = patched_ask({"qualify": {"score": "88", "reason": "fits"}})  # a numeric string is fine
assert run.process("finlytic.example")["score"] == 88
llm.ask = patched_ask({"qualify": {"score": 900, "reason": "fits"}})  # clamped to the 0-100 scale
assert run.process("finlytic.example")["score"] == 100
llm.ask = patched_ask({"pain_points": {"pain_points": [{"pain": "x"}, "junk"]}})  # unusable pain points
assert run.process("finlytic.example")["status"] == "no_hook"
llm.ask = patched_ask({"draft": {"subject": None}})
assert run.process("finlytic.example")["status"] == "error"

# 2. The model being unavailable (rate limit, outage) is not the company's fault: it propagates so a job can pause
llm.ask = patched_ask({"extract": llm.ModelUnavailable("rate limited")})
try:
    run.process("finlytic.example")
    raise SystemExit("ModelUnavailable was swallowed")
except llm.ModelUnavailable:
    pass
llm.ask = real_ask

# 3. Offline is per request: forcing it in one thread does not change another thread
config.OFFLINE = False
seen = {}


def worker():
    with config.forced_offline():
        seen["inner"] = config.offline()
        threading.Event().wait(0.05)


t = threading.Thread(target=worker)
t.start()
seen["outer"] = config.offline()
t.join()
assert seen == {"inner": True, "outer": False}, seen
config.OFFLINE = True

# 4. Hunter failures are reported as such, not as "nobody owns security"
config.HUNTER_API_KEY = "k"
contacts._hunter = lambda d: (_ for _ in ()).throw(contacts.LookupFailed("Hunter search failed: monthly quota used up"))
with config.forced_offline(False):
    c, why = contacts.find_contact("finlytic.example", 120)
assert c is None and "quota" in why, why
config.HUNTER_API_KEY = ""

# 5. Pages: private addresses, non-HTML and oversized bodies are refused
assert scrape.is_public_ip("8.8.8.8") and not scrape.is_public_ip("10.0.0.5") and not scrape.is_public_ip("169.254.169.254")


class FakeResp:
    def __init__(self, ctype, size, status=200):
        self.status_code, self.headers, self.size, self.url, self.encoding = status, {"Content-Type": ctype}, size, "https://x.com/", "utf-8"

    def iter_content(self, n):
        sent = 0
        while sent < self.size:
            sent += n
            yield b"<p>" + b"a" * (n - 3)

    def close(self):
        pass


assert scrape.read_body(FakeResp("application/pdf", 10))[1].startswith("not a web page")
text, note = scrape.read_body(FakeResp("text/html", 50_000_000))
assert len(text) <= scrape.MAX_BYTES and note == ""
# the page cache expires, so a warm server does not serve stale pages forever
scrape._cache[("old.example", "home")] = (0, ("stale", ""))
assert scrape._cached("old.example", "home") is None

# 6. Google News items with a missing or broken date are skipped, not fatal
assert shortlist.parse_date(None) is None and shortlist.parse_date("not a date") is None
assert shortlist.parse_date("Tue, 10 Nov 2026 08:00:00 GMT").year == 2026

# 7. Jobs: one company per step, saved as it goes, stops at the target, records the run when done
job = jobs.start(["finlytic.example", "brickbox.example", "cartwheel.example", "zenstack.example"], {}, target=2)
assert job["status"] == "running" and job["queue"][0] == "finlytic.example"
assert jobs.active()["id"] == job["id"]
try:
    jobs.start(["orbitdocs.example"], {})
    raise SystemExit("a second job started while one was running")
except jobs.Busy:
    pass
job = jobs.step(job["id"])
assert job["done"] == ["finlytic.example"] and job["qualified"] == 1
assert any(x["domain"] == "finlytic.example" and x["run"] == job["id"] for x in db.companies())
job = jobs.step(job["id"])
job = jobs.step(job["id"])  # cartwheel qualifies: target of 2 reached
assert job["status"] == "done" and job["qualified"] == 2 and "zenstack.example" not in job["done"], job
assert db.runs(1)[0]["ts"] == job["id"] and jobs.active() is None
assert jobs.step(job["id"])["status"] == "done"  # stepping a finished job is a no-op

# 8. A rate limit pauses the job and keeps the company queued for the resume
job = jobs.start(["orbitdocs.example", "vaultly.example"], {})
llm.ask = patched_ask({"extract": llm.ModelUnavailable("rate limited")})
job = jobs.step(job["id"])
assert job["status"] == "paused" and job["queue"] == ["orbitdocs.example", "vaultly.example"] and "rate" in job["note"], job
llm.ask = real_ask
job = jobs.resume(job["id"])
job = jobs.drain(job["id"], budget=1000)
assert job["status"] == "done" and job["done"] == ["orbitdocs.example", "vaultly.example"]

# 9. Two steps on the same job at once: only one claims the company
job = jobs.start(["mistline.example", "zenstack.example"], {})
assert db.claim_job(job["id"], 60) and not db.claim_job(job["id"], 60)
db.release_job(job["id"])
jobs.stop(job["id"])
assert db.get_job(job["id"])["status"] == "stopped" and jobs.active() is None

# 10. The web app: sign-in gate and every page renders on a fresh database
spec = importlib.util.spec_from_file_location("webapp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "api", "index.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
assert not hasattr(app, "APP_PASSWORD") and "prompt(" not in app.JS  # open to every reviewer, no sign-in
app.config.API_KEY, app.MAX_RUNS_PER_HOUR = "test-key", 100  # still offline: the app decided at import
code, out = app.post("/?p=nope", b"{}")
assert code == 404
code, out = app.post("/?p=run", b"not json")
assert code == 400 and "error" in out
code, out = app.post("/?p=run", b'{"domains": ["localhost"]}')
assert code == 400, (code, out)
code, out = app.post("/?p=run", b'{"domains": ["finlytic.example"]}')
assert code == 200 and out["job"]["status"] == "running", out
code, out = app.post("/?p=step", ('{"id": "%s"}' % out["job"]["id"]).encode())
assert code == 200 and out["job"]["status"] == "done", out
app.MAX_RUNS_PER_HOUR = 0
assert app.post("/?p=run", b'{"domains": ["finlytic.example"]}')[0] == 429  # the spend cap holds without sign-in
app.MAX_RUNS_PER_HOUR = 100
db.save_company({"domain": "broken.example", "status": "error", "reason": "Stopped at qualify: bad reply", "needs_human": True,
                 "flags": [], "fetches": 0})
for tab in ("overview", "companies", "review", "drafts", "feedback", "data", "prospects", "settings", "method", "run"):
    html = app.page(f"/?p={tab}")
    assert html.startswith("<!doctype html>") and "Traceback" not in html, tab
assert "broken.example" in app.page("/?p=review")
assert "Not found" in app.page("/?p=company&d=nope.example")
print("ok")
