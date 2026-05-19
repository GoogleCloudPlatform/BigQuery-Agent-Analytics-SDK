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

"""Trace latency analyzer — shows a timing tree for agent sessions.

Fetches the latest traces from BigQuery Agent Analytics and renders
an execution tree with per-span latency and a waterfall timeline.
Automatically stitches A2A remote agent sessions to show full
cross-agent latency breakdown.

Required environment variables:
    PROJECT_ID       - GCP project containing the traces table
    DATASET_ID       - BigQuery dataset name
    TABLE_ID         - BigQuery table name (e.g. agent_events)
    DATASET_LOCATION - BigQuery dataset location (e.g. us-central1)

Usage:
    python latency_report.py                          # latest trace
    python latency_report.py --limit 5                # last 5 traces
    python latency_report.py --session <session_id>   # specific session
    python latency_report.py --time-period 1h         # traces from last hour
    python latency_report.py --app-name my_agent      # filter by agent app
    python latency_report.py --verbose                # show questions/responses
    python latency_report.py --no-stitch              # skip A2A stitching
    python latency_report.py --no-waterfall           # skip waterfall chart
    python latency_report.py --sdk-tree               # show SDK built-in tree
    python latency_report.py --report                 # generate Markdown report
    python latency_report.py --output-json r.json     # write structured JSON
    python latency_report.py --env path/to/.env       # load a specific .env file
"""
import warnings

warnings.filterwarnings("ignore")

import argparse
from collections import Counter
from datetime import datetime
import json
import logging
import os
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.join(_script_dir, "..")

logger = logging.getLogger("latency_report")


def _configure_logging():
  """Configure logging format. Called once from main()."""
  log_level = os.environ.get("LOGLEVEL", "INFO").upper()
  logging.basicConfig(
      level=getattr(logging, log_level, logging.INFO),
      format="%(asctime)s [%(levelname)s] %(message)s",
      datefmt="%H:%M:%S",
  )
  for _noisy in (
      "google.genai",
      "google_genai",
      "google.adk",
      "google_adk",
      "google.auth",
      "google_auth",
      "httpx",
      "httpcore",
  ):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


def _load_dotenv(env_file=None):
  """Load .env file if present (optional convenience)."""
  try:
    from dotenv import load_dotenv

    if env_file:
      load_dotenv(env_file, override=True)
      return

    for candidate in [
        os.path.join(_script_dir, ".env"),
        os.path.join(_repo_root, ".env"),
    ]:
      if os.path.isfile(candidate):
        load_dotenv(candidate, override=False)
        break
  except ImportError:
    pass


def _positive_int(value):
  n = int(value)
  if n < 1:
    raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
  return n


def format_ms(ms):
  """Format milliseconds as human-readable."""
  if ms is None:
    return "?"
  if ms < 1000:
    return f"{ms:.0f}ms"
  elif ms < 60000:
    return f"{ms/1000:.1f}s"
  else:
    return f"{ms/60000:.1f}min"


def _span_label(span):
  """Build a concise label for a span."""
  # A2A interaction: show direction with arrow
  if span.event_type == "A2A_INTERACTION":
    remote = None
    if isinstance(span.content, dict):
      metadata = span.content.get("metadata", {})
      remote = metadata.get("adk_app_name")
    if remote:
      return f"{span.agent or '?'} ──► {remote} [A2A]"

  parts = []
  if span.agent:
    parts.append(span.agent)
  parts.append(span.event_type)
  if span.event_type in ("TOOL_STARTING", "TOOL_COMPLETED", "TOOL_ERROR"):
    tool = span.content.get("tool") if isinstance(span.content, dict) else None
    if tool:
      label = " > ".join(parts) if len(parts) > 1 else parts[0]
      return f"{label} ({tool})"
  return " > ".join(parts) if len(parts) > 1 else parts[0]


def _extract_text(span):
  """Extract text content from a span for verbose mode."""
  c = span.content
  if not isinstance(c, dict):
    return str(c)[:120] if c else None

  # User message
  text = c.get("text_summary") or c.get("text")
  if text:
    return text[:200]

  # LLM response text
  resp = c.get("response", "")
  if isinstance(resp, str) and resp and not resp.startswith("call:"):
    return resp[:200]

  # Function call
  if "function_call" in c:
    fc = c["function_call"]
    name = fc.get("name", "?")
    args = fc.get("args", {})
    return f"call: {name}({args})"

  # A2A response
  for artifact in c.get("artifacts", []):
    for part in artifact.get("parts", []):
      if part.get("text"):
        return part["text"][:200]

  return None


