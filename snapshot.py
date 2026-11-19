"""Turn runs/run_log.jsonl into data/snapshot.json, the data the web app shows.

Latest row per domain wins. Contact emails and passed-over names are dropped so the public page
carries job titles and reasons, not other people's contact details.
Run after a live run: python snapshot.py
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import config

rows = {}
for line in Path("runs/run_log.jsonl").read_text(encoding="utf-8").splitlines():
    r = json.loads(line)
    rows[r["domain"]] = r
for r in rows.values():
    c = r.get("contact")
    if c:
        c.pop("email", None)
        c["why"] = re.sub(r"\. Passed over: (.*)$", lambda m: f". Passed over {m.group(1).count('),') + 1} others.", c["why"])

hunter_left = None
if config.HUNTER_API_KEY:
    import requests
    try:
        acct = requests.get("https://api.hunter.io/v2/account", params={"api_key": config.HUNTER_API_KEY}, timeout=10).json()
        hunter_left = acct["data"]["requests"]["searches"]["remaining"]
    except (requests.RequestException, KeyError, ValueError):
        pass

Path("data").mkdir(exist_ok=True)
Path("data/snapshot.json").write_text(json.dumps(
    {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "hunter_left": hunter_left,
     "rows": list(rows.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{len(rows)} companies -> data/snapshot.json")
