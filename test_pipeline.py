"""Offline smoke check: each Evidence Map path ends where it should. Run: python test_pipeline.py"""
import os
import tempfile
os.environ["OFFLINE"] = "1"
os.environ["DATABASE_URL"] = os.environ["POSTGRES_URL"] = ""  # never touch a real database from a test
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
import run

EXPECT = {  # domain: (status, fetches beyond homepage)
    "finlytic.example": ("drafted", 1), "cartwheel.example": ("drafted", 2), "zenstack.example": ("drafted", 1),
    "orbitdocs.example": ("not_qualified", 1), "brickbox.example": ("disqualified", 0),
    "vaultly.example": ("disqualified", 0), "trustco.example": ("disqualified", 1), "mistline.example": ("manual_review", 2), "ghostco.example": ("failed", 0),
}
for d, (status, fetches) in EXPECT.items():
    r = run.process(d)
    assert (r["status"], r["fetches"]) == (status, fetches), (d, r["status"], r["fetches"], r["reason"])
    assert r["fetches"] <= 4
assert run.process("finlytic.example")["contact"]["name"] == "Rhea Menon"
assert run.process("cartwheel.example")["contact"]["name"] == "Arjun Iyer"  # not the Office Manager
import extract
assert extract.extract("finlytic.example", "home", "unrelated page text")["compliance_signals"] == []  # unverifiable quotes are dropped
assert "Vanta" in run.process("trustco.example")["reason"]
import db  # storage round trip: company upsert, events, learnings
db.save_company(run.public(run.process("finlytic.example", lessons=["Keep it short."])))
db.save_company({"domain": "finlytic.example", "status": "x"})
assert [r["status"] for r in db.companies()] == ["x"]
db.add_event("finlytic.example", "rating", {"value": 1})
assert db.events()[0]["data"] == {"value": 1}
db.set_learnings(["a", "b"]); db.set_learnings(["c"])
assert db.learnings() == ["c"]
assert run.process("finlytic.example")["facts"]["company_name"] == "Finlytic"
import drafts
assert drafts.company("hyphenate.ai", {}) == "Hyphenate"
import settings  # ICP settings: bad input is rejected, a stricter threshold changes the outcome, defaults restore it
try:
    settings.validate({**settings.DEFAULTS, "threshold": 150}); raise SystemExit("threshold 150 was accepted")
except ValueError:
    pass
settings.apply(settings.validate({**settings.DEFAULTS, "threshold": 90}))
assert run.process("finlytic.example")["status"] == "not_qualified"
settings.apply(settings.DEFAULTS)
assert run.process("finlytic.example")["status"] == "drafted"
import shortlist  # prospect search: keywords become queries; bad keywords and empty regions are rejected
settings.apply(settings.validate({**settings.DEFAULTS, "keywords": "cybersecurity, healthtech"}))
assert len(shortlist.queries()) == 2 and "cybersecurity" in shortlist.queries()[0]
for bad in ({"keywords": "a;b"}, {"keywords": "a,b,c,d,e,f"}, {"regions": []}):
    try:
        settings.validate({**settings.DEFAULTS, **bad}); raise SystemExit(f"{bad} was accepted")
    except ValueError:
        pass
settings.apply(settings.DEFAULTS)
assert shortlist.queries() == shortlist.QUERIES
db.save_shortlist({"a.com": {"funding_stage": "Series A"}}); db.save_shortlist({"a.com": {}, "b.com": {}}, keep_existing=True)
assert db.shortlist() == {"a.com": {"funding_stage": "Series A"}, "b.com": {}}
print("ok")