_A2A_NOISE_EVENTS = frozenset(
    {
        "USER_MESSAGE_RECEIVED",
        "INVOCATION_STARTING",
        "INVOCATION_COMPLETED",
    }
)


def stitch_a2a_traces(traces, client):
  """Fetch and inline A2A remote agent spans into parent traces.

  Mutates *traces* in place.

  When the supervisor calls a remote agent via A2A, only
  AGENT_STARTING/COMPLETED are logged in the parent session.
  The remote agent's internal LLM and tool spans live in a
  separate BQ session linked via content.metadata.adk_session_id.
  This fetches those sessions and inlines the spans as children.
  """
  for trace in traces:
    a2a_spans = []
    for span in trace.spans:
      if span.event_type != "A2A_INTERACTION":
        continue
      content = span.content
      if not isinstance(content, dict):
        continue
      metadata = content.get("metadata", {})
      remote_sid = metadata.get("adk_session_id")
      if remote_sid and span.span_id:
        agent_name = metadata.get("adk_app_name", "remote")
        a2a_spans.append((span.span_id, remote_sid, agent_name))

    for a2a_span_id, remote_sid, agent_name in a2a_spans:
      try:
        remote_trace = client.get_session_trace(remote_sid)
      except Exception as e:
        logger.warning("Could not fetch %s A2A session: %s", agent_name, e)
        continue

      interesting = [
          s for s in remote_trace.spans if s.event_type not in _A2A_NOISE_EVENTS
      ]
      if not interesting:
        continue

      # Reparent orphaned spans (whose parent was filtered out)
      # under the A2A interaction span in the parent trace
      kept_ids = {s.span_id for s in interesting if s.span_id}
      for rs in interesting:
        rs.attributes["_a2a_source"] = agent_name
        if not rs.parent_span_id or rs.parent_span_id not in kept_ids:
          rs.parent_span_id = a2a_span_id

      trace.spans.extend(interesting)
      logger.info(
          "Stitched %s: %d spans from A2A session %s...",
          agent_name,
          len(interesting),
          remote_sid[:12],
      )


def render_timing_tree(trace, verbose=False):
  """Render a trace as a timing tree with latency annotations."""
  lines = []

  # Header
  total = format_ms(trace.total_latency_ms)
  time_str = trace.start_time.strftime("%H:%M:%S") if trace.start_time else "?"
  lines.append(f"Session: {trace.session_id}")
  lines.append(f"Time: {time_str}  Total: {total}")
  lines.append("─" * 70)

  # Build parent-child tree
  by_id = {}
  for span in trace.spans:
    if span.span_id:
      by_id[span.span_id] = span
    span.children = []

  roots = []
  for span in trace.spans:
    parent = span.parent_span_id
    if parent and parent in by_id:
      by_id[parent].children.append(span)
    else:
      roots.append(span)

  def render_node(span, prefix="", is_last=True):
    connector = "└── " if is_last else "├── "
    label = _span_label(span)

    timing_parts = []
    if span.latency_ms is not None:
      timing_parts.append(format_ms(span.latency_ms))
    if span.time_to_first_token_ms is not None:
      timing_parts.append(f"ttft={format_ms(span.time_to_first_token_ms)}")
    timing = f" [{', '.join(timing_parts)}]" if timing_parts else ""

    status = ""
    if span.is_error:
      status = " !! ERROR"
      if span.error_message:
        status += f": {span.error_message[:60]}"

    lines.append(f"{prefix}{connector}{label}{timing}{status}")

    if verbose:
      text = _extract_text(span)
      if text:
        child_prefix = prefix + ("    " if is_last else "│   ")
        # Wrap text
        wrapped = text[:120]
        if len(text) > 120:
          wrapped += "..."
        lines.append(f'{child_prefix}  "{wrapped}"')

    child_prefix = prefix + ("    " if is_last else "│   ")
    children = span.children

    # Show A2A separator when entering stitched remote spans
    # Only show at the boundary (parent is not stitched, children are)
    parent_is_stitched = bool(span.attributes.get("_a2a_source"))
    if not parent_is_stitched:
      a2a_source = None
      for c in children:
        src = c.attributes.get("_a2a_source") if c.attributes else None
        if src:
          a2a_source = src
          break
      if a2a_source:
        lines.append(f"{child_prefix}┄┄┄ remote session ({a2a_source}) ┄┄┄")

    for i, child in enumerate(children):
      render_node(child, child_prefix, i == len(children) - 1)

  for i, root in enumerate(roots):
    render_node(root, "", i == len(roots) - 1)

  return "\n".join(lines)


