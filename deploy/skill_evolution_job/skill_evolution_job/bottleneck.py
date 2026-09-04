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

"""Automatic bottleneck detection for multi-agent skill evolution.

Analyzes a quality report to classify failures by source agent, then
recommends which agent(s) to evolve. Prevents wasted evolution cycles
on the wrong agent.

Agent names are loaded dynamically from agent_registry.json via
:mod:`skill_evolution_job.registry`.

Usage:
    from skill_evolution_job.bottleneck import detect_bottleneck

    result = detect_bottleneck(report, client, model_id)
    # result.recommendation = "<agent_name>" | "both" | "none"
    # result.routing_failures = [...], result.skill_failures = [...]
"""

from __future__ import annotations

from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
import json
import logging

from google import genai
from google.genai import types

from . import config
from . import evolve
from . import registry

logger = logging.getLogger(__name__)

_CLASSIFIER_TEMPLATE = """\
You are a failure classifier for a multi-agent system. The system has:
{agent_list}

You receive a failed conversation and must classify the failure source.

IMPORTANT: Prefer ROUTING_FAILURE or SKILL_FAILURE over TOOL_FAILURE or
ARCHITECTURE_FAILURE. Most failures are fixable by improving agent skills
or routing. Only use TOOL_FAILURE when the tool returned demonstrably
wrong data. Only use ARCHITECTURE_FAILURE for genuine infrastructure
issues (timeouts, crashes, state corruption).

Classification categories:

ROUTING_FAILURE: The routing agent made a bad decision. Signals:
- Routing agent answered a question directly instead of delegating to a sub-agent
- Routing agent sent the question to the wrong sub-agent
- Agent answered from "own knowledge" instead of using available tools
- Response says "I don't have access to" when the tool exists
- Agent provided generic info when specific data was available via tools
- Agent declined or refused a question that it could have answered

SKILL_FAILURE: A sub-agent received the question but its skill/instructions
were insufficient. This is the MOST COMMON failure type. Signals:
- Agent used the tool but with wrong keyword or topic mapping
- Agent got tool results but misinterpreted or ignored them
- Agent failed to handle edge cases (synonyms, implicit topics)
- Agent gave correct first answer but failed on follow-ups
- Agent's response was vague, incomplete, or lacked specifics
- Agent hallucinated facts instead of using the tool
- Tool doesn't cover the topic — the skill should teach graceful handling
- Agent did not know how to phrase the tool query

TOOL_FAILURE: The tool itself returned wrong data despite correct usage.
This is RARE. Only classify as TOOL_FAILURE when:
- The tool was called with the correct topic/keyword AND returned wrong data
- NOT when the agent called the tool with the wrong keyword (that's SKILL)
- NOT when the tool has no data for a topic (that's SKILL — handle gracefully)

ARCHITECTURE_FAILURE: Systemic infrastructure issue. This is RARE. Only when:
- Multi-turn state was genuinely lost (not just poor follow-up handling)
- Conversation timed out or crashed
- NOT when the agent just handled a multi-turn question poorly (that's SKILL)

Output format (JSON):
{{
  "classification": "ROUTING_FAILURE" | "SKILL_FAILURE" | "TOOL_FAILURE" | "ARCHITECTURE_FAILURE",
  "confidence": 0.0-1.0,
  "evidence": "one sentence explaining why",
  "agent_responsible": {agent_responsible_enum}
}}
"""


def _build_classifier_prompt(agents: dict) -> str:
  agent_lines = []
  for name, agent_cfg in agents.items():
    label = agent_cfg.get("label", name)
    agent_lines.append(f"- A **{name}** ({label})")
  agent_names = [f'"{name}"' for name in agents] + ['"system"']
  return _CLASSIFIER_TEMPLATE.format(
      agent_list="\n".join(agent_lines),
      agent_responsible_enum=" | ".join(agent_names),
  )


@dataclass
class BottleneckResult:
  """Result of bottleneck detection analysis."""

  total_failures: int = 0
  routing_failures: list = field(default_factory=list)
  skill_failures: list = field(default_factory=list)
  tool_failures: list = field(default_factory=list)
  architecture_failures: list = field(default_factory=list)
  recommendation: str = "both"
  confidence: float = 0.0
  summary: str = ""

  @property
  def routing_pct(self) -> float:
    return len(self.routing_failures) / max(self.total_failures, 1) * 100

  @property
  def skill_pct(self) -> float:
    return len(self.skill_failures) / max(self.total_failures, 1) * 100


def classify_failure(
    client: genai.Client,
    model_id: str,
    session: dict,
    classifier_prompt: str | None = None,
    default_agent: str = "unknown",
) -> dict:
  """Classify a single failure by source agent."""
  if classifier_prompt is None:
    classifier_prompt = _build_classifier_prompt(registry.agents_summary())

  conversation = evolve._format_conversation(session.get("conversation", []))
  question = session.get("question", "")
  verdict = session.get("metrics", {}).get("response_usefulness", {})
  quality = session.get("quality_scores", {})
  answered_by = session.get("answered_by", "")

  prompt = (
      f"<conversation>\n{conversation or question}\n</conversation>\n\n"
      f"Agent that answered: {answered_by}\n"
      f"Verdict: {verdict.get('category', '?')}\n"
      f"Justification: {verdict.get('justification', '')}\n"
      f"Tool calls: {session.get('tool_calls', 0)}\n"
      f"Corrections: {session.get('corrections', 0)}\n"
  )
  for dim in (
      "correctness",
      "tool_usage",
      "scope_compliance",
      "first_time_right",
  ):
    score_data = quality.get(dim, {})
    if score_data:
      prompt += (
          f"{dim}: {score_data.get('score', '?')}/2 —"
          f" {score_data.get('reason', '')}\n"
      )

  prompt += "\nClassify this failure. Output ONLY the JSON object."

  # One classifier call per failing session adds up fast (a 70-failure
  # report is 70 rapid calls) — back off and retry on per-minute quota.
  import time as _time

  response = None
  for attempt, delay in enumerate((15, 30, 60, 0)):
    try:
      response = client.models.generate_content(
          model=model_id,
          contents=prompt,
          config=types.GenerateContentConfig(
              system_instruction=classifier_prompt,
              temperature=0.1,
              response_mime_type="application/json",
          ),
      )
      break
    except Exception as e:  # noqa: BLE001
      transient = "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e)
      if not transient or delay == 0:
        raise
      logger.warning(
          "Classifier quota hit (attempt %d), retrying in %ds",
          attempt + 1,
          delay,
      )
      _time.sleep(delay)

  try:
    return json.loads(response.text.strip())
  except (json.JSONDecodeError, AttributeError):
    return {
        "classification": "SKILL_FAILURE",
        "confidence": 0.3,
        "evidence": "Could not parse classifier output",
        "agent_responsible": default_agent,
    }


