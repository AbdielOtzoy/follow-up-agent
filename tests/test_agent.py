import json
from datetime import datetime
from pathlib import Path

import pytest

from follow_up_agent.cli import main
from follow_up_agent.detect import find_missing_follow_ups
from follow_up_agent.drafter import MockDrafter, draft_with_retry
from follow_up_agent.models import Activity, Meeting
from follow_up_agent.pipeline import run
from follow_up_agent.sources import SampleSource
from follow_up_agent.store import Queue

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample"
NOW = datetime.fromisoformat("2026-11-20T17:00:00+00:00")


def meeting(**kw) -> Meeting:
    base = dict(id="M-1", owner_id="o-1", owner_name="Maya Chen", start="2026-11-10T16:00:00+00:00",
                title="Demo", deal_id="D-1", contact_name="Dana Brooks", contact_email="dana@example.com",
                notes="Good demo.\nNext: Us: send the plan.")
    return Meeting(**{**base, **kw})


def act(**kw) -> Activity:
    base = dict(id="A-1", type="email", owner_id="o-1", timestamp="2026-11-11T10:00:00+00:00", deal_id="D-1")
    return Activity(**{**base, **kw})


# --- detection ---------------------------------------------------------------

@pytest.mark.parametrize("activity,stale", [
    (None, True),
    (act(), False),                                          # owner emailed on the deal
    (act(owner_id="o-2"), True),                             # someone else's email does not count
    (act(type="note"), True),                                # internal note is not a follow-up
    (act(timestamp="2026-11-09T10:00:00+00:00"), True),      # before the meeting
    (act(deal_id="D-2"), True),                              # different deal
])
def test_detection_rules(activity, stale):
    got = find_missing_follow_ups([meeting()], [activity] if activity else [], days=3, now=NOW)
    assert bool(got) is stale


def test_not_due_yet_and_not_completed_are_ignored():
    ms = [meeting(id="recent", start="2026-11-19T10:00:00+00:00"), meeting(id="noshow", outcome="no_show")]
    assert find_missing_follow_ups(ms, [], days=3, now=NOW) == []


def test_no_deal_matches_on_contact():
    m = meeting(deal_id=None)
    assert find_missing_follow_ups([m], [act(deal_id=None, contact_email="dana@example.com")], days=3, now=NOW) == []
    assert find_missing_follow_ups([m], [act(deal_id=None, contact_email="x@example.com")], days=3, now=NOW) == [m]


def test_sample_data_expected_meetings():
    src = SampleSource(SAMPLE)
    ms, acts = src.fetch(datetime.fromisoformat("2026-10-01T00:00:00+00:00"))
    assert [m.id for m in find_missing_follow_ups(ms, acts, days=3, now=src.as_of())] == \
        ["M-1001", "M-1003", "M-1004", "M-1009"]


# --- validation and retry ----------------------------------------------------

def good_raw(m: Meeting) -> str:
    return MockDrafter().raw_draft(m, None)


def test_retry_once_then_succeeds():
    m = meeting()
    d = MockDrafter(script=["not json", good_raw(m)])
    draft, errors = draft_with_retry(d, m)
    assert draft is not None and len(errors) == 1 and d.calls == 2


def test_two_failures_mean_needs_review():
    m = meeting()
    bad = json.loads(good_raw(m))
    bad["email"]["body"] = "Hi [Name], thanks!"  # placeholder + no owner signature
    d = MockDrafter(script=[json.dumps(bad), json.dumps(bad), good_raw(m)])
    draft, errors = draft_with_retry(d, m)
    assert draft is None and len(errors) == 2 and d.calls == 2


def test_validator_rejects_too_many_steps():
    m = meeting()
    raw = json.loads(good_raw(m))
    raw["next_steps"] = ["x"] * 6
    draft, errors = draft_with_retry(MockDrafter(script=[json.dumps(raw)] * 2), m)
    assert draft is None and "next_steps" in errors[0]


# --- pipeline: idempotency, logging, alarms -------------------------------------

def do_run(tmp_path, drafter, source=None, days=3):
    return run(source or SampleSource(SAMPLE), drafter, Queue(tmp_path / "q.db"), days=days, lookback_days=30,
               now=NOW, log_dir=tmp_path / "runs")


def test_running_twice_does_not_duplicate_or_redraft(tmp_path):
    d = MockDrafter()
    first, second = do_run(tmp_path, d), do_run(tmp_path, d)
    assert (first.drafted, second.drafted, second.skipped_already_queued) == (4, 0, 4)
    assert d.calls == 4
    assert len(Queue(tmp_path / "q.db").list()) == 4
    assert len(list((tmp_path / "runs").glob("*.jsonl"))) == 2


def test_discarded_meeting_is_not_drafted_again(tmp_path):
    do_run(tmp_path, MockDrafter())
    Queue(tmp_path / "q.db").set("M-1004", "discarded", allowed_from=("pending",))
    assert do_run(tmp_path, MockDrafter()).drafted == 0


def test_zero_results_with_input_warns(tmp_path):
    rep = do_run(tmp_path, MockDrafter(), days=60)  # nothing is 60 days old: 0 results
    assert rep.meetings_in > 0 and rep.missing_follow_up == 0
    assert any("0 missing follow-ups" in w for w in rep.warnings)
    summary = json.loads((tmp_path / "runs" / f"{rep.run_id}.jsonl").read_text().splitlines()[-1])
    assert summary["event"] == "run_summary" and summary["warnings"] == rep.warnings


def test_empty_source_warns(tmp_path):
    class Empty:
        def fetch(self, since):
            return [], []
    assert any("0 meetings" in w for w in do_run(tmp_path, MockDrafter(), Empty()).warnings)


def test_transient_error_is_not_queued_and_retries_next_run(tmp_path):
    class Boom(MockDrafter):
        def raw_draft(self, m, feedback):
            raise ConnectionError("API down")
    rep = do_run(tmp_path, Boom())
    assert rep.failed == 4 and Queue(tmp_path / "q.db").list() == []
    assert do_run(tmp_path, MockDrafter()).drafted == 4


# --- CLI approval flow -------------------------------------------------------

def test_cli_approve_edit_discard(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("follow_up_agent.cli.STATE", tmp_path)
    assert main(["run"]) == 0
    assert main(["approve", "M-1001"]) == 0
    eml = (tmp_path / "outbox" / "M-1001.eml").read_text()
    assert "To: dana.brooks@example.com" in eml and "Hi Dana" in eml
    assert main(["approve", "M-1001"]) == 2  # already approved

    # a needs_review item cannot be approved until a human edits it
    q = Queue(tmp_path / "queue.db")
    q.set("M-1003", "needs_review", allowed_from=("pending",))
    assert main(["approve", "M-1003"]) == 2
    edited = tmp_path / "edit.json"
    edited.write_text(json.dumps({"summary": "Discovery call.", "next_steps": ["Us: share stories."],
                                  "email": {"subject": "Next steps", "body": "Hi Hannah,\n\nThanks.\n\nPriya"}}))
    assert main(["edit", "M-1003", "--from-file", str(edited)]) == 0
    assert q.get("M-1003")["status"] == "pending"

    # human edits go through the same validator
    edited.write_text(json.dumps({"summary": "x", "next_steps": ["y"], "email": {"subject": "s", "body": "Hi [Name]"}}))
    assert main(["edit", "M-1009", "--from-file", str(edited)]) == 2

    assert main(["discard", "M-1009", "--reason", "deal lost"]) == 0
    assert q.get("M-1009")["status"] == "discarded"


def test_evals_pass_in_mock_mode():
    assert main(["eval"]) == 0
