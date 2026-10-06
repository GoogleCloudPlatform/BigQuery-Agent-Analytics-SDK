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
    python quality_report.py --eval-spec eval_spec.json # ground scoring with scope + golden Q&A
    python quality_report.py --env path/to/.env   # load a specific .env file
    python quality_report.py --conversations-file results.json  # score local JSON
    python quality_report.py --eval-config path/to/custom.json  # override metric definitions
"""
import warnings

warnings.filterwarnings("ignore")

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import math
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
  if "=" in value:
    return value
  n = int(value)
  if n < 1:
    raise argparse.ArgumentTypeError("--samples must be 'all' or >= 1")
  return str(n)


_SAMPLES_DEFAULTS = {
    "unhelpful": 10,
    "partial": 5,
    "meaningful": 3,
    "declined": 3,
    "low": 3,
    "unknown": 3,
}


def _parse_samples(samples_str):
  """Parse --samples value into a resolved dict.

  Accepts:
    "all"                              → show everything
    "5"                                → cap all sections at 5
    "unhelpful=10,partial=5,low=3"     → per-category overrides

  Returns a dict mapping category names to int limits, or None for "all".
  The "low" key applies to all Low-dimension sections.
  """
  if samples_str is None:
    return dict(_SAMPLES_DEFAULTS)
  if samples_str == "all":
    return None
  if "=" in samples_str:
    result = dict(_SAMPLES_DEFAULTS)
    for pair in samples_str.split(","):
      pair = pair.strip()
      if "=" not in pair:
        raise argparse.ArgumentTypeError(
            f"Invalid samples pair: {pair!r}. Use key=value format."
        )
      key, val = pair.split("=", 1)
      key = key.strip().lower()
      val = val.strip()
      if val == "all":
        result[key] = None
      else:
        n = int(val)
        if n < 1:
          raise argparse.ArgumentTypeError(
              f"--samples value for {key!r} must be >= 1, got {n}"
          )
        result[key] = n
    return result
  n = int(samples_str)
  return {k: n for k in _SAMPLES_DEFAULTS}


def _get_sample_limit(samples_dict, category):
  """Get the sample limit for a category from parsed samples dict.

  Returns None to show all, or an int limit.
  """
  if samples_dict is None:
    return None
  if category in samples_dict:
    return samples_dict[category]
  if category.startswith("low_") or category.startswith("low "):
    return samples_dict.get("low")
  return samples_dict.get("_default", 5)


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
# Eval spec — optional grounding for scoring (scope, ground truth, golden Q&A)
# ---------------------------------------------------------------------------
#
# The eval spec is a single optional JSON file (``eval/data/eval_spec.json``,
# auto-discovered, or ``--eval-spec <path>``) with three optional fields:
#
#   {
#     "scope": "free-text description of what the agent handles",
#     "ground_truth": "free-text authoritative facts for correctness",
#     "golden_qa": [{"question", "expected_answer", "topic"?,
#                    "expected_behavior"?}]
#   }
#
# ``scope`` defines the boundary positively (out-of-scope is the complement —
# no need to enumerate out-of-scope topics). ``golden_qa`` grounds correctness
# per question via embedding similarity; entries with
# ``expected_behavior: "decline"`` (or ``topic: "out_of_scope"``) also act as
# scope-boundary examples.

_EVAL_SPEC_CACHE: dict[str, dict] = {}


def _load_eval_spec(spec_path=None):
  """Load the eval spec ({scope, ground_truth, golden_qa}) from JSON.

  When *spec_path* is given, loads that file.  ``"none"`` disables the spec
  (no auto-discovery).  Otherwise auto-discovers ``eval/data/eval_spec.json``
  relative to the repo root or script dir.  Returns None when nothing is found.

  Raises:
    FileNotFoundError: If an explicit *spec_path* does not exist.
  """
  if spec_path and spec_path.lower() == "none":
    return None

  cache_key = spec_path or "_AUTO_"
  if cache_key in _EVAL_SPEC_CACHE:
    return _EVAL_SPEC_CACHE[cache_key]

  if spec_path:
    if not os.path.isfile(spec_path):
      raise FileNotFoundError(f"Eval spec file not found: {spec_path}")
    with open(spec_path) as f:
      result = json.load(f)
    _EVAL_SPEC_CACHE[cache_key] = result
    return result

  for base in [_repo_root, _script_dir]:
    candidate = os.path.join(base, "eval", "data", "eval_spec.json")
    if os.path.isfile(candidate):
      logger.info("Auto-discovered eval spec: %s", candidate)
      with open(candidate) as f:
        result = json.load(f)
      _EVAL_SPEC_CACHE[cache_key] = result
      return result

  return None


def _build_scope_context(spec=None):
  """Build scope / ground-truth context for the LLM judge from the eval spec.

  Reads two optional free-text fields:
    - ``ground_truth``: authoritative facts the judge uses for correctness.
    - ``scope``: what the agent is designed to handle. Anything outside it is
      out of scope (a polite decline is then correct); anything inside it the
      agent fails to answer is unhelpful, not declined.
  """
  if not spec:
    return ""

  parts = []

  ground_truth = spec.get("ground_truth", "")
  if ground_truth:
    parts.append(
        "\n\nGROUND TRUTH DATA (use this to judge factual correctness):"
    )
    parts.append(ground_truth)

  scope = spec.get("scope", "")
  if scope:
    parts.append("\n\nAGENT SCOPE (use this to judge responses correctly):")
    parts.append(scope.strip())
    parts.append(
        "A question is OUT OF SCOPE only if it falls outside the agent scope"
        " described above. When the agent politely declines a genuinely"
        " out-of-scope question, that is CORRECT ('declined'). When the"
        " question is in scope but the agent fails to answer it, that is"
        " 'unhelpful', NOT 'declined'."
    )

  tools = spec.get("tools", "")
  if tools:
    parts.append(
        "\n\nAGENT TOOLS / CAPABILITIES (use this to attribute the cause of a"
        " failure):"
    )
    parts.append(tools.strip())

  return " ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Golden Q&A matching — optional correctness grounding + scope calibration
# ---------------------------------------------------------------------------

# Golden matching now lives in the SDK core (extracted from this script);
# these aliases keep this module's public/test surface stable.
from bigquery_agent_analytics.golden_matching import DEFAULT_GOLDEN_THRESHOLD as _DEFAULT_GOLDEN_THRESHOLD  # noqa: E402
from bigquery_agent_analytics.golden_matching import embed_texts as _embed_texts  # noqa: E402
from bigquery_agent_analytics.golden_matching import EMBEDDING_MODEL  # noqa: E402,F401
from bigquery_agent_analytics.golden_matching import match_golden_qa  # noqa: E402


def _inject_golden_summary(report, golden_metadata):
  """Enrich a quality-report dict with golden-match data.

  Adds ``golden_eval`` to each session and a ``golden_eval_summary`` block to
  the report summary (matched/unmatched counts split by usefulness, plus the
  list of golden-matched sessions the agent got wrong).
  """
  if not golden_metadata:
    return

  buckets = {
      "matched_meaningful": 0,
      "matched_unhelpful": 0,
      "matched_partial": 0,
      "unmatched_meaningful": 0,
      "unmatched_unhelpful": 0,
      "unmatched_partial": 0,
  }
  mismatches = []
  session_id_counts = Counter(
      session.get("session_id", "") for session in report.get("sessions", [])
  )

  for session in report.get("sessions", []):
    sid = session.get("session_id", "")
    selector = _resolved_selector_from_context(session)
    meta = (
        golden_metadata.get(selector)
        if selector is not None and selector in golden_metadata
        else None
    )
    if meta is None and selector is None and session_id_counts[sid] == 1:
      meta = golden_metadata.get(sid)
    if meta is None:
      session["golden_eval"] = None
      continue
    session["golden_eval"] = meta

    usefulness = (
        session.get("metrics", {})
        .get("response_usefulness", {})
        .get("category", "")
    )
    prefix = "matched" if meta["matched"] else "unmatched"
    # A correct decline counts as a positive outcome alongside meaningful.
    if usefulness in ("meaningful", "declined"):
      buckets[f"{prefix}_meaningful"] += 1
    elif usefulness == "unhelpful":
      buckets[f"{prefix}_unhelpful"] += 1
      if meta["matched"]:
        mismatches.append(
            {
                "question": session.get("question", ""),
                "expected_answer": meta.get("expected_answer", ""),
                "actual_response": (
                    session.get("response", session.get("final_response", ""))
                )[:300],
                "topic": meta.get("topic", ""),
                "similarity": meta["similarity"],
            }
        )
    else:
      buckets[f"{prefix}_partial"] += 1

  total_matched = (
      buckets["matched_meaningful"]
      + buckets["matched_unhelpful"]
      + buckets["matched_partial"]
  )
  total_unmatched = (
      buckets["unmatched_meaningful"]
      + buckets["unmatched_unhelpful"]
      + buckets["unmatched_partial"]
  )

  report["summary"]["golden_eval_summary"] = {
      "total_sessions": total_matched + total_unmatched,
      "matched": total_matched,
      "matched_meaningful": buckets["matched_meaningful"],
      "matched_unhelpful": buckets["matched_unhelpful"],
      "matched_partial": buckets["matched_partial"],
      "matched_meaningful_rate": (
          round(buckets["matched_meaningful"] / total_matched * 100, 1)
          if total_matched
          else 0
      ),
      "unmatched": total_unmatched,
      "unmatched_meaningful": buckets["unmatched_meaningful"],
      "unmatched_unhelpful": buckets["unmatched_unhelpful"],
      "unmatched_partial": buckets["unmatched_partial"],
      "unmatched_meaningful_rate": (
          round(buckets["unmatched_meaningful"] / total_unmatched * 100, 1)
          if total_unmatched
          else 0
      ),
      "mismatches": mismatches,
  }


def _resolved_selector_from_context(context):
  """Rebuild an exact selector from report-safe identity/scope fields."""
  from bigquery_agent_analytics.trace import ResolvedTraceSelector
  from bigquery_agent_analytics.trace import TraceIdentity
  from bigquery_agent_analytics.trace import TraceScope

  identity_fields = (
      "session_id",
      "user_id",
      "root_agent_name",
      "scope_signature",
  )
  if any(field not in context for field in identity_fields):
    return None
  if context["session_id"] is None or context["scope_signature"] is None:
    return None
  try:
    identity = TraceIdentity(
        session_id=context.get("session_id"),
        user_id=context.get("user_id"),
        root_agent_name=context.get("root_agent_name"),
    )
    scope = TraceScope(
        experiment_id=context.get("experiment_id"),
        custom_labels=context.get("custom_labels"),
    )
  except (TypeError, ValueError):
    return None
  signature = context.get("scope_signature")
  if signature is not None and signature != scope.scope_signature:
    return None
  return ResolvedTraceSelector(identity=identity, scope=scope)


# ---------------------------------------------------------------------------
# Eval config (prompts + metrics from external file)
# ---------------------------------------------------------------------------

_EVAL_CONFIG_CACHE: dict[str, dict] = {}


def _load_eval_config(eval_config_path=None):
  """Load evaluation config (prompts + metrics) from a JSON file.

  When *eval_config_path* is provided, loads from that path.  Otherwise
  auto-discovers ``eval/eval_config.json`` relative to the repo root or
  script directory (same pattern as eval-spec auto-discovery).

  The file is expected to contain:
    - ``metrics``: list of metric definitions (see eval/eval_config.json)

  Results are cached so the file is read only once.
  """
  cache_key = eval_config_path or "_AUTO_"
  if cache_key in _EVAL_CONFIG_CACHE:
    return _EVAL_CONFIG_CACHE[cache_key]

  if eval_config_path:
    if not os.path.isfile(eval_config_path):
      raise FileNotFoundError(f"Eval config file not found: {eval_config_path}")
    with open(eval_config_path) as f:
      result = json.load(f)
    _EVAL_CONFIG_CACHE[cache_key] = result
    logger.info("Loaded eval config from %s", eval_config_path)
    return result

  # Auto-discover eval_config.json from known locations
  for base in [_repo_root, _script_dir]:
    candidate = os.path.join(base, "eval", "eval_config.json")
    if os.path.isfile(candidate):
      logger.info("Auto-discovered eval config: %s", candidate)
      with open(candidate) as f:
        result = json.load(f)
      _EVAL_CONFIG_CACHE[cache_key] = result
      return result

  # No file anywhere: fall back to the SDK's canonical builtin rubrics
  # (the same data this repo ships in scripts/eval/eval_config.json).
  from bigquery_agent_analytics import builtin_metric_config

  logger.info("No eval_config.json found; using the SDK builtin rubrics.")
  result = builtin_metric_config()
  _EVAL_CONFIG_CACHE[cache_key] = result
  return result


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


def get_eval_metrics(eval_spec=None, eval_config=None):
  """Return the list of categorical metric definitions for quality evaluation.

  Metrics are loaded from *eval_config* (parsed dict, typically from
  ``eval/eval_config.json``).  Scope-aware metrics are dynamically enriched
  when *eval_spec* provides a ``scope`` (and/or ``ground_truth``) field, which
  also enables the ``declined`` category so the judge can credit correct
  out-of-scope refusals.
  """
  # The interpreter lives in the SDK core (evaluation_rubrics.build_metrics);
  # this wrapper derives the scope inputs from the eval spec and delegates.
  from bigquery_agent_analytics import build_metrics

  scope_context = _build_scope_context(eval_spec)
  has_scope = bool(eval_spec and eval_spec.get("scope"))

  if eval_config is None:
    eval_config = _load_eval_config()
  return build_metrics(
      eval_config, scope_context=scope_context, has_scope=has_scope
  )


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
        except json.JSONDecodeError:
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
    elif span.event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
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
  """Use LLM to count corrections and verifications in a conversation."""
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


_TURN_TAGGER_PROMPT = """\
Analyze this multi-turn conversation between a user and an agent.
Classify each USER turn and identify correction boundaries.
{scope_context}

CONVERSATION (turns numbered from 0):
{conversation}

For each USER turn, assign exactly one tag:
- CORRECTION: User tells the agent it is WRONG and provides the correct fact.
  Look for: "actually", "no", "that's wrong", "incorrect", contradicting a
  specific claim with a specific counter-fact, quoting a source that disagrees.
- VERIFY: User doubts the agent's answer without providing the correct fact.
  Look for: "are you sure", "can you check", "that doesn't sound right",
  "I was told differently", questioning without correcting.
- SPECIFICS: User asks for concrete details the agent omitted.
  Look for: "how many days exactly", "what's the percentage", "what date",
  asking for numbers/dates/limits the agent didn't provide.
- SCOPE: User flags the agent answered something it shouldn't have.
  Look for: "you shouldn't answer that", "that's not your area", pointing
  out the agent overstepped its domain.
- FOLLOWUP: Normal follow-up question or related topic. The agent's previous
  answer was acceptable.
- END: User is satisfied, conversation closing.

