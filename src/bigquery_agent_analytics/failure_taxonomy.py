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

"""G1-frozen failure taxonomy v0.1 over EvalBench failed-session flags.

This is the #435 Week 0 G1 freeze (``docs/week0_g1_taxonomy.md``). The
vocabulary is frozen at ``taxonomy_version: 0.1.0`` / ``g1_frozen: True``:
the SANA-neighborhood seven categories plus ``unknown`` as the residual
bucket. Names and spellings are stable and may only change through a
versioned taxonomy revision (the reserved revision week of
``docs/agentforensics_mvp_plan.md``).

Category *assignment* stays mechanical until the labeler study runs:
``categorize_failed_session`` maps the three boolean flags of the landed
failed-session contract (#451/#452 — ``SessionVerdict`` /
``classify_sessions`` / ``failed_sessions_sql`` in ``evalbench.py``) onto
frozen names:

- ``missing_completion`` → ``finalization``
- ``process_failed`` → ``tool blockers``
- ``score_failed`` → ``task/planning``

The other four SANA-neighborhood categories and ``unknown`` are in the
frozen vocabulary but are never emitted by this three-flag mapper; a row
with all flags false returns ``()``, not ``("unknown",)`` — a session no
mechanical gate tripped is not a mechanical failure.

Freezing G1 does **not** start the six-week clock: the clock starts only
when the first Week 1 snapshot job is kicked. This module still does not
classify sessions (``evalbench.classify_sessions`` remains the reference
implementation of the flags), and nothing here reaches BigQuery, an LLM,
or the network — everything is pure and deterministic.

The config uses the #431 ``evaluation_rubrics`` schema shape
(``{"metrics": [{"name", "definition", "categories": [{name, definition}]}]}``)
so ``evaluation_rubrics.build_metrics`` can interpret the core metric, and
encodes D2 (one taxonomy with dialects) as a ``dialects`` list of optional
per-benchmark *extension categories on the same core* — empty by default,
never a second taxonomy.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any

# G1-frozen taxonomy version. Bumping this requires a versioned taxonomy
# revision per the plan of record, not an edit.
TAXONOMY_VERSION = "0.1.0"

# The three mechanical flags of the landed failed-session contract, in the
# order ``failed_sessions_sql`` / ``SessionVerdict`` define them. These are
# the *input* contract of the mapper; they are not category names.
MECHANICAL_FLAGS = ("process_failed", "missing_completion", "score_failed")

# The G1-frozen vocabulary, in frozen order: the SANA-neighborhood seven,
# then ``unknown`` as the residual bucket. ``categorize_failed_session``
# returns tripped names in THIS order, never in flag order.
FROZEN_CATEGORY_NAMES = (
    "task/planning",
    "wrong source",
    "execution/computation",
    "incomplete evidence",
    "turn-waste",
    "finalization",
    "tool blockers",
    "unknown",
)

# Frozen names double as the core category ids. Kept as a distinct alias so
# pre-freeze importers of CORE_CATEGORY_IDS keep working; the three flag ids
# they used to hold live on as MECHANICAL_FLAGS.
CORE_CATEGORY_IDS = FROZEN_CATEGORY_NAMES

# Mechanical assignment until the labeler study: which frozen name each
# tripped flag maps to. The remaining frozen names (including ``unknown``)
# are never emitted by the three-flag mapper.
FLAG_TO_CATEGORY = {
    "missing_completion": "finalization",
    "process_failed": "tool blockers",
    "score_failed": "task/planning",
}

_FROZEN_CATEGORY_DEFINITIONS = {
    "task/planning": (
        "The agent formed a wrong or incomplete plan for the assigned task."
        " Mechanical assignment until the labeler study: mapped from the"
        " failed-session score_failed flag."
    ),
    "wrong source": (
        "The agent retrieved, cited, or relied on the wrong source."
        " Mechanical assignment until the labeler study: not emitted by the"
        " three-flag mapper."
    ),
    "execution/computation": (
        "The agent chose a reasonable plan but failed while executing or"
        " computing it. Mechanical assignment until the labeler study: not"
        " emitted by the three-flag mapper."
    ),
    "incomplete evidence": (
        "The agent stopped without gathering enough evidence to support a"
        " correct answer. Mechanical assignment until the labeler study: not"
        " emitted by the three-flag mapper."
    ),
    "turn-waste": (
        "The agent spent turns without advancing the task. Mechanical"
        " assignment until the labeler study: not emitted by the three-flag"
        " mapper."
    ),
    "finalization": (
        "The agent did not produce a completed final response. Mechanical"
        " assignment until the labeler study: mapped from the failed-session"
        " missing_completion flag."
    ),
    "tool blockers": (
        "A required tool was missing, failed, or never invoked. Mechanical"
        " assignment until the labeler study: mapped from the failed-session"
        " process_failed flag."
    ),
    "unknown": (
        "Residual bucket for failures that do not match a frozen category"
        " after labeling. In the vocabulary as residual; the mechanical"
        " three-flag mapper does not emit unknown (all flags false returns"
        " ())."
    ),
}

_TAXONOMY_CONFIG = {
    # Freeze bookkeeping (not part of the #431 metric schema, which
    # ignores unknown top-level keys).
    "taxonomy_version": TAXONOMY_VERSION,
    # G1 is frozen: category names below ARE the taxonomy vocabulary.
    "g1_frozen": True,
    # D2: optional per-benchmark extension categories on the same core (each
    # entry would carry a benchmark name plus #431-shaped categories with
    # optional ``insert_after``). Still empty at the freeze; an empty slot
    # is the encoding, not a placeholder to invent labels for.
    "dialects": [],
    # The #431 schema shape (``evaluation_rubrics``): the one core metric a
    # ``build_metrics()`` caller can interpret.
    "metrics": [
        {
            "name": "failure_category",
            "definition": (
                "Which G1-frozen failure categories (taxonomy v0.1.0) a"
                " failed session falls into. Assignment is mechanical from"
                " the failed-session flags until the labeler study runs."
            ),
            "categories": [
                {"name": name, "definition": _FROZEN_CATEGORY_DEFINITIONS[name]}
                for name in FROZEN_CATEGORY_NAMES
            ],
        }
    ],
}


def taxonomy_config() -> dict:
  """A deep copy of the G1-frozen taxonomy config (mutate freely).

  Same access pattern as ``evaluation_rubrics.builtin_metric_config``. The
  ``metrics`` entry follows the #431 config schema; ``taxonomy_version``,
  ``g1_frozen`` and ``dialects`` are freeze/D2 bookkeeping documented in
  the module docstring.
  """
  return copy.deepcopy(_TAXONOMY_CONFIG)


def scaffold_taxonomy_config() -> dict:
  """Compatibility wrapper for the pre-freeze name: ``taxonomy_config()``.

  The scaffold era ended with the G1 freeze; this returns the same frozen
  config (see CHANGELOG). New callers should use ``taxonomy_config``.
  """
  return taxonomy_config()


def categorize_failed_session(row: Any) -> tuple[str, ...]:
  """Map one failed-session row to its G1-frozen categories.

  Pure and deterministic: no BigQuery, no LLM, no network. ``row`` is one
  failed-session row -- a mapping (e.g. a ``failed_sessions`` /
  ``failed_sessions_sql`` row as a dict) or an object with attributes (e.g.
  a ``SessionVerdict``) -- carrying the three boolean flags of the landed
  contract: ``process_failed``, ``missing_completion``, ``score_failed``.
  Extra fields are ignored.

  Mechanical assignment until the labeler study: each tripped flag maps to
  its frozen name per ``FLAG_TO_CATEGORY``, and the tripped names are
  returned in ``FROZEN_CATEGORY_NAMES`` order (never flag order). A row can
  trip several (e.g. all three flags gives ``("task/planning",
  "finalization", "tool blockers")``). All three flags false returns
  ``()`` -- never ``("unknown",)``: ``unknown`` is the residual bucket of
  the labeling study, and a session no mechanical gate tripped is simply
  not a mechanical failure.

  Raises:
    ValueError: A flag is absent or not a bool -- silently defaulting a
      missing flag would miscategorize sessions.
  """
  tripped = {
      FLAG_TO_CATEGORY[flag] for flag in MECHANICAL_FLAGS if _flag(row, flag)
  }
  return tuple(name for name in FROZEN_CATEGORY_NAMES if name in tripped)


def _flag(row: Any, name: str) -> bool:
  """Read one required boolean flag from a mapping or attribute row."""
  missing = object()
  if isinstance(row, Mapping):
    value = row.get(name, missing)
  else:
    value = getattr(row, name, missing)
  if value is missing:
    raise ValueError(
        f"failed-session row is missing required flag {name!r}; expected the"
        " process_failed/missing_completion/score_failed contract of"
        " evalbench.classify_sessions / failed_sessions_sql"
    )
  if not isinstance(value, bool):
    raise ValueError(
        f"failed-session flag {name!r} must be a bool, got"
        f" {type(value).__name__}"
    )
  return value