def render_waterfall(trace):
  """Render a simple waterfall showing time distribution."""
  if not trace.start_time or not trace.total_latency_ms:
    return ""

  lines = []
  lines.append("")
  lines.append("Waterfall:")

  BAR_W = 40
  total_ms = trace.total_latency_ms or 1

  # Collect spans with latency, sorted by timestamp
  timed_spans = [
      s
      for s in trace.spans
      if s.latency_ms is not None
      and (
          s.latency_ms > 0
          or s.event_type in ("TOOL_COMPLETED", "TOOL_STARTING")
      )
  ]
  timed_spans.sort(key=lambda s: s.timestamp)

  if not timed_spans:
    lines.append("  (no per-span latency data)")
    return "\n".join(lines)

  # Build labels using _span_label — no truncation
  labels = [_span_label(span) for span in timed_spans]

  # Mark stitched A2A spans
  for i, span in enumerate(timed_spans):
    if span.attributes.get("_a2a_source"):
      labels[i] += " [A2A]"

  # Number duplicate labels so they're distinguishable
  counts = Counter(labels)
  seen = {}
  for i, lbl in enumerate(labels):
    if counts[lbl] > 1:
      seen[lbl] = seen.get(lbl, 0) + 1
      labels[i] = f"{lbl} #{seen[lbl]}"

  label_w = max(len(l) for l in labels) + 2  # pad for readability

  lines.append("─" * (label_w + BAR_W + 12))

  base_time = trace.start_time
  for span, label in zip(timed_spans, labels):
    offset_ms = (span.timestamp - base_time).total_seconds() * 1000
    start_pos = int(offset_ms / total_ms * BAR_W)
    bar_len = max(1, int(span.latency_ms / total_ms * BAR_W))
    start_pos = min(start_pos, BAR_W - 1)
    bar_len = min(bar_len, BAR_W - start_pos)

    bar = " " * start_pos + "█" * bar_len
    lines.append(f"  {label:<{label_w}} {bar} {format_ms(span.latency_ms)}")

  lines.append(f"  {'':{label_w}} {'─' * BAR_W}")
  # Time axis labels (approximate alignment; format_ms width varies)
  markers = f"  {'':{label_w}} 0{'':>{BAR_W//2-1}}{format_ms(total_ms/2)}{'':>{BAR_W//2-1}}{format_ms(total_ms)}"
  lines.append(markers)

  return "\n".join(lines)


