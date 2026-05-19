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
    python quality_report.py --config config.json # use scope definitions from config
    python quality_report.py --env path/to/.env   # load a specific .env file
"""
import warnings

warnings.filterwarnings("ignore")

import argparse
from datetime import datetime
import json
import logging
import os
import sys
import time


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
# Scope configuration
# ---------------------------------------------------------------------------

_AGENT_CONFIG_CACHE: dict[str, dict] = {}


def _load_agent_config(config_path=None):
  """Load agent config (scope decisions, etc.) from a JSON file.

  When --config is provided, loads from that path.  Otherwise checks
  for eval/data/agent_context.json relative to the repo root or script dir.
  Returns None if no config is found (scope-aware eval is disabled).

  Raises:
    FileNotFoundError: If an explicit config_path does not exist.
  """
  cache_key = config_path or "_AUTO_"
  if cache_key in _AGENT_CONFIG_CACHE:
    return _AGENT_CONFIG_CACHE[cache_key]

  if config_path:
    if not os.path.isfile(config_path):
      raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path) as f:
      result = json.load(f)
    _AGENT_CONFIG_CACHE[cache_key] = result
    return result

  # Auto-discover agent_context.json from known locations
  for base in [_repo_root, _script_dir]:
    candidate = os.path.join(base, "eval", "data", "agent_context.json")
    if os.path.isfile(candidate):
      logger.info("Auto-discovered agent context: %s", candidate)
      with open(candidate) as f:
        result = json.load(f)
      _AGENT_CONFIG_CACHE[cache_key] = result
      return result

  return None


def _build_scope_context(config=None):
  """Build scope context string for the LLM judge from config."""
  if not config:
    return ""

  scope_decisions = config.get("scope_decisions", [])
  oos_topics = [
      d["topic"] for d in scope_decisions if d.get("decision") == "out_of_scope"
  ]
  if not oos_topics:
    return ""

  parts = [
      "\n\nAGENT SCOPE CONTEXT (use this to judge responses correctly):",
      "The following topics are OUT OF SCOPE: " + ", ".join(oos_topics) + ".",
      "If the agent correctly declines a question about an out-of-scope "
      "topic (says it cannot help with that topic, suggests what it CAN "
      "help with), that is a MEANINGFUL response, not an unhelpful one.",
  ]
  return " ".join(parts)


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


def get_eval_metrics(config_path=None):
  """Return the list of categorical metric definitions for quality evaluation.

  Metrics returned:
    - ``response_usefulness`` — helpful, unhelpful, partial, or declined.
    - ``task_grounding`` — grounded in tool data vs fabricated.
    - ``correctness`` — factual accuracy of the response.
    - ``tool_usage`` — whether the agent used its tools correctly.
    - ``specificity`` — concrete details vs vague answer.
    - ``scope_compliance`` — stayed within defined scope.
    - ``first_time_right`` — correct on first attempt without corrections.
  """
  from bigquery_agent_analytics import CategoricalMetricCategory
  from bigquery_agent_analytics import CategoricalMetricDefinition

  config = _load_agent_config(config_path)
  scope_context = _build_scope_context(config)

  response_usefulness = CategoricalMetricDefinition(
      name="response_usefulness",
      definition=(
          "Whether the agent final response provides a genuinely useful, "
          "substantive answer to the user question. A response that apologizes, "
          "says it cannot help, returns no data, provides only generic filler, "
          "or loops without resolving the question is NOT useful -- UNLESS the "
          "question is outside the agent's defined scope, in which case a "
          "polite decline IS a correct and meaningful response." + scope_context
      ),
      categories=[
          CategoricalMetricCategory(
              name="meaningful",
              definition=(
                  "The response directly and substantively addresses the user "
                  "question with specific, actionable information."
              ),
          ),
          CategoricalMetricCategory(
              name="declined",
              definition=(
                  "The question is outside the agent's defined scope and the "
                  "agent correctly declined -- e.g. said it cannot help with "
                  "that topic, or suggested what it CAN help with. This is "
                  "the CORRECT behavior for out-of-scope questions."
              ),
          ),
          CategoricalMetricCategory(
              name="unhelpful",
              definition=(
                  "The response does NOT meaningfully answer the user question "
                  "AND the question IS within the agent's scope. Examples: "
                  "apologies for in-scope topics, saying 'I do not have that "
                  "information' when the agent has a tool that covers the topic, "
                  "empty data results, generic filler text, or the agent looping "
                  "without a resolution."
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
          "Whether the agent response is grounded in actual data retrieved "
          "from its tools, or is fabricated / hallucinated general knowledge."
      ),
      categories=[
          CategoricalMetricCategory(
              name="grounded",
              definition=(
                  "The response is clearly based on data retrieved from the "
                  "agent tools (search results, database lookups, API calls)."
              ),
          ),
          CategoricalMetricCategory(
              name="ungrounded",
              definition=(
                  "The response appears to be fabricated or based on the LLM "
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

  correctness = CategoricalMetricDefinition(
      name="correctness",
      definition=(
          "Whether the facts stated in the agent response are accurate. "
          "Evaluate based on the information the agent retrieved from its "
          "tools and whether it was conveyed faithfully."
      ),
      categories=[
          CategoricalMetricCategory(
              name="correct",
              definition=(
                  "All facts stated by the agent are accurate and consistent "
                  "with the tool results retrieved."
              ),
          ),
          CategoricalMetricCategory(
              name="mostly_correct",
              definition=(
                  "The response is mostly correct but contains a minor "
                  "inaccuracy, omission, or imprecise wording."
              ),
          ),
          CategoricalMetricCategory(
              name="incorrect",
              definition=(
                  "The response contains wrong facts, hallucinated information, "
                  "or claims contradicted by the tool results."
              ),
          ),
      ],
  )

  tool_usage = CategoricalMetricDefinition(
      name="tool_usage",
      definition=(
          "Whether the agent used its available tools correctly to answer "
          "the question, rather than relying on general knowledge."
      ),
      categories=[
          CategoricalMetricCategory(
              name="proper",
              definition=(
                  "The agent used its tools and based the answer on the "
                  "tool results. Tools were called with appropriate parameters."
              ),
          ),
          CategoricalMetricCategory(
              name="partial",
              definition=(
                  "The agent partially used tools, or tool usage was unclear "
                  "or incomplete. Some information may not be tool-derived."
              ),
          ),
          CategoricalMetricCategory(
              name="none",
              definition=(
                  "The agent answered from general knowledge without looking "
                  "up information via tools, even though tools were available "
                  "and the question warranted their use."
              ),
          ),
      ],
  )

  specificity = CategoricalMetricDefinition(
      name="specificity",
      definition=(
          "Whether the agent response provides specific, concrete details "
          "(numbers, dates, dollar amounts, limits) rather than vague or "
          "generic statements."
      ),
      categories=[
          CategoricalMetricCategory(
              name="specific",
              definition=(
                  "The response includes specific and complete details: exact "
                  "numbers, percentages, dollar amounts, dates, or limits."
              ),
          ),
          CategoricalMetricCategory(
              name="somewhat_specific",
              definition=(
                  "The response is somewhat specific but missing some key "
                  "details that would make it fully actionable."
              ),
          ),
          CategoricalMetricCategory(
              name="vague",
              definition=(
                  "The response is vague, generic, or missing key specifics "
                  "that the user needs to act on the information."
              ),
          ),
      ],
  )

  scope_compliance = CategoricalMetricDefinition(
      name="scope_compliance",
      definition=(
          "Whether the agent correctly handled the scope of the question. "
          "An agent should answer in-scope questions and politely decline "
          "out-of-scope ones." + scope_context
      ),
      categories=[
          CategoricalMetricCategory(
              name="compliant",
              definition=(
                  "The agent correctly answered an in-scope question OR "
                  "correctly declined an out-of-scope question."
              ),
          ),
          CategoricalMetricCategory(
              name="partially_compliant",
              definition=(
                  "The agent answered but with unnecessary caveats, excessive "
                  "hedging, or was partially out of scope."
              ),
          ),
          CategoricalMetricCategory(
              name="non_compliant",
              definition=(
                  "The agent tried to answer an out-of-scope question it "
                  "should have declined, OR refused to answer an in-scope "
                  "question it should have handled."
              ),
          ),
      ],
  )

  first_time_right = CategoricalMetricDefinition(
      name="first_time_right",
      definition=(
          "Whether the agent's FIRST response in the conversation was "
          "satisfactory, without needing user corrections or follow-ups "
          "to fix errors. For single-turn conversations, evaluate the "
          "only response. For multi-turn, focus on whether the first "
          "substantive answer was correct."
      ),
      categories=[
          CategoricalMetricCategory(
              name="correct",
              definition=(
                  "The first response was correct and complete. No correction "
                  "or significant clarification was needed from the user."
              ),
          ),
          CategoricalMetricCategory(
              name="clarification_needed",
              definition=(
                  "The first response was mostly right but needed minor "
                  "clarification or a follow-up to be fully useful."
              ),
          ),
          CategoricalMetricCategory(
              name="correction_needed",
              definition=(
                  "The first response was wrong, vague, or incomplete enough "
                  "that the user had to push back or correct the agent."
              ),
          ),
      ],
  )

  return [
      response_usefulness,
      task_grounding,
      correctness,
      tool_usage,
      specificity,
      scope_compliance,
      first_time_right,
  ]


# ---------------------------------------------------------------------------
# Trace helpers - extract Q&A and resolve A2A responses
# ---------------------------------------------------------------------------


def get_user_input(trace) -> str:
  """Return the last user message in the trace.

  Multi-turn sessions have multiple USER_MESSAGE_RECEIVED events.  We want
  the *last* one so that question/response pairs stay aligned — the response
  resolution helpers (get_a2a_response, get_responding_agent) already search
  in reverse and return the most recent answer.
  """
  result = ""
  for span in trace.spans:
    if span.event_type == "USER_MESSAGE_RECEIVED":
      c = span.content
      if isinstance(c, dict):
        text = c.get("text_summary") or c.get("text") or ""
      elif c:
        text = str(c)
      else:
        text = ""
      if text:
        result = text
  return result


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
  """Return the last A2A response in the trace.

  For multi-turn sessions we must return the *last* A2A interaction to stay
  aligned with get_user_input (which also returns the last user message).
  If the last A2A interaction has null/empty content (e.g. the remote agent
  returned nothing), we return ("(no response)", agent) rather than falling
  through to an earlier turn's response — that would create a misleading
  question/response mismatch in the quality report.
  """
  for span in reversed(trace.spans):
    if span.event_type == "A2A_INTERACTION":
      c = span.content
      if isinstance(c, dict):
        text, agent = _extract_a2a_text(c)
        agent = agent or span.agent or "remote_agent"
        return (text or "(no response)"), agent
      elif c is None:
        # Null content means the remote agent returned nothing
        return "(no response)", span.agent or "remote_agent"
      elif isinstance(c, str):
        try:
          parsed = json.loads(c)
          text, agent = _extract_a2a_text(parsed)
          agent = agent or span.agent or "remote_agent"
          return (text or "(no response)"), agent
        except (json.JSONDecodeError, TypeError):
          logger.warning(
              "Failed to parse A2A payload for session %s, skipping",
              getattr(trace, "session_id", "?"),
          )
          return "(no response)", span.agent or "remote_agent"
  return None, None


# ---------------------------------------------------------------------------
# Resolve responses for a batch of traces
# ---------------------------------------------------------------------------


def _count_trace_metrics(trace):
  """Extract multi-turn efficiency metrics from a trace."""
  user_turns = 0
  tool_calls = 0
  for span in trace.spans:
    if span.event_type == "USER_MESSAGE_RECEIVED":
      user_turns += 1
    elif span.event_type == "TOOL_COMPLETED":
      tool_calls += 1
  return user_turns, tool_calls


def _extract_conversation(trace):
  """Reconstruct the multi-turn conversation from trace spans.

  Returns a list of ``{"role": "user"|"agent", "text": str}`` dicts
  representing the full conversation in chronological order.
  """
  # Collect user messages with their span indices.
  user_msgs = []
  for i, span in enumerate(trace.spans):
    if span.event_type == "USER_MESSAGE_RECEIVED":
      c = span.content
      if isinstance(c, dict):
        text = c.get("text_summary") or c.get("text") or ""
      elif c:
        text = str(c)
      else:
        text = ""
      if text:
        user_msgs.append((i, text))

  if not user_msgs:
    return []

  turns = []
  for msg_idx, (span_idx, user_text) in enumerate(user_msgs):
    turns.append({"role": "user", "text": user_text})

    # Boundary: next user message or end of spans.
    end_idx = (
        user_msgs[msg_idx + 1][0]
        if msg_idx + 1 < len(user_msgs)
        else len(trace.spans)
    )

    # Walk backwards to find the last substantive LLM_RESPONSE for this turn.
    for span in reversed(trace.spans[span_idx:end_idx]):
      if span.event_type == "LLM_RESPONSE":
        c = span.content
        if isinstance(c, dict):
          text = c.get("response", "")
        elif c:
          text = str(c)
        else:
          text = ""
        if (
            text
            and not text.startswith("call:")
            and not _is_single_word_routing(text)
        ):
          turns.append({"role": "agent", "text": text})
          break

  return turns


def _infer_corrections(conversation, model):
  """Use LLM to count corrections and verifications in a conversation.

  Classifies each user follow-up message (after the first) as a correction,
  verification request, or normal follow-up.  Returns (corrections, verifications).
  """
  user_turns = [t for t in conversation if t["role"] == "user"]
  if len(user_turns) <= 1:
    return 0, 0

  formatted = []
  for t in conversation:
    role = "User" if t["role"] == "user" else "Agent"
    formatted.append(f"{role}: {t['text']}")
  conv_text = "\n\n".join(formatted)

  prompt = (
      "Analyze this conversation between a user and an AI agent.\n\n"
      f"<conversation>\n{conv_text}\n</conversation>\n\n"
      "Count user follow-up messages (all messages after the first question) "
      "and classify each as:\n"
      "- CORRECTION: The user disputes, corrects, or says the agent got "
      "something wrong\n"
      "- VERIFICATION: The user asks the agent to verify, double-check, or "
      "provide more specifics about a claim\n"
      "- FOLLOWUP: Normal continuation, new related question, or satisfied "
      "acknowledgment\n\n"
      'Return ONLY a JSON object: {"corrections": <int>, "verifications": <int>}'
  )

  try:
    from google import genai

    client = genai.Client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"temperature": 0.0},
    )
    raw = response.text.strip()
    # Strip markdown code fences if present.
    if raw.startswith("```"):
      lines = raw.split("\n")
      raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    result = json.loads(raw)
    return int(result.get("corrections", 0)), int(
        result.get("verifications", 0)
    )
  except Exception:
    logger.debug("Failed to infer corrections, defaulting to 0", exc_info=True)
    return 0, 0


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
        # Mark as A2A even for "(no response)" — the interaction happened,
        # so the session should be attributed to the remote agent in stats.
        is_a2a = True
        remote_lookups += 1

    latency_s = None
    if trace.total_latency_ms is not None:
      latency_s = round(trace.total_latency_ms / 1000, 1)

    user_turns, tool_calls = _count_trace_metrics(trace)
    conversation = _extract_conversation(trace) if user_turns > 1 else []

    results.append(
        {
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
            "user_turns": user_turns,
            "tool_calls": tool_calls,
            "conversation": conversation,
            "corrections": 0,
            "verifications": 0,
        }
    )

  if remote_lookups:
    logger.info("Resolved %d A2A responses", remote_lookups)

  return results


# ---------------------------------------------------------------------------
# Run evaluation
# ---------------------------------------------------------------------------


def run_evaluation(
    time_range=None,
    limit=100,
    model=None,
    persist=False,
    app_name=None,
    config_path=None,
    session_id=None,
    session_ids=None,
) -> dict:
  from bigquery_agent_analytics import CategoricalEvaluationConfig
  from bigquery_agent_analytics import TraceFilter

  model = model or EVAL_MODEL_ID
  client = get_client()

  metrics = get_eval_metrics(config_path=config_path)
  cat_config = CategoricalEvaluationConfig(
      metrics=metrics,
      endpoint=model,
      temperature=0.0,
      include_justification=True,
      persist_results=persist,
      results_table="quality_eval_results" if persist else None,
  )

  if session_id:
    trace_filter = TraceFilter(session_ids=[session_id])
  elif session_ids:
    trace_filter = TraceFilter(
        session_ids=session_ids,
        limit=len(session_ids),
    )
    if app_name:
      trace_filter.root_agent_name = app_name
  else:
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

  # Infer corrections/verifications for multi-turn sessions.
  mt_sessions = [r for r in resolved if r.get("user_turns", 0) > 1]
  if mt_sessions:
    logger.info(
        "Inferring corrections for %d multi-turn sessions...",
        len(mt_sessions),
    )
    for r in mt_sessions:
      conv = r.get("conversation", [])
      if conv:
        corrections, verifications = _infer_corrections(conv, model)
        r["corrections"] = corrections
        r["verifications"] = verifications

  return {
      "report": report,
      "resolved_map": resolved_map,
  }


def generate_quality_report(
    session_ids: list[str],
    model: str | None = None,
) -> dict:
  """Evaluate sessions and return a structured quality report dict.

  This is the main public API for programmatic use.  It combines
  ``run_evaluation`` (trace fetching, LLM scoring, correction inference)
  with ``_build_json_output`` (structured dict) in a single call.

  Args:
      session_ids: BigQuery session IDs to evaluate.
      model: Eval model override (default: EVAL_MODEL_ID env or
          gemini-2.5-flash).

  Returns:
      Dict with ``summary`` and ``sessions`` keys, compatible with
      evolve.py / bottleneck.py / score_and_compare.py.
  """
  # Ensure config is loaded (no-op if already initialized via main()).
  if PROJECT_ID is None:
    _load_config()
  if not model:
    model = os.getenv("EVAL_MODEL_ID", "gemini-2.5-flash")
  t0 = time.time()
  result = run_evaluation(session_ids=session_ids, model=model)
  elapsed = time.time() - t0

  output = _build_json_output(result["report"], result["resolved_map"])
  output["summary"]["elapsed_seconds"] = round(elapsed, 1)
  return output


def print_quality_report(report: dict):
  """Print a formatted quality report from a ``generate_quality_report`` dict.

  Accepts the structured dict returned by ``generate_quality_report``,
  NOT the raw SDK ``CategoricalEvaluationReport`` object.  For the raw
  object, use ``_print_eval_results`` instead.
  """
  summary = report["summary"]
  sessions = report.get("sessions", [])

  print("\n" + "=" * 70)
  print("  QUALITY REPORT")
  print("=" * 70)
  print(f"  Sessions:             {summary['total_sessions']}")
  print(f"  Meaningful:           {summary['meaningful']}")
  print(f"  Declined (correct):   {summary['declined']}")
  print(f"  Partial:              {summary['partial']}")
  print(f"  Unhelpful:            {summary['unhelpful']}")
  print(f"  Meaningful rate:      {summary['meaningful_rate']}%")

  if "correction_rate" in summary:
    total_c = sum(s.get("corrections", 0) for s in sessions)
    total_v = sum(s.get("verifications", 0) for s in sessions)
    print(
        f"  Correction rate:      {summary['correction_rate']}%"
        f" ({total_c} corrections)"
    )
    print(
        f"  Verification rate:    {summary['verification_rate']}%"
        f" ({total_v} verifications)"
    )

  if "avg_user_turns" in summary:
    print(f"  Avg user turns:       {summary['avg_user_turns']}")
  if "avg_tool_calls" in summary:
    print(f"  Avg tool calls:       {summary['avg_tool_calls']}")

  dim_avgs = summary.get("dimension_averages", {})
  if dim_avgs:
    print("\n  Quality Dimensions (0-2 scale):")
    for dim, avg in dim_avgs.items():
      bar = "#" * int(avg * 25)
      print(f"    {dim:<20s}: {avg:.2f} / 2.00  {bar}")

  problems = [
      s
      for s in sessions
      if s.get("metrics", {}).get("response_usefulness", {}).get("category")
      in ("unhelpful", "partial")
  ]
  if problems:
    print(f"\n  Problem Sessions ({len(problems)}):")
    for s in problems[:10]:
      cat = s["metrics"]["response_usefulness"]["category"]
      q = s.get("question", "")[:60]
      reason = (
          s.get("quality_scores", {})
          .get("correctness", {})
          .get("reason", "")[:80]
      )
      print(f"    [{cat}] {q}")
      if reason:
        print(f"      {reason}")

  print("=" * 70)


# ---------------------------------------------------------------------------
# Category labels
# ---------------------------------------------------------------------------


def _category_label(category):
  labels = {
      "meaningful": "\u2705 HELPFUL",
      "declined": "\u2705 DECLINED (OK)",
      "unhelpful": "\u274c NOT HELPFUL",
      "partial": "\u26a0\ufe0f  PARTIAL",
      "grounded": "\u2705 GROUNDED",
      "ungrounded": "\u274c NOT GROUNDED",
      "no_tool_needed": "\u2796 NO TOOL NEEDED",
      # correctness
      "correct": "\u2705 CORRECT",
      "mostly_correct": "\u26a0\ufe0f  MOSTLY CORRECT",
      "incorrect": "\u274c INCORRECT",
      # tool_usage
      "proper": "\u2705 PROPER",
      # "partial" already covered above
      "none": "\u274c NONE",
      # specificity
      "specific": "\u2705 SPECIFIC",
      "somewhat_specific": "\u26a0\ufe0f  SOMEWHAT SPECIFIC",
      "vague": "\u274c VAGUE",
      # scope_compliance
      "compliant": "\u2705 COMPLIANT",
      "partially_compliant": "\u26a0\ufe0f  PARTIALLY COMPLIANT",
      "non_compliant": "\u274c NON-COMPLIANT",
      # first_time_right
      "clarification_needed": "\u26a0\ufe0f  CLARIFICATION NEEDED",
      "correction_needed": "\u274c CORRECTION NEEDED",
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

  if args.session:
    trace_filter = TraceFilter(session_ids=[args.session])
  else:
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

  # Load session IDs from file if provided
  session_ids = None
  if args.session_ids_file:
    with open(args.session_ids_file) as _f:
      _data = json.load(_f)
    # Accepts either a list of objects with "session_id" keys
    # (e.g. output of examples/agent_improvement_cycle/eval/run_eval.py)
    # or a plain list of strings.
    if _data and isinstance(_data[0], dict):
      session_ids = [r["session_id"] for r in _data if r.get("session_id")]
    else:
      session_ids = [s for s in _data if s]
    if not session_ids:
      logger.error(
          "No session IDs found in %s — file may be empty or missing "
          "'session_id' fields.",
          args.session_ids_file,
      )
      sys.exit(1)
    logger.info(
        "Filtering to %d session IDs from %s",
        len(session_ids),
        args.session_ids_file,
    )

  t0 = time.time()
  try:
    config_path = getattr(args, "config", None)
    if config_path:
      logger.info("Scope config: %s", config_path)
    result = run_evaluation(
        time_range=args.time_period,
        limit=args.limit,
        model=model,
        persist=args.persist,
        app_name=args.app_name,
        config_path=config_path,
        session_id=args.session,
        session_ids=session_ids,
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
  result["report"].details["samples"] = args.samples or None
  _print_eval_results(
      result["report"],
      result["resolved_map"],
      samples=args.samples,
      unhelpful_threshold=args.threshold,
  )

  report_path = None
  if args.report:
    report_path = _write_md_report(
        result["report"], result["resolved_map"], args
    )

  if report_path:
    print(f"\n  Markdown report: {report_path}")

  if args.output_json:
    output = _build_json_output(result["report"], result["resolved_map"])
    if args.output_json == "-":
      json.dump(output, sys.stdout, indent=2, default=str)
      sys.stdout.write("\n")
      print("  JSON report: (stdout)", file=sys.stderr)
    else:
      json_path = os.path.abspath(args.output_json)
      os.makedirs(os.path.dirname(json_path), exist_ok=True)
      with open(json_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
      print(f"\n  JSON report: {json_path}")


def _group_by_category(report):
  by_category = {
      "unhelpful": [],
      "partial": [],
      "meaningful": [],
      "declined": [],
  }
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
          "declined": 0,
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
        elif mr.category == "declined":
          agent_stats[agent]["declined"] += 1
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
    "correctness": "Correctness",
    "tool_usage": "Tool Usage",
    "specificity": "Specificity",
    "scope_compliance": "Scope",
    "first_time_right": "First-Time Right",
}

# Maps category → numeric score (0-2) for dimension averaging.
_DIMENSION_SCORES = {
    "correctness": {"correct": 2, "mostly_correct": 1, "incorrect": 0},
    "tool_usage": {"proper": 2, "partial": 1, "none": 0},
    "specificity": {"specific": 2, "somewhat_specific": 1, "vague": 0},
    "scope_compliance": {
        "compliant": 2,
        "partially_compliant": 1,
        "non_compliant": 0,
    },
    "first_time_right": {
        "correct": 2,
        "clarification_needed": 1,
        "correction_needed": 0,
    },
}

_DIMENSION_NAMES = list(_DIMENSION_SCORES.keys())

# Short descriptions for the markdown report's Quality Dimensions table.
_DIMENSION_DESCRIPTIONS = {
    "correctness": "Are the facts in the response accurate?",
    "tool_usage": "Did the agent use its tools to verify facts?",
    "specificity": "Does the response include specific numbers, dates, limits?",
    "scope_compliance": "Did the agent correctly handle in-scope vs out-of-scope?",
    "first_time_right": "Was the first response correct without user corrections?",
}


def _compute_dimension_averages(report):
  """Compute average 0-2 score for each fine-grained dimension."""
  dim_totals = {d: [] for d in _DIMENSION_NAMES}
  for sr in report.session_results:
    for mr in sr.metrics:
      if mr.metric_name in _DIMENSION_SCORES:
        score_map = _DIMENSION_SCORES[mr.metric_name]
        score = score_map.get(mr.category, 0)
        dim_totals[mr.metric_name].append(score)
  return {
      d: round(sum(scores) / len(scores), 2) if scores else 0
      for d, scores in dim_totals.items()
  }


def _compute_multiturn_stats(resolved_map):
  """Compute multi-turn efficiency statistics from resolved traces."""
  user_turns = [r.get("user_turns", 0) for r in resolved_map.values()]
  tool_calls = [r.get("tool_calls", 0) for r in resolved_map.values()]
  corrections = [r.get("corrections", 0) for r in resolved_map.values()]
  verifications = [r.get("verifications", 0) for r in resolved_map.values()]
  total = len(user_turns)
  if not total:
    return {}
  mt_count = sum(1 for t in user_turns if t > 1)
  stats = {
      "avg_user_turns": round(sum(user_turns) / total, 1),
      "avg_tool_calls": round(sum(tool_calls) / total, 1),
      "multi_turn_sessions": mt_count,
  }
  if mt_count > 0:
    stats["correction_rate"] = round(
        sum(1 for c in corrections if c > 0) / total * 100, 1
    )
    stats["verification_rate"] = round(
        sum(1 for v in verifications if v > 0) / total * 100, 1
    )
    stats["avg_corrections"] = round(sum(corrections) / total, 2)
    stats["avg_verifications"] = round(sum(verifications) / total, 2)
  return stats


def _print_eval_results(
    report, resolved_map, samples=None, unhelpful_threshold=10.0
):
  hr = "\u2500" * 70

  by_category = _group_by_category(report)
  a2a_session_ids = {
      sid for sid, ctx in resolved_map.items() if ctx.get("is_a2a")
  }

  # --- Per-session details ---
  _default_samples = {
      "unhelpful": 10,
      "partial": 5,
      "meaningful": 3,
      "declined": 3,
      "unknown": 3,
  }
  for cat, cat_label in [
      ("unhelpful", "UNHELPFUL"),
      ("partial", "PARTIAL"),
      ("declined", "DECLINED (out-of-scope)"),
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

      # Primary metrics with justifications
      for mr in sr.metrics:
        if mr.metric_name not in ("response_usefulness", "task_grounding"):
          continue
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

      # Compact scorecard for quality dimensions
      dim_parts = []
      for mr in sr.metrics:
        if mr.metric_name in ("response_usefulness", "task_grounding"):
          continue
        display_name = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
        mr_label = _category_label(mr.category)
        dim_parts.append(f"{display_name}: {mr_label}")
      if dim_parts:
        print(f"  {'Dimensions:':<15}{' | '.join(dim_parts)}")

  # --- Per-agent breakdown ---
  agent_stats = _build_agent_stats(report, resolved_map)

  if agent_stats:
    total_helpful_all = sum(
        s["meaningful"] + s["declined"] for s in agent_stats.values()
    )
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
      helpful = stats["meaningful"] + stats["declined"]
      classified = helpful + stats["unhelpful"] + stats["partial"]
      helpful_pct = (helpful / classified * 100) if classified > 0 else 0
      unhelpful_pct = (
          (stats["unhelpful"] / classified * 100) if classified > 0 else 0
      )
      helpful_contrib = (
          (helpful / total_helpful_all * 100) if total_helpful_all > 0 else 0
      )
      unhelpful_contrib = (
          (stats["unhelpful"] / total_unhelpful_all * 100)
          if total_unhelpful_all > 0
          else 0
      )
      a2a_n = stats["a2a_count"]
      a2a_tag = (
          f" [A2A:{a2a_n}/{total}]"
          if 0 < a2a_n < total
          else " [A2A]"
          if a2a_n == total
          else ""
      )
      status = (
          "\U0001f7e2"
          if helpful_pct >= 80
          else ("\U0001f7e1" if helpful_pct >= 60 else "\U0001f534")
      )
      agent_name = f"{agent}{a2a_tag}"
      declined_tag = f"+{stats['declined']}d" if stats["declined"] else ""
      helpful_str = f"{stats['meaningful']}{declined_tag} ({helpful_pct:.0f}%)"
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
            f" [A2A:{a2a_n}/{stats['total']}]"
            if 0 < a2a_n < stats["total"]
            else " [A2A]"
            if a2a_n == stats["total"]
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
  declined_count = len(by_category.get("declined", []))
  unknown_count = len(by_category.get("unknown", []))
  total = report.total_sessions
  fp_rate = (fp_count / total * 100) if total > 0 else 0.0

  print(f"\n{'=' * 70}")
  print("QUALITY SUMMARY")
  print(f"{'=' * 70}")
  print(f"  Total sessions evaluated : {total}")
  print(f"  Meaningful               : {meaningful_count}")
  print(f"  Declined (out-of-scope)  : {declined_count}")
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

  # --- Dimension averages (0-2 scale) ---
  dim_avgs = _compute_dimension_averages(report)
  if any(v > 0 for v in dim_avgs.values()):
    print(f"\n  Quality Dimensions (0-2 scale):")
    for dim, avg in dim_avgs.items():
      bar = "#" * int(avg * 25)
      label = _METRIC_LABELS.get(dim, dim)
      print(f"    {label:<20s}: {avg:.2f} / 2.00  {bar}")

  # --- Multi-turn efficiency ---
  mt_stats = _compute_multiturn_stats(resolved_map)
  if mt_stats:
    print(f"\n  Multi-Turn Efficiency:")
    print(f"    Avg user turns       : {mt_stats['avg_user_turns']}")
    print(f"    Avg tool calls       : {mt_stats['avg_tool_calls']}")
    if mt_stats["multi_turn_sessions"] > 0:
      print(f"    Multi-turn sessions  : {mt_stats['multi_turn_sessions']}")
    if "correction_rate" in mt_stats:
      print(f"    Correction rate      : {mt_stats['correction_rate']}%")
      print(f"    Verification rate    : {mt_stats['verification_rate']}%")

  print("\n  Category Distributions:")
  for metric_name, dist in report.category_distributions.items():
    if metric_name not in ("response_usefulness", "task_grounding"):
      continue
    print(f"\n  [{metric_name}]")
    dist_total = sum(dist.values())
    for category, count in sorted(dist.items(), key=lambda x: -x[1]):
      pct = (count / dist_total * 100) if dist_total > 0 else 0.0
      bar = "#" * int(pct / 2)
      print(
          f"    {_category_label(category):18s}: {count:4d}  ({pct:5.1f}%) {bar}"
      )

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
    print(
        f"\n  WARNING: Unhelpful rate ({fp_rate:.1f}%) exceeds {unhelpful_threshold:.0f}% threshold!"
    )
  elif fp_rate > 0:
    print(
        f"\n  Unhelpful responses detected but below {unhelpful_threshold:.0f}% threshold."
    )
  else:
    print("\n  All responses were meaningful.")


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------


def _md_dimension_scorecard(sr):
  """Build a compact one-line scorecard for the 5 quality dimensions."""
  _SCORECARD_ICONS = {
      "correct": "\u2705",
      "mostly_correct": "\u26a0\ufe0f",
      "incorrect": "\u274c",
      "proper": "\u2705",
      "partial": "\u26a0\ufe0f",
      "none": "\u274c",
      "specific": "\u2705",
      "somewhat_specific": "\u26a0\ufe0f",
      "vague": "\u274c",
      "compliant": "\u2705",
      "partially_compliant": "\u26a0\ufe0f",
      "non_compliant": "\u274c",
      "clarification_needed": "\u26a0\ufe0f",
      "correction_needed": "\u274c",
  }
  parts = []
  for mr in sr.metrics:
    if mr.metric_name in ("response_usefulness", "task_grounding"):
      continue
    label = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
    icon = _SCORECARD_ICONS.get(mr.category, "\u2705")
    parts.append(f"{label} {icon}")
  return " | ".join(parts)


def _md_write_session_section(
    w, title, sessions, md_samples, resolved_map, a2a_session_ids
):
  """Write a section of per-session details to the markdown report."""
  shown = sessions if md_samples is None else sessions[:md_samples]
  w(f"## {title}")
  if len(shown) < len(sessions):
    w(f"\n*Showing {len(shown)} of {len(sessions)}*")
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

    # Primary metrics with justifications
    for mr in sr.metrics:
      if mr.metric_name not in ("response_usefulness", "task_grounding"):
        continue
      label = _category_label(mr.category)
      display = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
      w(f"- **{display}:** {label}")
      if mr.justification:
        w(f"  - *{mr.justification}*")

    # Compact scorecard for quality dimensions
    scorecard = _md_dimension_scorecard(sr)
    if scorecard:
      w(f"- **Dimensions:** {scorecard}")
    w("")


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
  declined_count = len(by_category.get("declined", []))
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
  w(f"| Declined (out-of-scope) | {declined_count} |")
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

  # --- Quality Dimensions (0-2 scale) ---
  dim_avgs = _compute_dimension_averages(report)
  if any(v > 0 for v in dim_avgs.values()):
    w("## Quality Dimensions")
    w("")
    w(
        "Each session is scored 0-2 on five dimensions. "
        "Scores are averaged across all sessions."
    )
    w("")
    w("| Dimension | Avg Score | Rating | What it measures |")
    w("|-----------|----------:|--------|------------------|")
    for dim, avg in dim_avgs.items():
      label = _METRIC_LABELS.get(dim, dim)
      rating = (
          "\U0001f7e2"
          if avg >= 1.5
          else ("\U0001f7e1" if avg >= 1.0 else "\U0001f534")
      )
      desc = _DIMENSION_DESCRIPTIONS.get(dim, "")
      w(f"| {label} | {avg:.2f} / 2.00 | {rating} | {desc} |")
    w("")
    w(
        "*Rating: "
        "\U0001f7e2 >= 1.50 (good) "
        "| \U0001f7e1 >= 1.00 (needs attention) "
        "| \U0001f534 < 1.00 (problem area)*"
    )
    w("")

  # --- Multi-Turn Efficiency ---
  mt_stats = _compute_multiturn_stats(resolved_map)
  if mt_stats:
    w("## Multi-Turn Efficiency")
    w("")
    w("| Metric | Value |")
    w("|--------|-------|")
    w(f"| Avg user turns | {mt_stats['avg_user_turns']} |")
    w(f"| Avg tool calls | {mt_stats['avg_tool_calls']} |")
    if mt_stats["multi_turn_sessions"] > 0:
      w(f"| Multi-turn sessions | {mt_stats['multi_turn_sessions']} |")
    if "correction_rate" in mt_stats:
      w(f"| Correction rate | {mt_stats['correction_rate']}% |")
      w(f"| Verification rate | {mt_stats['verification_rate']}% |")
    w("")

  # --- Category Distributions (primary metrics only) ---
  _PRIMARY_METRICS = {"response_usefulness", "task_grounding"}
  w("## Category Distributions")
  w("")
  for metric_name, dist in report.category_distributions.items():
    if metric_name not in _PRIMARY_METRICS:
      continue
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
    w(
        "| Agent | Sessions | Helpful | Declined | Unhelpful | Partial | Status |"
    )
    w("|-------|-------:|--------:|--------:|----------:|--------:|--------|")
    for agent, stats in sorted(
        agent_stats.items(), key=lambda x: -x[1]["total"]
    ):
      helpful = stats["meaningful"] + stats["declined"]
      classified = helpful + stats["unhelpful"] + stats["partial"]
      helpful_pct = (helpful / classified * 100) if classified > 0 else 0
      a2a_n = stats["a2a_count"]
      total = stats["total"]
      a2a_tag = (
          f" [A2A:{a2a_n}/{total}]"
          if 0 < a2a_n < total
          else " [A2A]"
          if a2a_n == total
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
          f"| {stats['declined']} "
          f"| {stats['unhelpful']} | {stats['partial']} | {status} |"
      )
    w("")

  # --- Unhelpful Sessions ---
  unhelpful_sessions = by_category.get("unhelpful", [])
  _md_samples = (
      None
      if args.samples == "all"
      else (int(args.samples) if args.samples else None)
  )
  if unhelpful_sessions:
    _md_write_session_section(
        w,
        "Unhelpful Sessions",
        unhelpful_sessions,
        _md_samples,
        resolved_map,
        a2a_session_ids,
    )

  # --- Declined Sessions ---
  declined_sessions = by_category.get("declined", [])
  if declined_sessions:
    _md_write_session_section(
        w,
        "Declined Sessions",
        declined_sessions,
        _md_samples,
        resolved_map,
        a2a_session_ids,
    )

  # --- Partial Sessions ---
  partial_sessions = by_category.get("partial", [])
  if partial_sessions:
    _md_write_session_section(
        w,
        "Partial Sessions",
        partial_sessions,
        _md_samples,
        resolved_map,
        a2a_session_ids,
    )

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
    quality_scores = {}
    for mr in sr.metrics:
      metrics[mr.metric_name] = {
          "category": mr.category,
          "justification": mr.justification,
      }
      if mr.metric_name in _DIMENSION_SCORES:
        score_map = _DIMENSION_SCORES[mr.metric_name]
        quality_scores[mr.metric_name] = {
            "score": score_map.get(mr.category, 0),
            "reason": mr.justification or "",
        }
    session_dict = {
        "session_id": sr.session_id,
        "question": ctx.get("question", ""),
        "response": ctx.get("response", ""),
        "answered_by": ctx.get("answered_by", ""),
        "is_a2a": ctx.get("is_a2a", False),
        "latency_s": ctx.get("latency_s"),
        "user_turns": ctx.get("user_turns", 0),
        "tool_calls": ctx.get("tool_calls", 0),
        "corrections": ctx.get("corrections", 0),
        "verifications": ctx.get("verifications", 0),
        "metrics": metrics,
        "quality_scores": quality_scores,
    }
    conversation = ctx.get("conversation", [])
    if conversation:
      session_dict["conversation"] = conversation
    sessions.append(session_dict)

  fp_count = len(by_category.get("unhelpful", []))
  partial_count = len(by_category.get("partial", []))
  meaningful_count = len(by_category.get("meaningful", []))
  declined_count = len(by_category.get("declined", []))
  total = report.total_sessions

  dim_avgs = _compute_dimension_averages(report)
  mt_stats = _compute_multiturn_stats(resolved_map)

  return {
      "summary": {
          "total_sessions": total,
          "meaningful": meaningful_count,
          "declined": declined_count,
          "partial": partial_count,
          "unhelpful": fp_count,
          "meaningful_rate": round(
              (meaningful_count + declined_count) / total * 100, 1
          )
          if total
          else 0,
          "unhelpful_rate": round(fp_count / total * 100, 1) if total else 0,
          "dimension_averages": dim_avgs,
          **mt_stats,
      },
      "category_distributions": {
          k: dict(v) for k, v in report.category_distributions.items()
      },
      "per_agent": {agent: dict(stats) for agent, stats in agent_stats.items()},
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
  %(prog)s --config config.json      Use scope definitions from config
      """,
  )
  parser.add_argument(
      "--limit",
      type=_positive_int,
      default=100,
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
      "--session",
      type=str,
      default=None,
      help="Evaluate a specific session by ID",
  )
  parser.add_argument(
      "--app-name",
      type=str,
      default=None,
      help="Filter to sessions from a specific agent app name. Matches the "
      "root_agent_name attribute set by BigQueryAgentAnalyticsPlugin; "
      "sessions from other sources may not populate this field",
  )
  parser.add_argument(
      "--output-json",
      type=str,
      default=None,
      metavar="PATH",
      help="Write structured evaluation results as JSON to the given file path "
      "(writes all sessions regardless of --samples)",
  )
  parser.add_argument(
      "--threshold",
      type=float,
      default=10.0,
      help="Unhelpful rate warning threshold in %% (default: 10)",
  )
  parser.add_argument(
      "--config",
      type=str,
      default=None,
      metavar="PATH",
      help="Path to a JSON config file with scope definitions. "
      "When provided, adds a 'declined' category for correctly "
      "refused out-of-scope questions. Expected format: "
      '{"scope_decisions": [{"topic": "...", "decision": "out_of_scope", '
      '"reason": "..."}]}. '
      "Only 'topic' and 'decision' are used; 'reason' is documentation-only.",
  )
  parser.add_argument(
      "--session-ids-file",
      type=str,
      default=None,
      metavar="PATH",
      help="JSON file containing session IDs to evaluate. Expects a list of "
      "objects with 'session_id' fields (e.g. the output of "
      "examples/agent_improvement_cycle/eval/run_eval.py). "
      "When set, only these sessions are evaluated — --limit and "
      "--time-period are ignored.",
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

  _configure_logging()
  _load_dotenv(env_file=args.env)
  _load_config()

  if args.eval:
    run_eval(args)
  else:
    run_browse(args)


if __name__ == "__main__":
  main()
