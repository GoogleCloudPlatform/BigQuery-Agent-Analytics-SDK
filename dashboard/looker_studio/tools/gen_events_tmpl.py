#!/usr/bin/env python3
"""Generate sql/events_v1.sql.tmpl — the logical all-events union query.

The 15 union branches are exactly the ADK 1.27.0 generated-view inventory
(_EVENT_VIEW_DEFS in bigquery_agent_analytics_plugin.py at tag v1.27.0),
which is the static v1 intersection: 1.36.1 and 2.4.0 are supersets.

Regenerating must be byte-identical (CI asserts no drift).
"""

VIEWS = [
    "user_message_received",
    "llm_request",
    "llm_response",
    "llm_error",
    "tool_starting",
    "tool_completed",
    "tool_error",
    "agent_starting",
    "agent_completed",
    "invocation_starting",
    "invocation_completed",
    "state_delta",
    "hitl_credential_request",
    "hitl_confirmation_request",
    "hitl_input_request",
]

COMMON = [
    "timestamp",
    "event_type",
    "agent",
    "session_id",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "status",
    "error_message",
    "is_truncated",
]

# Stable output fields beyond the common set. Each branch must produce the
# same names and types; branches that don't carry a field emit a typed NULL.
LLM_FIELDS = [
    ("usage_prompt_tokens", "INT64"),
    ("usage_completion_tokens", "INT64"),
    ("usage_total_tokens", "INT64"),
    ("llm_total_ms", "INT64"),
    ("ttft_ms", "INT64"),
    ("model_version", "STRING"),
]
TOOL_FIELDS = [
    ("tool_completed_name", "STRING"),
    ("tool_completed_origin", "STRING"),
    ("tool_completed_total_ms", "INT64"),
]
ERR_FIELDS = [
    ("tool_error_name", "STRING"),
    ("tool_error_origin", "STRING"),
    ("tool_error_total_ms", "INT64"),
]

# Token precedence per the governing contract (issue #365): usage_metadata
# first, content-derived generated column as fallback. BQAA has emitted two
# usage_metadata key families in real installations, so the stable reporting
# contract accepts both while preserving the pinned LookML names first.
# usage_metadata is a required source column; its absence must fail preflight,
# not this query.
LLM_EXPRS = {
    "usage_prompt_tokens": (
        "COALESCE("
        "SAFE_CAST(JSON_VALUE(usage_metadata, '$.prompt_token_count')"
        " AS INT64), "
        "SAFE_CAST(JSON_VALUE(usage_metadata, '$.prompt_tokens') AS INT64), "
        "usage_prompt_tokens)"
    ),
    "usage_completion_tokens": (
        "COALESCE("
        "SAFE_CAST(JSON_VALUE(usage_metadata, '$.candidates_token_count')"
        " AS INT64), "
        "SAFE_CAST(JSON_VALUE(usage_metadata, '$.completion_tokens')"
        " AS INT64), usage_completion_tokens)"
    ),
    "usage_total_tokens": (
        "COALESCE("
        "SAFE_CAST(JSON_VALUE(usage_metadata, '$.total_token_count')"
        " AS INT64), "
        "SAFE_CAST(JSON_VALUE(usage_metadata, '$.total_tokens') AS INT64), "
        "usage_total_tokens)"
    ),
    "llm_total_ms": "total_ms",
    "ttft_ms": "ttft_ms",
    "model_version": "model_version",
}
TOOL_EXPRS = {
    "tool_completed_name": "tool_name",
    "tool_completed_origin": "tool_origin",
    "tool_completed_total_ms": "total_ms",
}
ERR_EXPRS = {
    "tool_error_name": "tool_name",
    "tool_error_origin": "tool_origin",
    "tool_error_total_ms": "total_ms",
}


def branch(view: str) -> str:
  lines = ["  SELECT"]
  for c in COMMON:
    lines.append(f"    {c},")
  for fields, exprs, active in (
      (LLM_FIELDS, LLM_EXPRS, view == "llm_response"),
      (TOOL_FIELDS, TOOL_EXPRS, view == "tool_completed"),
      (ERR_FIELDS, ERR_EXPRS, view == "tool_error"),
  ):
    for name, typ in fields:
      expr = exprs[name] if active else f"CAST(NULL AS {typ})"
      lines.append(f"    {expr} AS {name},")
  lines[-1] = lines[-1].rstrip(",")
  lines.append("  FROM `{{PROJECT}}.{{DATASET}}.{{VIEW_PREFIX}}_" + view + "`")
  return "\n".join(lines)


def main() -> None:
  header = """\
-- events_v1.sql.tmpl — logical all-events union over the BQAA-generated
-- views (static v1 intersection = the ADK 1.27.0 inventory, 15 views).
--
-- The three logical placeholders (PROJECT, DATASET, VIEW_PREFIX) are
-- rendered to the executable sentinel bindings (template_bindings.yaml) by
-- tools/render_template.py, producing sql/events_v1.template.sql — the exact
-- SQL embedded in the canonical Looker Studio report. At hydration, the
-- Linking API sqlReplace substitutes the sentinel strings with user values.
--
-- Never references the raw agent_events table. Date-range parameters must be
-- enabled on the Looker Studio data source. Timezone frozen to UTC (v1);
-- @DS_START_DATE/@DS_END_DATE arrive as YYYYMMDD strings, end inclusive.
WITH events AS (
"""
  body = "\n  UNION ALL\n".join(branch(v) for v in VIEWS)
  footer = """
)
SELECT
  e.*,
  DATE(e.timestamp, 'UTC') AS event_date,
  CONCAT(e.trace_id, '|', e.span_id) AS event_pk,
  IF(e.event_type = 'LLM_RESPONSE',
     CONCAT(e.trace_id, '|', e.span_id), NULL) AS llm_response_pk,
  IF(e.event_type = 'TOOL_COMPLETED',
     CONCAT(e.trace_id, '|', e.span_id), NULL) AS tool_completed_pk,
  IF(e.event_type = 'TOOL_ERROR',
     CONCAT(e.trace_id, '|', e.span_id), NULL) AS tool_error_pk
FROM events AS e
WHERE e.timestamp >= TIMESTAMP(PARSE_DATE('%Y%m%d', @DS_START_DATE), 'UTC')
  AND e.timestamp < TIMESTAMP(
        DATE_ADD(PARSE_DATE('%Y%m%d', @DS_END_DATE), INTERVAL 1 DAY), 'UTC')
"""
  with open("sql/events_v1.sql.tmpl", "w") as fh:
    fh.write(header + body + footer)
  print(f"wrote sql/events_v1.sql.tmpl ({len(VIEWS)} branches)")


if __name__ == "__main__":
  main()
