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

"""Unit tests for graph_validation.validate_extracted_graph (#76).

Coverage:
- One positive + one negative case per failure code (11 codes
  across NODE/FIELD/EDGE scope).
- Adapter validate_extracted_graph_from_ontology smoke test.
- Type acceptor edge cases (bool != int64 / != double, naive vs
  tz-aware datetime).
- Regression: extract_bka_decision_event's output validates clean.
"""

from __future__ import annotations

import datetime
import pathlib
import tempfile

import pytest

# ------------------------------------------------------------------ #
# Spec + extracted-graph helpers                                       #
# ------------------------------------------------------------------ #


def _ontology_yaml() -> str:
  return (
      "ontology: TestGraph\n"
      "entities:\n"
      "  - name: Decision\n"
      "    keys:\n"
      "      primary: [decision_id]\n"
      "    properties:\n"
      "      - name: decision_id\n"
      "        type: string\n"
      "      - name: confidence\n"
      "        type: double\n"
      "      - name: occurred_at\n"
      "        type: timestamp\n"
      "  - name: Outcome\n"
      "    keys:\n"
      "      primary: [outcome_id]\n"
      "    properties:\n"
      "      - name: outcome_id\n"
      "        type: string\n"
      "relationships:\n"
      "  - name: HasOutcome\n"
      "    from: Decision\n"
      "    to: Outcome\n"
      "    properties:\n"
      "      - name: weight\n"
      "        type: double\n"
  )


def _binding_yaml(project: str = "p", dataset: str = "d") -> str:
  return (
      "binding: test_bind\n"
      "ontology: TestGraph\n"
      "target:\n"
      "  backend: bigquery\n"
      f"  project: {project}\n"
      f"  dataset: {dataset}\n"
      "entities:\n"
      "  - name: Decision\n"
      "    source: decisions\n"
      "    properties:\n"
      "      - name: decision_id\n"
      "        column: decision_id\n"
      "      - name: confidence\n"
      "        column: confidence\n"
      "      - name: occurred_at\n"
      "        column: occurred_at\n"
      "  - name: Outcome\n"
      "    source: outcomes\n"
      "    properties:\n"
      "      - name: outcome_id\n"
      "        column: outcome_id\n"
      "relationships:\n"
      "  - name: HasOutcome\n"
      "    source: edges\n"
      "    from_columns: [decision_id]\n"
      "    to_columns: [outcome_id]\n"
      "    properties:\n"
      "      - name: weight\n"
      "        column: weight\n"
  )


def _resolved_spec():
  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="graph_validation_"))
  ont_path = tmp / "ontology.yaml"
  bnd_path = tmp / "binding.yaml"
  ont_path.write_text(_ontology_yaml(), encoding="utf-8")
  bnd_path.write_text(_binding_yaml(), encoding="utf-8")

  ontology = load_ontology(str(ont_path))
  binding = load_binding(str(bnd_path), ontology=ontology)
  return resolve(ontology, binding), ontology, binding


def _node(node_id: str, entity: str, **props):
  from bigquery_agent_analytics.extracted_models import ExtractedNode
  from bigquery_agent_analytics.extracted_models import ExtractedProperty

  return ExtractedNode(
      node_id=node_id,
      entity_name=entity,
      labels=[entity],
      properties=[ExtractedProperty(name=k, value=v) for k, v in props.items()],
  )


def _edge(edge_id: str, rel: str, frm: str, to: str, **props):
  from bigquery_agent_analytics.extracted_models import ExtractedEdge
  from bigquery_agent_analytics.extracted_models import ExtractedProperty

  return ExtractedEdge(
      edge_id=edge_id,
      relationship_name=rel,
      from_node_id=frm,
      to_node_id=to,
      properties=[ExtractedProperty(name=k, value=v) for k, v in props.items()],
  )


def _graph(nodes=None, edges=None):
  from bigquery_agent_analytics.extracted_models import ExtractedGraph

  return ExtractedGraph(
      name="TestGraph",
      nodes=list(nodes or []),
      edges=list(edges or []),
  )