Also identify CORRECTION BOUNDARIES — the turn index where the user corrects
the agent. The pre-correction sub-trajectory ends ONE TURN BEFORE the
correction (i.e. the agent's wrong answer). The post-correction sub-trajectory
starts AT the correction turn and includes everything after.

For each correction boundary, extract:
- wrong_claim: what the agent said that was wrong (quote it)
- correct_fact: what the user said is right (quote it)
- agent_recovered: did the agent GENUINELY recover? Set to true ONLY if the
  agent looked up or verified the information (e.g. called a tool, cited a
  source, provided new details not in the user's correction). Set to false if
  the agent merely repeated or paraphrased the user's correction without
  independent verification — that is parroting, not recovery.

Return ONLY a JSON object:
{{"turn_tags": [
    {{"turn_index": 0, "role": "user", "tag": "...", "evidence": "brief reason"}},
    ...
  ],
  "correction_boundaries": [
    {{"turn_index": N, "wrong_claim": "...", "correct_fact": "...", "agent_recovered": true}},
    ...
  ],
  "sub_trajectories": [
    {{"label": "pre_correction_1", "start_turn": 0, "end_turn": N-1, "outcome": "wrong"}},
    {{"label": "post_correction_1", "start_turn": N, "end_turn": M, "outcome": "recovered"}}
  ]
}}

For sub_trajectory outcome after a correction, use:
- "recovered" — agent genuinely recovered (used tools, cited sources, added new info)
- "parroted" — agent just repeated the user's fact without verification
- "not_recovered" — agent did not accept the correction or continued with wrong info

Only tag USER turns (skip agent turns). If there are no corrections, return
empty correction_boundaries and a single sub_trajectory covering the whole
conversation.
"""


def _tag_conversation_turns(conversation, model, scope_context=""):
  """Classify each user turn and identify correction boundaries."""
  if not isinstance(conversation, list) or len(conversation) < 3:
    return None

  lines = []
  for i, turn in enumerate(conversation):
    role = "USER" if turn.get("role") == "user" else "AGENT"
    lines.append(f"[{i}] {role}: {turn.get('text', '')}")
  numbered = "\n".join(lines)

  ctx = ""
  if scope_context:
    ctx = f"\nCONTEXT:\n{scope_context}"

  prompt = _TURN_TAGGER_PROMPT.format(
      scope_context=ctx,
      conversation=numbered[:4000],
  )

  try:
    from google import genai
    from google.genai import types

    client = genai.Client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    raw = response.text.strip()
    if raw.startswith("```"):
      raw_lines = raw.split("\n")
      raw = "\n".join(
          raw_lines[1:-1] if raw_lines[-1].strip() == "```" else raw_lines[1:]
      )
    result = json.loads(raw)

    # Extract correction/verification counts from tags
    tags = result.get("turn_tags", [])
    result["corrections"] = sum(1 for t in tags if t.get("tag") == "CORRECTION")
    result["verifications"] = sum(1 for t in tags if t.get("tag") == "VERIFY")
    return result

  except Exception:
    logger.debug("Turn tagging failed, skipping", exc_info=True)
    return None


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

    # The conversation opener anchors the session's topic. `question` is the
    # *last* user message (aligned with the final response); golden matching
    # must use the FIRST one, or a correction session gets matched by its
    # pushback text ("I thought it was 10, right?") and never finds its
    # golden pair -- the conversations-file path already matches on turns[0].
    first_question = question
    for span in trace.spans:
      if span.event_type == "USER_MESSAGE_RECEIVED":
        c = span.content
        text = (
            (c.get("text_summary") or c.get("text") or "")
            if isinstance(c, dict)
            else (str(c) if c else "")
        )
        if text:
          first_question = text
          break

    result = {
        "session_id": trace.session_id,
        "user_id": (
            trace.identity.user_id
            if getattr(trace, "identity", None) is not None
            else getattr(trace, "user_id", None)
        ),
        "root_agent_name": (
            trace.identity.root_agent_name
            if getattr(trace, "identity", None) is not None
            else None
        ),
        "experiment_id": (
            trace.scope.experiment_id
            if getattr(trace, "scope", None) is not None
            else None
        ),
        "custom_labels": (
            trace.scope.labels_dict
            if getattr(trace, "scope", None) is not None
            else None
        ),
        "scope_signature": (
            trace.scope.scope_signature
            if getattr(trace, "scope", None) is not None
            else None
        ),
        "scope_coverage": getattr(trace, "scope_coverage", None),
        "time": (
            trace.start_time.strftime("%Y-%m-%d %H:%M:%S")
            if trace.start_time
            else "?"
        ),
        "question": question,
        "first_question": first_question,
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
    # Structured tool detail from the trace's TOOL_* spans, so BigQuery-scored
    # sessions carry the same {name, args} records local conversations do
    # (tool-aware analysts + compare_runs' tool-selection table). Emitted only
    # when the spans actually name their tools (or there were zero calls) --
    # key-absence stays a truthful "not captured" for older tables.
    tool_detail = [
        {"name": tc.get("tool_name") or "", "args": tc.get("args") or {}}
        for tc in trace.tool_calls
        if tc.get("tool_name") and tc.get("tool_name") != "unknown"
    ]
    if tool_detail or tool_calls == 0:
      result["tool_calls_detail"] = tool_detail
    results.append(result)

  if remote_lookups:
    logger.info("Resolved %d A2A responses", remote_lookups)

  return results


_REPORT_ATTRIBUTION_FIELDS = (
    "user_id",
    "root_agent_name",
    "scope_signature",
)
# U2 bounds one singular candidate-enumeration page at 64 exact
# identity/scope candidates. Explicit report session lists start with the same
# reservation, then grow until the returned population is provably complete.
_REPORT_INITIAL_CANDIDATES_PER_SESSION = 64
_REPORT_TRACE_LIMIT_CEILING = 1_048_576


class _ReportTracePopulationTruncatedError(RuntimeError):
  """A report listing saturated its last permitted completeness probe."""


def _resolved_entry_key(entry):
  """Stable identity/scope key for a resolved report context."""
  return (
      entry.get("session_id"),
      entry.get("user_id"),
      entry.get("root_agent_name"),
      entry.get("scope_signature"),
  )


class _ResolvedEntriesIndex(dict):
  """Dict-compatible report index with a reusable session lookup."""

  def __init__(self, indexed):
    super().__init__(indexed)
    by_session = {}
    by_attribution = {}
    for key, context in self.items():
      session_id = context.get(
          "session_id", key if isinstance(key, str) else None
      )
      by_session.setdefault(session_id, []).append(context)
      by_attribution.setdefault(_resolved_entry_key(context), []).append(
          context
      )
    self.by_session = by_session
    self.by_attribution = by_attribution


def _index_resolved_entries(entries):
  """Index resolved contexts without overwriting reused session IDs.

  Unique sessions retain their legacy string key. Colliding sessions use
  their full resolved identity/scope tuple. A duplicate full tuple is kept
  under an ordinal suffix rather than silently replacing earlier data.
  """
  grouped = {}
  for entry in entries:
    grouped.setdefault(entry.get("session_id"), []).append(entry)

  indexed = {}
  for session_id, candidates in grouped.items():
    if len(candidates) == 1:
      indexed[session_id] = candidates[0]
      continue
    for ordinal, entry in enumerate(candidates):
      key = _resolved_entry_key(entry)
      if key in indexed:
        logger.warning(
            "Duplicate resolved report candidate for session %s and the"
            " same identity/scope; retaining both with an ordinal key.",
            session_id,
        )
        key = (*key, ordinal)
      indexed[key] = entry
  return _ResolvedEntriesIndex(indexed)


def _resolved_candidates(resolved_map, session_id):
  """Return one session's candidates without rescanning a built index."""
  if isinstance(resolved_map, _ResolvedEntriesIndex):
    return list(resolved_map.by_session.get(session_id, ()))
  return [
      context
      for key, context in resolved_map.items()
      if context.get("session_id", key if isinstance(key, str) else None)
      == session_id
  ]


def _resolved_context_for_score(resolved_map, score):
  """Correlate a score to one context, failing closed on ambiguity."""
  session_id = score.session_id
  details = getattr(score, "details", None) or {}
  pinned_fields = [
      field for field in _REPORT_ATTRIBUTION_FIELDS if field in details
  ]
  exact_indexed_lookup = (
      isinstance(resolved_map, _ResolvedEntriesIndex)
      and type(session_id) is str
      and len(pinned_fields) == len(_REPORT_ATTRIBUTION_FIELDS)
      and all(
          type(details[field]) in (str, type(None))
          for field in _REPORT_ATTRIBUTION_FIELDS
      )
  )
  if exact_indexed_lookup:
    attribution = (
        session_id,
        *(details[field] for field in _REPORT_ATTRIBUTION_FIELDS),
    )
    candidates = list(resolved_map.by_attribution.get(attribution, ()))
  else:
    candidates = _resolved_candidates(resolved_map, session_id)
  if not candidates:
    return {}

  if pinned_fields and not exact_indexed_lookup:
    candidates = [
        context
        for context in candidates
        if all(context.get(field) == details[field] for field in pinned_fields)
    ]

  if len(candidates) == 1:
    return candidates[0]
  if candidates:
    logger.warning(
        "Ambiguous report correlation for session %s: %d trace candidates"
        " remain and the score has insufficient identity/scope attribution;"
        " omitting the association.",
        session_id,
        len(candidates),
    )
  else:
    logger.warning(
        "No report trace candidate matches the identity/scope attribution"
        " on score for session %s; omitting the association.",
        session_id,
    )
  return {}


def _resolved_context_for_session(resolved_map, session_id):
  """Return a legacy session context only when it is unique."""

  class _SessionOnlyScore:
    details = {}

    def __init__(self, value):
      self.session_id = value

  return _resolved_context_for_score(
      resolved_map, _SessionOnlyScore(session_id)
  )


def _report_identity_suffix(context):
  """Printable identity/scope attribution for a resolved report row."""
  dimensions = []
  for label, field in (
      ("user", "user_id"),
      ("root", "root_agent_name"),
      ("experiment", "experiment_id"),
      ("scope", "scope_signature"),
  ):
    value = context.get(field)
    if value is not None:
      dimensions.append(f"{label}={value}")
  if context.get("scope_signature") is None:
    coverage = context.get("scope_coverage")
    if coverage is not None:
      dimensions.append(f"scope_coverage={','.join(coverage)}")
  return f" [{', '.join(dimensions)}]" if dimensions else ""


# ---------------------------------------------------------------------------
# Local conversation support (no BigQuery required)
# ---------------------------------------------------------------------------


def _format_conversation_transcript(conv):
  """Convert a traffic-generator conversation dict to SDK transcript format.

  Produces the same ``user_input / agent_response`` lines as the
  ``CATEGORICAL_TRANSCRIPT_QUERY`` so that the categorical evaluator
  can process local conversations identically to BigQuery traces.
  """
  turns = conv.get("conversation", [])
  if turns:
    parts = []
    for turn in turns:
      role = turn.get("role", "user")
      text = turn.get("text", "")
      tag = turn.get("tag", "")
      if role == "user":
        tag_str = f" [{tag}]" if tag else ""
        parts.append(f"user_input{tag_str}: {text}")
      else:
        agent = conv.get("answered_by", "agent")
        parts.append(f"agent_response [{agent}]: {text}")
    return "\n".join(parts)

  # Fallback: single-turn
  q = conv.get("question", "")
  r = conv.get("final_response", conv.get("response", ""))
  agent = conv.get("answered_by", "agent")
  return f"user_input: {q}\nagent_response [{agent}]: {r}"


async def _build_resolved_map_from_conversations(
    conversations,
    model,
    concurrency=10,
    tag_turns=False,
    scope_context="",
):
  """Build a resolved_map from local conversation dicts.

  Returns the same ``{session_id: {...}}`` structure as
  ``resolve_trace_responses`` so downstream code (``_build_json_output``,
  ``_write_md_report``, ``_print_eval_results``) works unchanged.

  Infers corrections/verifications concurrently for multi-turn sessions.
  When ``tag_turns=True``, uses the full turn tagger instead of the simpler
  correction counter, adding ``turn_tags``, ``correction_boundaries``, and
  ``sub_trajectories`` to each resolved entry.
  """
  import asyncio

  # First pass: build entries, collect those needing inference
  entries = []
  to_infer = []
  for conv in conversations:
    sid = conv.get("session_id", f"local_{id(conv)}")
    turns = conv.get("conversation", [])
    user_turn_count = (
        sum(1 for t in turns if t.get("role") == "user") if turns else 1
    )
    tool_calls = conv.get("tool_calls", 0)
    corrections = conv.get("corrections", 0)
    verifications = conv.get("verifications", 0)
    needs_tagging = turns and user_turn_count > 1
    needs_inference = needs_tagging and corrections == 0 and verifications == 0
    entries.append(
        {
            "sid": sid,
            "conv": conv,
            "turns": turns,
            "user_turns": user_turn_count,
            "tool_calls": tool_calls,
            "corrections": corrections,
            "verifications": verifications,
        }
    )
    if tag_turns and needs_tagging:
      to_infer.append((len(entries) - 1, turns))
    elif needs_inference:
      to_infer.append((len(entries) - 1, turns))

  # Concurrent inference
  if to_infer:
    semaphore = asyncio.Semaphore(concurrency)

    if tag_turns:

      async def _infer_one(turns):
        async with semaphore:
          return await asyncio.to_thread(
              _tag_conversation_turns,
              turns,
              model,
              scope_context,
          )

      tag_results = await asyncio.gather(
          *[_infer_one(turns) for _, turns in to_infer]
      )
      for (idx, _), tag_data in zip(to_infer, tag_results):
        if tag_data:
          entries[idx]["corrections"] = tag_data.get("corrections", 0)
          entries[idx]["verifications"] = tag_data.get("verifications", 0)
          entries[idx]["turn_tags"] = tag_data.get("turn_tags", [])
          entries[idx]["correction_boundaries"] = tag_data.get(
              "correction_boundaries", []
          )
          entries[idx]["sub_trajectories"] = tag_data.get(
              "sub_trajectories", []
          )
    else:

      async def _infer_one(turns):
        async with semaphore:
          return await asyncio.to_thread(_infer_corrections, turns, model)

      infer_results = await asyncio.gather(
          *[_infer_one(turns) for _, turns in to_infer]
      )
      for (idx, _), (corr, verif) in zip(to_infer, infer_results):
        entries[idx]["corrections"] = corr
        entries[idx]["verifications"] = verif

  resolved = []
  for entry in entries:
    conv = entry["conv"]
    resolved_entry = {
        "session_id": entry["sid"],
        "question": conv.get("question", ""),
        "response": conv.get("final_response", conv.get("response", "")),
        "answered_by": conv.get("answered_by", "unknown"),
        "is_a2a": False,
        "latency_s": conv.get("latency_s"),
        "user_turns": entry["user_turns"],
        "tool_calls": entry["tool_calls"],
        "corrections": entry["corrections"],
        "verifications": entry["verifications"],
        "conversation": entry["turns"],
    }
    # Carry structured tool detail only when the conversation actually has it,
    # so a legacy conversations file (no such field) stays truthfully "absent".
    if "tool_calls_detail" in conv:
      resolved_entry["tool_calls_detail"] = conv["tool_calls_detail"]
    if tag_turns:
      resolved_entry["turn_tags"] = entry.get("turn_tags", [])
      resolved_entry["correction_boundaries"] = entry.get(
          "correction_boundaries", []
      )
      resolved_entry["sub_trajectories"] = entry.get("sub_trajectories", [])
    resolved.append(resolved_entry)
  return _index_resolved_entries(resolved)


# ---------------------------------------------------------------------------
# Run evaluation
# ---------------------------------------------------------------------------


def run_evaluation(
    time_range=None,
    limit=100,
    model=None,
    persist=False,
    app_name=None,
    eval_spec=None,
    session_id=None,
    session_ids=None,
    tag_turns=False,
    eval_config=None,
    custom_labels=None,
    golden_threshold=_DEFAULT_GOLDEN_THRESHOLD,
) -> dict:
  from bigquery_agent_analytics import CategoricalEvaluationConfig
  from bigquery_agent_analytics import TraceFilter
  from bigquery_agent_analytics.categorical_evaluator import CategoricalContextSource

  model = model or EVAL_MODEL_ID
  client = get_client()

  if eval_spec is None:
    eval_spec = _load_eval_spec()
  if not eval_spec or not eval_spec.get("golden_qa"):
    logger.warning(
        "No golden_qa in the eval spec: response_usefulness and task_grounding "
        "are LLM estimates WITHOUT ground truth and can mislabel verbose, "
        "tool-grounded answers as ungrounded/unhelpful. For trustworthy "
        "correctness, pass --eval-spec with a golden_qa list (question + "
        "expected_answer); the judge then grades against the expected answer "
        "(see summary.golden_eval_summary)."
    )
  metrics = get_eval_metrics(eval_spec=eval_spec, eval_config=eval_config)
  cat_config = CategoricalEvaluationConfig(
      metrics=metrics,
      endpoint=model,
      temperature=0.0,
      include_justification=True,
      persist_results=persist,
      results_table="quality_eval_results" if persist else None,
  )

  if session_id:
    trace_filter = TraceFilter(
        session_ids=[session_id],
        limit=_explicit_report_trace_limit([session_id]),
        custom_labels=custom_labels,
    )
  elif session_ids:
    trace_filter = TraceFilter(
        session_ids=session_ids,
        limit=_explicit_report_trace_limit(session_ids),
        custom_labels=custom_labels,
    )
    if app_name:
      trace_filter.root_agent_name = app_name
  else:
    effective_time_range = time_range
    if effective_time_range and effective_time_range.lower() == "all":
      effective_time_range = None

    if effective_time_range:
      trace_filter = TraceFilter.from_cli_args(
          last=effective_time_range, custom_labels=custom_labels
      )
    else:
      trace_filter = TraceFilter(custom_labels=custom_labels)
    trace_filter.limit = limit
    if app_name:
      trace_filter.root_agent_name = app_name

  # Fetch traces and match golden Q&A BEFORE evaluation so the server-side
  # judge and report enrichment use the same exact resolved selector keys.
  explicit_sessions = bool(session_id or session_ids)
  if explicit_sessions:
    traces, trace_filter = _list_complete_report_traces(client, trace_filter)
  else:
    traces = client.list_traces(filter_criteria=trace_filter)
  resolved = resolve_trace_responses(traces)
  resolved_map = _index_resolved_entries(resolved)

  golden_metadata = {}
  golden_ctx = {}
  golden_qa = (eval_spec or {}).get("golden_qa")
  if golden_qa:
    # Matching keys off the conversation's FIRST user message (the
    # topic anchor), consistent with the conversations-file path.
    question_by_selector = {}
    for ctx in resolved_map.values():
      selector = _resolved_selector_from_context(ctx)
      if selector is not None:
        question_by_selector[selector] = ctx.get("first_question") or ctx.get(
            "question", ""
        )
    golden_ctx, golden_metadata = match_golden_qa(
        question_by_selector, golden_qa, threshold=golden_threshold
    )

  evaluation_kwargs = dict(
      config=cat_config,
      filters=trace_filter,
  )
  if golden_ctx:
    evaluation_kwargs.update(
        per_session_context=golden_ctx,
        context_source=CategoricalContextSource.GOLDEN_EXPECTED_ANSWER,
    )
  report = client.evaluate_categorical(**evaluation_kwargs)

  all_session_ids = [sr.session_id for sr in report.session_results]
  logger.info("Resolving responses for %d sessions...", len(all_session_ids))

  # The pre-fetch and the evaluation run the same filter, but the
  # evaluation's transcript CTE can admit sessions the pre-fetch missed
  # (or vice versa); backfill any evaluated session we did not resolve.
  resolved_session_ids = {
      context.get("session_id") for context in resolved_map.values()
  }
  missing = [sid for sid in all_session_ids if sid not in resolved_session_ids]
  if missing:
    extra, _ = _list_complete_report_traces(
        client,
        TraceFilter(
            session_ids=missing,
            limit=_explicit_report_trace_limit(missing),
            custom_labels=custom_labels,
        ),
    )
    resolved_map = _index_resolved_entries(
        [*resolved_map.values(), *resolve_trace_responses(extra)]
    )
    resolved = list(resolved_map.values())

  # Infer corrections/verifications for multi-turn sessions (concurrent).
  mt_sessions = [
      r
      for r in resolved
      if r.get("user_turns", 0) > 1 and r.get("conversation")
  ]
  if mt_sessions:
    import asyncio

    if tag_turns:
      scope_context = _build_scope_context(eval_spec)
      logger.info(
          "Tagging turns for %d multi-turn sessions...",
          len(mt_sessions),
      )
      semaphore = asyncio.Semaphore(10)

      async def _tag_one(conv):
        async with semaphore:
          return await asyncio.to_thread(
              _tag_conversation_turns,
              conv,
              model,
              scope_context,
          )

      async def _tag_all():
        return await asyncio.gather(
            *[_tag_one(r["conversation"]) for r in mt_sessions]
        )

      tag_results = asyncio.run(_tag_all())
      for r, tag_data in zip(mt_sessions, tag_results):
        if tag_data:
          r["corrections"] = tag_data.get("corrections", 0)
          r["verifications"] = tag_data.get("verifications", 0)
          r["turn_tags"] = tag_data.get("turn_tags", [])
          r["correction_boundaries"] = tag_data.get("correction_boundaries", [])
          r["sub_trajectories"] = tag_data.get("sub_trajectories", [])
    else:
      logger.info(
          "Inferring corrections for %d multi-turn sessions...",
          len(mt_sessions),
      )
      semaphore = asyncio.Semaphore(10)

      async def _infer_one(conv):
        async with semaphore:
          return await asyncio.to_thread(_infer_corrections, conv, model)

      async def _infer_all():
        return await asyncio.gather(
            *[_infer_one(r["conversation"]) for r in mt_sessions]
        )

      results = asyncio.run(_infer_all())
      for r, (corrections, verifications) in zip(mt_sessions, results):
        r["corrections"] = corrections
        r["verifications"] = verifications

  return {
      "report": report,
      "resolved_map": resolved_map,
      "golden_metadata": golden_metadata,
  }


def generate_quality_report(
    session_ids: list[str],
    model: str | None = None,
    eval_spec: dict | None = None,
) -> dict:
  """Evaluate sessions and return a structured quality report dict.

  This is the main public API for programmatic use.  It combines
  ``run_evaluation`` (trace fetching, LLM scoring, correction inference)
  with ``_build_json_output`` (structured dict) in a single call.

  Args:
      session_ids: BigQuery session IDs to evaluate.
      model: Eval model override (default: EVAL_MODEL_ID env or
          gemini-2.5-flash).
      eval_spec: Optional eval spec dict ({scope, ground_truth, golden_qa}).
          When None, ``eval/data/eval_spec.json`` is auto-discovered.

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
  result = run_evaluation(
      session_ids=session_ids,
      model=model,
      eval_spec=eval_spec,
  )
  elapsed = time.time() - t0

  output = _build_json_output(result["report"], result["resolved_map"])
  _inject_golden_summary(output, result.get("golden_metadata"))
  output["summary"]["elapsed_seconds"] = round(elapsed, 1)
  return output


def run_evaluation_from_conversations(
    conversations,
    model=None,
    eval_spec=None,
    concurrency=10,
    tag_turns=False,
    eval_config=None,
    per_session_context=None,
    golden_threshold=_DEFAULT_GOLDEN_THRESHOLD,
):
  """Evaluate local conversations without BigQuery.

  Converts traffic-generator conversation dicts to transcripts, classifies
  them via the Gemini API, and returns the same ``{"report", "resolved_map"}``
  structure as ``run_evaluation`` so all downstream output functions work
  unchanged.

  Args:
      conversations: List of conversation dicts (traffic generator format).
      model: Eval model override.
      eval_spec: Optional eval spec dict ({scope, ground_truth, golden_qa}).
          When None, ``eval/data/eval_spec.json`` is auto-discovered. Provides
          scope grounding and, when ``golden_qa`` is present, per-question
          correctness grounding via embedding matching.
      concurrency: Max parallel API calls (default 10).
      tag_turns: When True, run the full turn tagger to classify each user
          turn and identify correction boundaries / sub-trajectories.
      per_session_context: Optional caller-supplied per-session judge context.
          Merged with (and overridden by) any golden-Q&A matches.
      golden_threshold: Cosine-similarity threshold for golden matching.

  Returns:
      Dict with ``report``, ``resolved_map``, and ``golden_metadata`` keys.
  """
  import asyncio

  from bigquery_agent_analytics import CategoricalEvaluationConfig
  from bigquery_agent_analytics.categorical_evaluator import build_categorical_report
  from bigquery_agent_analytics.categorical_evaluator import classify_sessions_via_api

  if eval_spec is None:
    eval_spec = _load_eval_spec()
  model = (
      model or EVAL_MODEL_ID or os.getenv("EVAL_MODEL_ID", "gemini-2.5-flash")
  )
  metrics = get_eval_metrics(eval_spec=eval_spec, eval_config=eval_config)
  cat_config = CategoricalEvaluationConfig(
      metrics=metrics,
      endpoint=model,
      temperature=0.0,
      include_justification=True,
  )

  scope_context = _build_scope_context(eval_spec)

  # Golden Q&A matching: inject per-question expected answers / decline notes
  # into the judge prompt for sessions whose question matches a golden entry.
  golden_metadata = {}
  golden_qa = (eval_spec or {}).get("golden_qa")
  if golden_qa:
    question_by_sid = {
        conv.get("session_id", f"local_{id(conv)}"): conv.get("question", "")
        for conv in conversations
    }
    golden_ctx, golden_metadata = match_golden_qa(
        question_by_sid, golden_qa, threshold=golden_threshold
    )
    per_session_context = {**(per_session_context or {}), **golden_ctx}

  transcripts = {}
  for conv in conversations:
    sid = conv.get("session_id", f"local_{id(conv)}")
    transcripts[sid] = _format_conversation_transcript(conv)

  logger.info(
      "Classifying %d local conversations (model=%s, concurrency=%d, tag_turns=%s)...",
      len(transcripts),
      model,
      concurrency,
      tag_turns,
  )

  async def _run_all():
    classify_task = classify_sessions_via_api(
        transcripts,
        cat_config,
        model,
        per_session_context=per_session_context,
    )
    resolve_task = _build_resolved_map_from_conversations(
        conversations,
        model,
        concurrency=concurrency,
        tag_turns=tag_turns,
        scope_context=scope_context,
    )
    return await asyncio.gather(classify_task, resolve_task)

  session_results, resolved_map = asyncio.run(_run_all())

  report = build_categorical_report(
      dataset="local_conversations",
      session_results=session_results,
      config=cat_config,
  )

  return {
      "report": report,
      "resolved_map": resolved_map,
      "golden_metadata": golden_metadata,
  }


def generate_quality_report_from_conversations(
    conversations,
    model=None,
    eval_spec=None,
    concurrency=10,
    tag_turns=False,
    trajectory_samples=0,
    per_session_context=None,
    golden_threshold=_DEFAULT_GOLDEN_THRESHOLD,
    eval_config=None,
    trace_filter=None,
) -> dict:
  """Evaluate local conversations and return a structured quality report.

  This is the public API for scoring conversations from a traffic generator
  or any local JSON file, without requiring BigQuery.  Returns the same
  dict structure as ``generate_quality_report``.

  Args:
      conversations: List of conversation dicts.
      model: Eval model override.
      eval_spec: Optional eval spec dict ({scope, ground_truth, golden_qa}).
          When None, ``eval/data/eval_spec.json`` is auto-discovered.
      concurrency: Max parallel API calls (default 10).
      tag_turns: When True, run the full turn tagger to add per-turn tags,
          correction boundaries, and sub-trajectories to the output.
      trajectory_samples: Number of execution traces to fetch from BigQuery.
      per_session_context: Optional caller-supplied per-session judge context
          (merged with golden-Q&A matches).
      golden_threshold: Cosine-similarity threshold for golden matching.
      eval_config: Optional metric-definition override (same as the CLI
          ``--eval-config``); when None the built-in metrics are used.
      trace_filter: Optional ``TraceFilter`` carrying app/label/time bounds
          for the trajectory fetch; only its session_ids/limit are replaced.

  Returns:
      Dict with ``summary`` and ``sessions`` keys. When the eval spec carries
      ``golden_qa``, a ``golden_eval_summary`` block and per-session
      ``golden_eval`` entries are included.
  """
  if PROJECT_ID is None:
    _load_config()
  t0 = time.time()
  result = run_evaluation_from_conversations(
      conversations,
      model=model,
      eval_spec=eval_spec,
      concurrency=concurrency,
      tag_turns=tag_turns,
      per_session_context=per_session_context,
      golden_threshold=golden_threshold,
      eval_config=eval_config,
  )
  elapsed = time.time() - t0

  trajectories = {}
  if trajectory_samples and trajectory_samples > 0:
    traj_sids, trajectory_map = _select_trajectory_population(
        result["report"],
        result["resolved_map"],
        trajectory_samples,
    )
    trajectories = _fetch_session_traces(
        traj_sids,
        trajectory_samples,
        max_traces=_trace_fetch_limit(trajectory_map, traj_sids),
        resolved_map=trajectory_map,
        base_filter=trace_filter,
    )

  output = _build_json_output(
      result["report"],
      result["resolved_map"],
      trajectories=trajectories,
  )
  output["summary"]["elapsed_seconds"] = round(elapsed, 1)
  _inject_golden_summary(output, result.get("golden_metadata"))
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
    print(
        f"\n  [{r['time']}] {r['session_id']}"
        f"{_report_identity_suffix(r)}{a2a_tag}"
    )
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

  conversations_file = getattr(args, "conversations_file", None)

  t0 = time.time()
  eval_spec = _load_eval_spec(getattr(args, "eval_spec", None))
  golden_threshold = getattr(
      args, "golden_threshold", _DEFAULT_GOLDEN_THRESHOLD
  )
  eval_config = _load_eval_config(getattr(args, "eval_config", None))

  # --dimensions primary: keep only the 2 primary metrics to cut LLM-judge
  # cost ~4x. Build a filtered copy so the cached config is not mutated.
  if getattr(args, "dimensions", "full") == "primary":
    eval_config = {
        **eval_config,
        "metrics": [
            m
            for m in eval_config.get("metrics", [])
            if m.get("name") in _PRIMARY_METRICS
        ],
    }
    logger.info(
        "Dimensions mode: primary — scoring only %s (skipping 5 quality "
        "dimensions)",
        ", ".join(sorted(_PRIMARY_METRICS)),
    )

  custom_labels = None
  if getattr(args, "label", None):
    custom_labels = {}
    for item in args.label:
      if "=" not in item:
        logger.error("--label requires KEY=VALUE format, got: %s", item)
        sys.exit(1)
      k, v = item.split("=", 1)
      custom_labels[k] = v

  # CLI-derived bounds for every trajectory fetch in this invocation: the
  # trace reads stay scoped to the same app/labels/time the scoring ran with.
  base_trace_filter = _enrichment_base_filter(args, custom_labels)

  if conversations_file:
    # --- Local conversations path (no BigQuery) ---
    logger.info("Source: local conversations file %s", conversations_file)
    logger.info("Evaluation model: %s", model)
    with open(conversations_file) as _f:
      data = json.load(_f)
    conversations = (
        data.get("conversations", []) if isinstance(data, dict) else data
    )
    if not conversations:
      logger.error("No conversations found in %s", conversations_file)
      sys.exit(1)
    total = len(conversations)
    if args.limit and args.limit < total:
      conversations = conversations[: args.limit]
      logger.info("Using %d of %d conversations (--limit)", args.limit, total)
    else:
      logger.info("Loaded %d conversations", total)

    try:
      if eval_spec:
        logger.info(
            "Eval spec: scope=%s, golden_qa=%d",
            bool(eval_spec.get("scope")),
            len(eval_spec.get("golden_qa") or []),
        )
      concurrency = getattr(args, "concurrency", 10)
      tag_turns = getattr(args, "tag_turns", False)
      result = run_evaluation_from_conversations(
          conversations,
          model=model,
          eval_spec=eval_spec,
          concurrency=concurrency,
          tag_turns=tag_turns,
          eval_config=eval_config,
          golden_threshold=golden_threshold,
      )
    except Exception:
      logger.exception("Evaluation failed")
      sys.exit(1)
  else:
    # --- BigQuery path (existing) ---
    logger.info(
        "Project: %s, Dataset: %s, Table: %s",
        PROJECT_ID,
        DATASET_ID,
        TABLE_ID,
    )
    logger.info("Location: %s", DATASET_LOCATION)
    logger.info("Evaluation model: %s", model)
    logger.info(
        "Parameters: time_period=%s, limit=%d, persist=%s, report=%s, "
        "samples=%s",
        args.time_period or "all",
        args.limit,
        args.persist,
        args.report,
        args.samples or "default (10/5/3)",
    )

    session_ids = None
    if args.session_ids_file:
      with open(args.session_ids_file) as _f:
        _data = json.load(_f)
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

    try:
      if eval_spec and eval_spec.get("scope"):
        logger.info("Eval spec scope active")
      tag_turns = getattr(args, "tag_turns", False)
      result = run_evaluation(
          time_range=args.time_period,
          limit=args.limit,
          model=model,
          persist=args.persist,
          app_name=args.app_name,
          eval_spec=eval_spec,
          session_id=args.session,
          session_ids=session_ids,
          tag_turns=tag_turns,
          eval_config=eval_config,
          custom_labels=custom_labels,
          golden_threshold=golden_threshold,
      )
    except Exception:
      logger.exception("Evaluation failed")
      sys.exit(1)

  elapsed = time.time() - t0

  # --- Shared post-processing ---
  result["report"].details["elapsed_seconds"] = round(elapsed, 1)
  result["report"].details["project"] = PROJECT_ID
  result["report"].details["dataset"] = f"{DATASET_ID}.{TABLE_ID}"
  result["report"].details["location"] = DATASET_LOCATION
  result["report"].details["eval_model"] = model
  if not conversations_file:
    result["report"].details["time_period"] = args.time_period or "all"
    result["report"].details["limit"] = args.limit
    result["report"].details["persist"] = args.persist
    if args.app_name:
      result["report"].details["app_name"] = args.app_name
    if custom_labels:
      result["report"].details["labels"] = ", ".join(
          f"{k}={v}" for k, v in custom_labels.items()
      )
  result["report"].details["samples"] = args.samples or None
  _print_eval_results(
      result["report"],
      result["resolved_map"],
      samples=args.samples,
      unhelpful_threshold=args.threshold,
  )

  # --- Trajectory fetching ---
  trajectories = {}
  trajectory_samples = getattr(args, "trajectory_samples", 0)
  tag_turns = getattr(args, "tag_turns", False)
  if trajectory_samples and trajectory_samples > 0:
    traj_sids, trajectory_map = _select_trajectory_population(
        result["report"],
        result["resolved_map"],
        trajectory_samples,
    )
    # Also fetch trajectories for all correction sessions (for inline display)
    if tag_turns:
      selected_contexts = list(trajectory_map.values())
      selected_keys = {
          _resolved_entry_key(context) for context in selected_contexts
      }
      for context in result["resolved_map"].values():
        if not context.get("correction_boundaries"):
          continue
        key = _resolved_entry_key(context)
        if key in selected_keys:
          continue
        selected_keys.add(key)
        selected_contexts.append(context)
      trajectory_map = _index_resolved_entries(selected_contexts)
      traj_sids = list(
          dict.fromkeys(
              ctx.get("session_id")
              for ctx in trajectory_map.values()
              if ctx.get("session_id")
          )
      )
    logger.info(
        "Fetching %d execution trajectories from BigQuery...", len(traj_sids)
    )
    trajectories = _fetch_session_traces(
        traj_sids,
        len(traj_sids),
        max_traces=_trace_fetch_limit(trajectory_map, traj_sids),
        resolved_map=trajectory_map,
        base_filter=base_trace_filter,
    )
    if trajectories:
      logger.info("Fetched %d trajectories", len(trajectories))
      for trace_obj in trajectories.values():
        ctx = _resolved_context_for_trace(result["resolved_map"], trace_obj)
        if ctx and ctx.get("answered_by") == "unknown":
          ctx["answered_by"] = get_responding_agent(trace_obj)
    else:
      logger.warning("No trajectories fetched (BQ may not be configured)")

  # Single-session mode: always fetch trajectory from BQ
  if args.session and not trajectories and not conversations_file:
    trajectories = _fetch_session_traces(
        [args.session],
        max_sessions=1,
        max_traces=_trace_fetch_limit(result["resolved_map"], [args.session]),
        resolved_map=result["resolved_map"],
        base_filter=base_trace_filter,
    )
    if trajectories:
      for trace_obj in trajectories.values():
        ctx = _resolved_context_for_trace(result["resolved_map"], trace_obj)
        if ctx and ctx.get("answered_by") == "unknown":
          ctx["answered_by"] = get_responding_agent(trace_obj)

  # Print execution trace to console for single-session mode
  if args.session and trajectories:
    trace_obj = _trace_for_session(trajectories, args.session)
    if trace_obj:
      hr = "─" * 70
      print(f"\n{'=' * 70}")
      print("EXECUTION TRACE")
      print(f"{'=' * 70}")
      print(_render_trace(trace_obj))
      ctx = _resolved_context_for_session(result["resolved_map"], args.session)
      sub_trajs = ctx.get("sub_trajectories", [])
      conversation = ctx.get("conversation", [])
      if sub_trajs and conversation:
        segments = _segment_trace_by_turns(
            trace_obj,
            conversation,
            sub_trajs,
        )
        if segments:
          print(f"\n{hr}")
          print("  SUB-TRAJECTORY SEGMENTATION")
          print(hr)
          for seg in segments:
            icon = "✅" if seg["outcome"] in ("correct", "recovered") else "❌"
            print(
                f"\n  {icon} {seg['label']} "
                f"(turns {seg['start_turn']}-{seg['end_turn']}) "
                f"→ {seg['outcome']}"
            )
            for line in seg["trace"].split("\n"):
              print(f"  {line}")
      print(f"{'=' * 70}\n")

  report_path = None
  md_dir = None
  md_out_path = None
  if args.output_json and args.output_json != "-":
    md_dir = os.path.dirname(os.path.abspath(args.output_json))
    # Name the markdown after the JSON (v0_test_report.json -> v0_test_report.md)
    # so every scored artifact has a human-readable twin with a stable name.
    if args.output_json.endswith(".json"):
      md_out_path = os.path.abspath(args.output_json)[: -len(".json")] + ".md"
  if args.report:
    report_path = _write_md_report(
        result["report"],
        result["resolved_map"],
        args,
        report_dir=md_dir,
        trajectories=trajectories,
        out_path=md_out_path,
    )

  if report_path:
    print(f"\n  Markdown report: {report_path}")

  if args.output_json:
    output = _build_json_output(
        result["report"],
        result["resolved_map"],
        trajectories=trajectories,
    )
    _inject_golden_summary(output, result.get("golden_metadata"))
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
    ctx = _resolved_context_for_score(resolved_map, sr)
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
#
# The middle-category names deliberately differ per dimension
# (``mostly_correct``, ``partial``, ``somewhat_specific``, ...): the LLM judge
# is given the full per-dimension vocabulary, and a name that fits the
# dimension produces better classifications than a generic ``medium``. Do not
# "normalize" them to a single shared word.
#
# ``correct`` appears as a category in both ``correctness`` and
# ``first_time_right``. That is fine — categories are always looked up keyed by
# metric_name, so the two never collide. ``tool_usage.no_tool_needed`` scores 2
# because not calling a tool is the *correct* outcome when none was needed
# (e.g. a greeting or a correctly-declined out-of-scope question); without it,
# those sessions would be penalised as a Tool Usage failure.
_DIMENSION_SCORES = {
    "correctness": {"correct": 2, "mostly_correct": 1, "incorrect": 0},
    "tool_usage": {"proper": 2, "no_tool_needed": 2, "partial": 1, "none": 0},
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

_DIMENSION_NAMES = list(_DIMENSION_SCORES.keys())  # order matters for rendering

_PRIMARY_METRICS = {"response_usefulness", "task_grounding"}

_SCORECARD_ICONS = {
    "correct": "✅",
    "mostly_correct": "⚠️",
    "incorrect": "❌",
    "proper": "✅",
    "no_tool_needed": "➖",  # neutral: no tool was needed (a correct outcome)
    "partial": "⚠️",
    "none": "❌",
    "specific": "✅",
    "somewhat_specific": "⚠️",
    "vague": "❌",
    "compliant": "✅",
    "partially_compliant": "⚠️",
    "non_compliant": "❌",
    "clarification_needed": "⚠️",
    "correction_needed": "❌",
}

# Maps dimension → its worst (score-0) category, used for "Low X" report
# sections. A dimension with no score-0 category is omitted rather than raising
# StopIteration at import time.
_DIMENSION_LOW_CATEGORIES = {
    dim: low_cat
    for dim, cats in _DIMENSION_SCORES.items()
    if (low_cat := next((c for c, s in cats.items() if s == 0), None))
}

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
        if mr.parse_error or mr.category not in score_map:
          continue
        dim_totals[mr.metric_name].append(score_map[mr.category])
  return {
      d: round(sum(scores) / len(scores), 2) if scores else 0
      for d, scores in dim_totals.items()
  }


def _has_dimension_data(dim_avgs):
  """True when the quality dimensions were actually scored.

  A run with ``--dimensions primary`` (or any run that scored no dimension
  metrics) yields all-zero averages. Treating that as real data would render a
  misleading "every dimension is 0.0 / failing" report, so all three output
  paths (console, markdown, JSON) gate the dimension block on this predicate.
  """
  return any(v > 0 for v in dim_avgs.values())


def _compute_multiturn_stats(resolved_map):
  """Compute multi-turn efficiency statistics from resolved traces."""
  user_turns = [r.get("user_turns", 0) for r in resolved_map.values()]
  tool_calls = [r.get("tool_calls", 0) for r in resolved_map.values()]
  corrections = [r.get("corrections", 0) for r in resolved_map.values()]
  verifications = [r.get("verifications", 0) for r in resolved_map.values()]
  total = len(user_turns)
  if not total:
    return {
        "avg_user_turns": 0,
        "avg_tool_calls": 0,
        "multi_turn_sessions": 0,
    }
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
  a2a_count = sum(bool(ctx.get("is_a2a")) for ctx in resolved_map.values())

  # --- Per-session details ---
  samples_dict = _parse_samples(samples)
  for cat, cat_label in [
      ("unhelpful", "UNHELPFUL"),
      ("partial", "PARTIAL"),
      ("declined", "DECLINED (out-of-scope)"),
      ("meaningful", "MEANINGFUL"),
      ("unknown", "UNCLASSIFIED (parse errors)"),
  ]:
    cat_limit = _get_sample_limit(samples_dict, cat)
    limit = len(by_category.get(cat, [])) if cat_limit is None else cat_limit
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
      ctx = _resolved_context_for_score(resolved_map, sr)
      question = ctx.get("question", "")
      response = ctx.get("response", "")
      answered_by = ctx.get("answered_by", "")

      a2a_tag = "  [A2A]" if ctx.get("is_a2a") else ""
      agent_tag = f"  \u2192 {answered_by}" if answered_by else ""
      print(
          f"\n  Session:     {sid}{_report_identity_suffix(ctx)}"
          f"{a2a_tag}{agent_tag}"
      )
      q = " ".join(question.split()) if question else "(none)"
      r = " ".join(response.split()) if response else "(none)"
      print(f"  Question:    {q}")
      print(f'  Response:    "{r}"')

      # Primary metrics with justifications
      for mr in sr.metrics:
        if mr.metric_name not in _PRIMARY_METRICS:
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
        if mr.metric_name in _PRIMARY_METRICS:
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
  if a2a_count:
    print(f"  A2A sessions detected    : {a2a_count}")

  # --- Failure breakdown: skill gap vs knowledge gap vs tool gap ---
  counts, _ = _failure_breakdown_from_report(report)
  total_sessions = report.total_sessions or 1
  if _has_failure_attribution_data(report) and any(counts.values()):
    unaddressable = counts["knowledge_gap"] + counts["tool_gap"]
    addressable = total_sessions - unaddressable
    good = sum(
        1
        for sr in report.session_results
        for mr in sr.metrics
        if mr.metric_name == "response_usefulness"
        and mr.category in ("meaningful", "declined")
    )
    addr_rate = (good / addressable * 100) if addressable else 0.0
    print(
        f"  Failure causes           : "
        f"skill={counts['skill_gap']} (evolution)  "
        f"knowledge={counts['knowledge_gap']} (add data)  "
        f"tool={counts['tool_gap']} (build tool)"
    )
    print(
        f"  Addressable meaningful   : {addr_rate:.1f}%"
        f"  (excludes {unaddressable} unaddressable gaps)"
    )

  # --- Dimension averages (0-2 scale) ---
  dim_avgs = _compute_dimension_averages(report)
  if _has_dimension_data(dim_avgs):
    print(f"\n  Quality Dimensions (0-2 scale):")
    for dim, avg in dim_avgs.items():
      bar = "#" * int(avg * 25)
      label = _METRIC_LABELS.get(dim, dim)
      print(f"    {label:<20s}: {avg:.2f} / 2.00  {bar}")
      desc = _DIMENSION_DESCRIPTIONS.get(dim)
      if desc:
        print(f"    {'':<20s}  ↳ {desc}")

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
    if metric_name not in _PRIMARY_METRICS:
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
# Execution trajectory fetching
# ---------------------------------------------------------------------------


def _import_render_timing_tree():
  """Import render_timing_tree from latency_report.py."""
  try:
    from latency_report import render_timing_tree

    return render_timing_tree
  except ImportError:
    pass
  try:
    import importlib.util

    _lr_path = os.path.join(_script_dir, "latency_report.py")
    spec = importlib.util.spec_from_file_location("latency_report", _lr_path)
    _lr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_lr)
    return _lr.render_timing_tree
  except Exception:
    return None


def _render_trace(trace, header=True):
  """Render a Trace object as a timing tree string."""
  render_fn = _import_render_timing_tree()
  if not render_fn:
    return ""
  rendered = render_fn(trace)
  if not header:
    lines = rendered.split("\n")
    if len(lines) > 3:
      return "\n".join(lines[3:])
  return rendered


def _segment_trace_by_turns(trace, conversation, sub_trajectories):
  """Segment an execution trace at correction boundaries.

  Maps conversation turn indices to USER_MESSAGE_RECEIVED spans in the trace,
  then splits the trace into sub-segments aligned with correction sub-trajectories.

  Returns a list of dicts: {label, outcome, start_turn, end_turn, trace: str}
  """
  if not sub_trajectories or not trace or not trace.spans or not conversation:
    return []

  user_msg_spans = sorted(
      [s for s in trace.spans if s.event_type == "USER_MESSAGE_RECEIVED"],
      key=lambda s: s.timestamp,
  )
  if not user_msg_spans:
    return []

  user_turn_indices = [
      i for i, t in enumerate(conversation) if t.get("role") == "user"
  ]

  conv_idx_to_trace_span = {}
  for j, conv_idx in enumerate(user_turn_indices):
    if j < len(user_msg_spans):
      conv_idx_to_trace_span[conv_idx] = j

  turn_timestamps = [s.timestamp for s in user_msg_spans]
  trace_end = trace.end_time or (
      max(s.timestamp for s in trace.spans) if trace.spans else None
  )

  from bigquery_agent_analytics.trace import Trace

  segments = []
  for st in sub_trajectories:
    start_turn = st.get("start_turn", 0)
    end_turn = st.get("end_turn", len(conversation) - 1)
    outcome = st.get("outcome", "")

    if outcome == "wrong" and end_turn > start_turn:
      next_st = next(
          (s for s in sub_trajectories if s.get("start_turn", 0) > start_turn),
          None,
      )
      if next_st:
        end_turn = min(end_turn, next_st.get("start_turn", end_turn) - 1)

    start_user_indices = [
        ci for ci in user_turn_indices if start_turn <= ci <= end_turn
    ]
    if not start_user_indices:
      continue

    first_ci = start_user_indices[0]
    last_ci = start_user_indices[-1]
    first_span_idx = conv_idx_to_trace_span.get(first_ci)
    last_span_idx = conv_idx_to_trace_span.get(last_ci)
    if first_span_idx is None:
      continue

    window_start = turn_timestamps[first_span_idx]
    is_last_segment = True
    if last_span_idx is not None and last_span_idx + 1 < len(turn_timestamps):
      window_end = turn_timestamps[last_span_idx + 1]
      is_last_segment = False
    else:
      window_end = trace_end

    if window_end is None:
      continue

    sub_spans = [
        s
        for s in trace.spans
        if s.timestamp >= window_start
        and (
            s.timestamp <= window_end
            if is_last_segment
            else s.timestamp < window_end
        )
    ]
    if not sub_spans:
      continue

    mini_trace = Trace(
        trace_id=trace.trace_id,
        session_id=trace.session_id,
        spans=sub_spans,
    )
    rendered = _render_trace(mini_trace, header=False)
    if rendered:
      segments.append(
          {
              "label": st.get("label", ""),
              "outcome": st.get("outcome", ""),
              "start_turn": start_turn,
              "end_turn": end_turn,
              "trace": rendered,
          }
      )

  return segments


def _trace_attribution(trace):
  identity = getattr(trace, "identity", None)
  scope = getattr(trace, "scope", None)
  return {
      "session_id": trace.session_id,
      "user_id": (
          identity.user_id
          if identity is not None
          else getattr(trace, "user_id", None)
      ),
      "root_agent_name": (
          identity.root_agent_name if identity is not None else None
      ),
      "scope_signature": (scope.scope_signature if scope is not None else None),
  }


class _TraceIndex(dict):
  """Dict-compatible trace index carrying its correlation contexts."""

  def __init__(self, indexed, contexts):
    super().__init__(indexed)
    self.contexts = contexts


def _index_traces(traces):
  """Index traces without collapsing reused session IDs."""
  entries = [{**_trace_attribution(trace), "_trace": trace} for trace in traces]
  contexts = _index_resolved_entries(entries)
  indexed = {key: entry["_trace"] for key, entry in contexts.items()}
  return _TraceIndex(indexed, contexts)


def _trace_contexts(trajectories):
  """Return a reusable collision-safe context index for traces."""
  contexts = getattr(trajectories, "contexts", None)
  if isinstance(contexts, _ResolvedEntriesIndex):
    return contexts
  return _index_resolved_entries(
      [
          {**_trace_attribution(trace), "_trace": trace}
          for trace in trajectories.values()
      ]
  )


def _trace_for_score(trajectories, score):
  """Correlate one score to one trace using reserved attribution details."""
  contexts = _trace_contexts(trajectories)
  context = _resolved_context_for_score(contexts, score)
  return context.get("_trace") if context else None


def _resolved_context_for_trace(resolved_map, trace):
  """Correlate one resolved trace to its report context."""
  attribution = _trace_attribution(trace)
  session_candidates = _resolved_candidates(
      resolved_map, attribution["session_id"]
  )
  if len(session_candidates) == 1 and not any(
      field in session_candidates[0] for field in _REPORT_ATTRIBUTION_FIELDS
  ):
    # Legacy/local report rows predate identity attribution. Preserve their
    # historical unique-session association without broadening collisions.
    return session_candidates[0]

  class _TraceAttribution:
    pass

  record = _TraceAttribution()
  record.session_id = attribution["session_id"]
  record.details = {
      field: attribution[field] for field in _REPORT_ATTRIBUTION_FIELDS
  }
  return _resolved_context_for_score(resolved_map, record)


def _trace_for_context(trajectories, context):
  """Correlate one resolved context to its exact trace."""

  class _ContextAttribution:
    pass

  record = _ContextAttribution()
  record.session_id = context.get("session_id")
  record.details = {
      field: context[field]
      for field in _REPORT_ATTRIBUTION_FIELDS
      if field in context
  }
  return _trace_for_score(trajectories, record)


def _trace_for_session(trajectories, session_id):
  """Return a session trace only when the report population is unique."""
  contexts = _trace_contexts(trajectories)
  context = _resolved_context_for_session(contexts, session_id)
  return context.get("_trace") if context else None


def _trace_fetch_limit(resolved_map, session_ids):
  """Return the resolved trace count needed for selected sessions.

  A legacy session id can resolve to multiple identity/scope candidates.
  ``TraceFilter.limit`` bounds resolved traces, not distinct session ids, so
  sizing it from the latter would silently discard collision candidates.
  """
  wanted = set(session_ids)
  resolved_count = sum(
      1
      for context in resolved_map.values()
      if context.get("session_id") in wanted
  )
  return max(len(wanted), resolved_count)


def _explicit_report_trace_limit(session_ids):
  """Initial size for a completeness-probed explicit-session listing."""
  return max(
      1,
      len(set(session_ids)) * _REPORT_INITIAL_CANDIDATES_PER_SESSION,
  )


def _list_complete_report_traces(client, filter_criteria):
  """List an explicit report population without a silent trace cap.

  ``list_traces`` returns at most ``TraceFilter.limit`` resolved traces. An
  exactly full page therefore proves neither completeness nor truncation.
  Double the limit until the result is shorter than the request. If the
  defensive ceiling is itself saturated, fail explicitly instead of dropping
  identity/scope candidates from a report.

  Returns:
      ``(traces, effective_filter)`` where ``effective_filter.limit`` is the
      completeness-proving request size and can be reused by the evaluator.
  """
  request = filter_criteria.snapshot()
  limit = max(1, request.limit)
  if limit > _REPORT_TRACE_LIMIT_CEILING:
    raise _ReportTracePopulationTruncatedError(
        "The requested report trace population exceeds the completeness"
        f" ceiling ({_REPORT_TRACE_LIMIT_CEILING}); narrow the explicit"
        " session set."
    )

  while True:
    request.limit = limit
    traces = client.list_traces(filter_criteria=request)
    if len(traces) < limit:
      return traces, request
    if limit >= _REPORT_TRACE_LIMIT_CEILING:
      raise _ReportTracePopulationTruncatedError(
          "The report trace population saturated the completeness ceiling"
          f" ({_REPORT_TRACE_LIMIT_CEILING}); no partial report was produced."
      )
    limit = min(limit * 2, _REPORT_TRACE_LIMIT_CEILING)


def _retain_report_traces(fetched, resolved_map):
  """Keep only traces provably attributable to the resolved report rows."""
  by_session = {}
  for trace in fetched:
    by_session.setdefault(trace.session_id, []).append(trace)

  retained = []
  unmatched_count = 0
  legacy_collision_count = 0
  for session_id, traces in by_session.items():
    contexts = _resolved_candidates(resolved_map, session_id)
    has_attributed_context = any(
        any(field in context for field in _REPORT_ATTRIBUTION_FIELDS)
        for context in contexts
    )
    if has_attributed_context:
      for trace in traces:
        if _resolved_context_for_trace(resolved_map, trace):
          retained.append(trace)
        else:
          unmatched_count += 1
      continue

    # A legacy report row has only a session id. It remains compatible when
    # the fetched population is singular, but cannot safely choose among a
    # reused session's identity/scope candidates.
    if len(contexts) == 1 and len(traces) == 1:
      retained.append(traces[0])
    elif contexts:
      legacy_collision_count += len(traces)
    else:
      unmatched_count += len(traces)

  if unmatched_count:
    logger.warning(
        "Omitting %d execution trace(s) outside the resolved report"
        " population.",
        unmatched_count,
    )
  if legacy_collision_count:
    logger.warning(
        "Omitting %d execution trace(s): a legacy report context cannot"
        " distinguish reused-session identity/scope candidates.",
        legacy_collision_count,
    )
  return retained


def _cli_base_trace_filter(app_name=None, custom_labels=None, time_period=None):
  """Build the CLI-derived base TraceFilter for trajectory fetches.

  Preserves the app/label/time bounds the evaluation ran with so the trace
  fetch cannot select rows outside the scored population's scope (e.g. a
  shared events table holding several passes that reuse session ids).
  Returns None when no bound is set (legacy unbounded fetch).
  """
  if not app_name and not custom_labels and time_period in (None, "", "all"):
    return None
  try:
    from bigquery_agent_analytics import TraceFilter as _TraceFilter
  except ImportError:
    return None
  last = None if time_period in (None, "", "all") else time_period
  base = _TraceFilter.from_cli_args(last=last, custom_labels=custom_labels)
  if app_name:
    base.root_agent_name = app_name
  return base


def _enrichment_base_filter(args, custom_labels):
  """Derive the trajectory-fetch bounds from the evaluator's own selector.

  Mirrors run_evaluation(): app + labels always apply; the time bound
  applies ONLY when sessions are selected by window. Explicit session
  selection (``--session`` / ``--session-ids-file``) documents
  ``--time-period`` as ignored and the evaluator builds its selector
  without a time bound — enrichment must drop it too, otherwise an older
  explicitly selected session scores but silently loses its trace.
  """
  explicit_session_mode = bool(
      getattr(args, "session", None) or getattr(args, "session_ids_file", None)
  )
  return _cli_base_trace_filter(
      app_name=getattr(args, "app_name", None),
      custom_labels=custom_labels,
      time_period=(
          None if explicit_session_mode else getattr(args, "time_period", None)
      ),
  )


def _fetch_session_traces(
    session_ids,
    max_sessions=3,
    *,
    max_traces=None,
    resolved_map=None,
    base_filter=None,
):
  """Fetch execution traces from BigQuery for the given session IDs.

  Returns a collision-safe mapping. Unique sessions keep their legacy
  ``session_id`` key; reused sessions are keyed by identity/scope tuples.
  When ``resolved_map`` is supplied, only traces matching that report
  population are retained; legacy session-only rows fail closed on collision.
  When ``base_filter`` is supplied (a ``TraceFilter``), the fetch delegates
  to a snapshot of it and replaces ONLY the requested ``session_ids`` and
  display ``limit`` — the caller's app/label/time bounds are preserved and
  the caller's filter object is never mutated.
  Silently returns empty dict if BQ is not configured or unavailable.
  """
  if not session_ids:
    return {}

  try:
    from bigquery_agent_analytics import Client
  except ImportError:
    logger.debug(
        "Cannot import bigquery_agent_analytics, skipping trajectories"
    )
    return {}

  if not _import_render_timing_tree():
    logger.debug("Cannot import latency_report, skipping trajectories")
    return {}

  if DATASET_ID == "local" or not PROJECT_ID:
    logger.debug("BQ not configured (DATASET_ID=local), skipping trajectories")
    return {}

  try:
    client = Client(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
        location=DATASET_LOCATION,
    )
  except Exception:
    logger.debug("Failed to create BQ client", exc_info=True)
    return {}

  # One batched listing operation (with completeness probes only when a page
  # saturates), instead of one get_session_trace query per session.
  wanted = list(dict.fromkeys(session_ids))[:max_sessions]
  trace_limit = max_traces if max_traces is not None else len(wanted)
  if resolved_map is not None:
    # The listing is necessarily session-wide because one report can contain
    # multiple identity/scope candidates. Reserve U2's full candidate bound so
    # a newer foreign candidate cannot consume the caller's smaller display
    # limit before attribution is checked below.
    trace_limit = max(trace_limit, _explicit_report_trace_limit(wanted))
  try:
    from bigquery_agent_analytics import TraceFilter as _TraceFilter

    if base_filter is not None:
      fetch_filter = base_filter.snapshot()
    else:
      fetch_filter = _TraceFilter()
    fetch_filter.session_ids = wanted
    fetch_filter.limit = trace_limit
    fetched, _ = _list_complete_report_traces(client, fetch_filter)
  except _ReportTracePopulationTruncatedError as exc:
    logger.warning("Skipping execution trajectories: %s", exc)
    return {}
  except Exception:
    logger.debug("Batch trace fetch failed", exc_info=True)
    return {}

  retained = [
      trace
      for trace in fetched
      if trace and trace.spans and trace.session_id in set(wanted)
  ]
  if resolved_map is not None:
    retained = _retain_report_traces(retained, resolved_map)
  return _index_traces(retained)


def _select_trajectory_population(report, resolved_map, n):
  """Pick N score-attributed contexts for trajectory display.

  Priority: unhelpful with corrections > unhelpful > partial > corrections > any.
  A session-only score that maps to multiple identity/scope contexts is
  deliberately omitted: fetching by its bare session ID could render every
  candidate as the explanation for one score.
  """
  by_category = _group_by_category(report)
  unhelpful = {id(sr) for sr in by_category.get("unhelpful", [])}
  partial = {id(sr) for sr in by_category.get("partial", [])}
  candidates = []
  for ordinal, score in enumerate(report.session_results):
    context = _resolved_context_for_score(resolved_map, score)
    if not context:
      continue
    correction = bool(context.get("correction_boundaries"))
    if id(score) in unhelpful:
      priority = 0 if correction else 1
    elif id(score) in partial:
      priority = 2
    elif correction:
      priority = 3
    else:
      priority = 4
    candidates.append((priority, ordinal, context))

  selected = []
  selected_keys = set()
  for _, _, context in sorted(candidates):
    key = _resolved_entry_key(context)
    if key in selected_keys:
      continue
    selected_keys.add(key)
    selected.append(context)
    if len(selected) >= n:
      break

  selected_map = _index_resolved_entries(selected)
  session_ids = list(
      dict.fromkeys(
          context.get("session_id")
          for context in selected
          if context.get("session_id")
      )
  )
  return session_ids, selected_map


def _select_trajectory_sessions(report, resolved_map, n):
  """Backward-compatible session-id view of safe trajectory selection."""
  session_ids, _ = _select_trajectory_population(report, resolved_map, n)
  return session_ids


def _md_write_trajectory_section(w, trajectories, resolved_map):
  """Write the Sample Trajectories section to the markdown report."""
  if not trajectories:
    return

  matched = []
  for trace_obj in trajectories.values():
    ctx = _resolved_context_for_trace(resolved_map, trace_obj)
    if not ctx:
      logger.warning(
          "Omitting an execution trace outside the resolved report population."
      )
      continue
    matched.append((trace_obj, ctx))
  if not matched:
    return

  w("## Sample Execution Trajectories")
  w("")
  w(
      "Full execution traces showing agent routing, tool calls, and LLM "
      "requests. These reveal *why* an answer was wrong — did the agent "
      "skip a tool call, call the wrong tool, or get misrouted?"
  )
  w("")

  for trace_obj, ctx in matched:
    sid = trace_obj.session_id
    # Skip correction sessions — their traces are shown in Correction Analysis
    if ctx.get("correction_boundaries"):
      continue
    question = ctx.get("question", "")
    answered_by = ctx.get("answered_by", "")
    q = " ".join(question.split()) if question else "(none)"

    w(f"### `{sid}`{_report_identity_suffix(ctx)}" f" → {answered_by}")
    w("")
    w(f"**Question:** {q}")
    w("")

    tree = (
        _render_trace(trace_obj)
        if hasattr(trace_obj, "spans")
        else str(trace_obj)
    )
    w("```")
    w(tree)
    w("```")
    w("")

    sub_trajs = ctx.get("sub_trajectories", [])
    conversation = ctx.get("conversation", [])
    if sub_trajs and conversation and hasattr(trace_obj, "spans"):
      segments = _segment_trace_by_turns(trace_obj, conversation, sub_trajs)
      if segments:
        w("**Sub-trajectory segmentation:**")
        w("")
        for seg in segments:
          outcome_icon = "+" if seg["outcome"] == "recovered" else "-"
          w(
              f"#### [{outcome_icon}] {seg['label']} "
              f"(turns {seg['start_turn']}-{seg['end_turn']}) "
              f"→ {seg['outcome']}"
          )
          w("")
          w("```")
          w(seg["trace"])
          w("```")
          w("")


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------


def _md_dimension_scorecard(sr):
  """Build a compact one-line scorecard for the 5 quality dimensions."""
  parts = []
  for mr in sr.metrics:
    # Only the 0-2 quality dimensions belong in the scorecard \u2014 skip primary
    # metrics and non-dimension categoricals (e.g. failure_attribution).
    if mr.metric_name not in _DIMENSION_SCORES:
      continue
    label = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
    icon = _SCORECARD_ICONS.get(mr.category, "\u2753")
    parts.append(f"{label} {icon}")
  return " | ".join(parts)


def _md_write_conversation(w, conversation, show_tags=False, turn_tags=None):
  """Write a <details> conversation block for multi-turn sessions."""
  if not conversation or len(conversation) < 2:
    return
  tag_by_idx = {}
  if show_tags and turn_tags:
    tag_by_idx = {t["turn_index"]: t.get("tag", "") for t in turn_tags}
  w("")
  w("  <details><summary>Conversation</summary>")
  w("")
  for i, turn in enumerate(conversation):
    role = turn.get("role", "user")
    text = turn.get("text", "")
    tag = ""
    if show_tags:
      tag = turn.get("inferred_tag", "") or tag_by_idx.get(i, "")
    if tag and role == "user":
      w(f"  **{role}** `[{tag}]`**:** {text}")
    else:
      w(f"  **{role}:** {text}")
    w("")
  w("  </details>")


def _md_write_session_section(
    w,
    title,
    sessions,
    md_samples,
    resolved_map,
    heading_level=2,
):
  """Write a section of per-session details to the markdown report."""
  h = "#" * heading_level
  sh = "#" * (heading_level + 1)
  shown = sessions if md_samples is None else sessions[:md_samples]
  w(f"{h} {title}")
  if len(shown) < len(sessions):
    w(f"\n*Showing {len(shown)} of {len(sessions)}*")
  w("")
  for sr in shown:
    sid = sr.session_id
    ctx = _resolved_context_for_score(resolved_map, sr)
    question = ctx.get("question", "")
    response = ctx.get("response", "")
    answered_by = ctx.get("answered_by", "")
    a2a_tag = " [A2A]" if ctx.get("is_a2a") else ""

    q = " ".join(question.split()) if question else "(none)"
    r = " ".join(response.split()) if response else "(none)"

    w(
        f"{sh} `{sid}`{_report_identity_suffix(ctx)}"
        f"{a2a_tag} \u2192 {answered_by}"
    )
    w("")
    w(f"- **Question:** {q}")
    r_display = (r[:500] + "\u2026") if len(r) > 500 else r
    w(f"- **Response:** {r_display}")

    for mr in sr.metrics:
      if mr.metric_name not in _PRIMARY_METRICS:
        continue
      label = _category_label(mr.category)
      display = _METRIC_LABELS.get(mr.metric_name, mr.metric_name)
      w(f"- **{display}:** {label}")
      if mr.justification:
        w(f"  - *{mr.justification}*")

    scorecard = _md_dimension_scorecard(sr)
    if scorecard:
      w(f"- **Dimensions:** {scorecard}")

    conversation = ctx.get("conversation", [])
    _md_write_conversation(w, conversation)
    w("")


def _md_find_low_dimension_sessions(report, dimension, low_category):
  """Find sessions that scored the lowest category on a dimension."""
  results = []
  for sr in report.session_results:
    for mr in sr.metrics:
      if mr.metric_name == dimension and mr.category == low_category:
        results.append((sr, mr))
        break
  return results


def _md_write_low_dimension_section(
    w,
    title,
    dimension_label,
    report,
    dimension,
    low_category,
    md_samples,
    resolved_map,
    heading_level=2,
):
  """Write a Low X Sessions section in the markdown report."""
  h = "#" * heading_level
  sh = "#" * (heading_level + 1)
  low_sessions = _md_find_low_dimension_sessions(
      report,
      dimension,
      low_category,
  )
  if not low_sessions:
    return
  shown = low_sessions if md_samples is None else low_sessions[:md_samples]
  w(f"{h} {title}")
  w("")
  if len(shown) < len(low_sessions):
    w(f"*Showing {len(shown)} of {len(low_sessions)}*")
    w("")
  for sr, mr in shown:
    sid = sr.session_id
    ctx = _resolved_context_for_score(resolved_map, sr)
    question = ctx.get("question", "")
    response = ctx.get("response", "")
    answered_by = ctx.get("answered_by", "")

    q = " ".join(question.split()) if question else "(none)"
    r = " ".join(response.split()) if response else "(none)"

    w(f"{sh} `{sid}`{_report_identity_suffix(ctx)} → {answered_by}")
    w("")
    w(f"- **Question:** {q}")
    r_display = (r[:500] + "…") if len(r) > 500 else r
    w(f"- **Response:** {r_display}")
    label = _category_label(mr.category)
    w(f"- **{dimension_label}:** {label}")
    if mr.justification:
      w(f"  - *{mr.justification}*")

    conversation = ctx.get("conversation", [])
    _md_write_conversation(w, conversation)
    w("")


def _md_has_turn_tags(resolved_map):
  """Check if any session in the resolved map has turn tag data."""
  for ctx in resolved_map.values():
    if ctx.get("turn_tags") or ctx.get("correction_boundaries"):
      return True
  return False


_TAG_ICONS = {
    "CORRECTION": "\U0001f534",
    "VERIFY": "\U0001f7e1",
    "SPECIFICS": "\U0001f535",
    "SCOPE": "\U0001f7e0",
    "FOLLOWUP": "✅",
    "END": "⬜",
}


def _diagnose_correction_trace(trace_obj):
  """Analyze a correction session trace and return a diagnosis string.

  Returns (diagnosis_text, failure_type) where failure_type is one of:
  'routing_failure', 'tool_failure', 'other', or None if no trace.
  """
  if not trace_obj or not hasattr(trace_obj, "spans") or not trace_obj.spans:
    return None, None

  tool_names = set()
  for s in trace_obj.spans:
    tn = getattr(s, "tool_name", None)
    if tn:
      tool_names.add(tn)

  routing_tools = {t for t in tool_names if "transfer" in t.lower()}
  domain_tools = tool_names - routing_tools
  agents = {
      s.agent
      for s in trace_obj.spans
      if s.agent and s.event_type == "LLM_RESPONSE"
  }

  if not tool_names and len(agents) <= 1:
    return (
        "Agent never routed to a specialist or called any tool — "
        "answered from general LLM knowledge only."
    ), "routing_failure"

  if routing_tools and not domain_tools and len(agents) > 1:
    routed_to = ", ".join(sorted(agents - {min(agents)}))
    return (
        f"Agent routed to {routed_to} but no domain tool was called."
    ), "tool_failure"

  return None, None


def _md_write_correction_analysis(
    w, resolved_map, md_samples, trajectories=None, heading_level=2
):
  """Write the Correction Analysis section."""
  sessions_with_tags = []
  sessions_with_corrections = []
  tag_counts = {}

  for ctx in resolved_map.values():
    sid = ctx.get("session_id")
    tags = ctx.get("turn_tags", [])
    boundaries = ctx.get("correction_boundaries", [])
    if tags:
      sessions_with_tags.append((sid, ctx))
      for t in tags:
        tag = t.get("tag", "")
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
    if boundaries:
      sessions_with_corrections.append((sid, ctx))

  if not sessions_with_tags:
    return

  h = "#" * heading_level
  h1 = "#" * (heading_level + 1)
  h2 = "#" * (heading_level + 2)
  w(f"{h} Correction Analysis")
  w("")
  w(
      "Turn-level classification of user behavior across multi-turn "
      "conversations. Each user turn is tagged to identify corrections, "
      "verifications, and other interaction patterns."
  )
  w("")

  # --- Tag Distribution ---
  w(f"{h1} Turn Tag Distribution")
  w("")
  w("| Tag | Count | Icon | Meaning |")
  w("|-----|------:|------|---------|")
  tag_descriptions = {
      "CORRECTION": "User corrects a factual error by the agent",
      "VERIFY": "User doubts the answer without providing the correct fact",
      "SPECIFICS": "User asks for concrete details the agent omitted",
      "SCOPE": "User flags the agent answered something outside its scope",
      "FOLLOWUP": "Normal follow-up question; previous answer was acceptable",
      "END": "User is satisfied, conversation closing",
  }
  for tag in ("CORRECTION", "VERIFY", "SPECIFICS", "SCOPE", "FOLLOWUP", "END"):
    count = tag_counts.get(tag, 0)
    icon = _TAG_ICONS.get(tag, "")
    desc = tag_descriptions.get(tag, "")
    w(f"| {tag} | {count} | {icon} | {desc} |")
  w("")

  total_tagged = len(sessions_with_tags)
  total_corrections = len(sessions_with_corrections)
  w(f"- **Sessions with turn tags:** {total_tagged}")
  w(f"- **Sessions with corrections:** {total_corrections}")
  w("")

  # --- Correction Boundaries ---
  def _correction_segment_heading(outcome, label):
    """Map a sub-trajectory outcome to its report heading, suffix, and icon."""
    if outcome == "wrong":
      return "Before correction", "agent got it wrong", "❌"
    if outcome == "recovered":
      return "After correction", "agent recovered", "✅"
    if outcome == "parroted":
      return (
          "After correction",
          "agent parroted user's fact without verification",
          "🔁",
      )
    if outcome == "not_recovered":
      return "After correction", "agent did not recover", "❌"
    return label, outcome, "➖"

  if sessions_with_corrections:
    w(f"{h1} Corrections")
    w("")
    w(
        "Conversations where the user corrected the agent. Shows what "
        "the agent got wrong, what the user corrected, and whether the "
        "agent recovered."
    )
    w("")

    shown = (
        sessions_with_corrections
        if md_samples is None
        else sessions_with_corrections[:md_samples]
    )
    if len(shown) < len(sessions_with_corrections):
      w(f"*Showing {len(shown)} of {len(sessions_with_corrections)}*")
      w("")

    if not trajectories:
      trajectories = {}

    routing_failures = []

    for _key, ctx in shown:
      sid = ctx.get("session_id")
      question = ctx.get("question", "")
      answered_by = ctx.get("answered_by", "")
      q = " ".join(question.split()) if question else "(none)"
      w(f"{h2} `{sid}`{_report_identity_suffix(ctx)} → {answered_by}")
      w("")
      w(f"- **Question:** {q}")

      for b in ctx.get("correction_boundaries", []):
        turn_idx = b.get("turn_index", "?")
        wrong = b.get("wrong_claim", "")
        correct = b.get("correct_fact", "")
        recovered = b.get("agent_recovered", False)
        recovered_icon = "✅ Yes" if recovered else "❌ No"
        w(f"- **Correction at turn {turn_idx}:**")
        w(f'  - Agent claimed: *"{wrong[:200]}"*')
        w(f'  - User corrected: *"{correct[:200]}"*')
        w(f"  - Agent recovered: {recovered_icon}")

      trace_obj = _trace_for_context(trajectories, ctx)
      diagnosis, failure_type = _diagnose_correction_trace(trace_obj)
      if diagnosis:
        w(f"- **Diagnosis:** {diagnosis}")
        if failure_type == "routing_failure":
          routing_failures.append((sid, answered_by, q, ctx))

      # Render sub-trajectories with inline execution traces
      sub_trajs = ctx.get("sub_trajectories", [])
      trace_obj = _trace_for_context(trajectories, ctx)
      conversation = ctx.get("conversation", [])

      if sub_trajs and trace_obj and hasattr(trace_obj, "spans"):
        segments = _segment_trace_by_turns(
            trace_obj,
            conversation,
            sub_trajs,
        )
        if segments:
          w("")
          # Seeded sessions (rows tagged custom_tags.seeded) carry event
          # streams RECONSTRUCTED from the recorded conversation, with tool
          # placement certified by the scored sub-trajectory outcomes --
          # label them so span trees are never mistaken for live telemetry.
          seeded_from = next(
              (
                  (s.attributes or {}).get("custom_tags", {}).get("seeded")
                  for s in trace_obj.spans
                  if (s.attributes or {}).get("custom_tags", {}).get("seeded")
              ),
              None,
          )
          if seeded_from:
            w(
                f"*Spans reconstructed from the recorded conversation"
                f" (seeded from `{seeded_from}`).*"
            )
            w("")
          for seg in segments:
            heading, outcome_suffix, outcome_icon = _correction_segment_heading(
                seg.get("outcome", "?"), seg.get("label", "Segment")
            )
            w(
                f"**{heading}** (turns {seg['start_turn']}–"
                f"{seg['end_turn']}) — {outcome_suffix} {outcome_icon}"
            )
            w("")
            w("```")
            w(seg["trace"])
            w("```")
            w("")
      elif sub_trajs:
        # No execution spans (conversations-path sessions have no trace tree),
        # so render the same Before/After blocks with the segment's dialogue
        # as the evidence instead of a span tree.
        w("")
        for st in sub_trajs:
          heading, outcome_suffix, outcome_icon = _correction_segment_heading(
              st.get("outcome", "?"), st.get("label", "Segment")
          )
          start = st.get("start_turn")
          end = st.get("end_turn")
          span = (
              f" (turns {start}–{end})"
              if start is not None and end is not None
              else ""
          )
          w(f"**{heading}**{span} — {outcome_suffix} {outcome_icon}")
          w("")
          if conversation and start is not None and end is not None:
            w("```")
            for i, turn in enumerate(conversation):
              if start <= i <= end:
                role = turn.get("role") or "?"
                text = " ".join((turn.get("text") or "").split())
                w(f"{role}: {text[:400]}")
            w("```")
            w("")

      _md_write_conversation(
          w,
          conversation,
          show_tags=True,
          turn_tags=ctx.get("turn_tags", []),
      )
      w("")

    if routing_failures:
      w(f"{h1} Routing Failures")
      w("")
      w(
          "Sessions where the supervisor agent answered from general LLM "
          "knowledge without routing to a specialist agent or calling any "
          "tool. These are prime candidates for improving the supervisor's "
          "routing prompt."
      )
      w("")
      w(
          f"**{len(routing_failures)}** of "
          f"{len(sessions_with_corrections)} correction sessions "
          f"had no tool or agent routing:"
      )
      w("")
      for sid, agent, question, ctx in routing_failures:
        w(f"- `{sid}`{_report_identity_suffix(ctx)} → {agent}: {question}")
      w("")

  # --- Tagged Conversations (no corrections) ---
  tagged_no_correction = [
      (sid, ctx)
      for sid, ctx in sessions_with_tags
      if not ctx.get("correction_boundaries")
  ]
  has_interesting = any(
      any(
          t.get("tag") in ("VERIFY", "SPECIFICS", "SCOPE")
          for t in ctx.get("turn_tags", [])
      )
      for _, ctx in tagged_no_correction
  )
  if has_interesting:
    w(f"{h1} Other Flagged Interactions")
    w("")
    w(
        "Sessions without corrections but with verification requests, "
        "specificity asks, or scope flags."
    )
    w("")

    interesting = [
        (sid, ctx)
        for sid, ctx in tagged_no_correction
        if any(
            t.get("tag") in ("VERIFY", "SPECIFICS", "SCOPE")
            for t in ctx.get("turn_tags", [])
        )
    ]
    shown = interesting if md_samples is None else interesting[:md_samples]
    if len(shown) < len(interesting):
      w(f"*Showing {len(shown)} of {len(interesting)}*")
      w("")

    for sid, ctx in shown:
      question = ctx.get("question", "")
      answered_by = ctx.get("answered_by", "")
      q = " ".join(question.split()) if question else "(none)"
      tags = ctx.get("turn_tags", [])
      flag_tags = [
          t for t in tags if t.get("tag") in ("VERIFY", "SPECIFICS", "SCOPE")
      ]

      w(f"{h2} `{sid}`{_report_identity_suffix(ctx)} → {answered_by}")
      w("")
      w(f"- **Question:** {q}")
      for ft in flag_tags:
        tag = ft.get("tag", "")
        icon = _TAG_ICONS.get(tag, "")
        evidence = ft.get("evidence", "")
        w(
            f"- **Turn {ft.get('turn_index', '?')}:** {icon} `{tag}` — {evidence}"
        )

      conversation = ctx.get("conversation", [])
      _md_write_conversation(
          w,
          conversation,
          show_tags=True,
          turn_tags=ctx.get("turn_tags", []),
      )
      w("")


def _write_md_report(
    report,
    resolved_map,
    args,
    report_dir=None,
    trajectories=None,
    out_path=None,
):
  lines = []
  w = lines.append

  if trajectories is None:
    trajectories = {}

  by_category = _group_by_category(report)
  a2a_count = sum(bool(ctx.get("is_a2a")) for ctx in resolved_map.values())

  fp_count = len(by_category.get("unhelpful", []))
  partial_count = len(by_category.get("partial", []))
  meaningful_count = len(by_category.get("meaningful", []))
  declined_count = len(by_category.get("declined", []))
  unknown_count = len(by_category.get("unknown", []))
  total = report.total_sessions
  fp_rate = (fp_count / total * 100) if total > 0 else 0.0
  dim_avgs = _compute_dimension_averages(report)
  mt_stats = _compute_multiturn_stats(resolved_map)
  agent_stats = _build_agent_stats(report, resolved_map)

  has_dims = _has_dimension_data(dim_avgs)
  low_dims = {}
  for dim, low_cat in _DIMENSION_LOW_CATEGORIES.items():
    sessions = _md_find_low_dimension_sessions(report, dim, low_cat)
    if sessions:
      low_dims[dim] = sessions

  # --- TOC ---
  w("# Quality Evaluation Report")
  w("<!-- TOC -->")
  toc = []
  toc.append("* [Quality Evaluation Report](#quality-evaluation-report)")
  toc.append("  * [Summary](#summary)")
  if has_dims:
    toc.append("  * [Quality Dimensions](#quality-dimensions)")
  toc.append("  * [Category Distributions](#category-distributions)")
  for metric_name in report.category_distributions:
    if metric_name in _PRIMARY_METRICS:
      toc.append(f"    * [{metric_name}](#{metric_name})")
  if agent_stats:
    toc.append("  * [Per-Agent Quality](#per-agent-quality)")
  if mt_stats:
    toc.append("  * [Multi-Turn Efficiency](#multi-turn-efficiency)")
  has_tags = _md_has_turn_tags(resolved_map)
  has_sample_sessions = (
      by_category.get("unhelpful")
      or by_category.get("declined")
      or low_dims
      or by_category.get("partial")
      or has_tags
  )
  if has_sample_sessions:
    toc.append("  * [Sample Sessions](#sample-sessions)")
    if by_category.get("unhelpful"):
      toc.append("    * [Unhelpful Sessions](#unhelpful-sessions)")
    if by_category.get("declined"):
      toc.append("    * [Declined Sessions](#declined-sessions)")
    for dim in low_dims:
      label = _METRIC_LABELS.get(dim, dim)
      title = f"Low {label} Sessions"
      anchor = title.lower().replace(" ", "-")
      toc.append(f"    * [{title}](#{anchor})")
    if by_category.get("partial"):
      toc.append("    * [Partial Sessions](#partial-sessions)")
    if has_tags:
      toc.append("    * [Correction Analysis](#correction-analysis)")
      toc.append("      * [Turn Tag Distribution](#turn-tag-distribution)")
      correction_sessions = [
          ctx
          for ctx in resolved_map.values()
          if ctx.get("correction_boundaries")
      ]
      if correction_sessions:
        toc.append("      * [Corrections](#corrections)")
        has_routing_failures = any(
            _diagnose_correction_trace(
                _trace_for_context(trajectories, context)
            )[1]
            == "routing_failure"
            for context in correction_sessions
        )
        if has_routing_failures:
          toc.append("      * [Routing Failures](#routing-failures)")
  if trajectories:
    toc.append("  * [Sample Trajectories]" "(#sample-execution-trajectories)")
  toc.append("  * [Execution Details](#execution-details)")
  for line in toc:
    w(line)
  w("<!-- TOC -->")
  w("")
  w("")

  # --- Summary ---
  w("## Summary")
  w("")

  model = args.model or EVAL_MODEL_ID
  # Reproduce the invocation, but never leak workstation paths into a
  # publishable artifact: absolute path arguments are reduced to basenames.
  cmd_parts = ["./scripts/quality_report.sh"]
  for arg in sys.argv[1:]:
    if arg.startswith(os.sep) or arg.startswith("~"):
      arg = os.path.basename(arg)
    cmd_parts.append(arg)
  if "--report" not in cmd_parts:
    cmd_parts.insert(1, "--report")
  w(f"Markdown report generated by `{' '.join(cmd_parts)}`.")
  w("")

  # Render metadata as a bullet list rather than trailing-double-space GFM
  # hard breaks — the latter trips `git diff --check` (PR #156/#174 L1).
  timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  w(f"- **Generated:** {timestamp}")
  w(f"- **Project:** {PROJECT_ID}")
  if DATASET_ID != "local":
    w(f"- **Dataset:** {DATASET_ID}.{TABLE_ID}")
    w(f"- **Location:** {DATASET_LOCATION}")
  w(f"- **Eval model:** {model}")
  w(f"- **Sessions:** {total}")
  w("")
  w("| Metric | Value |")
  w("|--------|-------|")
  w(f"| Total sessions | {total} |")
  w(f"| Meaningful | {meaningful_count} |")
  w(f"| Declined (out-of-scope) | {declined_count} |")
  w(f"| Partial | {partial_count} |")
  w(f"| Unhelpful | {fp_count} |")
  w(f"| Unhelpful rate | {fp_rate:.1f}% |")
  counts, gap_scores = _failure_breakdown_from_report(report)
  unaddressable = counts["knowledge_gap"] + counts["tool_gap"]
  addressable = total - unaddressable
  good = meaningful_count + declined_count
  addr_rate = (good / addressable * 100) if addressable else 0.0
  if _has_failure_attribution_data(report) and any(counts.values()):
    w(f"| &nbsp;&nbsp;↳ Skill gaps (evolution fixes) | {counts['skill_gap']} |")
    w(
        f"| &nbsp;&nbsp;↳ Knowledge gaps (add a fact) "
        f"| {counts['knowledge_gap']} |"
    )
    w(f"| &nbsp;&nbsp;↳ Tool gaps (build a tool) | {counts['tool_gap']} |")
    w(
        f"| **Addressable meaningful rate** "
        f"(excl. knowledge + tool gaps) | **{addr_rate:.1f}%** |"
    )
  if unknown_count:
    parse_error_metrics = report.details.get("parse_errors", "?")
    w(
        f"| Parse errors | {unknown_count} session(s) "
        f"({parse_error_metrics} metric evals) |"
    )
  if a2a_count:
    w(f"| A2A sessions | {a2a_count} |")
  w("")

  # --- Failure breakdown: which gaps evolution can vs cannot fix ---
  def _gap_questions(scores):
    out = []
    for sr in scores:
      q = _resolved_context_for_score(resolved_map, sr).get("question", "")
      if q:
        out.append(" ".join(q.split()))
    return out

  for gap_key, title, blurb in [
      (
          "knowledge_gap",
          "Knowledge Gaps (add a fact to existing data)",
          "In-scope questions the agent looked up correctly but its data source is"
          " silent on. Evolution cannot invent these facts — a human adds them:",
      ),
      (
          "tool_gap",
          "Tool Gaps (build a new tool / data source)",
          "Requests no tool can serve — a topic with no data source, or personal"
          " data / actions the agent has no capability for. An engineer must add a"
          " tool:",
      ),
  ]:
    questions = _gap_questions(gap_scores[gap_key])
    if not questions:
      continue
    w(f"### {title}")
    w("")
    w(blurb)
    w("")
    for q in questions[:15]:
      w(f"- {q[:160]}")
    if len(questions) > 15:
      w(f"- …and {len(questions) - 15} more")
    w("")

  # --- Quality Dimensions (0-2 scale) ---
  _samples_dict = _parse_samples(args.samples)

  if has_dims:
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

  # --- Category Distributions (primary metrics only) ---
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

  # --- Multi-Turn Efficiency ---
  if mt_stats:
    w("## Multi-Turn Efficiency")
    w("")
    w("| Metric | Value |")
    w("|--------|-------|")
    w(f"| Avg user turns | {mt_stats['avg_user_turns']} |")
    w(f"| Avg tool calls | {mt_stats['avg_tool_calls']} |")
    if mt_stats["multi_turn_sessions"] > 0:
      w(f"| Multi-turn sessions | {mt_stats['multi_turn_sessions']} |")
    w("")

  # --- Sample Sessions ---
  has_sample_sessions = (
      by_category.get("unhelpful")
      or by_category.get("declined")
      or low_dims
      or by_category.get("partial")
      or has_tags
  )
  if has_sample_sessions:
    w("## Sample Sessions")
    w("")

  unhelpful_sessions = by_category.get("unhelpful", [])
  if unhelpful_sessions:
    _md_write_session_section(
        w,
        "Unhelpful Sessions",
        unhelpful_sessions,
        _get_sample_limit(_samples_dict, "unhelpful"),
        resolved_map,
        heading_level=3,
    )

  declined_sessions = by_category.get("declined", [])
  if declined_sessions:
    _md_write_session_section(
        w,
        "Declined Sessions",
        declined_sessions,
        _get_sample_limit(_samples_dict, "declined"),
        resolved_map,
        heading_level=3,
    )

  for dim, low_cat in _DIMENSION_LOW_CATEGORIES.items():
    if dim not in low_dims:
      continue
    label = _METRIC_LABELS.get(dim, dim)
    _md_write_low_dimension_section(
        w,
        f"Low {label} Sessions",
        label,
        report,
        dim,
        low_cat,
        _get_sample_limit(_samples_dict, "low"),
        resolved_map,
        heading_level=3,
    )

  partial_sessions = by_category.get("partial", [])
  if partial_sessions:
    _md_write_session_section(
        w,
        "Partial Sessions",
        partial_sessions,
        _get_sample_limit(_samples_dict, "partial"),
        resolved_map,
        heading_level=3,
    )

  # --- Correction Analysis (turn tagging) ---
  if has_tags:
    _md_write_correction_analysis(
        w,
        resolved_map,
        _get_sample_limit(_samples_dict, "corrections"),
        trajectories=trajectories,
        heading_level=3,
    )

  # --- Sample Execution Trajectories ---
  if trajectories:
    _md_write_trajectory_section(w, trajectories, resolved_map)

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

  # Write file. An explicit out_path (e.g. <report>.json -> <report>.md) wins;
  # otherwise fall back to the timestamped name under report_dir.
  if out_path:
    report_path = out_path
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
  else:
    if report_dir is None:
      report_dir = os.path.join(_script_dir, "reports")
    os.makedirs(report_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(report_dir, f"quality_report_{ts}.md")
  # Strip trailing whitespace per physical line (conversation text often
  # carries it), so committed scorecards pass `git diff --check`. The writer
  # never relies on trailing-double-space GFM line breaks.
  text = "\n".join(lines)
  text = "\n".join(line.rstrip() for line in text.split("\n"))
  with open(report_path, "w") as f:
    f.write(text + "\n")

  return os.path.abspath(report_path)


def _render_md_from_json(json_path, args):
  """Re-render the markdown report from an existing scored JSON output.

  Pure formatting -- no model calls, no re-scoring: reconstructs the report
  objects from the JSON that ``--output-json`` wrote and feeds them to the
  same markdown writer. Writes ``<name>.md`` next to ``<name>.json`` and
  returns the md path.
  """
  from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport
  from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
  from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult

  with open(json_path) as f:
    data = json.load(f)
  sessions = data.get("sessions", [])

  session_results = []
  distributions = {}
  for s in sessions:
    metric_results = []
    for name, m in (s.get("metrics") or {}).items():
      m = m or {}
      metric_results.append(
          CategoricalMetricResult(
              metric_name=name,
              category=m.get("category"),
              justification=m.get("justification"),
          )
      )
      if m.get("category") is not None:
        by_cat = distributions.setdefault(name, {})
        by_cat[m["category"]] = by_cat.get(m["category"], 0) + 1
    session_results.append(
        CategoricalSessionResult(
            session_id=s.get("session_id", ""),
            metrics=metric_results,
            details={
                field: s.get(field)
                for field in _REPORT_ATTRIBUTION_FIELDS
                if field in s
            },
        )
    )

  # Preserve the ORIGINAL scoring run's provenance (project, dataset, eval
  # model, elapsed time) so the rendered report documents the run it came
  # from, plus a record of what this render added.
  details = dict(data.get("details") or {})
  details["rendered_from"] = os.path.basename(json_path)
  report = CategoricalEvaluationReport(
      dataset=f"rendered from {os.path.basename(json_path)}",
      total_sessions=len(session_results),
      category_distributions=distributions,
      details=details,
      session_results=session_results,
  )

  # Older JSONs fold the turn-tagging artifacts into the conversation
  # (inferred_tag per turn) and sub_trajectories; the Correction Analysis
  # section keys on turn_tags / correction_boundaries, so reconstruct them.
  for s in sessions:
    conversation = s.get("conversation") or []
    if "turn_tags" not in s:
      tags = []
      for i, turn in enumerate(conversation):
        tag = turn.get("inferred_tag") or turn.get("tag")
        if tag:
          tags.append({"turn_index": i, "tag": tag})
      if tags:
        s["turn_tags"] = tags
    if "correction_boundaries" not in s:
      boundaries = []
      for st in s.get("sub_trajectories") or []:
        outcome = st.get("outcome")
        if outcome not in ("recovered", "parroted", "not_recovered"):
          continue
        start = st.get("start_turn")
        boundary = {
            "turn_index": start,
            "agent_recovered": outcome == "recovered",
        }
        if start is not None and 0 <= start < len(conversation):
          boundary["correct_fact"] = (conversation[start].get("text") or "")[
              :200
          ]
          if start >= 1:
            boundary["wrong_claim"] = (
                conversation[start - 1].get("text") or ""
            )[:200]
        boundaries.append(boundary)
      if boundaries:
        s["correction_boundaries"] = boundaries

  resolved_map = _index_resolved_entries(sessions)

  # Pull execution traces from BigQuery for these session ids ONLY when the
  # env is explicitly configured (offline renders stay pure: no queries).
  # The spans live in the events table; without them the report falls back
  # to dialogue-only correction blocks.
  trajectories = {}
  if PROJECT_ID and DATASET_ID and DATASET_ID != "local":
    session_ids = [s.get("session_id", "") for s in sessions]
    trajectories = _fetch_session_traces(
        session_ids,
        max_sessions=len(session_ids),
        max_traces=_trace_fetch_limit(resolved_map, session_ids),
        resolved_map=resolved_map,
    )
    if trajectories:
      logger.info(
          "Fetched %d execution trace(s) from BigQuery (%s.%s.%s).",
          len(trajectories),
          PROJECT_ID,
          DATASET_ID,
          TABLE_ID,
      )
      # Mutate report.details (pydantic copies the dict at construction).
      report.details["traces_source"] = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"

  out_path = os.path.abspath(json_path)
  if out_path.endswith(".json"):
    out_path = out_path[: -len(".json")] + ".md"
  else:
    out_path += ".md"
  return _write_md_report(
      report,
      resolved_map,
      args,
      trajectories=trajectories,
      out_path=out_path,
  )


# ---------------------------------------------------------------------------
# Failure attribution — skill gap vs knowledge gap vs tool gap
# ---------------------------------------------------------------------------
#
# Every failure (response_usefulness == "unhelpful") has one root cause, and
# each points to a DIFFERENT fixer:
#   - skill_gap     -> the agent had the tool + data but misbehaved (routing,
#                      tool-use, parroting, hallucination). Fixed by SKILL
#                      EVOLUTION (automatic).
#   - knowledge_gap -> a tool that covers the topic was used correctly, but the
#                      specific fact is missing from its data. Fixed by a HUMAN
#                      adding a fact to the existing data source.
#   - tool_gap      -> no tool/capability can serve the request (a topic with no
#                      data source, or personal-data / action needs). Fixed by
#                      an ENGINEER building a new tool.
#
# The LLM judge's ``failure_attribution`` metric assigns the cause when present
# (it sees the tool inventory). Without it we fall back to a 2-way deterministic
# split (knowledge vs skill). Only skill gaps are addressable by evolution, so
# ``addressable_meaningful_rate`` excludes both knowledge and tool gaps.
_KNOWLEDGE_GAP_TOOL = {"proper"}
_KNOWLEDGE_GAP_CORRECTNESS = {"correct", "mostly_correct"}
_FAILURE_CLASSES = ("skill_gap", "knowledge_gap", "tool_gap")


def _failure_class(usefulness, tool, correctness, attribution=None):
  """Classify a single session's failure (or None if it is not a failure).

  Prefers the LLM judge's ``failure_attribution`` (3-way: skill/knowledge/tool)
  when available; otherwise falls back to a deterministic 2-way split — an
  unhelpful session where the agent used its tools and did not fabricate is a
  ``knowledge_gap``, anything else is a ``skill_gap``.
  """
  # Meaningful / correctly-declined responses are not failures, regardless of
  # any stray attribution — never count them as a gap (keeps addressable rate
  # <= 100%).
  if usefulness in ("meaningful", "declined"):
    return None
  # For an actual failure (unhelpful / partial), trust the judge's attribution
  # when it named a concrete gap; otherwise fall back to the deterministic
  # 2-way split (which only fires for unhelpful).
  if attribution in _FAILURE_CLASSES:
    return attribution
  if usefulness != "unhelpful":
    return None
  if tool in _KNOWLEDGE_GAP_TOOL and correctness in _KNOWLEDGE_GAP_CORRECTNESS:
    return "knowledge_gap"
  return "skill_gap"


def _has_failure_attribution_data(report):
  """True when failures can actually be attributed to a cause.

  The failure-cause taxonomy (skill/knowledge/tool gap) needs either the judge's
  ``failure_attribution`` metric, or both ``tool_usage`` and ``correctness`` (the
  deterministic 2-way fallback). When none were scored — e.g. ``--dimensions
  primary`` — ``_failure_class`` would default every failure to ``skill_gap``,
  which reads as "no knowledge/tool gaps, just evolution work" when it is really
  "those metrics weren't scored." So all output paths gate the failure breakdown
  on this predicate (analogous to ``_has_dimension_data``).
  """
  for sr in report.session_results:
    cats = {mr.metric_name for mr in sr.metrics}
    if "failure_attribution" in cats or (
        "tool_usage" in cats and "correctness" in cats
    ):
      return True
  return False


def _failure_breakdown_from_report(report):
  """Return (counts_by_class, attributed score rows by class)."""
  counts = {c: 0 for c in _FAILURE_CLASSES}
  gap_scores = {c: [] for c in _FAILURE_CLASSES}
  for sr in report.session_results:
    cats = {mr.metric_name: mr.category for mr in sr.metrics}
    fc = _failure_class(
        cats.get("response_usefulness"),
        cats.get("tool_usage"),
        cats.get("correctness"),
        cats.get("failure_attribution"),
    )
    if fc in counts:
      counts[fc] += 1
      gap_scores[fc].append(sr)
  return counts, gap_scores


def _classify_failures(report):
  """Tag each ``unhelpful`` session with a ``failure_class`` and add the
  skill/knowledge/tool-gap summary metrics in place."""
  sessions = report.get("sessions", [])
  summary = report.setdefault("summary", {})

  counts = {c: 0 for c in _FAILURE_CLASSES}
  gap_questions = {c: [] for c in _FAILURE_CLASSES}
  for s in sessions:
    metrics = s.get("metrics", {})
    fc = _failure_class(
        metrics.get("response_usefulness", {}).get("category"),
        metrics.get("tool_usage", {}).get("category"),
        metrics.get("correctness", {}).get("category"),
        metrics.get("failure_attribution", {}).get("category"),
    )
    if fc in counts:
      s["failure_class"] = fc
      counts[fc] += 1
      q = s.get("question", "")
      if q:
        gap_questions[fc].append(q)

  total = summary.get("total_sessions") or len(sessions)
  good = summary.get("meaningful", 0) + summary.get("declined", 0)
  # Only skill gaps are addressable by evolution; knowledge + tool gaps need a
  # human (add a fact) or an engineer (build a tool).
  unaddressable = counts["knowledge_gap"] + counts["tool_gap"]
  addressable = total - unaddressable
  summary["skill_gap"] = counts["skill_gap"]
  summary["knowledge_gap"] = counts["knowledge_gap"]
  summary["tool_gap"] = counts["tool_gap"]
  summary["knowledge_gap_rate"] = (
      round(counts["knowledge_gap"] / total * 100, 1) if total else 0
  )
  summary["tool_gap_rate"] = (
      round(counts["tool_gap"] / total * 100, 1) if total else 0
  )
  # Quality on questions the agent *can* answer (knowledge + tool gaps excluded)
  # — the ceiling skill evolution is actually working toward.
  summary["addressable_meaningful_rate"] = (
      round(good / addressable * 100, 1) if addressable else 0
  )
  summary["knowledge_gap_questions"] = gap_questions["knowledge_gap"][:50]
  summary["tool_gap_questions"] = gap_questions["tool_gap"][:50]


# ---------------------------------------------------------------------------
# JSON report output
# ---------------------------------------------------------------------------


def _build_json_output(report, resolved_map, trajectories=None):
  """Build a structured dict for JSON output of evaluation results."""
  by_category = _group_by_category(report)
  agent_stats = _build_agent_stats(report, resolved_map)

  sessions = []
  for sr in report.session_results:
    ctx = _resolved_context_for_score(resolved_map, sr)
    identity = getattr(sr, "identity", None)
    scope = getattr(sr, "scope", None)
    context_source = getattr(sr, "context_source", None)
    if hasattr(context_source, "value"):
      context_source = context_source.value
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
        "user_id": (
            identity.user_id
            if identity is not None
            else (getattr(sr, "details", None) or {}).get(
                "user_id", ctx.get("user_id")
            )
        ),
        "root_agent_name": (
            identity.root_agent_name
            if identity is not None
            else (getattr(sr, "details", None) or {}).get(
                "root_agent_name", ctx.get("root_agent_name")
            )
        ),
        "experiment_id": (
            scope.experiment_id
            if scope is not None
            else ctx.get("experiment_id")
        ),
        "custom_labels": (
            scope.labels_dict if scope is not None else ctx.get("custom_labels")
        ),
        "scope_signature": (
            scope.scope_signature
            if scope is not None
            else (getattr(sr, "details", None) or {}).get(
                "scope_signature", ctx.get("scope_signature")
            )
        ),
        "scope_coverage": ctx.get("scope_coverage"),
        "context_applied": getattr(sr, "context_applied", False),
        "context_source": context_source,
        "execution_mode": getattr(sr, "execution_mode", None),
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
    # Only emit structured tool detail when it was actually captured (the
    # conversations path records it; the BigQuery/trace path does not). Keeping
    # key-absence truthful lets consumers tell "not captured" from "no calls"
    # instead of rendering a false "(none)" for a BQ session that used tools.
    if "tool_calls_detail" in ctx:
      session_dict["tool_calls_detail"] = ctx["tool_calls_detail"]
    conversation = ctx.get("conversation", [])
    if conversation:
      turn_tags = ctx.get("turn_tags", [])
      if turn_tags:
        tag_by_idx = {t["turn_index"]: t for t in turn_tags}
        annotated = []
        for i, turn in enumerate(conversation):
          t = dict(turn)
          tag_info = tag_by_idx.get(i)
          if tag_info:
            t["inferred_tag"] = tag_info.get("tag", "")
            t["tag_evidence"] = tag_info.get("evidence", "")
          annotated.append(t)
        session_dict["conversation"] = annotated
      else:
        session_dict["conversation"] = conversation
    correction_boundaries = ctx.get("correction_boundaries", [])
    if correction_boundaries:
      session_dict["correction_boundaries"] = correction_boundaries
    sub_trajectories = ctx.get("sub_trajectories", [])
    if sub_trajectories:
      session_dict["sub_trajectories"] = sub_trajectories
    # Persist the turn-tagging artifacts too, so a scored JSON is
    # self-sufficient for re-rendering the markdown report (--render-json).
    if ctx.get("turn_tags"):
      session_dict["turn_tags"] = ctx["turn_tags"]
    if ctx.get("correction_boundaries"):
      session_dict["correction_boundaries"] = ctx["correction_boundaries"]
    trace_obj = _trace_for_score(trajectories, sr) if trajectories else None
    if trace_obj is not None:
      if hasattr(trace_obj, "spans"):
        session_dict["execution_trace"] = _render_trace(trace_obj)
        if sub_trajectories and conversation:
          segments = _segment_trace_by_turns(
              trace_obj,
              conversation,
              sub_trajectories,
          )
          if segments:
            session_dict["execution_sub_trajectories"] = segments
      else:
        session_dict["execution_trace"] = str(trace_obj)
    sessions.append(session_dict)

  fp_count = len(by_category.get("unhelpful", []))
  partial_count = len(by_category.get("partial", []))
  meaningful_count = len(by_category.get("meaningful", []))
  declined_count = len(by_category.get("declined", []))
  total = report.total_sessions

  dim_avgs = _compute_dimension_averages(report)
  mt_stats = _compute_multiturn_stats(resolved_map)

  output = {
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
          # Empty when dimensions were not scored (e.g. --dimensions primary),
          # so consumers don't read unscored dimensions as 0.0 / failing.
          "dimension_averages": (
              dim_avgs if _has_dimension_data(dim_avgs) else {}
          ),
          **mt_stats,
      },
      "category_distributions": {
          k: dict(v) for k, v in report.category_distributions.items()
      },
      "per_agent": {agent: dict(stats) for agent, stats in agent_stats.items()},
      "sessions": sessions,
      "details": {k: str(v) for k, v in report.details.items()},
  }
  # Only attribute failures when the metrics that drive attribution were scored;
  # otherwise skill_gap/knowledge_gap/tool_gap would all default to a misleading
  # N/0/0. When ungated, those keys are simply absent from the summary.
  if _has_failure_attribution_data(report):
    _classify_failures(output)
  return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
  parser = argparse.ArgumentParser(
      description="Quality evaluation report for agent traces in BigQuery",
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog="""
Examples:
  %(prog)s                           Evaluate most recent 100 sessions (default)
  %(prog)s --limit 50                Evaluate most recent 50 sessions
  %(prog)s --no-eval                 Browse Q&A pairs without evaluation
  %(prog)s --report                  Also generate a Markdown report
  %(prog)s --persist                 Evaluate and persist results to BQ
  %(prog)s --time-period 7d          Evaluate last 7 days
  %(prog)s --output-json report.json Write structured JSON output
  %(prog)s --env path/to/.env        Load env vars from a specific .env file
  %(prog)s --tag-turns               Classify each user turn and find corrections
  %(prog)s --trajectory-samples 5    Include 5 execution traces in the report

Filtering (all filters appear in the Execution Details section of the report):
  %(prog)s --app-name my_agent       Filter to a specific agent app
  %(prog)s --label version=v2.1      Filter by custom label
  %(prog)s --label version=v2 --label env=prod  Multiple labels (AND)
  %(prog)s --time-period 7d --app-name my_agent --label version=v2.1
                                     Combine filters (time + app + label)

  Labels match custom_tags set via BigQueryLoggerConfig.custom_tags when
  initializing the ADK plugin. Common uses: version tagging, deployment
  environment, experiment ID, A/B test variant.

Scope + golden grounding (--eval-spec):
  %(prog)s --eval-spec eval_spec.json --report

  The eval spec grounds scoring. 'scope' (free text) defines what the agent
  handles — anything outside it is out of scope, so a polite refusal is scored
  "declined" (correct) rather than "unhelpful". 'golden_qa' supplies expected
  answers matched per-question by embedding similarity to ground correctness.

  Example eval_spec.json:
    {
      "scope": "Answers HR policy questions: PTO, benefits, expenses, "
               "holidays. Does not handle salary, equity, or IT support.",
      "ground_truth": "PTO: 20 days/year ...",
      "golden_qa": [
        {"question": "How many PTO days?", "expected_answer": "20/year",
         "topic": "pto"},
        {"question": "What are the salary bands?",
         "expected_behavior": "decline", "topic": "out_of_scope"}
      ]
    }

  See scripts/eval/data/eval_spec.example.json for a full example.

Samples (controls how many sessions appear in each report section):
  %(prog)s --samples 5               Cap all sections at 5 sessions
  %(prog)s --samples all             Show every session (no limit)
  %(prog)s --samples unhelpful=10,partial=5,low=3
                                     Per-category: 10 unhelpful, 5 partial,
                                     3 for each Low-dimension section
  %(prog)s --samples unhelpful=all,declined=1
                                     All unhelpful, 1 declined, defaults for rest
  (without --samples)                Defaults: unhelpful=10, partial=5, others=3

  Categories: unhelpful, declined, partial, meaningful, low (all Low-* sections)

Full report:
  %(prog)s --report --limit 20 --app-name my_agent --label version=v2.1 \\
    --samples 3 --tag-turns --trajectory-samples 3 \\
    --eval-spec eval_spec.json --env path/to/.env

Custom metrics (overrides auto-discovered eval/eval_config.json):
  %(prog)s --eval-config path/to/custom_eval_config.json
      """,
  )
  parser.add_argument(
      "--limit",
      type=_positive_int,
      default=100,
      help="Evaluate the N most recent sessions (default: 100)",
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
      "--dimensions",
      choices=["full", "primary"],
      default="full",
      help="Which LLM-judge metrics to run. 'full' (default) scores all 8 "
      "metrics: 2 primary (response_usefulness, task_grounding), the 5 quality "
      "dimensions, and failure_attribution. 'primary' scores only the 2 primary "
      "metrics — about 4x cheaper (2 LLM calls/session instead of 8) but omits "
      "the Quality Dimensions table. Use --no-eval to skip evaluation entirely.",
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
      "--render-json",
      metavar="REPORT_JSON",
      help=(
          "Re-render the Markdown report from an existing --output-json file"
          " (pure formatting, no model calls); writes <name>.md next to it"
      ),
  )
  parser.add_argument(
      "--samples",
      type=_samples_arg,
      default=None,
      help="Max sessions to show per report section. Accepts a single "
      "number (caps all sections equally), 'all' (no limit), or "
      "comma-separated key=value pairs for per-category control. "
      "Categories: unhelpful, declined, partial, meaningful, low "
      "(all Low-dimension sections). "
      "Defaults: unhelpful=10, partial=5, all others=3",
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
      "--label",
      type=str,
      action="append",
      default=None,
      metavar="KEY=VALUE",
      help="Filter by custom label (repeatable). Matches custom_tags set "
      "via BigQueryLoggerConfig.custom_tags. "
      "Example: --label version=v2.1 --label env=prod",
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
      "--eval-spec",
      type=str,
      default=None,
      metavar="PATH",
      dest="eval_spec",
      help="Path to an eval-spec JSON file that grounds scoring. Three "
      "optional fields: 'scope' (free text describing what the agent "
      "handles — anything outside it is out of scope, so a polite decline "
      "is correct), 'ground_truth' (free-text authoritative facts), and "
      "'golden_qa' (list of {question, expected_answer, topic?, "
      "expected_behavior?} matched per-question by embedding similarity to "
      "ground correctness). Enables the 'declined' category. Auto-discovered "
      "from eval/data/eval_spec.json. Use 'none' to disable.",
  )
  parser.add_argument(
      "--golden-threshold",
      type=float,
      default=_DEFAULT_GOLDEN_THRESHOLD,
      metavar="FLOAT",
      help="Cosine-similarity threshold for golden_qa matching "
      "(default: 0.92). Lower matches more aggressively.",
  )
  parser.add_argument(
      "--eval-config",
      type=str,
      default=None,
      metavar="PATH",
      help="Path to a JSON file with metric definitions. By default, "
      "eval/eval_config.json is auto-discovered from the repo root or "
      "script directory. Use this flag to override with a custom file. "
      "See scripts/eval/eval_config.json for the expected format.",
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
      "--conversations-file",
      type=str,
      default=None,
      metavar="PATH",
      help="JSON file with local conversations to evaluate (no BigQuery "
      'required). Expects {"conversations": [...]} or a plain list of '
      "conversation dicts. When set, traces are scored locally via the "
      "Gemini API instead of being fetched from BigQuery.",
  )
  parser.add_argument(
      "--concurrency",
      type=int,
      default=10,
      help="Max parallel Gemini API calls for --conversations-file mode "
      "(default: 10).",
  )
  parser.add_argument(
      "--tag-turns",
      action="store_true",
      default=False,
      help="Run the full turn tagger on multi-turn conversations to classify "
      "each user turn (CORRECTION, VERIFY, SPECIFICS, SCOPE, FOLLOWUP, END) "
      "and identify correction boundaries and sub-trajectories.",
  )
  parser.add_argument(
      "--trajectory-samples",
      type=int,
      default=0,
      metavar="N",
      help="Fetch N execution traces from BigQuery and include them in the "
      "report. Prioritizes unhelpful and correction sessions.",
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

  if args.conversations_file:
    for var, default in [
        ("PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "local")),
        ("DATASET_ID", "local"),
        ("TABLE_ID", "conversations"),
        ("DATASET_LOCATION", os.getenv("GOOGLE_CLOUD_LOCATION", "local")),
    ]:
      os.environ.setdefault(var, default)

  if args.render_json:
    # Pure offline formatting: BigQuery configuration is OPTIONAL here. When
    # the env is configured, execution traces are fetched to enrich the
    # correction blocks; when it is absent, the report renders from the JSON
    # alone -- no model calls, no BigQuery, no config required.
    try:
      _load_config()
    except SystemExit:
      logger.info(
          "BigQuery env not configured -- rendering offline from the JSON"
          " (correction blocks fall back to the recorded dialogue)."
      )
    md_path = _render_md_from_json(args.render_json, args)
    print(f"Markdown report: {md_path}")
    return

  _load_config()

  if args.eval:
    run_eval(args)
  else:
    run_browse(args)


if __name__ == "__main__":
  main()
