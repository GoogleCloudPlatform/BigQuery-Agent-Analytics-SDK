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

Deliberately stdlib-only: the scheduled workflow that runs it must not
install packages, so nothing mutable executes in the job chain that ends
with an issue-writing token (#446 review). The three fields are extracted
with anchored line patterns instead of a YAML parser; the unit tests
cross-check the extraction against a real YAML load, so a restructuring of
the attestation block fails CI rather than silently misparsing here.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import re
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ATTESTATION_PATH = (
    REPOSITORY_ROOT
    / "dashboard"
    / "looker_studio"
    / "bindings"
    / "report_template.yaml"
)
BQCA_ATTESTATION_PATH = (
    REPOSITORY_ROOT
    / "dashboard"
    / "looker_studio"
    / "bindings"
    / "bqca_report_template.yaml"
)
DEFAULT_ATTESTATION_PATHS = (
    ATTESTATION_PATH,
    BQCA_ATTESTATION_PATH,
)

# Anchored to the attestation block's own two-space indentation, so the
# four-space `status:` of a known_live_issues entry can never match.
_NEXT_DUE_RE = re.compile(r'^  next_due_date: "(\d{4}-\d{2}-\d{2})"$', re.M)
_STATUS_RE = re.compile(r"^  status: (\S+)$", re.M)
_TRACKING_RE = re.compile(r"^  tracking_issue: (\S+)$", re.M)


def read_attestation_fields(
    text: str, source: Path = ATTESTATION_PATH
) -> dict[str, str]:
  """Extract next_due_date, status, and tracking_issue or die loudly."""
  block = text.split("external_access_verification:", 1)
  if len(block) != 2:
    raise SystemExit(f"no external_access_verification block in {source}")
  fields = {}
  for name, pattern in (
      ("next_due_date", _NEXT_DUE_RE),
      ("status", _STATUS_RE),
      ("tracking_issue", _TRACKING_RE),
  ):
    match = pattern.search(block[1])
    if match is None:
      raise SystemExit(
          f"could not extract {name} from the external_access_verification"
          f" block of {source}"
      )
    fields[name] = match.group(1)
  return fields


def staleness(
    fields: dict[str, str],
    today: datetime.date,
    source: Path = ATTESTATION_PATH,
) -> tuple[int, str]:
  """Return (exit_code, message) for the attestation's due state."""
  try:
    rel_source = source.resolve().relative_to(REPOSITORY_ROOT).as_posix()
  except ValueError:
    rel_source = str(source)
  next_due = datetime.date.fromisoformat(fields["next_due_date"])
  if today <= next_due:
    return 0, (
        f"external-access attestation ({rel_source}) is current: next"
        f" external_identity_link_access_check run is due by {next_due}"
        f" (status {fields['status']})."
    )
  return 1, (
      f"external-access attestation ({rel_source}) is OVERDUE: next_due_date"
      f" {next_due} has passed (today {today}, status {fields['status']},"
      f" tracking {fields['tracking_issue']}). Re-run the"
      " external_identity_link_access_check canary per its protocol in"
      f" {rel_source}, then record"
      " last_observed_date and the next next_due_date."
  )


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--today",
      default=None,
      help="ISO date overriding the wall clock (for tests).",
  )
  parser.add_argument(
      "--attestation-path",
      action="append",
      type=Path,
      default=None,
      help=(
          "Path to a report template binding YAML with"
          " external_access_verification (may be repeated; defaults to both"
          " report_template.yaml and bqca_report_template.yaml)."
      ),
  )
  args = parser.parse_args(argv)
  today = (
      datetime.date.fromisoformat(args.today)
      if args.today
      else datetime.date.today()
  )
  raw_paths = (
      args.attestation_path
      if args.attestation_path
      else list(DEFAULT_ATTESTATION_PATHS)
  )
  overall_code = 0
  for raw_path in raw_paths:
    path = (
        raw_path
        if raw_path.is_absolute() or raw_path.exists()
        else REPOSITORY_ROOT / raw_path
    )
    fields = read_attestation_fields(path.read_text(), source=path)
    code, message = staleness(fields, today, source=path)
    print(message, file=sys.stderr if code else sys.stdout)
    if code != 0:
      overall_code = code
  return overall_code


if __name__ == "__main__":
  sys.exit(main())
