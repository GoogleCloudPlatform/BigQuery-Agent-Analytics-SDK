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

"""MAKO-specific config for the context graph ontology pipeline.

This module is a thin wrapper around
:mod:`ontology_artifacts` that fixes the per-ontology
configuration to MAKO. The generic pipeline takes any
:class:`ontology_artifacts.OntologyConfig`; this file packages
the MAKO-specific bits (TTL path, namespace IRI, entity
allowlist, annotation prefix, graph name) and re-exports the
public functions with that config bound in.

The MAKO TTL is the canonical reference example because it's
a real production ontology with all the OWL TTL quirks the
generic pipeline normalizes through:

* ``owl:hasKey`` undeclared on most entities → ``FILL_IN``
  primary keys the resolver synthesizes to ``id``.
* Cross-namespace relationships into PROV-O / PKO / DCAT
  that the importer drops with an audit trail.
* ``rdfs:subClassOf`` inheritance that the v0 ``gm compile``
  doesn't support; the pipeline strips ``extends`` and
  records the loss.

See :mod:`example_ontologies.simple_request_flow_config` for
a tiny second config that exercises the same pipeline with a
simpler TTL — useful as a smoke test that the pipeline is
genuinely ontology-agnostic.

The MAKO demo's runnable agent (``mako_demo_agent.py``) and
event-population driver (``run_agent.py``) remain
MAKO-specific by design — the agent's tools mirror MAKO's
decision flow. Only the artifact pipeline is generalized.
"""

from __future__ import annotations

import json
import pathlib
from typing import Iterable, Optional

from ontology_artifacts import load_ontology as _load_ontology
from ontology_artifacts import make_binding as _make_binding
from ontology_artifacts import make_property_graph_sql as _make_property_graph_sql
from ontology_artifacts import make_table_ddl as _make_table_ddl
from ontology_artifacts import OntologyConfig
from ontology_artifacts import regenerate_snapshots as _regenerate_snapshots

from bigquery_ontology import Binding
from bigquery_ontology import Ontology

_FIXTURE_DIR = pathlib.Path(__file__).parent

# Authored-input path for the MAKO TTL.
TTL_PATH = _FIXTURE_DIR / "mako_core.ttl"

# MAKO namespace — passed to ``import_owl`` so we only pull
# entities under that IRI prefix (not the imported PROV-O /
# PKO / etc. classes).
_MAKO_NAMESPACE = "https://ontology.yahoo.com/mako/"

# Demo-focused entity allowlist. The full imported
# ``ontology.yaml`` contains the 18 MAKO-namespace entities;
# the binding scope is narrower so the notebook's narrative
# arc stays focused.
#
# Why these eleven, in two groups:
#
# * **Beats 1–4 hub** (six entities). ``DecisionExecution``
#   is the central node that ties everything together — per
#   MAKO's TTL, it's ``partOfSession`` an AgentSession,
#   ``atContextSnapshot`` a ContextSnapshot,
#   ``executedAtDecisionPoint`` a DecisionPoint, and
#   ``hasSelectionOutcome`` a SelectionOutcome. Drop any
#   one and the decision-flow story doesn't hold together.
# * **Beat 5 feedback / reward loop** (five entities). What
#   happened *after* the decision: ``BusinessConstraint`` +
#   ``ConstraintApplication`` capture policy evaluations,
#   ``RejectionReason`` records why each losing candidate
#   lost, ``OutcomeSignal`` carries observed real-world
#   results (clicks / conversions / viewability),
#   ``RewardComputation`` aggregates those signals into the
#   scalar RL reward via ``derivedReward`` edges.
DEMO_ENTITIES: tuple[str, ...] = (
    # Beats 1–4: the decision-flow hub + its six immediate
    # neighbors. ``DecisionExecution`` is the central hub the
    # rest hangs off of.
    "AgentSession",
    "DecisionExecution",
    "DecisionPoint",
    "Candidate",
    "SelectionOutcome",
    "ContextSnapshot",
    # Beat 5: feedback / reward loop. ``BusinessConstraint`` +
    # ``ConstraintApplication`` capture the "why did this
    # candidate get filtered?" audit trail;
    # ``RejectionReason`` records the "why did this candidate
    # lose?" explanation; ``OutcomeSignal`` carries observed
    # real-world results (click / conversion / viewability)
    # linked back to a DecisionExecution via
    # ``producedOutcome``; ``RewardComputation`` aggregates
    # OutcomeSignals into a scalar reward for RL training and
    # links back to the contributing signals via
    # ``derivedReward``.
    "BusinessConstraint",
    "ConstraintApplication",
    "RejectionReason",
    "OutcomeSignal",
    "RewardComputation",
)