def _find_dominant_agent(classifications: list[dict], failure_type: str) -> str:
  """Find the most frequently responsible agent for a failure type."""
  from collections import Counter

  agents = [
      c.get("classification", {}).get("agent_responsible", "")
      for c in classifications
      if c.get("classification", {}).get("classification") == failure_type
  ]
  if not agents:
    return ""
  return Counter(agents).most_common(1)[0][0]


def detect_bottleneck(
    report: dict,
    client: genai.Client,
    model_id: str = "gemini-2.5-flash",
    max_failures: int = 0,
    agents: dict | None = None,
) -> BottleneckResult:
  """Analyze a quality report and detect the primary bottleneck.

  Args:
      report: Quality report dict with sessions.
      client: Genai client.
      model_id: Model for classification calls.
      max_failures: Max failures to classify. 0 means classify all.
      agents: Agent registry dict. Loaded from agent_registry.json
          if not provided.

  Returns:
      BottleneckResult with classification and recommendation.
  """
  if agents is None:
    agents = registry.agents_summary()

  classifier_prompt = _build_classifier_prompt(agents)
  agent_names = list(agents.keys())
  default_agent = agent_names[0] if agent_names else "unknown"

  sessions = report.get("sessions", [])
  failures = [
      s
      for s in sessions
      if s.get("metrics", {}).get("response_usefulness", {}).get("category")
      in ("unhelpful", "partial")
  ]

  if not failures:
    return BottleneckResult(
        recommendation="none",
        summary="No failures to classify.",
    )

  result = BottleneckResult(total_failures=len(failures))
  all_classifications = []

  classify_set = failures if max_failures <= 0 else failures[:max_failures]

  def _classify_one(session):
    classification = classify_failure(
        client,
        model_id,
        session,
        classifier_prompt=classifier_prompt,
        default_agent=default_agent,
    )
    return {
        "question": session.get("question", "")[:80],
        "classification": classification,
    }

  # Modest parallelism: 10 workers on a large failure set trips per-minute
  # Gemini quota (each worker fires one classifier call per session).
  with ThreadPoolExecutor(
      max_workers=config._env_int("BOTTLENECK_WORKERS", 4)
  ) as executor:
    futures = {executor.submit(_classify_one, s): s for s in classify_set}
    for future in as_completed(futures):
      entry = future.result()
      all_classifications.append(entry)
      cat = entry["classification"].get("classification", "SKILL_FAILURE")
      if cat == "ROUTING_FAILURE":
        result.routing_failures.append(entry)
      elif cat == "SKILL_FAILURE":
        result.skill_failures.append(entry)
      elif cat == "TOOL_FAILURE":
        result.tool_failures.append(entry)
      else:
        result.architecture_failures.append(entry)

  routing_pct = result.routing_pct
  skill_pct = result.skill_pct

  routing_agent = (
      _find_dominant_agent(all_classifications, "ROUTING_FAILURE")
      or agent_names[0]
      if len(agent_names) > 0
      else "unknown"
  )
  skill_agent = _find_dominant_agent(all_classifications, "SKILL_FAILURE") or (
      agent_names[1]
      if len(agent_names) > 1
      else agent_names[0]
      if agent_names
      else "unknown"
  )

  actionable_pct = routing_pct + skill_pct

  # Co-evolve when both failure types are present (routing >= 10%).
  # Only skip co-evolution when routing is truly negligible.
  if routing_pct >= 10 and skill_pct >= 10:
    result.recommendation = "both"
    result.confidence = actionable_pct / 100
  elif routing_pct >= 60:
    result.recommendation = routing_agent
    result.confidence = routing_pct / 100
  elif skill_pct >= 60:
    result.recommendation = skill_agent
    result.confidence = skill_pct / 100
  elif actionable_pct < 40:
    result.recommendation = "both"
    result.confidence = actionable_pct / 100
  elif routing_pct > skill_pct:
    result.recommendation = routing_agent
    result.confidence = routing_pct / 100
  else:
    result.recommendation = skill_agent
    result.confidence = skill_pct / 100

  result.summary = (
      f"{result.total_failures} failures classified: "
      f"{len(result.routing_failures)} routing ({routing_pct:.0f}%), "
      f"{len(result.skill_failures)} skill ({skill_pct:.0f}%), "
      f"{len(result.tool_failures)} tool, "
      f"{len(result.architecture_failures)} architecture. "
      f"Recommendation: evolve {result.recommendation} "
      f"(confidence {result.confidence:.0%})"
  )

  logger.info("Bottleneck detection: %s", result.summary)
  return result
