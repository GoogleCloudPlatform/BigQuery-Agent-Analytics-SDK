#!/usr/bin/env python3
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

"""Quality evaluation report for agent traces stored in BigQuery.

Runs LLM-as-a-judge categorical evaluation over agent sessions using the
BigQuery Agent Analytics SDK.  Outputs a console summary and optionally
generates a Markdown report.

Required environment variables:
    PROJECT_ID       - GCP project containing the traces table
    DATASET_ID       - BigQuery dataset name
    TABLE_ID         - BigQuery table name (e.g. agent_events)
    DATASET_LOCATION - BigQuery dataset location (e.g. us-central1)

Optional environment variables:
    EVAL_MODEL_ID    - Model for evaluation (default: gemini-2.5-flash)
    GOOGLE_CLOUD_PROJECT  - GCP project for Vertex AI (defaults to PROJECT_ID)
    GOOGLE_CLOUD_LOCATION - Vertex AI location (default: global)

Usage:
    python quality_report.py                      # evaluate last 100 sessions
    python quality_report.py --limit 50           # evaluate last 50 sessions
    python quality_report.py --time-period 7d     # evaluate last 7 days
    python quality_report.py --report             # also generate markdown report
    python quality_report.py --no-eval            # browse Q&A only
    python quality_report.py --persist            # persist results to BigQuery
    python quality_report.py --samples 20         # show 20 sessions per category
    python quality_report.py --samples all        # show all sessions
    python quality_report.py --app-name my_agent  # filter to a specific agent
    python quality_report.py --output-json r.json # write structured JSON output
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime


def _positive_int(value):
  n = int(value)
  if n < 1:
    raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
  return n


def _samples_arg(value):
  if value == "all":
    return "all"
  n = int(value)
  if n < 1:
    raise argparse.ArgumentTypeError("--samples must be 'all' or >= 1")
  return str(n)

_script_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.join(_script_dir, "..")

logger = logging.getLogger("quality_report")


def _configure_logging():
  """Configure logging format. Called once from main()."""
  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s [%(levelname)s] %(message)s",
      datefmt="%H:%M:%S",
  )


def _load_dotenv():
  """Load .env file if present (optional convenience)."""
  try:
    from dotenv import load_dotenv

    for candidate in [
        os.path.join(_script_dir, ".env"),
        os.path.join(_repo_root, ".env"),
    ]:
      if os.path.isfile(candidate):
        load_dotenv(candidate, override=True)
        break
  except ImportError:
    pass


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
  val = os.environ.get(name)
  if not val:
    logger.error("Required environment variable %s is not set.", name)
    sys.exit(1)
  return val


def _load_config():
  """Load configuration from environment variables (called lazily)."""
  os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
  os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
  global PROJECT_ID, DATASET_ID, TABLE_ID, DATASET_LOCATION, EVAL_MODEL_ID
  PROJECT_ID = _require_env("PROJECT_ID")
  DATASET_ID = _require_env("DATASET_ID")
  TABLE_ID = _require_env("TABLE_ID")
  DATASET_LOCATION = _require_env("DATASET_LOCATION")
  EVAL_MODEL_ID = os.getenv("EVAL_MODEL_ID", "gemini-2.5-flash")


PROJECT_ID = None
DATASET_ID = None
TABLE_ID = None
DATASET_LOCATION = None
EVAL_MODEL_ID = None


# ---------------------------------------------------------------------------
# SDK client
# ---------------------------------------------------------------------------

def get_client():
  from bigquery_agent_analytics import Client

  return Client(
      project_id=PROJECT_ID,
      dataset_id=DATASET_ID,
      table_id=TABLE_ID,
      location=DATASET_LOCATION,
  )


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

def get_eval_metrics():
  from bigquery_agent_analytics import (
      CategoricalMetricCategory,
      CategoricalMetricDefinition,
  )

  response_usefulness = CategoricalMetricDefinition(
      name="response_usefulness",
      definition=(
          "Whether the agent's final response provides a genuinely useful, "
          "substantive answer to the user's question. A response that apologizes, "
          "says it cannot help, returns no data, provides only generic filler, "
          "or loops without resolving the question is NOT useful."
      ),
      categories=[
          CategoricalMetricCategory(
              name="meaningful",
              definition=(
                  "The response directly and substantively addresses the user's "
                  "question with specific, actionable information."
              ),
          ),
          CategoricalMetricCategory(
              name="unhelpful",
              definition=(
                  "The response technically succeeded (no error) but does NOT "
                  "meaningfully answer the user's question. Examples: apologies, "
                  "'I don't have that information', empty data results, generic "
                  "filler text, or the agent looping without a resolution."
              ),
          ),
          CategoricalMetricCategory(
              name="partial",
              definition=(
                  "The response partially addresses the question but is "
                  "incomplete, missing key details, or only tangentially relevant."
              ),
          ),
      ],
  )

  task_grounding = CategoricalMetricDefinition(
      name="task_grounding",
      definition=(
          "Whether the agent's response is grounded in actual data retrieved "
          "from its tools, or is fabricated / hallucinated general knowledge."
      ),
      categories=[
          CategoricalMetricCategory(
              name="grounded",
              definition=(
                  "The response is clearly based on data retrieved from the "
                  "agent's tools (search results, database lookups, API calls)."
              ),
          ),
          CategoricalMetricCategory(
              name="ungrounded",
              definition=(
                  "The response appears to be fabricated or based on the LLM's "
                  "general knowledge rather than actual tool results. The tool "
                  "may have returned empty data and the agent filled in anyway."
              ),
          ),
          CategoricalMetricCategory(
              name="no_tool_needed",
              definition=(
                  "The question did not require tool usage and a direct LLM "
                  "response was appropriate."
              ),
          ),
      ],
  )

  return [response_usefulness, task_grounding]


# ---------------------------------------------------------------------------
# Trace helpers - extract Q&A and resolve A2A responses
# ---------------------------------------------------------------------------

def get_user_input(trace) -> str:
  for span in trace.spans:
    if span.event_type == "USER_MESSAGE_RECEIVED":
      c = span.content
      if isinstance(c, dict):
        return c.get("text_summary") or c.get("text") or ""
      elif c:
        return str(c)
  return ""


def get_responding_agent(trace) -> str:
  for span in reversed(trace.spans):
    if span.event_type == "LLM_RESPONSE":
      c = span.content
      if isinstance(c, dict):
        resp = c.get("response", "")
        if resp and not resp.startswith("call:"):
          return span.agent or "unknown"
  return "no_response"


def _is_single_word_routing(response: str) -> bool:
  if not response:
    return True
  stripped = response.strip()
  return len(stripped.split()) <= 1 and len(stripped) < 20


def _extract_a2a_text(payload) -> tuple:
  if not isinstance(payload, dict):
    return (str(payload) if payload else None), None

  text_parts = []
  for artifact in payload.get("artifacts", []):
    for part in artifact.get("parts", []):
      if part.get("kind") == "text" and part.get("text"):
        text_parts.append(part["text"])

  if not text_parts:
    for msg in payload.get("history", []):
      if msg.get("role") == "agent":
        for part in msg.get("parts", []):
          if part.get("kind") == "text" and part.get("text"):
            text_parts.append(part["text"])

  meta = payload.get("metadata", {})
  agent_name = meta.get("adk_app_name") or meta.get("adk_author")
  text = " ".join(text_parts) if text_parts else None
  return text, agent_name


def get_a2a_response(trace) -> tuple:
  for span in reversed(trace.spans):
    if span.event_type == "A2A_INTERACTION":
      c = span.content
      if isinstance(c, dict):
        text, agent = _extract_a2a_text(c)
        if text:
          return text, agent or span.agent or "remote_agent"
      elif isinstance(c, str):
        try:
          parsed = json.loads(c)
          text, agent = _extract_a2a_text(parsed)
          if text:
            return text, agent or span.agent or "remote_agent"
        except (json.JSONDecodeError, TypeError):
          logger.warning(
              "Failed to parse A2A payload for session, skipping"
          )
          return None, None
  return None, None


# ---------------------------------------------------------------------------
# Resolve responses for a batch of traces
# ---------------------------------------------------------------------------

def resolve_trace_responses(traces):
  results = []
  remote_lookups = 0

  for trace in traces:
    question = get_user_input(trace)
    if not question:
      continue

    response = trace.final_response
    if response:
      stripped = response.strip()
      if stripped.startswith("call:") or _is_single_word_routing(stripped):
        response = None
    answered_by = get_responding_agent(trace)
    is_a2a = False

    if not response:
      a2a_resp, a2a_agent = get_a2a_response(trace)
      if a2a_resp:
        response = a2a_resp
        answered_by = a2a_agent
        is_a2a = True
        remote_lookups += 1

    latency_s = None
    if trace.total_latency_ms is not None:
      latency_s = round(trace.total_latency_ms / 1000, 1)

    results.append({
        "session_id": trace.session_id,
        "time": (
            trace.start_time.strftime("%Y-%m-%d %H:%M:%S")
            if trace.start_time
            else "?"
        ),
        "question": question,
        "answered_by": answered_by,
        "response": (response or ""),
        "latency_s": latency_s,
        "is_a2a": is_a2a,
    })

  if remote_lookups:
    logger.info("Resolved %d A2A responses", remote_lookups)

  return results


# ---------------------------------------------------------------------------
# Run evaluation
# ---------------------------------------------------------------------------

def run_evaluation(
    time_range=None, limit=100, model=None, persist=False, app_name=None
) -> dict:
  from bigquery_agent_analytics import CategoricalEvaluationConfig, TraceFilter

  model = model or EVAL_MODEL_ID
  client = get_client()

  metrics = get_eval_metrics()
  cat_config = CategoricalEvaluationConfig(
      metrics=metrics,
      endpoint=model,
      temperature=0.0,
      include_justification=True,
      persist_results=persist,
      results_table="quality_eval_results" if persist else None,
  )

  effective_time_range = time_range
  if effective_time_range and effective_time_range.lower() == "all":
    effective_time_range = None

  if effective_time_range:
    trace_filter = TraceFilter.from_cli_args(last=effective_time_range)
  else:
    trace_filter = TraceFilter()
  trace_filter.limit = limit
  if app_name:
    trace_filter.root_agent_name = app_name

  report = client.evaluate_categorical(config=cat_config, filters=trace_filter)

  all_session_ids = [sr.session_id for sr in report.session_results]
  logger.info("Resolving responses for %d sessions...", len(all_session_ids))

  traces = client.list_traces(
      filter_criteria=TraceFilter(
          session_ids=all_session_ids, limit=len(all_session_ids)
      )
  )
  resolved = resolve_trace_responses(traces)
  resolved_map = {r["session_id"]: r for r in resolved}

  return {
      "report": report,
      "resolved_map": resolved_map,
  }


# ---------------------------------------------------------------------------
# Category labels
# ---------------------------------------------------------------------------

def _category_label(category):
  labels = {
      "meaningful": "\u2705 HELPFUL",
      "unhelpful": "\u274c NOT HELPFUL",
      "partial": "\u26a0\ufe0f  PARTIAL",
      "grounded": "\u2705 GROUNDED",
      "ungrounded": "\u274c NOT GROUNDED",
      "no_tool_needed": "\u2796 NO TOOL NEEDED",
  }
  return labels.get(category, (category or "?").upper())


# ---------------------------------------------------------------------------
# Browse mode (--no-eval)
# ---------------------------------------------------------------------------

def run_browse(args):
  from bigquery_agent_analytics import TraceFilter

  client = get_client()
  logger.info(
      "Project: %s, Dataset: %s, Table: %s", PROJECT_ID, DATASET_ID, TABLE_ID
  )

  time_range = args.time_period
  if time_range and time_range.lower() == "all":
    time_range = None
  if time_range:
    trace_filter = TraceFilter.from_cli_args(last=time_range)
  else:
    trace_filter = TraceFilter()
  trace_filter.limit = args.limit
  if args.app_name:
    trace_filter.root_agent_name = args.app_name

  traces = client.list_traces(filter_criteria=trace_filter)
  logger.info("Fetched %d sessions", len(traces))

  results = resolve_trace_responses(traces)

  if not results:
    print("\n  No sessions found.")
    return

  total = len(results)
  with_response = sum(1 for r in results if r["response"])
  no_response = total - with_response
  a2a_count = sum(1 for r in results if r.get("is_a2a"))

  print(f"\n{'=' * 90}")
  summary = (
      f"  {total} sessions  |  {with_response} with response  "
      f"|  {no_response} no response"
  )
  if a2a_count:
    summary += f"  |  {a2a_count} A2A"
  print(summary)
  print(f"{'=' * 90}")

  for r in results:
    a2a_tag = "  [A2A]" if r.get("is_a2a") else ""
    print(f"\n  [{r['time']}] {r['session_id']}{a2a_tag}")
    print(f"    Question:  {r['question']}")
    print(f"    Agent:     {r['answered_by']}")
    if r["response"]:
      resp = " ".join(r["response"].split())
      print(f'    Response:  "{resp}"')
    else:
      print("    Response:  (none)")
    if r.get("latency_s") is not None:
      print(f"    Latency:   {r['latency_s']}s")

  print(f"\n{'=' * 90}\n")


# ---------------------------------------------------------------------------
# Eval mode (default)
# ---------------------------------------------------------------------------

def run_eval(args):
  model = args.model or EVAL_MODEL_ID
  logger.info(
      "Project: %s, Dataset: %s, Table: %s", PROJECT_ID, DATASET_ID, TABLE_ID
  )
  logger.info("Location: %s", DATASET_LOCATION)
  logger.info("Evaluation model: %s", model)
  logger.info(
      "Parameters: time_period=%s, limit=%d, persist=%s, report=%s, samples=%s",
      args.time_period or "all",
      args.limit,
      args.persist,
      args.report,
      args.samples or "default (10/5/3)",
  )

  t0 = time.time()
  try:
    result = run_evaluation(
        time_range=args.time_period,
        limit=args.limit,
        model=model,
        persist=args.persist,
        app_name=args.app_name,
    )
  except Exception:
    logger.exception("Evaluation failed")
    sys.exit(1)
  elapsed = time.time() - t0

  result["report"].details["elapsed_seconds"] = round(elapsed, 1)
  result["report"].details["project"] = PROJECT_ID
  result["report"].details["dataset"] = f"{DATASET_ID}.{TABLE_ID}"
  result["report"].details["location"] = DATASET_LOCATION
  result["report"].details["eval_model"] = model
  result["report"].details["time_period"] = args.time_period or "all"
  result["report"].details["limit"] = args.limit
  result["report"].details["persist"] = args.persist
  result["report"].details["samples"] = args.samples or "default (10/5/3)"
  _print_eval_results(result["report"], result["resolved_map"], samples=args.samples,
                      unhelpful_threshold=args.threshold)

  report_path = None
  if args.report:
    report_path = _write_md_report(result["report"], result["resolved_map"], args)

  if report_path:
    print(f"\n  Markdown report: {report_path}")

  if args.output_json:
    output = _build_json_output(result["report"], result["resolved_map"])
    if args.output_json == "-":
      json.dump(output, sys.stdout, indent=2, default=str)
      print(f"\n  JSON report: (stdout)")
    else:
      json_path = os.path.abspath(args.output_json)
      os.makedirs(os.path.dirname(json_path), exist_ok=True)
      with open(json_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
      print(f"\n  JSON report: {json_path}")


def _group_by_category(report):
  by_category = {"unhelpful": [], "partial": [], "meaningful": []}
  for sr in report.session_results:
    for mr in sr.metrics:
      if mr.metric_name == "response_usefulness":
        cat = mr.category or "unknown"
        by_category.setdefault(cat, []).append(sr)
        break
  return by_category


def _build_agent_stats(report, resolved_map):
  agent_stats = {}
  for sr in report.session_results:
    ctx = resolved_map.get(sr.session_id, {})
    agent = ctx.get("answered_by") or "unknown"
    if agent not in agent_stats:
      agent_stats[agent] = {
          "total": 0,
          "meaningful": 0,
          "unhelpful": 0,
          "partial": 0,
          "unclassified": 0,
          "a2a_count": 0,
      }
    agent_stats[agent]["total"] += 1
    if ctx.get("is_a2a"):
      agent_stats[agent]["a2a_count"] += 1
    found_usefulness = False
    for mr in sr.metrics:
      if mr.metric_name == "response_usefulness":
        found_usefulness = True
        if mr.category == "meaningful":
          agent_stats[agent]["meaningful"] += 1
        elif mr.category == "unhelpful":
          agent_stats[agent]["unhelpful"] += 1
        elif mr.category == "partial":
          agent_stats[agent]["partial"] += 1
        else:
          agent_stats[agent]["unclassified"] += 1
        break
    if not found_usefulness:
      agent_stats[agent]["unclassified"] += 1
  return agent_stats


_METRIC_LABELS = {
    "response_usefulness": "Usefulness",
    "task_grounding": "Grounding",
}


def _print_eval_results(report, resolved_map, samples=None, unhelpful_threshold=10.0):
  hr = "\u2500" * 70

  by_category = _group_by_category(report)
  a2a_session_ids = {
      sid for sid, ctx in resolved_map.items() if ctx.get("is_a2a")
  }

  # --- Per-session details ---
  _default_samples = {"unhelpful": 10, "partial": 5, "meaningful": 3, "unknown": 3}
  for cat, cat_label in [
      ("unhelpful", "UNHELPFUL"),
      ("partial", "PARTIAL"),
      ("meaningful", "MEANINGFUL"),
      ("unknown", "UNCLASSIFIED (parse errors)"),
  ]:
    limit = (
        len(by_category.get(cat, []))
        if samples == "all"
        else (int(samples) if samples else _default_samples.get(cat, 5))
    )
    sessions = by_category.get(cat, [])
    if not sessions:
      continue

    print(f"\n{hr}")
    print(
        f"  {cat_label} Sessions "
        f"(showing {min(len(sessions), limit)} of {len(sessions)})"
    )
    print(hr)

    for sr in sessions[:limit]:
      sid = sr.session_id
      ctx = resolved_map.get(sid, {})
      question = ctx.get("question", "")
      response = ctx.get("response", "")
      answered_by = ctx.get("answered_by", "")

      a2a_tag = "  [A2A]" if sid in a2a_session_ids else ""
      agent_tag = f"  \u2192 {answered_by}" if answered_by else ""
      print(f"\n  Session:     {sid}{a2a_tag}{agent_tag}")
      q = " ".join(question.split()) if question else "(none)"
      r = " ".join(response.split()) if response else "(none)"
      print(f"  Question:    {q}")
      print(f'  Response:    "{r}"')

      for mr in sr.metrics:
        mr_label = _category_label(mr.category)
        if mr.parse_error:
          mr_label += "  [parse error]"
        display_name = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
        print(f"  {display_name + ':':<15}{mr_label}")
        if mr.justification:
          print(f"  {'Reason:':<15}{mr.justification}")
        if mr.parse_error and mr.raw_response:
          raw = mr.raw_response[:300]
          print(f"  {'Raw LLM out:':<15}{repr(raw)}")

  # --- Per-agent breakdown ---
  agent_stats = _build_agent_stats(report, resolved_map)

  if agent_stats:
    total_helpful_all = sum(s["meaningful"] for s in agent_stats.values())
    total_unhelpful_all = sum(s["unhelpful"] for s in agent_stats.values())

    print(f"\n{hr}")
    print("  PER-AGENT QUALITY")
    print(hr)

    hdr = (
        f"  {'Agent':<30s} {'Sess':>4s}  {'Status':>6s}  "
        f"{'Helpful':>12s}  {'Unhelpful':>12s}  "
        f"{'Partial':>7s}  {'Errors':>6s}  "
        f"{'% of All':>8s}  {'% of All':>8s}"
    )
    hdr2 = (
        f"  {'':<30s} {'':>4s}  {'':>6s}  "
        f"{'':>12s}  {'':>12s}  "
        f"{'':>7s}  {'':>6s}  "
        f"{'Helpful':>8s}  {'Unhelpful':>8s}"
    )
    print(hdr)
    print(hdr2)
    print("  " + "\u2500" * 106)

    for agent, stats in sorted(
        agent_stats.items(), key=lambda x: -x[1]["total"]
    ):
      total = stats["total"]
      classified = stats["meaningful"] + stats["unhelpful"] + stats["partial"]
      helpful_pct = (
          (stats["meaningful"] / classified * 100) if classified > 0 else 0
      )
      unhelpful_pct = (
          (stats["unhelpful"] / classified * 100) if classified > 0 else 0
      )
      helpful_contrib = (
          (stats["meaningful"] / total_helpful_all * 100)
          if total_helpful_all > 0
          else 0
      )
      unhelpful_contrib = (
          (stats["unhelpful"] / total_unhelpful_all * 100)
          if total_unhelpful_all > 0
          else 0
      )
      a2a_n = stats["a2a_count"]
      a2a_tag = (
          f" [A2A:{a2a_n}/{total}]" if 0 < a2a_n < total
          else " [A2A]" if a2a_n == total
          else ""
      )
      status = (
          "\U0001f7e2"
          if helpful_pct >= 80
          else ("\U0001f7e1" if helpful_pct >= 60 else "\U0001f534")
      )
      agent_name = f"{agent}{a2a_tag}"
      helpful_str = f"{stats['meaningful']} ({helpful_pct:.0f}%)"
      unhelpful_str = f"{stats['unhelpful']} ({unhelpful_pct:.0f}%)"
      partial_str = str(stats["partial"])
      errors_str = str(stats.get("unclassified", 0))

      line = (
          f"  {agent_name:<30s} {total:>4d}  {status:>6s}  "
          f"{helpful_str:>12s}  {unhelpful_str:>12s}  "
          f"{partial_str:>7s}  {errors_str:>6s}  "
          f"{helpful_contrib:>7.0f}%  {unhelpful_contrib:>7.0f}%"
      )
      print(line)

    unhelpful_agents = [
        (a, s) for a, s in agent_stats.items() if s["unhelpful"] > 0
    ]
    if unhelpful_agents:
      print("\n  " + "\u2500" * 50)
      print("  UNHELPFUL CONTRIBUTION RANKING (worst first):")
      print("  " + "\u2500" * 50)
      for agent, stats in sorted(
          unhelpful_agents, key=lambda x: -x[1]["unhelpful"]
      ):
        contrib = (
            (stats["unhelpful"] / total_unhelpful_all * 100)
            if total_unhelpful_all > 0
            else 0
        )
        bar = "\u2588" * int(contrib / 2)
        a2a_n = stats["a2a_count"]
        a2a_tag = (
            f" [A2A:{a2a_n}/{stats['total']}]" if 0 < a2a_n < stats["total"]
            else " [A2A]" if a2a_n == stats["total"]
            else ""
        )
        agent_name = f"{agent}{a2a_tag}"
        print(
            f"  {agent_name:<40s} {stats['unhelpful']:>3d}"
            f"  ({contrib:>5.1f}%)  {bar}"
        )

  # --- Summary ---
  fp_count = len(by_category.get("unhelpful", []))
  partial_count = len(by_category.get("partial", []))
  meaningful_count = len(by_category.get("meaningful", []))
  unknown_count = len(by_category.get("unknown", []))
  total = report.total_sessions
  fp_rate = (fp_count / total * 100) if total > 0 else 0.0

  print(f"\n{'=' * 70}")
  print("QUALITY SUMMARY")
  print(f"{'=' * 70}")
  print(f"  Total sessions evaluated : {total}")
  print(f"  Meaningful               : {meaningful_count}")
  print(f"  Partial                  : {partial_count}")
  print(f"  Unhelpful                : {fp_count}")
  print(f"  Unhelpful rate           : {fp_rate:.1f}%")
  if unknown_count:
    parse_error_metrics = report.details.get("parse_errors", "?")
    print(
        f"  Parse errors             : "
        f"{unknown_count} session(s) ({parse_error_metrics} metric evals)"
    )
  if a2a_session_ids:
    print(f"  A2A sessions detected    : {len(a2a_session_ids)}")

  print("\n  Category Distributions:")
  for metric_name, dist in report.category_distributions.items():
    print(f"\n  [{metric_name}]")
    dist_total = sum(dist.values())
    for category, count in sorted(dist.items(), key=lambda x: -x[1]):
      pct = (count / dist_total * 100) if dist_total > 0 else 0.0
      bar = "#" * int(pct / 2)
      print(f"    {_category_label(category):18s}: {count:4d}  ({pct:5.1f}%) {bar}")

  hide_keys = {"parse_errors", "parse_error_rate"}
  print("\n  Execution Details:")
  for key, value in report.details.items():
    if key in hide_keys:
      continue
    v = str(value)[:120]
    print(f"    {key}: {v}")
  print(f"    created_at: {report.created_at.isoformat()}")

  print(f"{'=' * 70}")

  if fp_rate > unhelpful_threshold:
    print(f"\n  WARNING: Unhelpful rate ({fp_rate:.1f}%) exceeds {unhelpful_threshold:.0f}% threshold!")
  elif fp_rate > 0:
    print(f"\n  Unhelpful responses detected but below {unhelpful_threshold:.0f}% threshold.")
  else:
    print("\n  All responses were meaningful.")


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------

def _write_md_report(report, resolved_map, args):
  lines = []
  w = lines.append

  timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  w("# Quality Evaluation Report")
  w("")
  w(f"**Generated:** {timestamp}  ")
  w(f"**Project:** {PROJECT_ID}  ")
  w(f"**Dataset:** {DATASET_ID}.{TABLE_ID}  ")
  w(f"**Location:** {DATASET_LOCATION}  ")
  model = args.model or EVAL_MODEL_ID
  w(f"**Eval model:** {model}  ")
  w(f"**Sessions:** {report.total_sessions}  ")
  w("")

  by_category = _group_by_category(report)
  a2a_session_ids = {
      sid for sid, ctx in resolved_map.items() if ctx.get("is_a2a")
  }

  fp_count = len(by_category.get("unhelpful", []))
  partial_count = len(by_category.get("partial", []))
  meaningful_count = len(by_category.get("meaningful", []))
  unknown_count = len(by_category.get("unknown", []))
  total = report.total_sessions
  fp_rate = (fp_count / total * 100) if total > 0 else 0.0

  # --- Summary ---
  w("## Summary")
  w("")
  w("| Metric | Value |")
  w("|--------|-------|")
  w(f"| Total sessions | {total} |")
  w(f"| Meaningful | {meaningful_count} |")
  w(f"| Partial | {partial_count} |")
  w(f"| Unhelpful | {fp_count} |")
  w(f"| Unhelpful rate | {fp_rate:.1f}% |")
  if unknown_count:
    parse_error_metrics = report.details.get("parse_errors", "?")
    w(
        f"| Parse errors | {unknown_count} session(s) "
        f"({parse_error_metrics} metric evals) |"
    )
  if a2a_session_ids:
    w(f"| A2A sessions | {len(a2a_session_ids)} |")
  w("")

  # --- Category Distributions ---
  w("## Category Distributions")
  w("")
  for metric_name, dist in report.category_distributions.items():
    w(f"### {metric_name}")
    w("")
    w("| Category | Count | % |")
    w("|----------|------:|--:|")
    dist_total = sum(dist.values())
    for category, count in sorted(dist.items(), key=lambda x: -x[1]):
      pct = (count / dist_total * 100) if dist_total > 0 else 0.0
      label = _category_label(category)
      w(f"| {label} | {count} | {pct:.1f}% |")
    w("")

  # --- Per-Agent Quality ---
  agent_stats = _build_agent_stats(report, resolved_map)
  if agent_stats:
    w("## Per-Agent Quality")
    w("")
    w("| Agent | Sessions | Helpful | Unhelpful | Partial | Status |")
    w("|-------|-------:|--------:|----------:|--------:|--------|")
    for agent, stats in sorted(
        agent_stats.items(), key=lambda x: -x[1]["total"]
    ):
      classified = stats["meaningful"] + stats["unhelpful"] + stats["partial"]
      helpful_pct = (
          (stats["meaningful"] / classified * 100) if classified > 0 else 0
      )
      a2a_n = stats["a2a_count"]
      total = stats["total"]
      a2a_tag = (
          f" [A2A:{a2a_n}/{total}]" if 0 < a2a_n < total
          else " [A2A]" if a2a_n == total
          else ""
      )
      status = (
          "\U0001f7e2"
          if helpful_pct >= 80
          else ("\U0001f7e1" if helpful_pct >= 60 else "\U0001f534")
      )
      w(
          f"| {agent}{a2a_tag} | {stats['total']} "
          f"| {stats['meaningful']} ({helpful_pct:.0f}%) "
          f"| {stats['unhelpful']} | {stats['partial']} | {status} |"
      )
    w("")

  # --- Unhelpful Sessions ---
  unhelpful_sessions = by_category.get("unhelpful", [])
  _md_samples = None if args.samples == "all" else (int(args.samples) if args.samples else None)
  if unhelpful_sessions:
    shown = unhelpful_sessions if _md_samples is None else unhelpful_sessions[:_md_samples]
    w("## Unhelpful Sessions")
    if len(shown) < len(unhelpful_sessions):
      w(f"\n*Showing {len(shown)} of {len(unhelpful_sessions)}*")
    w("")
    for sr in shown:
      sid = sr.session_id
      ctx = resolved_map.get(sid, {})
      question = ctx.get("question", "")
      response = ctx.get("response", "")
      answered_by = ctx.get("answered_by", "")
      a2a_tag = " [A2A]" if sid in a2a_session_ids else ""

      q = " ".join(question.split()) if question else "(none)"
      r = " ".join(response.split()) if response else "(none)"

      w(f"### `{sid}`{a2a_tag} \u2192 {answered_by}")
      w("")
      w(f"- **Question:** {q}")
      r_display = (r[:500] + "\u2026") if len(r) > 500 else r
      w(f"- **Response:** {r_display}")
      for mr in sr.metrics:
        label = _category_label(mr.category)
        display = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
        w(f"- **{display}:** {label}")
        if mr.justification:
          w(f"  - *{mr.justification}*")
      w("")

  # --- Partial Sessions ---
  partial_sessions = by_category.get("partial", [])
  if partial_sessions:
    shown = partial_sessions if _md_samples is None else partial_sessions[:_md_samples]
    w("## Partial Sessions")
    if len(shown) < len(partial_sessions):
      w(f"\n*Showing {len(shown)} of {len(partial_sessions)}*")
    w("")
    for sr in shown:
      sid = sr.session_id
      ctx = resolved_map.get(sid, {})
      question = ctx.get("question", "")
      response = ctx.get("response", "")
      answered_by = ctx.get("answered_by", "")
      a2a_tag = " [A2A]" if sid in a2a_session_ids else ""

      q = " ".join(question.split()) if question else "(none)"
      r = " ".join(response.split()) if response else "(none)"

      w(f"### `{sid}`{a2a_tag} \u2192 {answered_by}")
      w("")
      w(f"- **Question:** {q}")
      r_display = (r[:500] + "\u2026") if len(r) > 500 else r
      w(f"- **Response:** {r_display}")
      for mr in sr.metrics:
        label = _category_label(mr.category)
        display = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
        w(f"- **{display}:** {label}")
        if mr.justification:
          w(f"  - *{mr.justification}*")
      w("")

  # --- Execution Details ---
  w("## Execution Details")
  w("")
  hide_keys = {"parse_errors", "parse_error_rate"}
  for key, value in report.details.items():
    if key in hide_keys:
      continue
    w(f"- **{key}:** {str(value)[:200]}")
  w(f"- **created_at:** {report.created_at.isoformat()}")
  w("")

  # Write file
  report_dir = os.path.join(_script_dir, "reports")
  os.makedirs(report_dir, exist_ok=True)
  ts = datetime.now().strftime("%Y%m%d_%H%M%S")
  report_path = os.path.join(report_dir, f"quality_report_{ts}.md")
  with open(report_path, "w") as f:
    f.write("\n".join(lines) + "\n")

  return os.path.abspath(report_path)


# ---------------------------------------------------------------------------
# JSON report output
# ---------------------------------------------------------------------------

def _build_json_output(report, resolved_map):
  """Build a structured dict for JSON output of evaluation results."""
  by_category = _group_by_category(report)
  agent_stats = _build_agent_stats(report, resolved_map)

  sessions = []
  for sr in report.session_results:
    ctx = resolved_map.get(sr.session_id, {})
    metrics = {}
    for mr in sr.metrics:
      metrics[mr.metric_name] = {
          "category": mr.category,
          "justification": mr.justification,
      }
    sessions.append({
        "session_id": sr.session_id,
        "question": ctx.get("question", ""),
        "response": ctx.get("response", ""),
        "answered_by": ctx.get("answered_by", ""),
        "is_a2a": ctx.get("is_a2a", False),
        "latency_s": ctx.get("latency_s"),
        "metrics": metrics,
    })

  fp_count = len(by_category.get("unhelpful", []))
  partial_count = len(by_category.get("partial", []))
  meaningful_count = len(by_category.get("meaningful", []))
  total = report.total_sessions

  return {
      "summary": {
          "total_sessions": total,
          "meaningful": meaningful_count,
          "partial": partial_count,
          "unhelpful": fp_count,
          "meaningful_rate": round(meaningful_count / total * 100, 1) if total else 0,
          "unhelpful_rate": round(fp_count / total * 100, 1) if total else 0,
      },
      "category_distributions": {
          k: dict(v) for k, v in report.category_distributions.items()
      },
      "per_agent": {
          agent: dict(stats) for agent, stats in agent_stats.items()
      },
      "sessions": sessions,
      "details": {k: str(v) for k, v in report.details.items()},
  }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
  parser = argparse.ArgumentParser(
      description="Quality evaluation report for agent traces in BigQuery",
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog="""
Examples:
  %(prog)s                           Evaluate last 100 sessions (default)
  %(prog)s --limit 50                Evaluate last 50 sessions
  %(prog)s --no-eval                 Browse Q&A pairs without evaluation
  %(prog)s --report                  Also generate a Markdown report
  %(prog)s --persist                 Evaluate and persist results to BQ
  %(prog)s --time-period 7d          Evaluate last 7 days
  %(prog)s --samples 20              Show up to 20 sessions per category
  %(prog)s --samples all             Show all sessions per category
  %(prog)s --app-name my_agent       Filter to a specific agent app
  %(prog)s --output-json report.json Write structured JSON output
      """,
  )
  parser.add_argument(
      "--limit", type=_positive_int, default=100,
      help="Number of sessions (default: 100)",
  )
  parser.add_argument(
      "--eval",
      action="store_true",
      default=True,
      help="Run full quality evaluation (default: on)",
  )
  parser.add_argument(
      "--no-eval",
      dest="eval",
      action="store_false",
      help="Browse Q&A pairs without evaluation",
  )
  parser.add_argument(
      "--time-period",
      type=str,
      default="all",
      help="Time range: 24h, 7d, or 'all' (default: all)",
  )
  parser.add_argument(
      "--persist",
      action="store_true",
      help="Persist evaluation results to BigQuery",
  )
  parser.add_argument(
      "--model",
      type=str,
      default=None,
      help="Model for evaluation (default: EVAL_MODEL_ID or gemini-2.5-flash)",
  )
  parser.add_argument(
      "--report",
      action="store_true",
      help="Generate a Markdown report in scripts/reports/",
  )
  parser.add_argument(
      "--samples",
      type=_samples_arg,
      default=None,
      help="Max sample sessions to display per category, or 'all' (default: 10/5/3)",
  )
  parser.add_argument(
      "--app-name",
      type=str,
      default=None,
      help="Filter to sessions from a specific agent app name (root_agent_name)",
  )
  parser.add_argument(
      "--output-json",
      type=str,
      default=None,
      metavar="PATH",
      help="Write structured evaluation results as JSON to the given file path",
  )
  parser.add_argument(
      "--threshold",
      type=float,
      default=10.0,
      help="Unhelpful rate warning threshold in %% (default: 10)",
  )

  args = parser.parse_args()

  _configure_logging()
  _load_dotenv()
  _load_config()

  if args.eval:
    run_eval(args)
  else:
    run_browse(args)


if __name__ == "__main__":
  main()
