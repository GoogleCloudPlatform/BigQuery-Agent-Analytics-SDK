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

"""4-layer cost-aware trace search router (Layer 0 rules + Layer 1 fast LLM).

Routes incoming trace search queries across:
  - ``KEYWORD`` (BigQuery ``SEARCH`` index lookup on compact trajectories),
  - ``NATURAL_LANGUAGE`` (BigQuery ``AI.SEARCH`` semantic + rerank path), and
  - ``HYBRID`` (explicit RRF union combining keyword and semantic matches).

Layer 0 settles empty, quoted, session/invocation ID, error constant, and
explicitly mixed queries at 0 ms and $0 model cost. Ambiguous natural-language
queries fall through to Layer 1, supporting both Developer API
``client.decisions.create(model="gemini-decision-1", ...)`` (``v1beta``,
0 output tokens) and Vertex AI ``gemini-4-hydrogen`` (``thinking_level="LOW"``
on ``global``, or ``thinking_budget=0`` for ``gemini-2.5-*`` overrides).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
import json
import os
import re
import sys
import time
from typing import Any

ROUTE_KEYWORD = "KEYWORD"
ROUTE_NATURAL_LANGUAGE = "NATURAL_LANGUAGE"
ROUTE_HYBRID = "HYBRID"
_VALID_ROUTES = frozenset({ROUTE_KEYWORD, ROUTE_NATURAL_LANGUAGE, ROUTE_HYBRID})

_QUOTED_PHRASE_RE = re.compile(r'"[^"]+"')
_ID_TOKEN_RE = re.compile(
    r"\b(?:sess_[a-zA-Z0-9_-]+|inv_[a-zA-Z0-9_-]+|cuj-[a-zA-Z0-9_-]+"
    r"|ORD-[0-9]+|ACCT-[0-9]+"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
_ERROR_CONST_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:_[A-Z0-9]+)+\b")
_SNAKE_TOOL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]+")

_NL_BEHAVIOR_HINT_RE = re.compile(
    r"\b(?:why|how|when|where|which|agent|user|customer|told|claimed|"
    r"contradiction|stuck|repeatedly|loop|failed|failure|succeed|succeeded|"
    r"timeout|timeouts|behavior|pattern|explain|confused|silently)\b",
    re.IGNORECASE,
)

# Reference per-token pricing used for transparent cost reporting.
_DECISIONS_COST_PER_CALL_USD = 0.000046
_FLASH_INPUT_USD_PER_M = 0.30
_FLASH_OUTPUT_USD_PER_M = 2.50

_ROUTER_SYSTEM_INSTRUCTION = (
    "You classify agent trace search queries into KEYWORD (exact token, ID, "
    "quoted phrase, or error code lookup), NATURAL_LANGUAGE (behavioral or "
    "conceptual search over agent interactions), or HYBRID (mixed). "
    "Respond strictly as JSON matching the requested schema."
)


@dataclass(frozen=True)
class RouteDecision:
  """Structured routing decision emitted by Layer 0 or Layer 1."""

  mode: str
  layer: str
  confidence: float
  reason: str
  latency_ms: float
  input_tokens: int = 0
  output_tokens: int = 0
  cost_usd: float = 0.0
  raw_choice: str | None = None
  probabilities: dict[str, float] = field(default_factory=dict)

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def _extract_code_spans_and_nl_words(query: str) -> tuple[list[str], list[str]]:
  """Splits exact identifier/tool/error tokens from behavioral prose words."""
  code_tokens: list[str] = []
  remaining = query
  for pattern in (_ID_TOKEN_RE, _ERROR_CONST_RE, _SNAKE_TOOL_RE):
    matches = pattern.findall(remaining)
    if matches:
      code_tokens.extend(matches)
      remaining = pattern.sub(" ", remaining)
  nl_words = _ALPHA_WORD_RE.findall(remaining)
  return code_tokens, nl_words


def classify_layer0(query: str | None) -> RouteDecision | None:
  """Applies deterministic $0 / 0 ms Layer-0 routing rules.

  Returns:
      A ``RouteDecision`` when deterministic rules classify the query with
      certainty, or ``None`` to escalate ambiguous queries to Layer 1.
  """
  text = (query or "").strip()
  if not text:
    return RouteDecision(
        mode=ROUTE_KEYWORD,
        layer="L0_RULE",
        confidence=1.0,
        reason="empty_or_metadata_only",
        latency_ms=0.0,
        raw_choice=ROUTE_KEYWORD,
    )

  if _QUOTED_PHRASE_RE.search(text):
    unquoted = _QUOTED_PHRASE_RE.sub(" ", text)
    _, outside_words = _extract_code_spans_and_nl_words(unquoted)
    if len(outside_words) < 4:
      return RouteDecision(
          mode=ROUTE_KEYWORD,
          layer="L0_RULE",
          confidence=1.0,
          reason="quoted_phrase",
          latency_ms=0.0,
          raw_choice=ROUTE_KEYWORD,
      )

  code_tokens, nl_words = _extract_code_spans_and_nl_words(text)
  if code_tokens:
    if len(nl_words) >= 4:
      return RouteDecision(
          mode=ROUTE_HYBRID,
          layer="L0_RULE",
          confidence=0.95,
          reason="mixed_code_token_and_natural_language",
          latency_ms=0.0,
          raw_choice=ROUTE_HYBRID,
      )
    return RouteDecision(
        mode=ROUTE_KEYWORD,
        layer="L0_RULE",
        confidence=1.0,
        reason="exact_identifier_or_error_constant",
        latency_ms=0.0,
        raw_choice=ROUTE_KEYWORD,
    )

  return None


def _build_decisions_client(api_key: str) -> Any:
  """Constructs a Developer API genai.Client configured for v1beta."""
  from google import genai

  return genai.Client(
      api_key=api_key,
      http_options={"api_version": "v1beta"},
  )


def model_uses_thinking_level(model_name: str | None) -> bool:
  """Returns True when a Vertex Gemini model uses thinking_level (Gemini 4+)."""
  low = str(model_name or "").strip().lower()
  return (
      "gemini-4" in low or "hydrogen" in low or "neon" in low or "carbon" in low
  )


def thinking_config_for_model(model_name: str, types_mod: Any) -> Any:
  """Returns ThinkingConfig matching Gemini 4 Hydrogen (LOW) or 2.5 (budget=0)."""
  if model_uses_thinking_level(model_name):
    return types_mod.ThinkingConfig(thinking_level="LOW")
  return types_mod.ThinkingConfig(thinking_budget=0)


def _build_vertex_client(
    *, project_id: str | None = None, location: str = "global"
) -> Any:
  """Constructs a Vertex AI genai.Client using Application Default Creds."""
  from google import genai

  kwargs: dict[str, Any] = {"vertexai": True, "location": location}
  if project_id:
    kwargs["project"] = project_id
  return genai.Client(**kwargs)


def _estimate_tokens(text: str) -> int:
  return max(1, (len(text) + 3) // 4) if text else 0


class QueryRouter:
  """Dual-backend 4-layer trace search query router."""

  def __init__(
      self,
      *,
      backend: str = "auto",
      project_id: str | None = None,
      location: str = "global",
      api_key: str | None = None,
      decisions_model: str = "gemini-decision-1",
      vertex_model: str = "gemini-4-hydrogen",
      hybrid_threshold: float = 0.70,
      offline: bool = False,
      decisions_client: Any = None,
      vertex_client: Any = None,
  ) -> None:
    if backend not in {"auto", "decisions", "vertex", "offline"}:
      raise ValueError(f"Unsupported backend: {backend!r}")
    self.backend = backend
    self.project_id = project_id
    self.location = location
    self.api_key = (
        api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
    )
    self.decisions_model = decisions_model
    self.vertex_model = vertex_model
    self.hybrid_threshold = float(hybrid_threshold)
    self.offline = offline or backend == "offline"
    self._decisions_client = decisions_client
    self._vertex_client = vertex_client

  def classify(self, query: str | None) -> RouteDecision:
    """Classifies a query through Layer 0 rules, escalating only if needed."""
    layer0 = classify_layer0(query)
    if layer0 is not None:
      return layer0

    text = (query or "").strip()
    if self.offline:
      return self._classify_offline_heuristic(text)

    if self.backend in {"auto", "decisions"}:
      dec_client = self._get_decisions_client()
      if dec_client is not None and hasattr(dec_client, "decisions"):
        return self._classify_via_decisions_api(dec_client, text)
      if self.backend == "decisions":
        raise RuntimeError(
            "Decisions API backend selected, but client.decisions is not "
            "available in the active google-genai installation or GEMINI_API_KEY "
            "is unset."
        )

    if self.backend in {"auto", "vertex"}:
      v_client = self._get_vertex_client()
      return self._classify_via_vertex(v_client, text)

    raise RuntimeError(f"Unable to route query with backend={self.backend!r}")

  def _get_decisions_client(self) -> Any:
    if self._decisions_client is not None:
      return self._decisions_client
    if not self.api_key:
      return None
    self._decisions_client = _build_decisions_client(self.api_key)
    return self._decisions_client

  def _get_vertex_client(self) -> Any:
    if self._vertex_client is not None:
      return self._vertex_client
    self._vertex_client = _build_vertex_client(
        project_id=self.project_id,
        location=self.location,
    )
    return self._vertex_client

  def _classify_via_decisions_api(
      self, client: Any, query: str
  ) -> RouteDecision:
    t0 = time.perf_counter()
    response = client.decisions.create(
        model=self.decisions_model,
        input=query,
        questions={
            "route": {
                "type": "choice",
                "instructions": "Classify the agent trace search query.",
                "criteria": {
                    ROUTE_KEYWORD: (
                        "Exact error code, session/tool identifier, or "
                        "literal log token lookup"
                    ),
                    ROUTE_NATURAL_LANGUAGE: (
                        "Conceptual or behavioral description of an agent "
                        "interaction or failure pattern"
                    ),
                },
            }
        },
    )
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    ans = response.answers["route"]
    raw_choice = str(ans.choice).strip().upper()
    if raw_choice not in _VALID_ROUTES:
      raw_choice = ROUTE_HYBRID
    raw_conf = getattr(ans, "confidence", None)
    try:
      confidence = float(0.5 if raw_conf is None else raw_conf)
    except (TypeError, ValueError):
      confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    probabilities = {
        str(k): float(v)
        for k, v in (getattr(ans, "probabilities", None) or {}).items()
    }
    mode = ROUTE_HYBRID if confidence < self.hybrid_threshold else raw_choice
    reason = (
        f"decisions_low_confidence({confidence:.2f}<{self.hybrid_threshold:.2f})"
        if mode == ROUTE_HYBRID and raw_choice != ROUTE_HYBRID
        else f"decisions_choice:{raw_choice}"
    )
    return RouteDecision(
        mode=mode,
        layer="L1_DECISIONS_API",
        confidence=confidence,
        reason=reason,
        latency_ms=latency_ms,
        input_tokens=_estimate_tokens(query) + 45,
        output_tokens=0,
        cost_usd=_DECISIONS_COST_PER_CALL_USD,
        raw_choice=raw_choice,
        probabilities=probabilities,
    )

  def _classify_via_vertex(self, client: Any, query: str) -> RouteDecision:
    from google.genai import types

    schema = {
        "type": "OBJECT",
        "properties": {
            "route": {
                "type": "STRING",
                "enum": [
                    ROUTE_KEYWORD,
                    ROUTE_NATURAL_LANGUAGE,
                    ROUTE_HYBRID,
                ],
            },
            "confidence": {"type": "NUMBER"},
            "reason": {"type": "STRING"},
        },
        "required": ["route", "confidence", "reason"],
    }
    is_hydrogen = model_uses_thinking_level(self.vertex_model)
    config = types.GenerateContentConfig(
        system_instruction=_ROUTER_SYSTEM_INSTRUCTION,
        temperature=0.0,
        max_output_tokens=1024 if is_hydrogen else 256,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=thinking_config_for_model(self.vertex_model, types),
    )
    t0 = time.perf_counter()
    response = client.models.generate_content(
        model=self.vertex_model,
        contents=f"Query: {query}",
        config=config,
    )
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    raw_text = (getattr(response, "text", None) or "{}").strip()
    try:
      parsed = json.loads(raw_text)
    except json.JSONDecodeError:
      parsed = {}
    if not isinstance(parsed, dict):
      parsed = {}
    raw_choice = str(parsed.get("route") or ROUTE_HYBRID).strip().upper()
    if raw_choice not in _VALID_ROUTES:
      raw_choice = ROUTE_HYBRID
    raw_conf = parsed.get("confidence")
    try:
      confidence = float(0.5 if raw_conf is None else raw_conf)
    except (TypeError, ValueError):
      confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    reason_prefix = "vertex_hydrogen" if is_hydrogen else "vertex_flash"
    reason = str(parsed.get("reason") or f"{reason_prefix}:{raw_choice}")

    mode = ROUTE_HYBRID if confidence < self.hybrid_threshold else raw_choice
    if mode == ROUTE_HYBRID and raw_choice != ROUTE_HYBRID:
      reason = (
          f"vertex_low_confidence({confidence:.2f}<{self.hybrid_threshold:.2f})"
      )

    usage = getattr(response, "usage_metadata", None)
    in_tok = int(
        getattr(usage, "prompt_token_count", 0) or _estimate_tokens(query) + 60
    )
    cand_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts_tok = int(getattr(usage, "thoughts_token_count", 0) or 0)
    out_tok = (cand_tok + thoughts_tok) or 18
    cost_usd = round(
        (in_tok * _FLASH_INPUT_USD_PER_M + out_tok * _FLASH_OUTPUT_USD_PER_M)
        / 1_000_000.0,
        6,
    )
    return RouteDecision(
        mode=mode,
        layer="L1_VERTEX_HYDROGEN" if is_hydrogen else "L1_VERTEX_FLASH",
        confidence=confidence,
        reason=reason,
        latency_ms=latency_ms,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost_usd,
        raw_choice=raw_choice,
    )

  def _classify_offline_heuristic(self, query: str) -> RouteDecision:
    hints = _NL_BEHAVIOR_HINT_RE.findall(query)
    words = _ALPHA_WORD_RE.findall(query)
    if len(hints) >= 1 and len(words) >= 4:
      return RouteDecision(
          mode=ROUTE_NATURAL_LANGUAGE,
          layer="L1_OFFLINE_HEURISTIC",
          confidence=0.91,
          reason="offline_behavioral_intent",
          latency_ms=0.2,
          input_tokens=_estimate_tokens(query) + 45,
          output_tokens=0,
          cost_usd=0.0,
          raw_choice=ROUTE_NATURAL_LANGUAGE,
      )
    if len(words) >= 2:
      return RouteDecision(
          mode=ROUTE_HYBRID,
          layer="L1_OFFLINE_HEURISTIC",
          confidence=0.62,
          reason="offline_ambiguous_hybrid_fallback",
          latency_ms=0.2,
          input_tokens=_estimate_tokens(query) + 45,
          output_tokens=0,
          cost_usd=0.0,
          raw_choice=ROUTE_NATURAL_LANGUAGE,
      )
    return RouteDecision(
        mode=ROUTE_KEYWORD,
        layer="L1_OFFLINE_HEURISTIC",
        confidence=0.85,
        reason="offline_short_token_lookup",
        latency_ms=0.2,
        input_tokens=_estimate_tokens(query) + 45,
        output_tokens=0,
        cost_usd=0.0,
        raw_choice=ROUTE_KEYWORD,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Classify a trace search query via Layer 0 + Layer 1 router."
  )
  parser.add_argument("--query", required=True, help="Search query text.")
  parser.add_argument(
      "--backend",
      default="auto",
      choices=["auto", "decisions", "vertex", "offline"],
      help="Layer 1 classifier backend.",
  )
  parser.add_argument("--project-id", default=None, help="Vertex project ID.")
  parser.add_argument("--location", default="global", help="Vertex region.")
  parser.add_argument(
      "--vertex-model",
      default="gemini-4-hydrogen",
      help="Vertex Gemini model ID for Layer 1 fallback.",
  )
  parser.add_argument(
      "--offline",
      action="store_true",
      help="Run purely locally without network calls.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = _parse_args(argv)
  router = QueryRouter(
      backend=args.backend,
      project_id=args.project_id,
      location=args.location,
      vertex_model=args.vertex_model,
      offline=args.offline,
  )
  decision = router.classify(args.query)
  print(json.dumps(decision.to_dict(), indent=2))
  return 0


if __name__ == "__main__":
  sys.exit(main())
