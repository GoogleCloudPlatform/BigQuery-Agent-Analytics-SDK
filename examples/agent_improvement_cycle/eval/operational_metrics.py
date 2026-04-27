# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Operational metrics gate for the improvement cycle.

Runs the SDK's deterministic evaluators (latency, token_efficiency,
turn_count) against sessions logged to BigQuery. Supports two modes:

  Baseline mode (single set):
    python operational_metrics.py \\
        --sessions reports/expected_session_ids_cycle_1.json \\
        --label "V1"

  Comparison mode (before/after):
    python operational_metrics.py \\
        --before-sessions reports/expected_session_ids_cycle_1.json \\
        --after-sessions reports/expected_session_ids_cycle_1_fresh.json \\
        --before-label "V1" --after-label "V2"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from dotenv import load_dotenv

# Load .env from demo directory
_env_path = os.path.join(os.path.dirname(__file__), "../.env")
if os.path.exists(_env_path):
    load_dotenv(dotenv_path=_env_path)

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

import google.auth

_, _auth_project = google.auth.default()

PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_ID = os.getenv("DATASET_ID", "agent_logs")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")

# Metrics to evaluate and their default thresholds
METRICS = {
    "latency": {"threshold": 10000, "unit": "ms", "label": "Avg latency"},
    "token_efficiency": {"threshold": 50000, "unit": "tokens", "label": "Total tokens"},
    "turn_count": {"threshold": 10, "unit": "turns", "label": "Turn count"},
    "error_rate": {"threshold": 0.1, "unit": "rate", "label": "Tool error rate"},
}


def _load_session_ids(path: str) -> list[str]:
    """Extract session IDs from eval result JSON."""
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return [r["session_id"] for r in data if r.get("session_id")]
    if isinstance(data, dict) and "sessions" in data:
        return [s["session_id"] for s in data["sessions"] if s.get("session_id")]
    return []


def run_metrics(session_ids: list[str]) -> dict:
    """Run deterministic evaluators against specific sessions.

    Returns dict of metric_name -> {label, unit, threshold, total,
    passed, failed, pass_rate, avg_observed}.
    """
    from bigquery_agent_analytics import Client
    from bigquery_agent_analytics.evaluators import CodeEvaluator
    from bigquery_agent_analytics.trace import TraceFilter

    client = Client(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
        location=DATASET_LOCATION,
    )

    results = {}
    for metric_name, cfg in METRICS.items():
        if metric_name == "latency":
            evaluator = CodeEvaluator.latency(threshold_ms=cfg["threshold"])
        elif metric_name == "token_efficiency":
            evaluator = CodeEvaluator.token_efficiency(max_tokens=cfg["threshold"])
        elif metric_name == "turn_count":
            evaluator = CodeEvaluator.turn_count(max_turns=cfg["threshold"])
        elif metric_name == "error_rate":
            evaluator = CodeEvaluator.error_rate(max_error_rate=cfg["threshold"])
        else:
            continue

        filters = TraceFilter(session_ids=session_ids)
        report = client.evaluate(evaluator=evaluator, filters=filters)

        # Extract observed values from session scores.
        # Details structure: {"metric_latency": {"observed": 3200, "budget": 10000, ...}}
        observed_values = []
        for ss in report.session_scores:
            for detail_key, detail_val in ss.details.items():
                if isinstance(detail_val, dict) and detail_val.get("observed") is not None:
                    observed_values.append(detail_val["observed"])

        avg_observed = (
            sum(observed_values) / len(observed_values) if observed_values else 0
        )

        results[metric_name] = {
            "label": cfg["label"],
            "unit": cfg["unit"],
            "threshold": cfg["threshold"],
            "total": report.total_sessions,
            "passed": report.passed_sessions,
            "failed": report.failed_sessions,
            "pass_rate": report.pass_rate,
            "avg_observed": round(avg_observed, 1),
        }

    return results


def print_baseline(metrics: dict, label: str):
    """Print a single-set metrics table (baseline)."""
    print("")
    print(f"  {'Metric':<18}  {label:>14}  {'Budget':>14}  {'Status':>8}")
    print(f"  {'─' * 18}  {'─' * 14}  {'─' * 14}  {'─' * 8}")

    for metric_name, cfg in METRICS.items():
        m = metrics.get(metric_name, {})
        val = m.get("avg_observed", "n/a")
        unit = cfg["unit"]
        threshold = cfg["threshold"]
        # Compare average observed value against budget, not per-session pass rate.
        if isinstance(val, (int, float)):
            status = "PASS" if val <= threshold else "WARN"
        else:
            status = "PASS"

        v_str = f"{val} {unit}" if isinstance(val, (int, float)) else str(val)
        t_str = f"{threshold} {unit}"
        print(f"  {cfg['label']:<18}  {v_str:>14}  {t_str:>14}  {status:>8}")

    print("")


