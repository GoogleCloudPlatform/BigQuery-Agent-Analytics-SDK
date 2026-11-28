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

"""Combine the rule engine and the LLM judge into one diagnosis (#435).

The rules (``rule_taxonomy``) and the judge (``failure_judge``) read the
same session in different ways: one with fixed checks on the event
stream, one by reading a blind dossier. A label is accepted only when
both name the same category for the session. Otherwise the session is
left for review, with both opinions attached.

=============  ==========================================  ========
outcome        when                                        accepted
=============  ==========================================  ========
excluded       the judge excluded the run                  no
agreed         the rules' primary label and the judge's    yes
               label have the same category
disagreed      both labeled it, with different categories  no
engine_only    only the rules labeled it                   no
judge_only     only the judge labeled it                   no
none           neither did                                 no
=============  ==========================================  ========

An accepted diagnosis takes the judge's anchor (span and target kind);
``anchors_agree`` says whether the rules' label is on the same span, or
whether both are gaps after the last step. On
44 human-labeled KramaBench runs (#435), with the built-in rules,
agreement covered 8 or 9 runs and was right on all but one. With the
contributors' benchmark rules added as ``checks`` it covered 27 runs,
23 right. See ``docs/failure_diagnosis.md``.

Pure and deterministic; the only model call is the backend's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import dataclasses
import re
from typing import Any, Optional

from .failure_judge import judge_failed_sessions
from .failure_judge import JudgeBackend
from .failure_judge import JudgeResult
from .failure_judge import STATUS_EXCLUDED
from .failure_judge import STATUS_LABELED
from .failure_taxonomy import TAXONOMY_VERSION
from .rule_taxonomy import collect_session_facts
from .rule_taxonomy import label_failed_session_rules
from .rule_taxonomy import primary_label
from .rule_taxonomy import RuleCheck
from .rule_taxonomy import RuleFailureLabel
from .rule_taxonomy import SessionFacts
from .rule_taxonomy import ToolProfile
from .session_dossier import build_session_dossier
from .span_taxonomy import TARGET_GAP_AFTER_SPAN

OUTCOME_EXCLUDED = "excluded"
OUTCOME_AGREED = "agreed"
OUTCOME_DISAGREED = "disagreed"
OUTCOME_ENGINE_ONLY = "engine_only"
OUTCOME_JUDGE_ONLY = "judge_only"
OUTCOME_NONE = "none"
OUTCOMES = (
    OUTCOME_EXCLUDED,
    OUTCOME_AGREED,
    OUTCOME_DISAGREED,
    OUTCOME_ENGINE_ONLY,
    OUTCOME_JUDGE_ONLY,
    OUTCOME_NONE,
)

# Model families by the start of a model name, after any path prefix
# ("publishers/google/models/gemini-2.5-pro" is "gemini-2.5-pro"). A judge
# from the agent's own family tends to share its blind spots.
MODEL_FAMILIES = (
    ("google", ("gemini", "gemma", "palm", "bison")),
    ("anthropic", ("claude",)),
    ("openai", ("gpt", "o1", "o3", "o4", "chatgpt")),
    ("alibaba", ("qwen", "qwq")),
    ("deepseek", ("deepseek",)),
    ("meta", ("llama",)),
    ("mistral", ("mistral", "mixtral", "codestral", "ministral")),
    ("xai", ("grok",)),
)
_FAMILY_RE = re.compile(
    "|".join(
        rf"(?P<{family}>(?:{'|'.join(prefixes)})(?![a-z]))"
        for family, prefixes in MODEL_FAMILIES
    )
)


def model_family(model_id: Optional[str]) -> Optional[str]:
  """The family of a model name, or None when it is not recognized.

  A BigQuery remote model reference (``project.dataset.model``) names a
  model the caller created, so its family is not known from the name.
  """
  if not isinstance(model_id, str) or not model_id.strip():
    return None
  name = model_id.strip().lower().rsplit("/", 1)[-1]
  if name.count(".") >= 2 and not re.match(r"[a-z]+-?\d", name):
    return None
  match = _FAMILY_RE.match(name.split(":", 1)[0])
  return match.lastgroup if match else None


def agent_models(facts: SessionFacts) -> tuple[str, ...]:
  """The models the agent called, from its LLM_REQUEST rows, in order."""
  models: list[str] = []
  for span in facts.spans:
    if span.event_type != "LLM_REQUEST":
      continue
    attributes = span.attributes if isinstance(span.attributes, Mapping) else {}
    model = attributes.get("model")
    if isinstance(model, str) and model.strip() and model not in models:
      models.append(model)
  return tuple(models)


@dataclasses.dataclass(frozen=True)
class SessionDiagnosis:
  """The combined diagnosis of one failed session.

  ``failure_category``, ``span_id``, ``trace_id`` and ``target_kind`` are
  set only when ``accepted``. ``engine`` is the rules' primary label and
  ``engine_secondary`` the categories of their other labels. ``judge`` is
  the judge's full result, None when no judge ran. ``anchors_agree`` is
  None unless both sides labeled the session.

  ``agent_models`` are the models the agent called (``LLM_REQUEST`` rows,
  ``attributes.model``). ``same_family`` is True when the judge's model
  is from one of their families, False when it is from none of them, and
  None when a family is unknown or no judge ran. It is a flag for review;
  it does not change ``accepted``.
  """

  session_id: str
  outcome: str
  accepted: bool
  failure_category: Optional[str]
  span_id: Optional[str]
  trace_id: Optional[str]
  target_kind: Optional[str]
  anchors_agree: Optional[bool]
  engine: Optional[RuleFailureLabel]
  engine_secondary: tuple[str, ...]
  judge: Optional[JudgeResult]
  taxonomy_version: str = TAXONOMY_VERSION
  agent_models: tuple[str, ...] = ()
  same_family: Optional[bool] = None


def combine_diagnosis(
    rule_labels: Sequence[RuleFailureLabel],
    judge: Optional[JudgeResult],
    *,
    session_id: Optional[str] = None,
    agent_models: Sequence[str] = (),
) -> SessionDiagnosis:
  """Combine the rules' labels and the judge's result for one session.

  ``session_id`` is needed only when neither side has one to give.
  ``agent_models`` (see ``agent_models()``) let the diagnosis flag a judge
  from the agent's own model family.

  Raises:
    ValueError: the labels and the result are about different sessions,
      or no session id is known.
  """
  sessions = {label.session_id for label in rule_labels}
  if judge is not None:
    sessions.add(judge.session_id)
  if session_id is not None:
    sessions.add(session_id)
  if len(sessions) != 1:
    raise ValueError(
        "rule labels, judge result and session_id must name one session,"
        f" got {sorted(sessions)!r}"
    )
  sid = sessions.pop()
  engine = primary_label(rule_labels)
  secondary = tuple(
      label.failure_category
      for label in rule_labels
      if label is not engine and label.failure_category
  )
  judged = (
      judge.label
      if judge is not None and judge.status == STATUS_LABELED
      else None
  )
  fields: dict[str, Any] = dict(
      session_id=sid,
      accepted=False,
      failure_category=None,
      span_id=None,
      trace_id=None,
      target_kind=None,
      anchors_agree=None,
      engine=engine,
      engine_secondary=secondary,
      judge=judge,
      agent_models=tuple(agent_models),
      same_family=_same_family(judge, agent_models),
  )
  if judge is not None and judge.status == STATUS_EXCLUDED:
    return SessionDiagnosis(outcome=OUTCOME_EXCLUDED, **fields)
  if engine is not None and judged is not None:
    # Two gap labels both say no step is at fault. Rules anchor that gap
    # on the last call or the last row, so their spans may differ.
    fields["anchors_agree"] = engine.span_id == judged.span_id or (
        engine.target_kind == judged.target_kind == TARGET_GAP_AFTER_SPAN
    )
    if engine.failure_category != judged.failure_category:
      return SessionDiagnosis(outcome=OUTCOME_DISAGREED, **fields)
    fields.update(
        accepted=True,
        failure_category=judged.failure_category,
        span_id=judged.span_id,
        trace_id=judged.trace_id,
        target_kind=judged.target_kind,
    )
    return SessionDiagnosis(outcome=OUTCOME_AGREED, **fields)
  if engine is not None:
    return SessionDiagnosis(outcome=OUTCOME_ENGINE_ONLY, **fields)
  if judged is not None:
    return SessionDiagnosis(outcome=OUTCOME_JUDGE_ONLY, **fields)
  return SessionDiagnosis(outcome=OUTCOME_NONE, **fields)


def _same_family(
    judge: Optional[JudgeResult], models: Sequence[str]
) -> Optional[bool]:
  if judge is None:
    return None
  judge_family = model_family(judge.judge_model)
  families = {model_family(m) for m in models}
  if judge_family is None or not families or None in families:
    return None
  return judge_family in families


def diagnose_failed_session(
    session_events: Sequence[Mapping[str, Any]],
    backend: Optional[JudgeBackend] = None,
    *,
    profile: Optional[ToolProfile] = None,
    reference_answer: Any = None,
    checks: Sequence[RuleCheck] = (),
    task: Optional[str] = None,
    context: Optional[Mapping[str, str]] = None,
    rubric: Optional[str] = None,
    exclusions: Sequence[str] = (),
    max_attempts: int = 2,
) -> SessionDiagnosis:
  """Run the rules and, with a backend, the judge on one failed session.

  Takes the rows ``label_failed_session_rules`` takes. Without
  ``backend`` only the rules run, so nothing is accepted. The judge
  never sees the rules' output.

  Raises:
    ValueError: as ``label_failed_session_rules`` and
      ``judge_failed_sessions`` do.
  """
  rule_labels = label_failed_session_rules(
      session_events,
      profile=profile,
      reference_answer=reference_answer,
      checks=checks,
  )
  facts = collect_session_facts(
      session_events, profile=profile, reference_answer=reference_answer
  )
  judge = None
  if backend is not None:
    dossier = build_session_dossier(facts, task=task, context=context)
    (judge,) = judge_failed_sessions(
        [dossier],
        backend,
        rubric=rubric,
        exclusions=exclusions,
        max_attempts=max_attempts,
    )
  return combine_diagnosis(
      rule_labels,
      judge,
      session_id=facts.session_id,
      agent_models=agent_models(facts),
  )
