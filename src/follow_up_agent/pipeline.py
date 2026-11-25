"""One run: fetch -> detect -> draft (skipping meetings already queued) -> queue -> log."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .detect import find_missing_follow_ups
from .drafter import Drafter, draft_with_retry
from .sources import Source
from .store import Queue


@dataclass
class RunReport:
    run_id: str
    started_at: str
    meetings_in: int = 0
    activities_in: int = 0
    missing_follow_up: int = 0
    skipped_already_queued: int = 0
    drafted: int = 0
    needs_review: int = 0
    failed: int = 0  # transient errors (API down, etc.): not queued, retried next run
    warnings: list[str] = field(default_factory=list)


def run(source: Source, drafter: Drafter, queue: Queue, *, days: int, lookback_days: int,
        now: datetime, log_dir: Path) -> RunReport:
    started = datetime.now(timezone.utc)
    rep = RunReport(run_id=f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
                    started_at=started.isoformat(timespec="seconds"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"{rep.run_id}.jsonl").open("w")

    def event(kind: str, **kw) -> None:
        log.write(json.dumps({"event": kind, **kw}, default=str) + "\n")

    try:
        meetings, activities = source.fetch(now - timedelta(days=lookback_days))
        rep.meetings_in, rep.activities_in = len(meetings), len(activities)
        stale = find_missing_follow_ups(meetings, activities, days=days, now=now)
        rep.missing_follow_up = len(stale)

        for m in stale:
            if queue.has(m.id):  # idempotency: never draft (or pay for) the same meeting twice
                rep.skipped_already_queued += 1
                event("skip_already_queued", meeting_id=m.id)
                continue
            try:
                draft, errors = draft_with_retry(drafter, m)
            except Exception as e:  # noqa: BLE001 - one bad meeting must not sink the run
                rep.failed += 1
                event("draft_failed", meeting_id=m.id, error=repr(e))
                continue
            queue.add(m, draft, errors, rep.run_id)
            if draft:
                rep.drafted += 1
            else:
                rep.needs_review += 1
            event("queued", meeting_id=m.id, status="pending" if draft else "needs_review",
                  attempts=len(errors) + (1 if draft else 0), errors=errors)

        # Silent-failure alarms: a run that "succeeds" with nothing to show is suspicious.
        if rep.meetings_in == 0:
            rep.warnings.append(f"source returned 0 meetings in the last {lookback_days} days: "
                                "check the connection, token scopes or date filter")
        elif rep.missing_follow_up == 0:
            rep.warnings.append(f"{rep.meetings_in} meetings in, 0 missing follow-ups: either everyone "
                                "followed up, or matching is broken (owner/deal IDs?). Spot-check one.")
        if rep.failed:
            rep.warnings.append(f"{rep.failed} meetings failed to draft; they will be retried next run")
    finally:
        event("run_summary", **asdict(rep))
        log.close()
    return rep
