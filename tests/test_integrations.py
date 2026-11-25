"""The Claude and HubSpot adapters, exercised without network: fake HTTP, real SDK / real parsing."""

import json
from datetime import datetime

import anthropic
import httpx2

from follow_up_agent.detect import find_missing_follow_ups
from follow_up_agent.drafter import ClaudeDrafter, MockDrafter, draft_with_retry
from follow_up_agent.sources import HubSpotSource
from tests.test_agent import meeting


def fake_claude(captured: list, text: str, stop="end_turn"):
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}})
    return anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_claude_request_shape_and_parse():
    m, captured = meeting(), []
    good = MockDrafter().raw_draft(m, None)
    draft, errors = draft_with_retry(ClaudeDrafter(client=fake_claude(captured, good)), m)
    assert draft and not errors
    body = json.loads(captured[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured[0].headers["anthropic-beta"]
    assert "<meeting_notes>" in body["messages"][0]["content"]


def test_claude_refusal_and_bad_output_end_in_needs_review():
    m, captured = meeting(), []
    draft, errors = draft_with_retry(ClaudeDrafter(client=fake_claude(captured, "{}", stop="refusal")), m)
    assert draft is None and "refusal" in errors[0]
    # second attempt carries the validator's feedback
    assert "rejected by the validator" in json.loads(captured[1].content)["messages"][0]["content"]


HUBSPOT = {
    "/crm/v3/owners": {"results": [{"id": "11", "firstName": "Maya", "lastName": "Chen"}]},
    "/crm/v3/objects/deals": {"results": [{"id": "900", "properties": {"dealname": "Acme - Pilot"}}]},
    "/crm/v3/objects/contacts": {"results": [{"id": "500", "properties": {
        "firstname": "Dana", "lastname": "Brooks", "email": "dana@example.com", "company": "Acme"}}]},
    "/crm/v3/objects/meetings": {"results": [{"id": "1", "properties": {
        "hs_timestamp": "2026-11-10T16:00:00Z", "hs_meeting_start_time": "2026-11-10T16:00:00Z",
        "hubspot_owner_id": "11", "hs_meeting_title": "Demo", "hs_meeting_body": "<p>Good demo.</p>",
        "hs_meeting_outcome": "COMPLETED"},
        "associations": {"deals": {"results": [{"id": "900"}]}, "contacts": {"results": [{"id": "500"}]}}}],
        "paging": {"next": {"after": "abc"}}},
    "/crm/v3/objects/meetings?after": {"results": []},
    "/crm/v3/objects/emails": {"results": []},
    "/crm/v3/objects/calls": {"results": [{"id": "7", "properties": {
        "hs_timestamp": "2026-11-12T10:00:00Z", "hubspot_owner_id": "11"},
        "associations": {"deals": {"results": [{"id": "900"}]}}}]},
}


def fake_hubspot(url: str, token: str) -> dict:
    assert token == "pat-test"
    path = url.split("api.hubapi.com")[1]
    key = path.split("?")[0] + ("?after" if "after=" in path else "")
    return HUBSPOT[key]


def test_hubspot_mapping_and_pagination():
    src = HubSpotSource(token="pat-test", get=fake_hubspot)
    ms, acts = src.fetch(datetime.fromisoformat("2026-11-01T00:00:00+00:00"))
    assert len(ms) == 1
    m = ms[0]
    assert (m.id, m.owner_name, m.deal_name, m.contact_name, m.notes) == \
        ("hubspot-meeting-1", "Maya Chen", "Acme - Pilot", "Dana Brooks", "Good demo.")
    # the call on the same deal two days later is a follow-up
    assert find_missing_follow_ups(ms, acts, days=3, now=datetime.fromisoformat("2026-11-20T00:00:00+00:00")) == []
    assert find_missing_follow_ups(ms, [a for a in acts if a.type != "call"], days=3,
                                   now=datetime.fromisoformat("2026-11-20T00:00:00+00:00")) == ms
