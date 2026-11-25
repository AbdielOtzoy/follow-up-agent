"""Eval runner: fixed meetings + expectations about the draft. Checks are deterministic string
rules, so a failure always points at a specific sentence. Run with --llm claude to grade the real model."""

from __future__ import annotations

import json
from pathlib import Path

from .drafter import Drafter, draft_with_retry
from .models import Meeting


def grade(case: dict, drafter: Drafter) -> list[str]:
    """Returns a list of failures (empty = pass)."""
    meeting = Meeting(**case["meeting"])
    draft, errors = draft_with_retry(drafter, meeting)
    exp = case.get("expect", {})
    if draft is None:
        return [] if exp.get("needs_review") else [f"no valid draft after retry: {errors}"]
    if exp.get("needs_review"):
        return ["expected needs_review, got a draft"]
    text = f"{draft.email.subject}\n{draft.email.body}".lower()
    fails = [f"missing {w!r}" for w in exp.get("must_mention", []) if w.lower() not in text]
    fails += [f"mentions forbidden {w!r}" for w in exp.get("must_not_mention", []) if w.lower() in text]
    for group in exp.get("must_mention_any", []):
        if not any(w.lower() in text for w in group):
            fails.append(f"missing any of {group}")
    if (n := exp.get("max_words")) and len(draft.email.body.split()) > n:
        fails.append(f"body has {len(draft.email.body.split())} words (> {n})")
    if errors and exp.get("first_try", True):
        fails.append(f"needed a retry: {errors}")
    return fails


def run_evals(drafter: Drafter, cases_path: Path) -> int:
    cases = json.loads(cases_path.read_text())["cases"]
    failed = 0
    for c in cases:
        fails = grade(c, drafter)
        failed += bool(fails)
        print(f"{'PASS' if not fails else 'FAIL'}  {c['id']:<28} {c['why']}")
        for f in fails:
            print(f"      - {f}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0
