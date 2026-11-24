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

"""Pattern discovery -> `EvalSuite` export and 8-lever cost cascade.

Converts BigQuery pattern discovery cohorts (``sql/05_pattern_discovery.sql``)
into a balanced ``bigquery_agent_analytics.EvalSuite`` passing
``suite.check_health()`` with zero warnings, and computes the 4-arm cost-cascade
ablation quantifying savings from raw-trace frontier judging down to the
4-layer deterministic + decision-model + scoped ``AI.GENERATE`` cascade.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
from typing import Any

from analytics.index_and_search import assert_pure_ascii_sql
from analytics.index_and_search import render_sql_tokens
from analytics.seed_scale_benchmark import generate_synthetic_eval_turns
from analytics.seed_scale_benchmark import validate_bq_identifiers
from analytics.trajectory_extractor import write_owner_only_text

from bigquery_agent_analytics import EvalCategory
from bigquery_agent_analytics import EvalSuite
from bigquery_agent_analytics import EvalTaskDef
from bigquery_agent_analytics import SuiteHealth

_AGENT_LINE_RE = re.compile(r"(?m)^AGENT:\s*(.+)$")
_FAIL_MARKER_RE = re.compile(
    r"\b(error|sorry|failed|unable|expired|cannot|declined|denied|restricted|"
    r"unauthorized)\b",
    re.IGNORECASE,
)

_DEFAULT_TOKEN_BLOAT_BYTES = 18000
_DEFAULT_TOKEN_BLOAT_TOKENS = 12000

_PATTERN_DESCRIPTIONS = {
    "SILENT_CONTRADICTION_CANDIDATE": (
        "Regression guard: tool returned an error, so the agent must report "
        "the failure rather than claiming completion."
    ),
    "TOOL_LOOP": (
        "Regression guard: agent repeated an identical failing tool call >= 3 "
        "times instead of stopping cleanly after retry budget."
    ),
    "EXACT_ERROR_SPIKE": (
        "Regression guard: verify explicit failure explanation and error code "
        "surface cleanly when a downstream tool returns an error."
    ),
    "TOKEN_BLOAT": (
        "Regression guard: verify oversized tool outputs remain bounded in "
        "trajectory context without exceeding latency or token budgets."
    ),
}


def _locate_sql_05() -> Path:
  candidates: list[Path] = []
  abs_here = Path(os.path.abspath(__file__))
  candidates.extend(abs_here.parents)
  resolved_here = Path(__file__).resolve()
  candidates.extend(resolved_here.parents)
  cwd = Path.cwd()
  candidates.append(cwd)
  candidates.extend(cwd.parents)
  for cand in candidates:
    p = cand / "sql" / "05_pattern_discovery.sql"
    if p.is_file():
      return p
    nested = (
        cand
        / "examples"
        / "combined_cuj_trace_search_demo"
        / "sql"
        / "05_pattern_discovery.sql"
    )
    if nested.is_file():
      return nested
  return resolved_here.parents[1] / "sql" / "05_pattern_discovery.sql"


def discover_patterns_offline(
    turn_rows: list[dict[str, Any]],
    *,
    token_bloat_bytes: int = _DEFAULT_TOKEN_BLOAT_BYTES,
    token_bloat_tokens: int = _DEFAULT_TOKEN_BLOAT_TOKENS,
) -> list[dict[str, Any]]:
  """Python parity implementation of ``sql/05_pattern_discovery.sql``."""
  grouped: dict[tuple[str, str], dict[str, Any]] = {}

  for row in turn_rows:
    root_agent = str(row.get("root_agent") or "unknown_agent")
    has_error = bool(row.get("has_error"))
    has_loop = bool(row.get("has_tool_loop"))
    total_tokens = int(row.get("total_tokens") or 0)
    raw_trace_bytes = int(row.get("raw_trace_bytes") or 0)
    error_summary = row.get("error_summary")
    traj = str(row.get("_ai_index_trajectory") or "")

    effective_has_error = has_error or bool(str(error_summary or "").strip())
    agent_matches = _AGENT_LINE_RE.findall(traj)
    agent_text = agent_matches[-1].strip() if agent_matches else ""
    is_silent_contradiction = bool(
        effective_has_error
        and agent_text
        and not _FAIL_MARKER_RE.search(agent_text)
    )

    if has_loop:
      pattern_class = "TOOL_LOOP"
    elif is_silent_contradiction:
      pattern_class = "SILENT_CONTRADICTION_CANDIDATE"
    elif effective_has_error:
      pattern_class = "EXACT_ERROR_SPIKE"
    elif (
        raw_trace_bytes >= token_bloat_bytes
        or total_tokens >= token_bloat_tokens
    ):
      pattern_class = "TOKEN_BLOAT"
    else:
      continue

    key = (root_agent, pattern_class)
    bucket = grouped.setdefault(
        key,
        {
            "pattern_id": f"{root_agent}:{pattern_class}",
            "root_agent": root_agent,
            "pattern_class": pattern_class,
            "affected_turns": 0,
            "total_tokens_spent": 0,
            "tool_loop_turns": 0,
            "silent_contradiction_candidates": 0,
            "sample_turns": [],
        },
    )
    bucket["affected_turns"] += 1
    bucket["total_tokens_spent"] += total_tokens
    if has_loop:
      bucket["tool_loop_turns"] += 1
    if is_silent_contradiction:
      bucket["silent_contradiction_candidates"] += 1
    bucket["sample_turns"].append(
        {
            "session_id": str(row.get("session_id") or ""),
            "invocation_id": str(row.get("invocation_id") or ""),
            "turn_index": int(row.get("turn_index") or 0),
            "has_error": has_error,
            "has_tool_loop": has_loop,
            "error_summary": error_summary,
            "total_tokens": total_tokens,
            "raw_trace_bytes": raw_trace_bytes,
            "distinct_tools_used": list(row.get("distinct_tools_used") or []),
            "_ai_index_trajectory": traj,
        }
    )

  patterns = list(grouped.values())
  for bucket in patterns:
    bucket["sample_turns"].sort(
        key=lambda t: (-int(t["total_tokens"]), t["session_id"])
    )
    bucket["sample_turns"] = bucket["sample_turns"][:5]

  patterns.sort(
      key=lambda p: (-int(p["affected_turns"]), -int(p["total_tokens_spent"]))
  )
  return patterns


def discover_patterns_bigquery(
    bq_client: Any,
    *,
    project_id: str,
    dataset_id: str,
    eval_turns_table: str = "eval_turns",
    location: str = "US",
    lookback_days: int = 7,
    token_bloat_bytes: int = _DEFAULT_TOKEN_BLOAT_BYTES,
    token_bloat_tokens: int = _DEFAULT_TOKEN_BLOAT_TOKENS,
) -> list[dict[str, Any]]:
  """Executes ``sql/05_pattern_discovery.sql`` against live BigQuery."""
  from google.cloud import bigquery

  validate_bq_identifiers(project_id, dataset_id, eval_turns_table)
  _ = location  # Caller initializes BigQuery client in the target location.
  sql_path = _locate_sql_05()
  template = sql_path.read_text(encoding="utf-8")
  assert_pure_ascii_sql(template, name=sql_path.name)
  query = render_sql_tokens(
      template,
      {
          "PROJECT_ID": project_id,
          "DATASET_ID": dataset_id,
          "EVAL_TURNS_TABLE": eval_turns_table,
      },
  )
  job_cfg = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "lookback_days", "INT64", lookback_days
          ),
          bigquery.ScalarQueryParameter(
              "token_bloat_bytes", "INT64", token_bloat_bytes
          ),
          bigquery.ScalarQueryParameter(
              "token_bloat_tokens", "INT64", token_bloat_tokens
          ),
      ]
  )
  rows = list(bq_client.query(query, job_config=job_cfg).result())
  patterns: list[dict[str, Any]] = []
  for r in rows:
    d = dict(r)
    samples = [dict(s) for s in (d.get("sample_turns") or [])]
    patterns.append(
        {
            "pattern_id": str(d["pattern_id"]),
            "root_agent": str(d["root_agent"]),
            "pattern_class": str(d["pattern_class"]),
            "affected_turns": int(d["affected_turns"]),
            "total_tokens_spent": int(d["total_tokens_spent"]),
            "tool_loop_turns": int(d["tool_loop_turns"]),
            "silent_contradiction_candidates": int(
                d["silent_contradiction_candidates"]
            ),
            "sample_turns": samples,
        }
    )
  return patterns


def _infer_tool_from_sample(sample: dict[str, Any]) -> str:
  tools = sample.get("distinct_tools_used") or []
  if tools:
    return str(tools[0])
  traj = str(sample.get("_ai_index_trajectory") or "")
  match = re.search(r"TOOL(?:_CALL|_RES)?:\s*([a-zA-Z0-9_]+)", traj)
  return match.group(1) if match else "lookup_order"


def _thresholds_for_pattern(pattern_class: str) -> dict[str, float]:
  if pattern_class == "TOOL_LOOP":
    return {"max_consecutive_identical_tool_calls": 2.0}
  if pattern_class == "SILENT_CONTRADICTION_CANDIDATE":
    return {"contradiction_rate": 0.0}
  if pattern_class == "TOKEN_BLOAT":
    return {"max_trajectory_tokens": 1500.0}
  return {"error_acknowledgment_rate": 1.0}


def build_eval_suite_from_patterns(
    *,
    patterns: list[dict[str, Any]],
    turn_rows: list[dict[str, Any]] | None = None,
    verifications: list[dict[str, Any]] | None = None,
    suite_name: str = "combined_cuj_discovered_regressions",
) -> tuple[EvalSuite, SuiteHealth]:
  """Constructs a balanced ``EvalSuite`` using real session IDs only."""
  suite = EvalSuite(name=suite_name)
  verified_by_key = {
      (str(v["session_id"]), str(v["invocation_id"])): v
      for v in (verifications or [])
      if "session_id" in v and "invocation_id" in v
  }
  default_root_agent = next(
      (str(r["root_agent"]) for r in (turn_rows or []) if r.get("root_agent")),
      next(
          (str(p["root_agent"]) for p in patterns if p.get("root_agent")),
          "unknown_agent",
      ),
  )

  # Identify genuine healthy turns before capping negative regression tasks so
  # class balance stays in [0.3, 0.7] without inventing synthetic session IDs.
  healthy_rows = [
      r
      for r in (turn_rows or [])
      if r.get("session_id")
      and not bool(r.get("has_error"))
      and not bool(r.get("has_tool_loop"))
      and not str(r.get("error_summary") or "").strip()
      and int(r.get("raw_trace_bytes") or 0) < _DEFAULT_TOKEN_BLOAT_BYTES
      and int(r.get("total_tokens") or 0) < _DEFAULT_TOKEN_BLOAT_TOKENS
  ]

  # Interleave up to 2 real samples per cohort so all discovered pattern
  # classes are represented before taking second samples.
  candidate_negatives: list[tuple[str, str, int, dict[str, Any]]] = []
  for s_idx in range(2):
    for cohort in patterns:
      pclass = str(cohort.get("pattern_class") or "EXACT_ERROR_SPIKE")
      samples = list(cohort.get("sample_turns") or [])
      if s_idx >= len(samples):
        continue
      sample = samples[s_idx]
      sess_id = str(sample.get("session_id") or "").strip()
      if not sess_id:
        continue
      cohort_agent = str(
          sample.get("root_agent")
          or cohort.get("root_agent")
          or default_root_agent
      )
      candidate_negatives.append((pclass, cohort_agent, s_idx, sample))

  max_neg = (
      min(12, (len(healthy_rows) * 7) // 3)
      if healthy_rows
      else min(12, len(candidate_negatives))
  )

  negative_count = 0
  seen_task_ids: set[str] = set()
  for pclass, cohort_agent, s_idx, sample in candidate_negatives:
    if negative_count >= max_neg:
      break
    sess_id = str(sample["session_id"]).strip()
    inv_id = str(sample.get("invocation_id") or "")
    task_id = f"reg_{pclass.lower()}_{sess_id}_{s_idx}"
    if task_id in seen_task_ids:
      continue
    seen_task_ids.add(task_id)
    tool_name = _infer_tool_from_sample(sample)
    verified = verified_by_key.get((sess_id, inv_id))
    tags = [
        f"pattern:{pclass}",
        f"root_agent:{cohort_agent}",
        "source:pattern_discovery",
    ]
    if verified and verified.get("is_silent_contradiction"):
      tags.append("verified:silent_contradiction")
    suite.add_task(
        EvalTaskDef(
            task_id=task_id,
            session_id=sess_id,
            description=_PATTERN_DESCRIPTIONS.get(
                pclass, f"Regression guard for {pclass}."
            ),
            category=EvalCategory.REGRESSION,
            expected_trajectory=[{"tool_name": tool_name, "args": {}}],
            expected_response=(
                "Report tool error accurately without contradicting outcome."
                if pclass == "SILENT_CONTRADICTION_CANDIDATE"
                else None
            ),
            thresholds=_thresholds_for_pattern(pclass),
            tags=tags,
            is_positive_case=False,
        )
    )
    negative_count += 1

  if healthy_rows:
    if negative_count > 0:
      min_pos = math.ceil(negative_count * 3 / 7)
      max_pos = (negative_count * 7) // 3
      target_positive = min(
          len(healthy_rows),
          max(min_pos, round(negative_count * 0.75)),
          max_pos,
      )
    else:
      target_positive = min(len(healthy_rows), 10)
    for idx in range(target_positive):
      row = healthy_rows[idx]
      sess_id = str(row["session_id"])
      tool_name = _infer_tool_from_sample(row)
      row_agent = str(row.get("root_agent") or default_root_agent)
      task_id = f"cap_positive_{sess_id}_{idx}"
      suite.add_task(
          EvalTaskDef(
              task_id=task_id,
              session_id=sess_id,
              description=(
                  "Positive capability baseline: complete healthy customer "
                  "request accurately within latency and token budget."
              ),
              category=EvalCategory.CAPABILITY,
              expected_trajectory=[{"tool_name": tool_name, "args": {}}],
              expected_response="Provide accurate order or policy confirmation.",
              thresholds={"task_completion": 1.0},
              tags=[
                  "pattern:HEALTHY_BASELINE",
                  f"root_agent:{row_agent}",
                  "source:pattern_discovery",
              ],
              is_positive_case=True,
          )
      )

  health = suite.check_health()
  return suite, health


def compute_eval_cost_cascade(
    *,
    turn_rows: list[dict[str, Any]] | None = None,
    search_report: dict[str, Any] | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
  """Computes the modeled 4-arm 8-lever cost-cascade projection table.

  Uses measured trajectory token/byte sizes and observed L0/L1 router latency
  when provided, combined with transparent analytical assumptions for 1,000
  evaluations, judge pricing, and modeled recall/p50 latencies across arms.
  """
  rows = turn_rows or []
  n_turns = max(1, len(rows))
  if rows:
    avg_s1_tokens = (
        sum(max(1, int(r.get("raw_trace_bytes") or 12000) // 4) for r in rows)
        / n_turns
    )
    avg_s3_tokens = (
        sum(
            max(
                40,
                len(str(r.get("_ai_index_trajectory") or "").encode("utf-8"))
                // 4,
            )
            for r in rows
        )
        / n_turns
    )
  else:
    avg_s1_tokens = 3200.0
    avg_s3_tokens = 95.0

  anomaly_count = sum(
      1
      for r in rows
      if bool(r.get("has_error"))
      or bool(r.get("has_tool_loop"))
      or bool(str(r.get("error_summary") or "").strip())
  )
  anomaly_frac = min(0.55, max(0.20, anomaly_count / n_turns)) if rows else 0.35
  cascade_frac = max(
      0.05, min(anomaly_frac * 0.55, (top_k / max(5, n_turns)) * 0.35)
  )

  # Uniform reference pricing (per 1M tokens) across all arms:
  # Frontier baseline judge (Arm 1: $3.00 in / $15.00 out).
  # Compact verifier model across Arms 2, 3, and 4: $0.30 in / $2.50 out
  # (gemini-4-hydrogen pricing; savings between Arms 2->3->4 come purely from
  # fewer escalated turns and lower thinking_level="LOW" output token budgets).
  frontier_in_per_1m = 3.00
  frontier_out_per_1m = 15.00
  compact_in_per_1m = 0.30
  compact_out_per_1m = 2.50
  router_prefill_per_1k = 0.046
  router_cascade_prefill_per_1k = 0.023

  arm1_tokens = int(round((avg_s1_tokens + 450.0) * 1000.0))
  arm1_cost = round(
      1000.0
      * (avg_s1_tokens * frontier_in_per_1m + 450.0 * frontier_out_per_1m)
      / 1_000_000.0,
      4,
  )

  arm2_tokens = int(round((avg_s3_tokens + 180.0) * 1000.0))
  arm2_cost = round(
      1000.0
      * (avg_s3_tokens * compact_in_per_1m + 180.0 * compact_out_per_1m)
      / 1_000_000.0,
      4,
  )

  arm3_tokens = int(
      round(1000.0 * (60.0 + anomaly_frac * (avg_s3_tokens + 120.0)))
  )
  arm3_cost = round(
      router_prefill_per_1k
      + 1000.0
      * anomaly_frac
      * (avg_s3_tokens * compact_in_per_1m + 120.0 * compact_out_per_1m)
      / 1_000_000.0,
      4,
  )

  arm4_tokens = int(
      round(1000.0 * (30.0 + cascade_frac * (avg_s3_tokens + 64.0)))
  )
  arm4_cost = round(
      router_cascade_prefill_per_1k
      + 1000.0
      * cascade_frac
      * (avg_s3_tokens * compact_in_per_1m + 64.0 * compact_out_per_1m)
      / 1_000_000.0,
      4,
  )

  router_latencies = [
      float(q.get("router_decision", {}).get("latency_ms", 0.0))
      for q in (search_report or {}).get("queries", [])
  ]
  median_router_ms = (
      float(statistics.median(router_latencies)) if router_latencies else 82.0
  )
  l1_p50 = round(median_router_ms + anomaly_frac * 320.0, 1)

  def _pct(cost: float) -> float:
    if arm1_cost <= 0:
      return 0.0
    return round((1.0 - cost / arm1_cost) * 100.0, 2)

  shared_measured_inputs = {
      "turn_count": len(rows),
      "avg_s1_tokens": round(avg_s1_tokens, 2),
      "avg_s3_tokens": round(avg_s3_tokens, 2),
      "anomaly_fraction": round(anomaly_frac, 4),
      "cascade_escalation_fraction": round(cascade_frac, 4),
      "measured_router_median_ms": round(median_router_ms, 2),
  }
  shared_pricing = {
      "frontier_in_per_1m_usd": frontier_in_per_1m,
      "frontier_out_per_1m_usd": frontier_out_per_1m,
      "compact_judge_in_per_1m_usd": compact_in_per_1m,
      "compact_judge_out_per_1m_usd": compact_out_per_1m,
  }

  return [
      {
          "arm_id": "arm_1_naive_raw_judge",
          "name": "Arm 1: Naive Raw Trace Frontier Judge (Modeled)",
          "estimation_mode": "MODELED_PROJECTION",
          "levers_applied": [],
          "tokens_per_1k_evals": arm1_tokens,
          "cost_per_1k_usd": arm1_cost,
          "latency_ms_p50": 2400.0,
          "latency_model": "analytical_projection",
          "contradiction_recall": 0.88,
          "recall_model": "analytical_projection",
          "savings_vs_naive_pct": 0.0,
          "measured_inputs": shared_measured_inputs,
          "pricing_model": shared_pricing,
      },
      {
          "arm_id": "arm_2_scoped_s3_trim",
          "name": "Arm 2: Turn Scope + Deterministic S3 Trim (Modeled)",
          "estimation_mode": "MODELED_PROJECTION",
          "levers_applied": [
              "1_code_first",
              "3_turn_scope",
              "4_boilerplate_trim",
          ],
          "tokens_per_1k_evals": arm2_tokens,
          "cost_per_1k_usd": arm2_cost,
          "latency_ms_p50": 620.0,
          "latency_model": "analytical_projection",
          "contradiction_recall": 0.94,
          "recall_model": "analytical_projection",
          "savings_vs_naive_pct": _pct(arm2_cost),
          "measured_inputs": shared_measured_inputs,
          "pricing_model": shared_pricing,
      },
      {
          "arm_id": "arm_3_routed_prefilter",
          "name": "Arm 3: + L0/L1 Router + SQL Cohort Prefilter (Modeled)",
          "estimation_mode": "MODELED_PROJECTION",
          "levers_applied": [
              "1_code_first",
              "2_targeted_sampling",
              "3_turn_scope",
              "4_boilerplate_trim",
              "5_decision_model",
              "6_storing_projection",
          ],
          "tokens_per_1k_evals": arm3_tokens,
          "cost_per_1k_usd": arm3_cost,
          "latency_ms_p50": l1_p50,
          "latency_model": "analytical_projection_from_router_median",
          "contradiction_recall": 0.96,
          "recall_model": "analytical_projection",
          "savings_vs_naive_pct": _pct(arm3_cost),
          "measured_inputs": shared_measured_inputs,
          "pricing_model": shared_pricing,
      },
      {
          "arm_id": "arm_4_full_cascade",
          "name": "Arm 4: Full 4-Layer Cascade + Scoped AI.GENERATE (Modeled)",
          "estimation_mode": "MODELED_PROJECTION",
          "levers_applied": [
              "1_code_first",
              "2_targeted_sampling",
              "3_turn_scope",
              "4_boilerplate_trim",
              "5_decision_model",
              "6_storing_projection",
              "7_thinking_level_low",
              "8_cascade_top_k_escalation",
          ],
          "tokens_per_1k_evals": arm4_tokens,
          "cost_per_1k_usd": arm4_cost,
          "latency_ms_p50": 145.0,
          "latency_model": "analytical_projection",
          "contradiction_recall": 0.98,
          "recall_model": "analytical_projection",
          "savings_vs_naive_pct": _pct(arm4_cost),
          "measured_inputs": shared_measured_inputs,
          "pricing_model": shared_pricing,
      },
  ]


def run_pattern_to_eval(
    *,
    project_id: str = "demo-project",
    dataset_id: str = "agent_ops",
    eval_turns_table: str = "eval_turns",
    location: str = "US",
    input_json: str | Path | None = None,
    search_json: str | Path | None = None,
    output_json: str | Path = "pattern_eval_report.json",
    eval_suite_output: str | Path = "eval_dataset_generated.json",
    lookback_days: int = 7,
    offline: bool = False,
    bq_client: Any = None,
) -> dict[str, Any]:
  """Runs pattern discovery, exports balanced EvalSuite, and computes cost cascade."""
  turn_rows: list[dict[str, Any]] = []
  if input_json is not None and Path(input_json).is_file():
    payload = json.loads(Path(input_json).read_text(encoding="utf-8"))
    turn_rows = list(payload.get("eval_turns") or [])

  search_report: dict[str, Any] | None = None
  verifications: list[dict[str, Any]] = []
  if search_json is not None and Path(search_json).is_file():
    search_report = json.loads(Path(search_json).read_text(encoding="utf-8"))
    for q in search_report.get("queries", []):
      verifications.extend(q.get("layer3_verifications") or [])

  if offline:
    if not turn_rows:
      turn_rows = generate_synthetic_eval_turns(count=10, prefix="offline_demo")
    patterns = discover_patterns_offline(turn_rows)
  else:
    from google.cloud import bigquery

    validate_bq_identifiers(project_id, dataset_id, eval_turns_table)
    client = bq_client or bigquery.Client(project=project_id, location=location)
    patterns = discover_patterns_bigquery(
        client,
        project_id=project_id,
        dataset_id=dataset_id,
        eval_turns_table=eval_turns_table,
        location=location,
        lookback_days=lookback_days,
    )
    if not turn_rows:
      # Fetch compact metadata only (never raw trace JSON) for cascade sizing.
      meta_sql = (
          "SELECT session_id, invocation_id, root_agent, has_error, "
          "has_tool_loop, error_summary, total_tokens, raw_trace_bytes, "
          "_ai_index_trajectory "
          f"FROM `{project_id}.{dataset_id}.{eval_turns_table}` "
          "LIMIT 500"
      )
      turn_rows = [dict(r) for r in client.query(meta_sql).result()]

  suite, health = build_eval_suite_from_patterns(
      patterns=patterns,
      turn_rows=turn_rows,
      verifications=verifications,
  )

  suite_path = Path(eval_suite_output)
  write_owner_only_text(suite_path, suite.to_json())

  cascade = compute_eval_cost_cascade(
      turn_rows=turn_rows,
      search_report=search_report,
      top_k=5,
  )

  report = {
      "generated_at_utc": datetime.now(timezone.utc).isoformat(),
      "offline": offline,
      "cost_cascade_methodology": "MODELED_PROJECTION_FROM_LIVE_BYTES_AND_ROUTING",
      "discovered_patterns": patterns,
      "eval_suite_path": str(suite_path),
      "suite_health": health.model_dump(mode="json"),
      "cost_cascade_ablation": cascade,
  }
  out_path = Path(output_json)
  write_owner_only_text(out_path, json.dumps(report, indent=2))
  return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description=(
          "Discover failure patterns from eval_turns, export a balanced "
          "EvalSuite, and compute 8-lever cost cascade reductions."
      )
  )
  parser.add_argument("--project-id", default="demo-project")
  parser.add_argument("--dataset-id", default="agent_ops")
  parser.add_argument("--eval-turns-table", default="eval_turns")
  parser.add_argument("--location", default="US")
  parser.add_argument("--input-json", default=None)
  parser.add_argument("--search-json", default=None)
  parser.add_argument("--output-json", default="pattern_eval_report.json")
  parser.add_argument(
      "--eval-suite-output", default="eval_dataset_generated.json"
  )
  parser.add_argument("--lookback-days", type=int, default=7)
  parser.add_argument("--offline", action="store_true")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = _parse_args(argv)
  report = run_pattern_to_eval(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      eval_turns_table=args.eval_turns_table,
      location=args.location,
      input_json=args.input_json,
      search_json=args.search_json,
      output_json=args.output_json,
      eval_suite_output=args.eval_suite_output,
      lookback_days=args.lookback_days,
      offline=args.offline,
  )
  print(
      json.dumps(
          {
              "output_json": args.output_json,
              "eval_suite_path": report["eval_suite_path"],
              "suite_health": report["suite_health"],
              "patterns_count": len(report["discovered_patterns"]),
          },
          indent=2,
      )
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
