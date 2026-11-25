"""Where meetings and activities come from. Sample JSON by default; HubSpot is optional and read-only."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .models import Activity, Meeting


class Source(Protocol):
    def fetch(self, since: datetime) -> tuple[list[Meeting], list[Activity]]: ...


class SampleSource:
    """Reads `meetings.json` and `activities.json` from a folder. All shipped data is fictional."""

    def __init__(self, folder: Path):
        self.folder = folder

    def as_of(self) -> datetime | None:
        raw = json.loads((self.folder / "meetings.json").read_text())
        return datetime.fromisoformat(raw["as_of"]) if "as_of" in raw else None

    def fetch(self, since: datetime) -> tuple[list[Meeting], list[Activity]]:
        meetings = [Meeting(**m) for m in json.loads((self.folder / "meetings.json").read_text())["meetings"]]
        activities = [Activity(**a) for a in json.loads((self.folder / "activities.json").read_text())["activities"]]
        # a later meeting on the same deal counts as a follow-up, as it does in a CRM timeline
        activities += [Activity(id=m.id, type="meeting", owner_id=m.owner_id, timestamp=m.start, deal_id=m.deal_id,
                                contact_email=m.contact_email) for m in meetings if m.outcome == "completed"]
        return [m for m in meetings if m.start >= since], [a for a in activities if a.timestamp >= since]


# --- HubSpot ----------------------------------------------------------------
# shortcut: untested against a live portal (only against recorded-shape fixtures in tests/);
# list endpoints + client-side date filter, fine for a few thousand records, move to the
# search API with a date filter before using it on a large portal.

API = "https://api.hubapi.com"
ENGAGEMENT_TYPES = {"emails": "email", "calls": "call", "meetings": "meeting"}


def _http_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


class HubSpotSource:
    """Reads meetings, emails and calls (with deal/contact associations), owners and contacts.

    Needs a private-app token (HUBSPOT_TOKEN) with read-only scopes for owners, contacts, deals and
    the meeting/email/call objects. It never writes to HubSpot."""

    def __init__(self, token: str | None = None, get: Callable[[str, str], dict] = _http_get):
        self.token = token or os.environ["HUBSPOT_TOKEN"]
        self.get = get

    def _list(self, obj: str, props: list[str], assoc: list[str]) -> list[dict]:
        out, after = [], None
        while True:
            q = {"limit": 100, "properties": ",".join(props)}
            if assoc:
                q["associations"] = ",".join(assoc)
            if after:
                q["after"] = after
            page = self.get(f"{API}/crm/v3/objects/{obj}?{urllib.parse.urlencode(q)}", self.token)
            out += page.get("results", [])
            after = page.get("paging", {}).get("next", {}).get("after")
            if not after:
                return out

    @staticmethod
    def _first_assoc(rec: dict, kind: str) -> str | None:
        res = rec.get("associations", {}).get(kind, {}).get("results", [])
        return str(res[0]["id"]) if res else None

    def fetch(self, since: datetime) -> tuple[list[Meeting], list[Activity]]:
        owners = {str(o["id"]): f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
                  for o in self.get(f"{API}/crm/v3/owners?limit=500", self.token)["results"]}
        deals = {d["id"]: d["properties"].get("dealname") for d in self._list("deals", ["dealname"], [])}
        contacts = {c["id"]: c["properties"] for c in
                    self._list("contacts", ["firstname", "lastname", "email", "company"], [])}

        meetings, activities = [], []
        for obj, kind in ENGAGEMENT_TYPES.items():
            for r in self._list(obj, ["hs_timestamp", "hubspot_owner_id", "hs_meeting_title", "hs_meeting_body",
                                      "hs_meeting_start_time", "hs_meeting_outcome"], ["deals", "contacts"]):
                p = r["properties"]
                ts = datetime.fromisoformat((p.get("hs_meeting_start_time") or p["hs_timestamp"]).replace("Z", "+00:00"))
                if ts < since or not p.get("hubspot_owner_id"):
                    continue
                deal_id, contact_id = self._first_assoc(r, "deals"), self._first_assoc(r, "contacts")
                c = contacts.get(contact_id or "", {})
                email = c.get("email")
                activities.append(Activity(id=f"{obj}:{r['id']}", type=kind, owner_id=p["hubspot_owner_id"],
                                           timestamp=ts, deal_id=deal_id, contact_email=email))
                if kind == "meeting":
                    meetings.append(Meeting(
                        id=f"hubspot-meeting-{r['id']}", owner_id=p["hubspot_owner_id"],
                        owner_name=owners.get(p["hubspot_owner_id"], "Owner"), start=ts,
                        title=p.get("hs_meeting_title") or "Meeting", notes=_strip_html(p.get("hs_meeting_body") or ""),
                        deal_id=deal_id, deal_name=deals.get(deal_id or ""), company=c.get("company"),
                        contact_name=f"{c.get('firstname') or ''} {c.get('lastname') or ''}".strip() or None,
                        contact_email=email, outcome=_OUTCOME.get((p.get("hs_meeting_outcome") or "COMPLETED").upper(), "scheduled"),
                    ))
        return meetings, activities


# Empty outcome (common in HubSpot) counts as completed; an unknown value is skipped, to be safe.
_OUTCOME = {"COMPLETED": "completed", "SCHEDULED": "scheduled", "NO_SHOW": "no_show",
            "CANCELED": "canceled", "RESCHEDULED": "canceled"}


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ").strip()
