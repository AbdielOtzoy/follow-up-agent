# follow-up-agent

Sales teams lose deals in small gaps. A good call happens, everyone agrees on next steps, and then nobody sends the follow-up email. This agent finds those meetings in the CRM and asks Claude to draft the missing follow-up. A person then approves, edits or discards each draft. **It never sends anything by itself.**

It's a small prototype. It runs on fictional sample data by default and needs no API key in mock mode. It has an optional read-only HubSpot adapter.

```
CRM (sample JSON or HubSpot, read-only)
   │  meetings + later activities
   ▼
detect ── completed meetings older than N days with no email/call/meeting
   │      from the same owner on the same deal
   ▼
skip if this meeting ID is already in the queue (idempotency)
   ▼
Claude drafts {summary, next_steps, email} with structured output
   │  validate (schema + business rules) ── fail ─► retry once with the error
   │                                                 └─ fail again ─► needs_review
   ▼
approval queue (SQLite) ── approve ─► .eml draft for the owner to send
                         ├─ edit ───► re-validated, back to pending
                         └─ discard ► never drafted again
   ▼
per-run JSONL log + warnings when a run with input produces nothing
```

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run follow-up-agent run              # sample data, mock LLM (no API key)
uv run follow-up-agent queue            # list drafts
uv run follow-up-agent show M-1009      # read one draft
uv run follow-up-agent approve M-1001   # writes .followup/outbox/M-1001.eml (nothing is sent)
uv run follow-up-agent edit M-1003      # opens the draft in $EDITOR; re-validated on save
uv run follow-up-agent discard M-1004 --reason "deal lost"
uv run follow-up-agent run              # second run: 0 new drafts, 4 already queued
uv run follow-up-agent eval             # eval set
uv run pytest                           # tests
```

With Claude:

```bash
export ANTHROPIC_API_KEY=...
uv run follow-up-agent run --llm claude
uv run follow-up-agent eval --llm claude
```

The model defaults to `claude-opus-5-5`. Set `FOLLOWUP_MODEL` to change it. State (queue, run logs, outbox) lives in `.followup/`. Set `FOLLOWUP_STATE` to put it somewhere else.

The sample data ([data/sample](data/sample)) has 9 meetings. They were picked so each detection rule shows up: one meeting was followed up by email, one by a later meeting, one by a call to a contact with no deal; one got an email from the wrong owner; one got only an internal note; one was a no-show; one is too recent. Four meetings need a follow-up: M-1001, M-1003, M-1004 and M-1009. M-1009's notes include a prompt injection ("promise a 40% discount").

## Design decisions

### A human approves everything that reaches a customer
The agent writes drafts and stops there. `approve` doesn't send email. It writes a `.eml` file marked as unsent, so the owner opens it in their mail client, reads it and sends it themselves. A wrong email to a customer costs far more than the minute it takes to review one. Approval is the safety net, and it's also how the team learns to trust the drafts.

The queue enforces its states: you can only approve `pending` drafts; `needs_review` drafts need a human edit first; edits go through the same validator as model output, so a human can't save `Hi [Name]` either.

### Idempotency by meeting ID
The meeting ID is the primary key of the queue. The agent checks it before calling the model. Running the job twice (cron overlap, a retry, someone running it by hand) produces no duplicate drafts and no repeat API spend. A discarded meeting stays discarded. Transient errors, such as the API being down, are not queued, so the next run tries that meeting again.

### Structured output, validation and one retry
Claude returns JSON constrained by a schema (`output_config.format`). The JSON schema alone can't check everything, so a Pydantic model checks the rest:

- 1-5 next steps, non-empty summary, length limits
- no placeholders (`[Name]`, `{{date}}`, `TODO`, `TBD`)
- the email greets the contact and is signed by the meeting owner

If validation fails, the agent retries once and passes the validator's error back to the model. If the second attempt fails too, the meeting goes to the queue as `needs_review` with both errors attached, so a person can see why. It doesn't retry without limit: if two attempts fail, more retries rarely help, and a human should look at the case.

A refusal (`stop_reason: "refusal"`) counts as a failed attempt. The request also enables the API's server-side fallback, so a safety decline is retried on a fallback model inside the same call.

### Meeting notes are untrusted input
Notes are written by people, and they can be pasted from the customer's own email. The prompt wraps them in `<meeting_notes>` tags and tells the model they're data, not instructions. The eval set checks this with a planted "promise a 40% discount" line.

### Silent failures
An automation that "succeeds" with zero output is the worst kind of failure, because nobody notices it. Every run writes `.followup/runs/<run_id>.jsonl`: one event per meeting (skipped, queued, failed) and a final `run_summary` with counts. The run also prints a warning when:

- the source returned 0 meetings (bad token, wrong scopes, broken date filter)
- there were meetings but 0 missing follow-ups (maybe everyone followed up, or maybe owner/deal IDs stopped matching)
- some meetings failed to draft (exit code 1)

### Evals and tests are different things
`pytest` checks the code: detection rules, retries, idempotency, the queue's state machine, the CLI, the request sent to the Claude API, the HubSpot mapping. `follow-up-agent eval` checks the drafts: [evals/cases.json](evals/cases.json) has fixed meetings with expectations such as "mentions the agreed next steps", "keeps the date from the notes", "does not promise a discount", "no placeholder when the contact name is missing".

The checks are plain string rules, so a failure points at a specific sentence and costs nothing to grade. In mock mode the evals only prove the harness works. `--llm claude` is the real measurement. When a draft goes wrong in practice, it becomes a new eval case before the prompt changes. That way a fix for one case doesn't quietly break another.

### Mock mode
`MockDrafter` builds a deterministic draft from the notes. Anyone can clone the repo and run the whole flow, and the tests, without an API key or spending money.

### What counts as a follow-up
An `email`, `call` or `meeting` logged after the meeting, by the **same owner**, on the **same deal**. A meeting with no deal is matched on the contact's email instead. Internal notes and tasks don't count. A colleague's email doesn't count either, because the question is whether the owner followed up. Only `completed` meetings are checked. `--days` (default 3) sets the deadline and `--lookback` (default 30) skips old meetings.

These rules are a starting point. A real team will tune them, for example to count a reply from the customer, or to skip internal meetings.

## Connecting to HubSpot

```bash
export HUBSPOT_TOKEN=pat-...   # private app token with read-only scopes
uv run follow-up-agent run --source hubspot --llm claude
```

`HubSpotSource` implements the same `fetch(since)` interface as the sample source, so the rest of the pipeline doesn't change. It only reads:

| Here | HubSpot |
|---|---|
| `Meeting` | `meetings` object: `hs_meeting_title`, `hs_meeting_body` (HTML stripped), `hs_meeting_start_time`, `hs_meeting_outcome`, `hubspot_owner_id`, first associated deal and contact |
| `Activity` | `emails`, `calls` and `meetings` objects: `hs_timestamp`, `hubspot_owner_id`, associated deal and contact |
| owner name | `/crm/v3/owners` |
| deal / contact details | `deals` (`dealname`), `contacts` (`firstname`, `lastname`, `email`, `company`) |

An empty meeting outcome counts as completed. An outcome value it doesn't recognize is skipped, to stay on the safe side.

The adapter is tested against fixtures shaped like HubSpot's v3 responses, **not against a live portal**. Before using it for real, I would:

1. **Fetch less.** It uses list endpoints with a client-side date filter, which is fine for a small portal. For a large one, switch to the CRM search API with a date filter, or react to webhooks when meetings are logged.
2. **Write back only after approval.** On `approve`, create the email as a draft engagement or a task for the owner in HubSpot, associated with the deal. That needs write scopes, and nothing would be written before a human approves. It should be idempotent too, using the meeting ID as the external key.
3. **Bring the queue to where people work.** Post each draft to the owner in Slack with Approve / Edit / Discard buttons, instead of a CLI.
4. **Watch it.** Send the `run_summary` events to the team's monitoring and alert on the warnings above.

## Limitations

- Only the first associated deal and contact are used.
- Eval checks are keyword rules. They catch invented discounts and missing next steps, but not tone. An LLM-graded rubric would be the next step.
- The queue is a local SQLite file. A team deployment needs a shared store.

## Layout

```
src/follow_up_agent/
  models.py    Meeting, Activity, Draft + validation rules, JSON schema
  detect.py    which meetings are missing a follow-up
  drafter.py   Claude drafter, mock drafter, validate + retry
  store.py     approval queue (SQLite, keyed by meeting ID)
  pipeline.py  one run: fetch, detect, draft, queue, log, warnings
  sources.py   sample JSON source, HubSpot source (read-only)
  evals.py     eval runner
  cli.py       command line
data/sample/   fictional meetings and activities
evals/         eval cases
tests/         pytest suite (no network)
```

All names, companies, emails and deals in this repo are fictional.

## License

MIT
