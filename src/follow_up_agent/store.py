"""Approval queue in SQLite. The meeting ID is the primary key, which is what makes runs idempotent."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Draft, Meeting

# pending -> approved | discarded; needs_review -> (edit) -> pending
STATUSES = ("pending", "needs_review", "approved", "discarded")


class Queue:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS drafts (
                meeting_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                meeting TEXT NOT NULL,
                draft TEXT,
                errors TEXT,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                note TEXT
            )"""
        )

    def has(self, meeting_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM drafts WHERE meeting_id=?", (meeting_id,)).fetchone() is not None

    def add(self, meeting: Meeting, draft: Draft | None, errors: list[str], run_id: str) -> bool:
        """Insert once per meeting. Returns False if the meeting was already queued (any status)."""
        now = _now()
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO drafts VALUES (?,?,?,?,?,?,?,?,NULL)",
                (meeting.id, "pending" if draft else "needs_review", meeting.model_dump_json(),
                 draft.model_dump_json() if draft else None, json.dumps(errors), run_id, now, now),
            )
        return cur.rowcount == 1

    def list(self, status: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM drafts", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return self.db.execute(q + " ORDER BY created_at, meeting_id", args).fetchall()

    def get(self, meeting_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM drafts WHERE meeting_id=?", (meeting_id,)).fetchone()
        if row is None:
            raise KeyError(f"no draft for meeting {meeting_id}")
        return row

    def set(self, meeting_id: str, status: str, *, draft: Draft | None = None, note: str | None = None,
            allowed_from: tuple[str, ...]) -> None:
        current = self.get(meeting_id)["status"]
        if current not in allowed_from:
            raise ValueError(f"meeting {meeting_id} is {current}; expected one of {allowed_from}")
        with self.db:
            self.db.execute(
                "UPDATE drafts SET status=?, draft=COALESCE(?, draft), note=COALESCE(?, note), updated_at=?"
                " WHERE meeting_id=?",
                (status, draft.model_dump_json() if draft else None, note, _now(), meeting_id),
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
