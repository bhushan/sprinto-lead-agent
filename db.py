"""Tiny storage layer: Postgres when DATABASE_URL is set (Vercel + Neon), a SQLite file when SQLITE_PATH is set (local).
With neither, `enabled` is False and the web app stays read-only on data/snapshot.json."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
PATH = os.getenv("SQLITE_PATH")
enabled = bool(URL or PATH)

SCHEMA = [
    "create table if not exists companies(domain text primary key, data text, updated text)",
    "create table if not exists events(ts text, domain text, kind text, data text)",
    "create table if not exists learnings(ts text, rule text)",
    "create table if not exists runs(ts text, n integer, drafted integer)",
    "create table if not exists shortlist(domain text primary key, data text)",
    "create table if not exists people(domain text, name text, title text, email text, primary key(domain, name))",
    "create table if not exists settings(key text primary key, data text, updated text)",
    "create table if not exists jobs(id text primary key, status text, data text, lease text, updated text)",
]
_ready = False
_conn = None
_lock = threading.Lock()  # one connection per server process, used by one query at a time


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect():
    if URL:
        import psycopg
        return psycopg.connect(URL, autocommit=True, connect_timeout=10, prepare_threshold=None)
    import sqlite3
    return sqlite3.connect(PATH, timeout=15, check_same_thread=False)


def _run(sql, args):
    global _conn, _ready
    if _conn is None:
        _conn = _connect()
    cur = _conn.cursor()
    if not _ready:
        for s in SCHEMA:
            cur.execute(s)
        _ready = True
    cur.execute(sql.replace("?", "%s") if URL else sql, args)
    rows = cur.fetchall() if cur.description else []
    if not URL:
        _conn.commit()
    return rows


def _sql(sql, args=()):
    """Run one statement on the shared connection. A connection the database closed (idle timeout, restart) is
    reopened once and the statement retried."""
    global _conn
    with _lock:
        try:
            return _run(sql, args)
        except Exception as ex:
            if not _is_connection_error(ex):
                raise
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            return _run(sql, args)


def _is_connection_error(ex):
    if URL:
        import psycopg
        return isinstance(ex, (psycopg.OperationalError, psycopg.InterfaceError))
    return False


def companies():
    return [json.loads(d) for (d,) in _sql("select data from companies order by updated desc")]


def save_company(row):
    _sql("insert into companies(domain, data, updated) values(?, ?, ?) on conflict(domain) do update set "
         "data = excluded.data, updated = excluded.updated", (row["domain"], json.dumps(row, ensure_ascii=False), _now()))


def last_update():
    r = _sql("select max(updated) from companies")
    return r[0][0] if r else None


def add_event(domain, kind, data, ts=None):
    _sql("insert into events(ts, domain, kind, data) values(?, ?, ?, ?)", (ts or _now(), domain, kind, json.dumps(data, ensure_ascii=False)))


def events(limit=50):
    return [{"ts": t, "domain": d, "kind": k, "data": json.loads(x)}
            for t, d, k, x in _sql("select ts, domain, kind, data from events order by ts desc limit " + str(int(limit)))]


def events_of(kinds):
    """Every event of these kinds, oldest first (the CRM log)."""
    marks = ", ".join("?" for _ in kinds)
    return [{"ts": t, "domain": d, "kind": k, "data": json.loads(x)}
            for t, d, k, x in _sql(f"select ts, domain, kind, data from events where kind in ({marks}) order by ts", tuple(kinds))]


def learnings():
    return [r for (r,) in _sql("select rule from learnings order by ts")]


def sent_since(iso):
    """Emails actually sent from the site (first emails and follow-ups) since iso."""
    r = _sql("select count(*) from events where kind in ('sent', 'followup_sent') and ts > ?", (iso,))
    return r[0][0] if r else 0


def set_learnings(rules):
    _sql("delete from learnings")
    for r in rules:
        _sql("insert into learnings(ts, rule) values(?, ?)", (_now(), r))


def now():
    return _now()


def add_run(n, drafted, ts=None):
    _sql("insert into runs(ts, n, drafted) values(?, ?, ?)", (ts or _now(), n, drafted))


def runs(limit=10):
    return [{"ts": t, "n": n, "drafted": d} for t, n, d in _sql("select ts, n, drafted from runs order by ts desc limit " + str(int(limit)))]


def save_shortlist(seeds, keep_existing=False):
    """seeds: {domain: evidence dict}. Adds to the stored shortlist; keep_existing leaves known domains untouched."""
    for d, v in seeds.items():
        _sql("insert into shortlist(domain, data) values(?, ?) on conflict(domain) do "
             + ("nothing" if keep_existing else "update set data = excluded.data"), (d, json.dumps(v, ensure_ascii=False)))


def shortlist():
    return {d: json.loads(x) for d, x in _sql("select domain, data from shortlist")}


def get_settings():
    r = _sql("select data from settings where key = ?", ("icp",))
    return json.loads(r[0][0]) if r else None


def set_settings(s):
    """s: validated settings dict, or None to go back to the defaults."""
    if s is None:
        _sql("delete from settings where key = ?", ("icp",))
    else:
        _sql("insert into settings(key, data, updated) values(?, ?, ?) on conflict(key) do update set "
             "data = excluded.data, updated = excluded.updated", ("icp", json.dumps(s), _now()))


def add_people(rows):
    """rows: [(domain, name, title, email)]. Adding the same name at a domain again updates the title and email."""
    for d, n, t, em in rows:
        _sql("insert into people(domain, name, title, email) values(?, ?, ?, ?) on conflict(domain, name) do update set "
             "title = excluded.title, email = excluded.email", (d, n, t, em))


def people(domain):
    return [{"name": n, "title": t, "email": em} for n, t, em in
            _sql("select name, title, email from people where domain = ? order by name", (domain,))]


def all_people():
    return [{"domain": d, "name": n, "title": t, "has_email": bool(em)} for d, n, t, em in
            _sql("select domain, name, title, email from people order by domain, name")]


def remove_person(domain, name):
    _sql("delete from people where domain = ? and name = ?", (domain, name))


# Jobs: a run of the pipeline, advanced one company per request so no request nears the hosting time limit.

def save_job(job):
    _sql("insert into jobs(id, status, data, updated) values(?, ?, ?, ?) on conflict(id) do update set "
         "status = excluded.status, data = excluded.data, updated = excluded.updated",
         (job["id"], job["status"], json.dumps(job, ensure_ascii=False), _now()))


def get_job(job_id):
    r = _sql("select data, updated from jobs where id = ?", (job_id,))
    return {**json.loads(r[0][0]), "updated": r[0][1]} if r else None


def jobs_list(limit=10):
    return [{**json.loads(d), "updated": u} for d, u in
            _sql("select data, updated from jobs order by id desc limit " + str(int(limit)))]


def jobs_since(iso):
    r = _sql("select count(*) from jobs where id > ?", (iso,))
    return r[0][0] if r else 0


def claim_job(job_id, seconds):
    """Take the job for `seconds` so a second tab or the cron cannot process the same company. True if taken."""
    now = datetime.now(timezone.utc)
    until = (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    r = _sql("update jobs set lease = ? where id = ? and (lease is null or lease < ?) returning id",
             (until, job_id, now.isoformat(timespec="seconds")))
    return bool(r)


def release_job(job_id):
    _sql("update jobs set lease = null where id = ?", (job_id,))
