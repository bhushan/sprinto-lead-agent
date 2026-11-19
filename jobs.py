"""A run of the pipeline as a stored job, advanced one company per request.

A hosted function has a hard time limit, and a browser tab can close mid-run. So a run is a row in the database:
each step() researches one company, saves it and the job's progress, and returns. The browser calls step() until
the job is done; the weekly cron calls drain(), which steps until its time budget is spent and pauses the rest.
A lease stops two tabs (or a tab and the cron) from researching the same company at once.

Statuses: running, paused (the model was unavailable, or the cron ran out of time; resume it), done, stopped.
"""
from datetime import datetime, timedelta, timezone

import contacts
import db
import llm
import run
import shortlist

MAX_PER_RUN = 5          # companies looked at per run: bounds model and Hunter spend
STEP_LEASE = 330         # seconds; longer than the 300 s function limit, so a crashed step frees the job by itself
STALE_MINUTES = 15       # a running job nobody stepped for this long is shown as paused
COMPANY_SECONDS = 100    # drain() starts another company only with at least this much budget left
QUALIFIED = ("drafted", "no_contact", "no_hook", "awaiting_confirm")


class Busy(Exception):
    """Another run is in progress."""


def _now():
    return datetime.now(timezone.utc)


def _stale(job):
    try:
        return _now() - datetime.fromisoformat(job["updated"]) > timedelta(minutes=STALE_MINUTES)
    except (KeyError, TypeError, ValueError):
        return True


def active():
    """The job in progress, if any. A running job that nobody advanced for STALE_MINUTES is marked paused."""
    for job in db.jobs_list(5):
        if job["status"] == "running":
            if not _stale(job):
                return job
            _set(job, "paused", "Nobody advanced this run for a while. Resume it to continue.")
    return None


def _set(job, status, note=None):
    job["status"] = status
    if note is not None:
        job["note"] = note
    db.save_job(job)
    if status in ("done", "stopped") and job["done"] and not job.get("recorded"):
        db.add_run(len(job["done"]), job["drafted"], job["id"])
        job["recorded"] = True
        db.save_job(job)
    return job


def _new(queue, seeds, target, given, discover=False):
    if active():
        raise Busy("A run is already in progress. Wait for it to finish or stop it on the Run page.")
    job = {"id": _now().isoformat(timespec="milliseconds"), "status": "running", "discover": discover,
           "queue": queue[:MAX_PER_RUN], "seeds": {d: seeds.get(d) or {} for d in queue[:MAX_PER_RUN]},
           "done": [], "qualified": 0, "drafted": 0, "target": target, "given": given, "note": "", "last": None}
    db.save_job(job)
    return db.get_job(job["id"])


def start(domains, seeds, target=None, given=None):
    """A run over the given domains (already validated), stopping early once `target` companies qualify."""
    return _new(list(dict.fromkeys(domains)), seeds, target, given)


def start_auto(target, seeds):
    """Find companies for me: research what is waiting in the queue, or discover new companies first."""
    have = {r["domain"] for r in db.companies()}
    waiting = [d for d in {**seeds, **db.shortlist()} if d not in have]
    return _new(waiting, seeds, target, None, discover=not waiting)


def _discover(job):
    """The first step of an automatic run with an empty queue: search the news for new companies."""
    found = shortlist.find(15, budget=150)
    if found:
        db.save_shortlist(shortlist.seeds(found))
    have = {r["domain"] for r in db.companies()}
    seeds = shortlist.seeds(found)
    job["queue"] = [d for d in found if d not in have][:MAX_PER_RUN]
    job["seeds"] = {d: seeds.get(d) or {} for d in job["queue"]}
    job["discover"] = False
    if not job["queue"]:
        return _set(job, "done", "No new companies found in the news right now. Try again later, or run specific domains.")
    job["note"] = f"Found {len(found)} companies in the news; researching up to {len(job['queue'])}."
    return _set(job, "running")


def step(job_id):
    """Advance a running job by one company (or by its discovery search). Returns the job after the step.

    If another request holds the job, nothing is done and the job is returned with busy=True."""
    job = db.get_job(job_id)
    if not job or job["status"] != "running":
        return job
    if not db.claim_job(job_id, STEP_LEASE):
        return {**job, "busy": True}
    try:
        if job.get("discover"):
            return _discover(job)
        if not job["queue"]:
            return _set(job, "done", _summary(job))
        domain = job["queue"][0]
        try:
            row = run.process(domain, contacts.CSV, False, job["seeds"].get(domain), db.learnings(), job.get("given"))
        except llm.ModelUnavailable as ex:
            return _set(job, "paused", f"Paused before {domain}: {ex}. Resume when it is back; nothing was lost.")
        row["run"] = job_id
        db.save_company(run.public(row))
        job["queue"].pop(0)
        job["done"].append(domain)
        job["qualified"] += row["status"] in QUALIFIED
        job["drafted"] += row["status"] == "drafted"
        job["last"] = {"domain": domain, "status": row["status"]}
        if not job["queue"] or (job["target"] and job["qualified"] >= job["target"]):
            return _set(job, "done", _summary(job))
        job["note"] = ""
        return _set(job, "running")
    finally:
        db.release_job(job_id)


def _summary(job):
    return f"Done: {len(job['done'])} companies looked at, {job['qualified']} qualified, {job['drafted']} drafted."


def resume(job_id):
    job = db.get_job(job_id)
    if not job or job["status"] != "paused":
        return job
    if (other := active()) and other["id"] != job_id:
        raise Busy("Another run is in progress.")
    return _set(job, "running", "")


def stop(job_id):
    job = db.get_job(job_id)
    if job and job["status"] in ("running", "paused"):
        _set(job, "stopped", _summary(job).replace("Done", "Stopped"))
    return job


def drain(job_id, budget):
    """Step until the job ends or fewer than COMPANY_SECONDS of `budget` are left; then pause it for a resume."""
    end = _now() + timedelta(seconds=budget)
    job = db.get_job(job_id)
    while job and job["status"] == "running" and not job.get("busy"):
        if (end - _now()).total_seconds() < COMPANY_SECONDS:
            return _set(job, "paused", f"Time ran out with {len(job['queue'])} companies left. Resume it on the Run page.")
        job = step(job_id)
    return job