def print_comparison(before: dict, after: dict, before_label: str, after_label: str):
    """Print a before/after comparison table."""
    print("")
    print(f"  {'Metric':<18} {'':>4}  {before_label:>14}  {after_label:>14}  {'Budget':>14}  {'Status':>8}")
    print(f"  {'─' * 18} {'':>4}  {'─' * 14}  {'─' * 14}  {'─' * 14}  {'─' * 8}")

    all_pass = True
    for metric_name, cfg in METRICS.items():
        b = before.get(metric_name, {})
        a = after.get(metric_name, {})
        label = cfg["label"]
        unit = cfg["unit"]
        b_val = b.get("avg_observed", "n/a")
        a_val = a.get("avg_observed", "n/a")
        threshold = cfg["threshold"]

        if isinstance(a_val, (int, float)) and isinstance(b_val, (int, float)):
            delta = a_val - b_val
            direction = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
        else:
            direction = "?"

        # Compare average observed value against budget, not per-session pass rate.
        if isinstance(a_val, (int, float)):
            status = "PASS" if a_val <= threshold else "WARN"
        else:
            status = "PASS"
        if status == "WARN":
            all_pass = False

        b_str = f"{b_val} {unit}" if isinstance(b_val, (int, float)) else str(b_val)
        a_str = f"{a_val} {unit}" if isinstance(a_val, (int, float)) else str(a_val)
        t_str = f"{threshold} {unit}"

        print(f"  {label:<18} {direction:>4}  {b_str:>14}  {a_str:>14}  {t_str:>14}  {status:>8}")

    print("")
    if all_pass:
        print("  All operational metrics within budget.")
    else:
        print("  Some metrics exceeded budget. Review thresholds.")
    print("")
    return all_pass


def main():
    parser = argparse.ArgumentParser(
        description="Run operational metrics: baseline or before/after comparison."
    )
    # Baseline mode
    parser.add_argument(
        "--sessions",
        help="Path to eval results JSON (baseline mode).",
    )
    parser.add_argument(
        "--label", default="Current",
        help="Label for baseline column.",
    )
    # Comparison mode
    parser.add_argument(
        "--before-sessions",
        help="Path to eval results JSON from before improvement.",
    )
    parser.add_argument(
        "--after-sessions",
        help="Path to eval results JSON from after improvement.",
    )
    parser.add_argument("--before-label", default="Before")
    parser.add_argument("--after-label", default="After")
    # Common
    parser.add_argument(
        "--output", default=None,
        help="Optional path to save JSON results.",
    )
    args = parser.parse_args()

    # Baseline mode: single set
    if args.sessions:
        ids = _load_session_ids(args.sessions)
        if not ids:
            print(f"  No session IDs found in {args.sessions}", file=sys.stderr)
            sys.exit(1)

        print(f"  Evaluating {len(ids)} {args.label} sessions...")
        results = run_metrics(ids)
        print_baseline(results, args.label)

        if args.output:
            output = {"label": args.label, "sessions": len(ids), "metrics": results}
            with open(args.output, "w") as f:
                json.dump(output, f, indent=2, default=str)
            print(f"  Saved to: {args.output}")
        sys.exit(0)

    # Comparison mode: before/after
    if not args.before_sessions or not args.after_sessions:
        parser.error("Provide --sessions (baseline) or both --before-sessions and --after-sessions (comparison).")

    before_ids = _load_session_ids(args.before_sessions)
    after_ids = _load_session_ids(args.after_sessions)

    if not before_ids:
        print(f"  No session IDs found in {args.before_sessions}", file=sys.stderr)
        sys.exit(1)
    if not after_ids:
        print(f"  No session IDs found in {args.after_sessions}", file=sys.stderr)
        sys.exit(1)

    print(f"  Evaluating {len(before_ids)} {args.before_label} sessions and {len(after_ids)} {args.after_label} sessions...")

    before_results = run_metrics(before_ids)
    after_results = run_metrics(after_ids)

    all_pass = print_comparison(
        before_results, after_results,
        args.before_label, args.after_label,
    )

    if args.output:
        output = {
            "before": {"label": args.before_label, "sessions": len(before_ids), "metrics": before_results},
            "after": {"label": args.after_label, "sessions": len(after_ids), "metrics": after_results},
            "all_pass": all_pass,
        }
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"  Saved to: {args.output}")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