# ------------------------------------------------------------------ #
# Clean baseline                                                       #
# ------------------------------------------------------------------ #


class TestCleanBaseline:

  def test_well_formed_graph_validates_clean(self):
    """Clean baseline uses key-segment node_ids in the format the
    materializer's ``_parse_key_segment`` parses:
    ``{session}:{entity}:k1=v1,k2=v2``. Validates clean end-to-end.
    """
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    d1_id = "sess1:Decision:decision_id=d1"
    o1_id = "sess1:Outcome:outcome_id=o1"
    graph = _graph(
        nodes=[
            _node(d1_id, "Decision", decision_id="d1", confidence=0.9),
            _node(o1_id, "Outcome", outcome_id="o1"),
        ],
        edges=[
            _edge("d1->o1", "HasOutcome", d1_id, o1_id, weight=1.0),
        ],
    )

    report = validate_extracted_graph(spec, graph)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"
    assert report.failures == ()


# ------------------------------------------------------------------ #
# NODE-scope codes                                                     #
# ------------------------------------------------------------------ #


class TestNodeScopeCodes:

  def test_unknown_entity(self):
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(nodes=[_node("x1", "NotADeclaredEntity", decision_id="x1")])

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "unknown_entity"]
    assert len(failures) == 1
    assert failures[0].scope is FallbackScope.NODE
    assert failures[0].observed == "NotADeclaredEntity"

  def test_missing_node_id(self):
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    # Bypass validation by constructing with empty node_id.
    bad = ExtractedNode(
        node_id="",
        entity_name="Decision",
        labels=["Decision"],
        properties=[ExtractedProperty(name="decision_id", value="d1")],
    )
    graph = _graph(nodes=[bad])

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "missing_node_id"]
    assert len(failures) == 1
    assert failures[0].scope is FallbackScope.NODE

  def test_duplicate_node_id(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node("d1", "Decision", decision_id="d1"),
            _node("d1", "Decision", decision_id="d1"),  # dup
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "duplicate_node_id"]
    assert len(failures) == 1
    assert failures[0].observed == "d1"

  def test_duplicate_node_id_detected_on_unknown_entity_nodes(self):
    """Duplicate-detection runs at the graph level, before the
    per-node entity-specific checks. Two nodes with the same
    node_id but unknown entity_name must still trigger
    duplicate_node_id (alongside the unknown_entity failures).
    Earlier behavior set up nodes_by_id via setdefault() and skipped
    the duplicate path entirely for unknown-entity nodes."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node("ghost", "NotADeclaredEntity", decision_id="g"),
            _node("ghost", "NotADeclaredEntity", decision_id="g"),
        ]
    )

    report = validate_extracted_graph(spec, graph)
    dup = [f for f in report.failures if f.code == "duplicate_node_id"]
    unk = [f for f in report.failures if f.code == "unknown_entity"]
    assert len(dup) == 1
    assert len(unk) == 2  # both nodes are unknown entities

  def test_missing_key(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    # decision_id key column is absent on the extracted node.
    graph = _graph(nodes=[_node("d1", "Decision", confidence=0.9)])

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "missing_key"]
    assert len(failures) == 1
    assert failures[0].expected == "decision_id"

  def test_missing_key_when_value_is_empty_string(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(nodes=[_node("d1", "Decision", decision_id="")])

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "missing_key"]
    assert len(failures) == 1


# ------------------------------------------------------------------ #
# FIELD-scope codes                                                    #
# ------------------------------------------------------------------ #


class TestFieldScopeCodes:

  def test_unknown_property(self):
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                no_such_property="hello",
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "unknown_property"]
    assert len(failures) == 1
    assert failures[0].scope is FallbackScope.FIELD
    assert failures[0].observed == "no_such_property"

  def test_type_mismatch_string_value_on_double_property(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                confidence="not-a-number",  # should be double
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(failures) == 1
    assert failures[0].expected == "double"

  def test_unsupported_type_list_on_scalar_property(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                confidence=[0.1, 0.2, 0.3],  # list on scalar
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "unsupported_type"]
    assert len(failures) == 1

  def test_unsupported_type_dict_on_scalar_property(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                confidence={"value": 0.9},
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "unsupported_type"]
    assert len(failures) == 1

  def test_bool_rejected_for_int64_and_double(self):
    """bool is a subclass of int but must be rejected for int64
    and double sdk_types per the issue body."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node("d1", "Decision", decision_id="d1", confidence=True),
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(failures) == 1

  def test_naive_datetime_rejected_for_timestamp(self):
    """timestamp expects tz-aware datetime."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    naive = datetime.datetime(2026, 5, 4, 12, 0, 0)
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                occurred_at=naive,
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(failures) == 1

  def test_tz_aware_datetime_accepted_for_timestamp(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    aware = datetime.datetime(
        2026, 5, 4, 12, 0, 0, tzinfo=datetime.timezone.utc
    )
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                occurred_at=aware,
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    assert report.ok is True

  def test_iso_string_accepted_for_timestamp(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                occurred_at="2026-05-04T12:00:00Z",
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    assert report.ok is True


# ------------------------------------------------------------------ #
# EDGE-scope codes                                                     #
# ------------------------------------------------------------------ #


class TestEdgeScopeCodes:

  # Key-segment IDs used across this class. Match the materializer's
  # _parse_key_segment expectation: {session}:{entity}:k=v[,...].
  _D1 = "sess1:Decision:decision_id=d1"
  _O1 = "sess1:Outcome:outcome_id=o1"
  _WRONG_AS_DECISION = "sess1:Decision:decision_id=wrong"

  def test_unknown_relationship(self):
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(self._D1, "Decision", decision_id="d1"),
            _node(self._O1, "Outcome", outcome_id="o1"),
        ],
        edges=[
            _edge("e1", "NotADeclaredRel", self._D1, self._O1),
        ],
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "unknown_relationship"]
    assert len(failures) == 1
    assert failures[0].scope is FallbackScope.EDGE

  def test_unresolved_endpoint(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    ghost = "sess1:Outcome:outcome_id=ghost"
    graph = _graph(
        nodes=[_node(self._D1, "Decision", decision_id="d1")],
        edges=[
            _edge(
                "e1",
                "HasOutcome",
                self._D1,
                ghost,  # parses, but not in nodes
            )
        ],
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "unresolved_endpoint"]
    assert len(failures) == 1
    assert failures[0].observed == ghost

  def test_wrong_endpoint_entity(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(self._D1, "Decision", decision_id="d1"),
            _node(
                self._WRONG_AS_DECISION,
                "Decision",
                decision_id="wrong",
            ),  # should be Outcome
        ],
        edges=[
            _edge(
                "e1",
                "HasOutcome",
                self._D1,
                self._WRONG_AS_DECISION,
            )
        ],
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "wrong_endpoint_entity"]
    assert len(failures) == 1
    assert failures[0].observed == "Decision"
    assert failures[0].expected == "Outcome"

  def test_missing_endpoint_key_short_node_id(self):
    """node_id 'd1' / 'o1' don't match the materializer's
    _parse_key_segment format ({session}:{entity}:k=v); the
    materializer would silently produce empty FK columns at INSERT
    time. The validator must catch this — earlier behavior that
    only checked the endpoint node's properties would miss it."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node("d1", "Decision", decision_id="d1"),
            _node("o1", "Outcome", outcome_id="o1"),
        ],
        edges=[_edge("e1", "HasOutcome", "d1", "o1")],
    )

    report = validate_extracted_graph(spec, graph)
    endpoint_failures = [
        f for f in report.failures if f.code == "missing_endpoint_key"
    ]
    # One per missing endpoint key column on each side of the edge.
    assert len(endpoint_failures) == 2
    expected_cols = {f.expected for f in endpoint_failures}
    assert expected_cols == {"decision_id", "outcome_id"}

  def test_missing_endpoint_key_segment_missing_column(self):
    """Even with the right format, if a column is missing from the
    parsed segment, the validator must flag it."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    # Build a node_id that parses but doesn't include outcome_id.
    bad_outcome = "sess1:Outcome:something_else=foo"
    graph = _graph(
        nodes=[
            _node(self._D1, "Decision", decision_id="d1"),
            _node(bad_outcome, "Outcome", outcome_id="o1"),
        ],
        edges=[_edge("e1", "HasOutcome", self._D1, bad_outcome)],
    )

    report = validate_extracted_graph(spec, graph)
    endpoint_failures = [
        f for f in report.failures if f.code == "missing_endpoint_key"
    ]
    assert any(f.expected == "outcome_id" for f in endpoint_failures)


# ------------------------------------------------------------------ #
# Adapter                                                              #
# ------------------------------------------------------------------ #


class TestRenamedColumnRoundTrip:
  """Regression: when the binding renames an ontology property to a
  different physical column, an extractor emitting the *logical*
  name must (a) validate clean and (b) materialize through to the
  renamed physical column at INSERT time. Earlier behavior had the
  validator accept the logical name but the materializer drop it
  silently."""

  def _renamed_spec(self):
    """Spec where Decision.confidence (ontology) → conf_score
    (binding column)."""
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    ont_yaml = (
        "ontology: RenameTest\n"
        "entities:\n"
        "  - name: Decision\n"
        "    keys:\n"
        "      primary: [decision_id]\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        type: string\n"
        "      - name: confidence\n"
        "        type: double\n"
        "relationships: []\n"
    )
    bnd_yaml = (
        "binding: rename_test\n"
        "ontology: RenameTest\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: p\n"
        "  dataset: d\n"
        "entities:\n"
        "  - name: Decision\n"
        "    source: decisions\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        column: decision_id\n"
        "      - name: confidence\n"
        "        column: conf_score\n"  # renamed
        "relationships: []\n"
    )
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rename_test_"))
    (tmp / "ont.yaml").write_text(ont_yaml, encoding="utf-8")
    (tmp / "bnd.yaml").write_text(bnd_yaml, encoding="utf-8")
    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
    return resolve(ontology, binding)

  def test_logical_name_validates_and_materializes(self):
    """Extractor emits 'confidence' (logical name); spec has
    column='conf_score'. Validator says ok; materializer routes to
    the physical column. Without the materializer fix, this would
    silently drop the value at INSERT."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph
    from bigquery_agent_analytics.ontology_materializer import _route_node

    spec = self._renamed_spec()
    decision_entity = next(e for e in spec.entities if e.name == "Decision")
    node_id = "sess1:Decision:decision_id=d1"
    node = _node("dummy", "Decision", decision_id="d1", confidence=0.9)
    # Replace node_id manually since _node helper takes a node_id arg.
    from bigquery_agent_analytics.extracted_models import ExtractedNode

    node = ExtractedNode(
        node_id=node_id,
        entity_name="Decision",
        labels=["Decision"],
        properties=node.properties,
    )
    graph = _graph(nodes=[node])

    # (a) Validator accepts logical name.
    report = validate_extracted_graph(spec, graph)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

    # (b) Materializer routes 'confidence' → 'conf_score'.
    row = _route_node(node, decision_entity, session_id="sess1")
    assert (
        row.get("conf_score") == 0.9
    ), f"materializer dropped logical-name property; row={row!r}"
    # The logical name must NOT appear as a column in the row —
    # extractor emits it but the materializer maps to physical.
    assert "confidence" not in row

  def test_physical_column_name_also_works(self):
    """Extractors emitting the physical column name directly
    continue to work (backward-compat)."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph
    from bigquery_agent_analytics.ontology_materializer import _route_node

    spec = self._renamed_spec()
    decision_entity = next(e for e in spec.entities if e.name == "Decision")
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty

    node = ExtractedNode(
        node_id="sess1:Decision:decision_id=d1",
        entity_name="Decision",
        labels=["Decision"],
        properties=[
            ExtractedProperty(name="decision_id", value="d1"),
            ExtractedProperty(name="conf_score", value=0.9),  # physical
        ],
    )
    graph = _graph(nodes=[node])

    report = validate_extracted_graph(spec, graph)
    assert report.ok is True

    row = _route_node(node, decision_entity, session_id="sess1")
    assert row.get("conf_score") == 0.9


class TestOntologyAdapter:

  def test_adapter_delegates_to_resolve(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph_from_ontology

    _, ontology, binding = _resolved_spec()
    graph = _graph(
        nodes=[_node("d1", "Decision", decision_id="d1", confidence=0.9)]
    )

    report = validate_extracted_graph_from_ontology(ontology, binding, graph)
    assert report.ok is True


# ------------------------------------------------------------------ #
# Report shape                                                         #
# ------------------------------------------------------------------ #


class TestReportShape:

  def test_by_scope_filter(self):
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    # Mix one NODE-scope + one FIELD-scope failure.
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                confidence="not-a-number",  # FIELD: type_mismatch
                spurious="x",  # FIELD: unknown_property
            ),
            _node("d2", "Decision"),  # NODE: missing_key
        ]
    )

    report = validate_extracted_graph(spec, graph)
    field_only = report.by_scope(FallbackScope.FIELD)
    node_only = report.by_scope(FallbackScope.NODE)
    assert all(f.scope is FallbackScope.FIELD for f in field_only)
    assert all(f.scope is FallbackScope.NODE for f in node_only)
    assert len(field_only) >= 2
    assert len(node_only) >= 1

  def test_ok_property(self):
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport

    empty = ValidationReport()
    assert empty.ok is True

    not_ok = ValidationReport(
        failures=(
            ValidationFailure(
                scope=FallbackScope.NODE,
                code="unknown_entity",
                path="nodes[0].entity_name",
            ),
        ),
    )
    assert not_ok.ok is False


# ------------------------------------------------------------------ #
# Regression: extract_bka_decision_event output validates clean        #
# ------------------------------------------------------------------ #


class TestBkaDecisionEventRegression:

  def test_bka_extractor_output_validates_clean(self):
    """extract_bka_decision_event's current output must validate
    clean against its declared entity. The validator must not
    accidentally break existing hand-written extractor code per
    the issue's success criteria."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    # Build a spec containing the entity the extractor produces.
    bka_ontology = (
        "ontology: BkaTest\n"
        "entities:\n"
        "  - name: mako_DecisionPoint\n"
        "    keys:\n"
        "      primary: [decision_id]\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        type: string\n"
        "      - name: outcome\n"
        "        type: string\n"
        "      - name: confidence\n"
        "        type: double\n"
        "      - name: alternatives_considered\n"
        "        type: string\n"
        "relationships: []\n"
    )
    bka_binding = (
        "binding: bka_test\n"
        "ontology: BkaTest\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: p\n"
        "  dataset: d\n"
        "entities:\n"
        "  - name: mako_DecisionPoint\n"
        "    source: decision_points\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        column: decision_id\n"
        "      - name: outcome\n"
        "        column: outcome\n"
        "      - name: confidence\n"
        "        column: confidence\n"
        "      - name: alternatives_considered\n"
        "        column: alternatives_considered\n"
        "relationships: []\n"
    )
    from bigquery_agent_analytics.extracted_models import ExtractedGraph
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_test_"))
    (tmp / "ont.yaml").write_text(bka_ontology, encoding="utf-8")
    (tmp / "bnd.yaml").write_text(bka_binding, encoding="utf-8")
    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
    spec = resolve(ontology, binding)

    # Run the extractor against a representative event.
    event = {
        "session_id": "sess-1",
        "span_id": "span-1",
        "event_type": "bka_decision",
        "content": {
            "decision_id": "d1",
            "outcome": "approved",
            "confidence": 0.92,
        },
    }
    result = extract_bka_decision_event(event, spec=None)
    assert len(result.nodes) == 1

    graph = ExtractedGraph(name="BkaTest", nodes=result.nodes, edges=[])

    report = validate_extracted_graph(spec, graph)
    assert report.ok is True, (
        "extract_bka_decision_event output must validate clean. "
        f"Failures: {[(f.code, f.detail) for f in report.failures]}"
    )
