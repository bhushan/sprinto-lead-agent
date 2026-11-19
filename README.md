# sprinto-outreach

7-step cold-outreach pipeline for Sprinto (MESA Case 2): scrape, extract, qualify
(agent loop), find the contact, pain points, draft, send.

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # OFFLINE=1 needs no keys
python run.py               # reads data/domains.txt, writes outbox/ and runs/run_log.jsonl
python test_pipeline.py     # one check per Evidence Map path
```

OFFLINE=1 reads cached pages from fixtures/pages and canned model replies from
fixtures/llm_responses.json. No network, no cost. Company names are fictional.

## How each step works

1. **Scrape** (`scrape.py`): single fetch per page, cached, robots.txt respected, 1s delay, HTML cleaned
   to text (nav, scripts and forms dropped). A failure records its reason and the company goes to a human.
2. **Extract** (`extract.py`): the model fills named fields (buyer, certifications, funding stage,
   open roles...), never a free-form summary.
3. **Qualify** (`qualify.py`): an agent loop driven by an Evidence Map. Code, not the model, checks
   whether the fields are filled, picks the next page, enforces the fetch limit (worst case 4 beyond
   the homepage) and decides when to stop.

   | Question | Look at | Limit | Unanswered | Stop early |
   |---|---|---|---|---|
   | Already certified? | /security, /trust | 1 | treat as not certified, flag | yes: disqualify |
   | B2B SaaS? | /product, /about | 1 | manual review | yes: disqualify |
   | Right stage? | /careers, /pricing, /customers | 2 | qualify at lower rank (-10), flag | no |

   Then the model scores ICP fit 0-100 from the facts; 70 or more qualifies.
4. **Contact** (`contacts.py`): Hunter.io Domain Search if `HUNTER_API_KEY` is set, otherwise
   `data/contacts.csv`. Candidates are ranked by who owns security work (security/compliance, then
   engineering leadership, then founder at companies under 50 people), never by "manager" in the
   title. If nobody qualifies, a human is flagged.
5. **Pain points** (`drafts.py`): exactly two, each with the fact that backs it.
6. **Draft** (`drafts.py`): addressed to the contact by name, opening with a specific signal from the
   company's own pages. Any email that misses the first name is flagged.
7. **Send** (`send.py`): drafts to `outbox/` by default. A real Gmail send needs `OFFLINE=0`,
   `TO_ADDRESS` set to your own address, `GMAIL_APP_PASSWORD`, and the explicit `--send` flag.

Every row in `runs/run_log.jsonl` records the evidence, fetches, score, contact and why they were
chosen, pain points, hook and flags, so each decision can be defended.

## Web app

`api/index.py` is a one-file Vercel Python function: Overview (funnel, pipeline), Companies (filters,
per-company evidence, contact, pain points, email draft), and Run (offline demo). It shows
`data/snapshot.json`, the last live run. Refresh it with `python run.py --domains domains.txt && python snapshot.py`
and push; Vercel redeploys. The "Review and send" button opens your own mail app with the draft filled in,
so nothing is sent from the site and no Gmail password is needed.

## Stage 1 and guards

- `python shortlist.py 20` finds recently funded SaaS companies on Google News (Series A-C in the
  headline, last 2 years, excluded sectors dropped) and resolves each to a domain only when the
  homepage title names the company and the page shares a topic word with the headline. The funding
  stage and its headline are stored as evidence for the "right stage?" question, then
  `python run.py --domains data/shortlist.txt` runs them.
- A quote the model returns is kept only if it appears word for word on the page it read. Dropped
  quotes are counted per company (`rejected_quotes`).
- A trust center on Vanta, Drata or Sprinto (detected in the raw HTML) disqualifies a company.

## Database, web runs and the feedback loop

With a database connected the web app stores companies, runs and feedback instead of reading only the snapshot.
Needed in the Vercel project's environment variables:

| Variable | Why |
|---|---|
| `DATABASE_URL` | Postgres. Add Neon from the Vercel Marketplace (Storage tab) and it sets this for you |
| `GEMINI_API_KEY` | Live runs and learning |
| `HUNTER_API_KEY` | Contacts for live runs (one search per company that reaches that step) |
| `APP_PASSWORD` | Optional. If set, runs, learning and feedback ask for it. If not, runs are capped at 3 an hour for everyone to protect credits |

- **Run pipeline** (web): up to 5 companies per run (about 30 s each, inside the 300 s limit), from a list you
  type or the next ones in `data/shortlist.txt`. Results are saved and show up everywhere.
- **Feedback loop:** "Approve & send" records the draft before and after your edits; Good/Poor rates it.
  "Learn from feedback" asks the model for at most 5 style rules from the last 20 events and stores them. Every
  new draft (CLI or web) is written with those rules in its prompt.
- Locally, set `SQLITE_PATH=/tmp/outreach.db` to try the same code on SQLite.

## ICP settings

The ICP settings page (stored in the database) changes, without code: the score needed to draft, the penalty for an
unknown funding stage, which funding stages count, sectors to skip in Find new companies, how many pages each
Evidence Map question may fetch, the job-title words used to pick the contact, and the sender name. They apply to the
next run. `settings.py` holds the defaults and the validation. Anyone who can open the site can change them, so add
sign-in before sharing the address widely.

## One-click run and the weekly schedule

"Find companies for me" (Run page) refreshes the funding shortlist when nothing is waiting, then researches companies
one by one until the chosen number qualify (up to 5 looked at per click). Every run gets an id; the Overview run
selector, the status chip and the Companies filters use it. `vercel.json` also schedules the same job every Monday
05:00 UTC through Vercel Cron; the route only accepts Vercel's `CRON_SECRET` bearer header.
