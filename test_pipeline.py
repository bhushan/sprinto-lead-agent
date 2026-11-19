"""Offline smoke check: each Evidence Map path ends where it should. Run: python test_pipeline.py"""
import os
import tempfile
os.environ["OFFLINE"] = "1"
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
print("ok")
