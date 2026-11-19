# Outreach Agent

Cold outreach for compliance automation: **discover, scrape, qualify, find the contact, draft, approve, send.**
The app finds and scores. The LLM writes. A human approves every send.

**Live app:** [sprinto-lead-agent-omega.vercel.app](https://sprinto-lead-agent-omega.vercel.app) (open to reviewers, no sign-in)

Built for MESA Case 2 (Sprinto). Case study build, not affiliated with Sprinto.

## Where to look first

| Page | What it shows |
|---|---|
| [Overview](https://sprinto-lead-agent-omega.vercel.app/?p=overview) | Funnel from news to sent email, why companies dropped, run selector |
| [Companies](https://sprinto-lead-agent-omega.vercel.app/?p=companies) | Every researched company with its outcome, tier and fit score. Open one for the evidence trail |
| [Run pipeline](https://sprinto-lead-agent-omega.vercel.app/?p=run) | Find companies for me, run specific domains, or [the offline demo](https://sprinto-lead-agent-omega.vercel.app/?p=run&demo=1) on fictional companies |
| [Review queue](https://sprinto-lead-agent-omega.vercel.app/?p=review) | What the rules could not settle, with the action for each |
| [Drafts and sends](https://sprinto-lead-agent-omega.vercel.app/?p=drafts) | Edit, rate Good or Poor, approve and send |
| [CRM log](https://sprinto-lead-agent-omega.vercel.app/?p=crm) | Every approved email, how it went out, the day-14 follow-up and replies |
| [Method](https://sprinto-lead-agent-omega.vercel.app/?p=method) | The Evidence Map, the nine checks and the five pre-build decisions |

## Component map

| Actor | Role |
|---|---|
| Growth Lead (human) | Sets ICP, keywords and regions; adds own websites and contacts; hits Run (or the Monday run does) |
| Discovery (Google News) | Funding rounds and security or compliance hires, queued with the headline as evidence |
| App (Python) | Scrapes the site and extra pages (robots-aware), drops early, scores /100, tiers A/B/C |
| LLM (Gemini) | Extracts facts and quotes (code verifies each quote is on the page); writes two pain points and an email under 90 words (code checks it, one rewrite) |
| Database (Neon Postgres) | ICP settings, my contacts, rules learned from edits, companies with verdict and evidence trail, runs |
| Hunter.io | Domain search for a named contact, skipped when you added one yourself |
| Review queue (human) | Flagged companies: add a contact, re-run. Optionally confirm fit and contact before any email is drafted |
| Send (Gmail) | Approved drafts go to your own inbox only, 10 a day; mail-app fallback. Day-14 follow-up and CRM log |

## How each step works

1. **Discover** (`shortlist.py`): Google News funding and hiring stories, filtered by stage, sector and age. A company
   counts only when its homepage title names it and the page shares a topic word with the headline. Feeds and
   homepages are read in parallel within a time budget.
2. **Scrape** (`scrape.py`): one cached fetch per page, robots.txt respected, 1 s delay, public addresses only,
   web pages only, at most 1.5 MB read. HTML is cleaned to text. A failure records its reason and goes to a human.
3. **Extract** (`extract.py`): the model fills named fields, each coerced to its type. A quote that is not on the
   page word for word is dropped and counted.
4. **Qualify** (`qualify.py`): an agent loop driven by the Evidence Map. Code, not the model, decides when the
   evidence is enough, which page to fetch next and when to stop (worst case 4 fetches beyond the homepage).

   | Question | Look at | Limit | Unanswered | Stop early |
   |---|---|---|---|---|
   | Already certified? | /security, /trust | 1 | treat as not certified, flag | yes: disqualify |
   | B2B SaaS? | /product, /about | 1 | manual review | yes: disqualify |
   | Right stage? | /careers, /pricing, /customers | 2 | qualify at lower rank (-10), flag | no |

   The model then scores ICP fit 0 to 100. Tier A at 85 or more, B from 70, C is a near miss within 15 below.
   Only A and B get an email. A trust center on Vanta, Drata or Sprinto disqualifies.
5. **Contact** (`contacts.py`): your own contacts first, else Hunter.io Domain Search, ranked by who owns security
   work (security and compliance, then engineering leadership, then founders under 50 people). A failed Hunter
   search (quota, bad key) is reported as such, not as "nobody found".
6. **Pain points and draft** (`drafts.py`): exactly two pain points, each backed by a fact. The email opens with the
   strongest hook and is checked in code: first name, company name, under about 90 words, one soft question, no
   stock phrases, no invented numbers, no claim that the company lacks a certification.
7. **Review and send** (`send.py`): a human edits and approves. Edits and ratings feed "Learn from feedback", which
   turns them into at most 5 style rules added to every new draft.
8. **Confirm before drafting** (ICP settings, off by default): the run stops once the contact is found, and the
   Review queue shows the fit reason and the chosen person with **Confirm and draft** or **Drop it**. Drafting then
   uses the stored research, with no new page fetches.
9. **Follow-up and CRM log** (`crm.py`): every approved email is logged with its channel (inbox or mail app). A first
   email with no reply after 14 days is due a follow-up under 60 words with a new angle, checked in code (no "just
   following up"), and waits for approval like every other email. **Mark replied** stops it. The Monday run drafts
   due follow-ups too.

## Running on Vercel

One Python function (`api/index.py`, no framework) on Fluid Compute, Neon Postgres from the Vercel Marketplace, and
a weekly Vercel Cron.

- **Runs are jobs** (`jobs.py`). A run is a database row; each request researches one company, saves it and the
  progress, and returns, so no request nears the 300 s limit. The browser advances the job and shows progress.
  If the tab closes or a step times out, nothing is lost: Run pipeline shows **Continue it here** and **Stop it**.
  A lease stops two tabs, or a tab and the cron, from researching the same company.
- **Failures are contained.** An unusable model reply or an unexpected page ends that company as "Needs a human"
  with the reason. A model rate limit or outage pauses the job and keeps the company queued for a resume.
- **Monday run:** `vercel.json` schedules the cron every Monday 05:00 UTC. It continues a paused run or starts a new
  one, works for up to 270 s and pauses whatever is left. The route only accepts Vercel's `CRON_SECRET` header.
- **Open to reviewers, spend capped:** there is no sign-in. Instead: 3 new runs an hour, 5 companies a run, 10 emails
  a day, and mail only ever goes to your own address. Contact emails are stored but never shown.
- **Per-request state:** the offline demo forces fixtures for its own request only; the page cache expires after
  10 minutes; one database connection per server process, reopened if Neon closes it.

### Environment variables

| Variable | Why |
|---|---|
| `DATABASE_URL` | Neon Postgres. Add Neon from the Vercel Marketplace (Storage tab) and it is set for you |
| `GEMINI_API_KEY` or `OPENROUTER_API_KEY` | Live runs and learning. A Google key calls Gemini directly; an OpenRouter key (`sk-or-...`) sends the same prompts to the same Gemini model through OpenRouter. `GEMINI_MODEL` overrides the model (default `gemini-3.6-flash`) |
| `HUNTER_API_KEY` | Contacts for live runs (one search per company that qualifies) |
| `TO_ADDRESS`, `GMAIL_APP_PASSWORD` | Optional. Send approved drafts to your own inbox (Google app password, needs 2-step verification). Without them the button opens your mail app |
| `CRON_SECRET` | Set. Vercel sends it to the weekly run |

Without a database the site stays read-only on `data/snapshot.json` (the last live run) and the offline demo works.

## Local

```
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env               # OFFLINE=1 needs no keys
.venv/bin/python run.py            # data/domains.txt -> outbox/ and runs/run_log.jsonl
.venv/bin/python test_pipeline.py  # one check per Evidence Map path
.venv/bin/python test_stability.py # bad model replies, rate limits, jobs, limits, every page renders
```

OFFLINE=1 reads cached pages from `fixtures/pages` and canned model replies from `fixtures/llm_responses.json`.
No network, no cost. Company names in the fixtures are fictional. Set `SQLITE_PATH=/tmp/outreach.db` to try the
database features on SQLite. `python shortlist.py 20` refreshes the shortlist; `python snapshot.py` turns the last
run log into `data/snapshot.json`.