# MAKO_CONFIG packages the MAKO-specific bits. The generic
# pipeline in :mod:`ontology_artifacts` accepts any
# ``OntologyConfig``; this is one such config.
MAKO_CONFIG = OntologyConfig(
    ttl_path=TTL_PATH,
    include_namespace=_MAKO_NAMESPACE,
    entity_allowlist=DEMO_ENTITIES,
    annotation_prefix="mako_demo",
    graph_name="mako_demo_graph",
    snapshot_dir=_FIXTURE_DIR,
)

# Snapshot-output paths (back-compat — derived from MAKO_CONFIG).
ONTOLOGY_PATH = MAKO_CONFIG.ontology_path
BINDING_PATH = MAKO_CONFIG.binding_path
TABLE_DDL_PATH = MAKO_CONFIG.table_ddl_path
PROPERTY_GRAPH_PATH = MAKO_CONFIG.property_graph_path


def load_mako_ontology() -> tuple[Ontology, str]:
  """Import the MAKO TTL and resolve FILL_IN primary keys.

  Thin wrapper around
  :func:`ontology_artifacts.load_ontology` with
  :data:`MAKO_CONFIG`.
  """
  return _load_ontology(MAKO_CONFIG)


def make_binding(
    ontology: Ontology,
    *,
    project: str,
    dataset: str,
    entity_filter: Optional[Iterable[str]] = None,
) -> Binding:
  """Construct a MAKO-scoped ``Binding`` for the given target.

  Thin wrapper around :func:`ontology_artifacts.make_binding`
  with :data:`MAKO_CONFIG`. ``entity_filter`` defaults to
  :data:`DEMO_ENTITIES` (MAKO's 11-entity demo scope — six
  Beat 1–4 hub entities plus five Beat 5 feedback / reward
  loop entities).
  """
  return _make_binding(
      ontology,
      MAKO_CONFIG,
      project=project,
      dataset=dataset,
      entity_filter=entity_filter,
  )


def make_table_ddl(binding: Binding, *, ontology: Ontology) -> str:
  """Thin wrapper around :func:`ontology_artifacts.make_table_ddl`."""
  return _make_table_ddl(binding, ontology=ontology)


def make_property_graph_sql(
    binding: Binding,
    *,
    ontology: Ontology,
    graph_name: str = "mako_demo_graph",
) -> str:
  """Thin wrapper around
  :func:`ontology_artifacts.make_property_graph_sql`.
  Defaults ``graph_name`` to MAKO's ``mako_demo_graph``.
  """
  return _make_property_graph_sql(
      binding, ontology=ontology, graph_name=graph_name
  )


def regenerate_snapshots(
    *,
    project: str = "test-project-0728-467323",
    dataset: str = "context_graph",
) -> dict:
  """Regenerate the MAKO demo's TTL-derived artifact snapshots.

  Idempotent: byte-identical output across runs for the same
  ``(project, dataset)`` pair. Returns a small summary dict
  for the notebook's setup cell to display.

  Does NOT produce events — events come from running
  ``mako_demo_agent.py`` against this same
  ``(project, dataset)`` with the BQ AA plugin enabled.
  """
  return _regenerate_snapshots(MAKO_CONFIG, project=project, dataset=dataset)


if __name__ == "__main__":  # pragma: no cover
  import argparse

  parser = argparse.ArgumentParser(
      description=(
          "Regenerate the context graph demo snapshot files "
          "from the authored mako_core.ttl input."
      ),
  )
  parser.add_argument("--project", default="test-project-0728-467323")
  parser.add_argument("--dataset", default="context_graph")
  args = parser.parse_args()
  summary = regenerate_snapshots(project=args.project, dataset=args.dataset)
  print(json.dumps(summary, indent=2, sort_keys=True))
