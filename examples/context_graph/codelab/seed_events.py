"""Synthetic agent_events generator for the BQAA codelab (compatibility shim).

The maintained command is now ``bqaa seed-events``. This wrapper forwards to
the same SDK module (``bigquery_agent_analytics.seed_events``) that backs the
CLI, so the downloaded codelab kit keeps working:

    python seed_events.py \\
        --project-id "$PROJECT_ID" \\
        --dataset-id "$DATASET" \\
        --sessions 5

Prefer ``bqaa seed-events`` once the SDK is installed.
"""

from __future__ import annotations

import argparse

from bigquery_agent_analytics.formatter import format_output
from bigquery_agent_analytics.seed_events import run_seed_events


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--project-id", required=True)
  parser.add_argument("--dataset-id", required=True)
  parser.add_argument("--sessions", type=int, default=5)
  parser.add_argument("--seed", type=int, default=None)
  parser.add_argument(
      "--format",
      dest="fmt",
      default="json",
      help="Output format: json|text|table.",
  )
  args = parser.parse_args()

  result = run_seed_events(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      sessions=args.sessions,
      seed=args.seed,
  )
  print(format_output(result.to_json(), args.fmt))
  # Mirror the CLI: BigQuery insert errors are reported (ok=False), not
  # raised, so fail the shell exit explicitly for downloaded-kit users.
  if not result.ok:
    raise SystemExit(1)


if __name__ == "__main__":
  main()