def render_summary_table(traces):
  """Render a summary table across multiple traces."""
  if len(traces) <= 1:
    return ""

  lines = []
  lines.append("")
  lines.append("=" * 70)
  lines.append("Summary")
  lines.append("=" * 70)

  latencies = [t.total_latency_ms for t in traces if t.total_latency_ms]
  if not latencies:
    lines.append("  No latency data available")
    return "\n".join(lines)

  latencies.sort()
  avg = sum(latencies) / len(latencies)
  p50 = latencies[len(latencies) // 2]
  p95 = latencies[int(len(latencies) * 0.95)]
  mn = latencies[0]
  mx = latencies[-1]

  lines.append(f"  Sessions: {len(traces)}")
  lines.append(f"  Avg:  {format_ms(avg)}")
  lines.append(f"  P50:  {format_ms(p50)}")
  lines.append(f"  P95:  {format_ms(p95)}")
  lines.append(f"  Min:  {format_ms(mn)}")
  lines.append(f"  Max:  {format_ms(mx)}")

  # Per-agent breakdown
  agent_latencies = {}
  for trace in traces:
    for span in trace.spans:
      if span.latency_ms and span.agent:
        agent_latencies.setdefault(span.agent, []).append(span.latency_ms)

  if agent_latencies:
    lines.append("")
    lines.append("  Per-agent latency (avg):")
    for agent, lats in sorted(
        agent_latencies.items(), key=lambda x: -sum(x[1]) / len(x[1])
    ):
      a = sum(lats) / len(lats)
      lines.append(f"    {agent:<30} {format_ms(a):>8}  (n={len(lats)})")

  return "\n".join(lines)


def _build_json_output(traces):
  """Build a structured dict for JSON output of latency results."""
  latencies = [t.total_latency_ms for t in traces if t.total_latency_ms]
  latencies.sort()

  summary = {"sessions": len(traces)}
  if latencies:
    summary.update(
        {
            "avg_ms": round(sum(latencies) / len(latencies), 1),
            "p50_ms": latencies[len(latencies) // 2],
            "p95_ms": latencies[int(len(latencies) * 0.95)],
            "min_ms": latencies[0],
            "max_ms": latencies[-1],
        }
    )

  agent_latencies = {}
  for trace in traces:
    for span in trace.spans:
      if span.latency_ms and span.agent:
        agent_latencies.setdefault(span.agent, []).append(span.latency_ms)

  per_agent = {}
  for agent, lats in agent_latencies.items():
    per_agent[agent] = {
        "avg_ms": round(sum(lats) / len(lats), 1),
        "count": len(lats),
    }

  sessions = []
  for trace in traces:
    sessions.append(
        {
            "session_id": trace.session_id,
            "total_latency_ms": trace.total_latency_ms,
            "start_time": trace.start_time.isoformat()
            if trace.start_time
            else None,
            "span_count": len(trace.spans),
        }
    )

  return {
      "summary": summary,
      "per_agent": per_agent,
      "sessions": sessions,
  }


def _write_md_report(
    traces, project_id, dataset_id, table_id, dataset_location
):
  """Write a Markdown latency report to scripts/reports/."""
  lines = []
  w = lines.append

  timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  w("# Latency Report")
  w("")
  w(f"**Generated:** {timestamp}  ")
  w(f"**Project:** {project_id}  ")
  w(f"**Dataset:** {dataset_id}.{table_id}  ")
  w(f"**Location:** {dataset_location}  ")
  w(f"**Sessions:** {len(traces)}  ")
  w("")

  for trace in traces:
    w("```")
    w(render_timing_tree(trace))
    waterfall = render_waterfall(trace)
    if waterfall:
      w(waterfall)
    w("```")
    w("")

  if len(traces) > 1:
    w("## Summary")
    w("")
    w("```")
    w(render_summary_table(traces))
    w("```")
    w("")

  report_dir = os.path.join(_script_dir, "reports")
  os.makedirs(report_dir, exist_ok=True)
  ts = datetime.now().strftime("%Y%m%d_%H%M%S")
  report_path = os.path.join(report_dir, f"latency_report_{ts}.md")
  with open(report_path, "w") as f:
    f.write("\n".join(lines) + "\n")

  return os.path.abspath(report_path)


def main():
  parser = argparse.ArgumentParser(
      description="Trace latency analyzer for agent sessions in BigQuery",
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog="""\
examples:
  %(prog)s                          Show the latest trace
  %(prog)s --limit 5                Show last 5 traces with summary stats
  %(prog)s --time-period 1h         Traces from the last hour
  %(prog)s --session <id>           Specific session
  %(prog)s --app-name my_agent      Filter by root agent name
  %(prog)s --verbose                Show questions and responses
  %(prog)s --no-stitch              Skip A2A session stitching
  %(prog)s --env path/to/.env       Load environment from a specific .env file
      """,
  )
  parser.add_argument(
      "--limit",
      type=_positive_int,
      default=1,
      help="Number of recent traces to fetch (default: 1)",
  )
  parser.add_argument(
      "--session",
      type=str,
      help="Fetch a specific session by ID",
  )
  parser.add_argument(
      "--time-period",
      type=str,
      default="all",
      help="Time range filter: 1h, 30m, 7d, or 'all' (default: all)",
  )
  parser.add_argument(
      "--app-name",
      type=str,
      default=None,
      help="Filter to sessions from a specific agent app name (root_agent_name)",
  )
  parser.add_argument(
      "--verbose",
      "-v",
      action="store_true",
      help="Show content details (questions, responses)",
  )
  parser.add_argument(
      "--no-waterfall",
      action="store_true",
      help="Skip the waterfall chart",
  )
  parser.add_argument(
      "--no-stitch",
      action="store_true",
      help="Don't fetch and inline A2A remote agent sessions",
  )
  parser.add_argument(
      "--sdk-tree",
      action="store_true",
      help="Also show the SDK's built-in trace.render() output",
  )
  parser.add_argument(
      "--report",
      action="store_true",
      help="Generate a Markdown report in scripts/reports/",
  )
  parser.add_argument(
      "--output-json",
      type=str,
      default=None,
      metavar="PATH",
      help="Write structured latency results as JSON to the given file path. "
      "Use '-' to write JSON to stdout (human output goes to stderr)",
  )
  parser.add_argument(
      "--env",
      type=str,
      default=None,
      metavar="PATH",
      help="Path to .env file to load (overrides default .env discovery). "
      "Use this to point at a different agent's environment, e.g. "
      "--env examples/agent_improvement_cycle/.env",
  )
  args = parser.parse_args()

  if args.sdk_tree and args.output_json == "-":
    parser.error(
        "--sdk-tree cannot be combined with --output-json - "
        "(SDK trace.render() writes directly to stdout, bypassing "
        "the JSON-to-stdout routing)"
    )

  _configure_logging()
  _load_dotenv(env_file=args.env)

  from bigquery_agent_analytics import Client
  from bigquery_agent_analytics import TraceFilter

  project_id = os.getenv("PROJECT_ID")
  dataset_id = os.getenv("DATASET_ID")
  table_id = os.getenv("TABLE_ID")
  dataset_location = os.getenv("DATASET_LOCATION")

  for var in ("PROJECT_ID", "DATASET_ID", "TABLE_ID", "DATASET_LOCATION"):
    if not os.getenv(var):
      logger.error(
          "Required environment variable %s is not set. "
          "Set it in your shell, create a .env file, or pass --env.",
          var,
      )
      sys.exit(1)

  client = Client(
      project_id=project_id,
      dataset_id=dataset_id,
      table_id=table_id,
      location=dataset_location,
  )

  # Build filter
  time_period = args.time_period
  if time_period and time_period.lower() == "all":
    time_period = None
  if args.session:
    trace_filter = TraceFilter(session_ids=[args.session])
  elif time_period:
    trace_filter = TraceFilter.from_cli_args(last=time_period)
  else:
    trace_filter = TraceFilter()

  if args.app_name:
    trace_filter.root_agent_name = args.app_name
  trace_filter.limit = args.limit

  # When --output-json -, send human-readable output to stderr
  # so that stdout contains only machine-readable JSON.
  json_to_stdout = args.output_json == "-"
  out = sys.stderr if json_to_stdout else sys.stdout

  logger.info(
      "Fetching traces from %s.%s.%s...",
      project_id,
      dataset_id,
      table_id,
  )
  traces = client.list_traces(filter_criteria=trace_filter)

  if not traces:
    logger.info("No traces found.")
    sys.exit(0)

  logger.info("Found %d trace(s)", len(traces))
  if not args.no_stitch:
    stitch_a2a_traces(traces, client)
  print(file=out)

  for trace in traces:
    # Custom timing tree
    print(render_timing_tree(trace, verbose=args.verbose), file=out)

    # Waterfall
    if not args.no_waterfall:
      print(render_waterfall(trace), file=out)

    # SDK's built-in render (opt-in, truncates at 120 chars)
    if args.sdk_tree:
      print(file=out)
      print("SDK Trace Tree:", file=out)
      print("─" * 70, file=out)
      trace.render(color=True)

    print(file=out)
    print("═" * 70, file=out)
    print(file=out)

  # Summary across traces
  print(render_summary_table(traces), file=out)

  # Markdown report
  if args.report:
    report_path = _write_md_report(
        traces, project_id, dataset_id, table_id, dataset_location
    )
    print(f"\n  Markdown report: {report_path}", file=out)

  # JSON output
  if args.output_json:
    output = _build_json_output(traces)
    if json_to_stdout:
      json.dump(output, sys.stdout, indent=2, default=str)
      sys.stdout.write("\n")
      print("  JSON report: (stdout)", file=sys.stderr)
    else:
      json_path = os.path.abspath(args.output_json)
      os.makedirs(os.path.dirname(json_path), exist_ok=True)
      with open(json_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
      print(f"\n  JSON report: {json_path}", file=out)


if __name__ == "__main__":
  main()
