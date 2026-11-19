"""Tiny storage layer: Postgres when DATABASE_URL is set (Vercel + Neon), a SQLite file when SQLITE_PATH is set (local).
With neither, `enabled` is False and the web app stays read-only on data/snapshot.json."""
import json
import os
from datetime import datetime, timezone

URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
PATH = os.getenv("SQLITE_PATH")
enabled = bool(URL or PATH)

SCHEMA = [
    "create table if not exists companies(domain text primary key, data text, updated text)",
    "create table if not exists events(ts text, domain text, kind text, data text)",
    "create table if not exists learnings(ts text, rule text)",
    "create table if not exists runs(ts text, n integer, drafted integer)",
]
_ready = False


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sql(sql, args=()):
    global _ready
    if URL:
        import psycopg
        c = psycopg.connect(URL, autocommit=True, connect_timeout=10)
    else:
        import sqlite3
        c = sqlite3.connect(PATH)
    try:
        cur = c.cursor()
        if not _ready:
            for s in SCHEMA:
                cur.execute(s)
            _ready = True
        cur.execute(sql.replace("?", "%s") if URL else sql, args)
        rows = cur.fetchall() if cur.description else []
        if not URL:
            c.commit()
        return rows
    finally:
        c.close()


def companies():
    return [json.loads(d) for (d,) in _sql("select data from companies order by updated desc")]


def save_company(row):
    _sql("insert into companies(domain, data, updated) values(?, ?, ?) on conflict(domain) do update set "
         "data = excluded.data, updated = excluded.updated", (row["domain"], json.dumps(row, ensure_ascii=False), _now()))


def last_update():
    r = _sql("select max(updated) from companies")
    return r[0][0] if r else None


def add_event(domain, kind, data):
    _sql("insert into events(ts, domain, kind, data) values(?, ?, ?, ?)", (_now(), domain, kind, json.dumps(data, ensure_ascii=False)))


def events(limit=50):
    return [{"ts": t, "domain": d, "kind": k, "data": json.loads(x)}
            for t, d, k, x in _sql("select ts, domain, kind, data from events order by ts desc limit " + str(int(limit)))]


def learnings():
    return [r for (r,) in _sql("select rule from learnings order by ts")]


def set_learnings(rules):
    _sql("delete from learnings")
    for r in rules:
        _sql("insert into learnings(ts, rule) values(?, ?)", (_now(), r))


def add_run(n, drafted):
    _sql("insert into runs(ts, n, drafted) values(?, ?, ?)", (_now(), n, drafted))


def runs(limit=10):
    return [{"ts": t, "n": n, "drafted": d} for t, n, d in _sql("select ts, n, drafted from runs order by ts desc limit " + str(int(limit)))]
