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

"""Mechanical failure-taxonomy SCAFFOLD over EvalBench failed-session flags.

This is #435 slice 8: the thinnest testable bridge between the failed-session
contract that already landed (#451/#452 -- ``SessionVerdict`` /
``classify_sessions`` / ``failed_sessions_sql`` in ``evalbench.py``) and the
future AgentForensics failure taxonomy. It maps the three mechanical flags a
failed-session row already carries -- ``process_failed``,
``missing_completion``, ``score_failed`` -- to categories in a versioned
config. Nothing here interprets *why* an agent failed; that judgment layer is
the G1 taxonomy study in ``docs/agentforensics_mvp_plan.md`` and has not run.

What this module is NOT:

- It is **not** G1. The category names are scaffold ids derived from the
  flag names, marked ``g1_frozen: False`` in the config; nothing downstream
  may treat them as the frozen taxonomy vocabulary. SANA's seven categories
  stay unfrozen and do not appear here.
- It does **not** start the six-week MVP clock, the partner job, the D4
  boundary, or live-trace ingestion.
- It does **not** classify sessions: ``evalbench.classify_sessions`` remains
  the reference implementation of the flags. This module only maps an
  already-classified row to category ids.

The config uses the #431 ``evaluation_rubrics`` schema shape
(``{"metrics": [{"name", "definition", "categories": [{name, definition}]}]}``)
so ``evaluation_rubrics.build_metrics`` could interpret the core metric later,
and encodes D2 (one taxonomy with dialects) as a ``dialects`` list of optional
per-benchmark *extension categories on the same core* -- empty by default,
never a second taxonomy.

No BigQuery, no LLM, no network: everything here is pure and deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any

# Obviously-scaffold version label. This is NOT taxonomy v0.1 (the G1
# starting point per the plan of record); it must be replaced wholesale when
# the G1 study produces frozen names.
SCAFFOLD_TAXONOMY_VERSION = "0.0.0-scaffold"

# The three mechanical flags of the landed failed-session contract, in the
# order ``failed_sessions_sql`` / ``SessionVerdict`` define them. Category
# ids deliberately equal the flag names: they describe *which gate tripped*,
# not a failure mode, and renaming them here would only invent unfrozen
# vocabulary.
CORE_CATEGORY_IDS = ("process_failed", "missing_completion", "score_failed")

_CORE_CATEGORY_DEFINITIONS = {
    "process_failed": (
        "The session emitted at least one ERROR event (non-zero returncode"
        " or source error fields). Mechanical: read from the"
        " process_failed flag of the failed-session contract."
    ),
    "missing_completion": (
        "The session has no AGENT_COMPLETED event: the agent never produced"
        " a final response. Mechanical: read from the missing_completion"
        " flag of the failed-session contract."
    ),
    "score_failed": (
        "The per-benchmark score policy (EvalScorePolicy) was not met;"
        " missing scores fail by default. returncode == 0 means completed,"
        " never passed. Mechanical: read from the score_failed flag of the"
        " failed-session contract."
    ),
}

_SCAFFOLD_TAXONOMY_CONFIG = {
    # Scaffold bookkeeping (not part of the #431 metric schema, which
    # ignores unknown top-level keys).
    "taxonomy_version": SCAFFOLD_TAXONOMY_VERSION,
    # Nothing in this config is G1-frozen vocabulary. Consumers must check
    # this flag before treating category names as the taxonomy.
    "g1_frozen": False,
    # D2: optional per-benchmark extension categories on the same core (each
    # entry would carry a benchmark name plus #431-shaped categories with
    # optional ``insert_after``). Empty until G1 work fills it; an empty
    # slot is the encoding, not a placeholder to invent labels for.
    "dialects": [],
    # The #431 schema shape (``evaluation_rubrics``): the one core metric a
    # future ``build_metrics()`` caller could interpret.
    "metrics": [
        {
            "name": "failure_category",
            "definition": (
                "Which mechanical gate(s) of the EvalBench failed-session"
                " contract a session tripped. Scaffold only: these are not"
                " failure modes and not the G1 taxonomy."
            ),
            "categories": [
                {"name": name, "definition": _CORE_CATEGORY_DEFINITIONS[name]}
                for name in CORE_CATEGORY_IDS
            ],
        }
    ],
}


def scaffold_taxonomy_config() -> dict:
  """A deep copy of the scaffold taxonomy config (mutate freely).

  Same access pattern as ``evaluation_rubrics.builtin_metric_config``. The
  ``metrics`` entry follows the #431 config schema; ``taxonomy_version``,
  ``g1_frozen`` and ``dialects`` are scaffold/D2 bookkeeping documented in
  the module docstring.
  """
  return copy.deepcopy(_SCAFFOLD_TAXONOMY_CONFIG)


def categorize_failed_session(row: Any) -> tuple[str, ...]:
  """Map one failed-session row to its mechanical scaffold categories.

  Pure and deterministic: no BigQuery, no LLM, no network. ``row`` is one
  failed-session row -- a mapping (e.g. a ``failed_sessions`` /
  ``failed_sessions_sql`` row as a dict) or an object with attributes (e.g.
  a ``SessionVerdict``) -- carrying the three boolean flags of the landed
  contract: ``process_failed``, ``missing_completion``, ``score_failed``.
  Extra fields are ignored.

  Returns the tripped category ids in ``CORE_CATEGORY_IDS`` order; a row can
  trip several (e.g. process_failed AND missing_completion). All three flags
  false returns ``()``: this scaffold does not invent an ``unknown`` bucket,
  because a session no mechanical gate tripped is simply not a mechanical
  failure (the future G1 taxonomy owns residual categories).

  Raises:
    ValueError: A flag is absent or not a bool -- silently defaulting a
      missing flag would miscategorize sessions.
  """
  return tuple(
      category for category in CORE_CATEGORY_IDS if _flag(row, category)
  )


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
