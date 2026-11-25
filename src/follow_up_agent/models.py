"""Data shapes. Input records come from a CRM source; Draft is what the LLM must return."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Meeting(BaseModel):
    id: str
    owner_id: str
    owner_name: str
    start: datetime
    title: str
    notes: str = ""
    deal_id: str | None = None
    deal_name: str | None = None
    company: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    outcome: Literal["completed", "scheduled", "no_show", "canceled"] = "completed"


class Activity(BaseModel):
    """Anything that happened after a meeting: an email, a call, a note."""

    id: str
    type: Literal["email", "call", "meeting", "note", "task"]
    owner_id: str
    timestamp: datetime
    deal_id: str | None = None
    contact_email: str | None = None


# --- LLM output -------------------------------------------------------------

PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{\{|\}\}|<[A-Z_ ]+>|\bTODO\b|\bTBD\b|lorem ipsum", re.I)


class Email(BaseModel):
    subject: str
    body: str


class Draft(BaseModel):
    summary: str = Field(description="2-4 sentences: what was discussed and agreed.")
    next_steps: list[str] = Field(description="1-5 concrete actions, each with who does it.")
    email: Email

    @field_validator("summary")
    @classmethod
    def _summary(cls, v: str) -> str:
        if not v.strip() or len(v) > 800:
            raise ValueError("summary must be non-empty and at most 800 characters")
        return v

    @field_validator("next_steps")
    @classmethod
    def _steps(cls, v: list[str]) -> list[str]:
        if not 1 <= len(v) <= 5 or any(not s.strip() for s in v):
            raise ValueError("next_steps must have 1-5 non-empty items")
        return v

    @model_validator(mode="after")
    def _email(self) -> "Draft":
        e = self.email
        if not e.subject.strip() or len(e.subject) > 120:
            raise ValueError("email.subject must be non-empty and at most 120 characters")
        if not e.body.strip() or len(e.body) > 2000:
            raise ValueError("email.body must be non-empty and at most 2000 characters")
        if m := PLACEHOLDER.search(e.subject + "\n" + e.body):
            raise ValueError(f"email contains a placeholder: {m.group(0)!r}")
        return self


def check_draft_for(meeting: Meeting, draft: Draft) -> None:
    """Rules that depend on the meeting, not just the shape. Raises ValueError."""
    first = (meeting.contact_name or "").split(" ")[0]
    if first and first.lower() not in draft.email.body.lower():
        raise ValueError(f"email.body must address the contact by name ({first})")
    if meeting.owner_name.split(" ")[0].lower() not in draft.email.body.lower():
        raise ValueError("email.body must be signed by the meeting owner")


# JSON Schema sent to the API (structured outputs). Hand-written: the API needs
# additionalProperties=false everywhere and ignores length limits, which Draft enforces.
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "email": {
            "type": "object",
            "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["subject", "body"],
            "additionalProperties": False,
        },
    },
    "required": ["summary", "next_steps", "email"],
    "additionalProperties": False,
}
