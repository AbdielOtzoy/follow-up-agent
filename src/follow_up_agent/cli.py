"""Command line: run the agent, review the queue, run evals."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from .drafter import ClaudeDrafter, MockDrafter, validate
from .models import Draft, Meeting
from .pipeline import run
from .sources import HubSpotSource, SampleSource
from .store import Queue

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample"
STATE = Path(os.environ.get("FOLLOWUP_STATE", ".followup"))


def make_drafter(llm: str):
    return ClaudeDrafter() if llm == "claude" else MockDrafter()


def cmd_run(a) -> int:
    if a.source == "hubspot":
        source, now = HubSpotSource(), datetime.now(timezone.utc)
    else:
        source = SampleSource(Path(a.data))
        now = source.as_of() or datetime.now(timezone.utc)
    if a.now:
        now = datetime.fromisoformat(a.now)
    print(f"source={a.source} llm={a.llm} as_of={now:%Y-%m-%d} follow-up window={a.days}d")
    rep = run(source, make_drafter(a.llm), Queue(STATE / "queue.db"), days=a.days,
              lookback_days=a.lookback, now=now, log_dir=STATE / "runs")
    print(f"meetings={rep.meetings_in} missing_follow_up={rep.missing_follow_up} drafted={rep.drafted} "
          f"needs_review={rep.needs_review} already_queued={rep.skipped_already_queued} failed={rep.failed}")
    for w in rep.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    print(f"log: {STATE / 'runs' / (rep.run_id + '.jsonl')}")
    return 1 if rep.failed else 0


def cmd_queue(a) -> int:
    rows = Queue(STATE / "queue.db").list(a.status)
    if not rows:
        print("queue is empty")
    for r in rows:
        m = json.loads(r["meeting"])
        print(f"{r['status']:<13} {r['meeting_id']:<22} {m['start'][:10]}  {m['owner_name']:<16} {m['title']}")
    return 0


def cmd_show(a) -> int:
    r = Queue(STATE / "queue.db").get(a.meeting_id)
    m = json.loads(r["meeting"])
    print(f"[{r['status']}] {m['title']} ({m['start'][:10]}) owner={m['owner_name']} contact={m.get('contact_email')}")
    if r["draft"]:
        d = Draft.model_validate_json(r["draft"])
        print(f"\nSummary: {d.summary}\nNext steps:")
        print("\n".join(f"  - {s}" for s in d.next_steps))
        print(f"\nSubject: {d.email.subject}\n\n{d.email.body}")
    errors = json.loads(r["errors"] or "[]")
    if errors:
        print("\nValidator errors:\n" + "\n".join(f"  {i + 1}. {e}" for i, e in enumerate(errors)))
    return 0


def cmd_approve(a) -> int:
    q = Queue(STATE / "queue.db")
    r = q.get(a.meeting_id)
    q.set(a.meeting_id, "approved", allowed_from=("pending",))
    m, d = Meeting.model_validate_json(r["meeting"]), Draft.model_validate_json(r["draft"])
    msg = EmailMessage()
    msg["To"] = m.contact_email or ""
    msg["Subject"] = d.email.subject
    msg["X-Unsent"] = "1"  # opens as an editable draft in Outlook/Apple Mail
    msg.set_content(d.email.body, cte="8bit")
    out = STATE / "outbox" / f"{a.meeting_id}.eml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(msg))
    print(f"approved. Draft for {m.owner_name} to send: {out}  (nothing was sent)")
    return 0


def cmd_edit(a) -> int:
    q = Queue(STATE / "queue.db")
    r = q.get(a.meeting_id)
    if r["status"] not in ("pending", "needs_review"):
        raise ValueError(f"meeting {a.meeting_id} is {r['status']}; only pending or needs_review can be edited")
    m = Meeting.model_validate_json(r["meeting"])
    start = r["draft"] or json.dumps({"summary": "", "next_steps": [""], "email": {"subject": "", "body": ""}})
    if a.from_file:
        raw = Path(a.from_file).read_text()
    else:
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            f.write(json.dumps(json.loads(start), indent=2))
        subprocess.call([os.environ.get("EDITOR", "vi"), f.name])
        raw = Path(f.name).read_text()
    draft = validate(m, raw)  # human edits go through the same validator
    q.set(a.meeting_id, "pending", draft=draft, note="edited by human", allowed_from=("pending", "needs_review"))
    print("saved; status is now pending. Approve it with: follow-up-agent approve", a.meeting_id)
    return 0


def cmd_discard(a) -> int:
    Queue(STATE / "queue.db").set(a.meeting_id, "discarded", note=a.reason or "discarded",
                                  allowed_from=("pending", "needs_review"))
    print("discarded. It will not be drafted again.")
    return 0


def cmd_eval(a) -> int:
    from .evals import run_evals

    return run_evals(make_drafter(a.llm), Path(a.cases))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="follow-up-agent", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="find meetings without follow-up and queue drafts")
    r.add_argument("--source", choices=["sample", "hubspot"], default="sample")
    r.add_argument("--data", default=str(SAMPLE), help="folder with meetings.json/activities.json")
    r.add_argument("--llm", choices=["mock", "claude"], default="mock", help="mock needs no API key")
    r.add_argument("--days", type=int, default=3, help="a meeting needs a follow-up within N days")
    r.add_argument("--lookback", type=int, default=30, help="ignore meetings older than this many days")
    r.add_argument("--now", help="ISO datetime to treat as now (default: sample as_of / real now)")
    r.set_defaults(fn=cmd_run)

    q = sub.add_parser("queue", help="list drafts")
    q.add_argument("--status", choices=["pending", "needs_review", "approved", "discarded"])
    q.set_defaults(fn=cmd_queue)

    for name, fn, help_ in [("show", cmd_show, "show one draft"), ("approve", cmd_approve, "approve a draft"),
                            ("edit", cmd_edit, "edit a draft in $EDITOR"), ("discard", cmd_discard, "discard a draft")]:
        s = sub.add_parser(name, help=help_)
        s.add_argument("meeting_id")
        if name == "edit":
            s.add_argument("--from-file", help="read the edited draft JSON from a file instead of $EDITOR")
        if name == "discard":
            s.add_argument("--reason")
        s.set_defaults(fn=fn)

    e = sub.add_parser("eval", help="run the eval set")
    e.add_argument("--llm", choices=["mock", "claude"], default="mock")
    e.add_argument("--cases", default=str(ROOT / "evals" / "cases.json"))
    e.set_defaults(fn=cmd_eval)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (KeyError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
