"""Which meetings never got a follow-up from their owner?"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import Activity, Meeting

FOLLOW_UP_TYPES = {"email", "call", "meeting"}  # a note or a task is not a follow-up to the customer


def is_follow_up(meeting: Meeting, a: Activity) -> bool:
    if a.type not in FOLLOW_UP_TYPES or a.owner_id != meeting.owner_id or a.timestamp <= meeting.start:
        return False
    if meeting.deal_id:
        return a.deal_id == meeting.deal_id
    return bool(meeting.contact_email) and a.contact_email == meeting.contact_email


def find_missing_follow_ups(
    meetings: list[Meeting], activities: list[Activity], *, days: int, now: datetime
) -> list[Meeting]:
    """Completed meetings older than `days` with no follow-up by the same owner on the same deal
    (or the same contact, when the meeting has no deal)."""
    cutoff = now - timedelta(days=days)
    return [
        m
        for m in sorted(meetings, key=lambda m: m.start)
        if m.outcome == "completed"
        and m.start <= cutoff
        and not any(is_follow_up(m, a) for a in activities)
    ]
