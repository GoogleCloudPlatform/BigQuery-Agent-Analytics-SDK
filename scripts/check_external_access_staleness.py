#!/usr/bin/env python3
"""Fail once the Looker Studio external-access attestation is overdue.

The end-to-end copy canary in
dashboard/looker_studio/bindings/report_template.yaml is a manual, monthly
protocol (#445): CI's unit tests enforce the attestation's internal
consistency but deliberately never compare its dates against the wall clock,
because a unit test that turns red purely by time passing would fail
unrelated PRs. This script is the wall-clock half of that contract. A
scheduled workflow runs it weekly; when today is past `next_due_date` it
exits non-zero so the workflow can open or bump a tracking issue.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import sys

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ATTESTATION_PATH = (
    REPOSITORY_ROOT
    / "dashboard"
    / "looker_studio"
    / "bindings"
    / "report_template.yaml"
)


def staleness(report: dict, today: datetime.date) -> tuple[int, str]:
  """Return (exit_code, message) for the attestation's due state."""
  attestation = report["external_access_verification"]
  next_due = datetime.date.fromisoformat(attestation["next_due_date"])
  if today <= next_due:
    return 0, (
        "external-access attestation is current: next"
        f" external_identity_link_access_check run is due by {next_due}"
        f" (status {attestation['status']})."
    )
  return 1, (
      f"external-access attestation is OVERDUE: next_due_date {next_due} has"
      f" passed (today {today}, status {attestation['status']}, tracking"
      f" {attestation['tracking_issue']}). Re-run the"
      " external_identity_link_access_check canary per its protocol in"
      " dashboard/looker_studio/bindings/report_template.yaml, then record"
      " last_observed_date and the next next_due_date."
  )


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--today",
      default=None,
      help="ISO date overriding the wall clock (for tests).",
  )
  args = parser.parse_args(argv)
  today = (
      datetime.date.fromisoformat(args.today)
      if args.today
      else datetime.date.today()
  )
  report = yaml.safe_load(ATTESTATION_PATH.read_text())
  code, message = staleness(report, today)
  print(message, file=sys.stderr if code else sys.stdout)
  return code


if __name__ == "__main__":
  sys.exit(main())
