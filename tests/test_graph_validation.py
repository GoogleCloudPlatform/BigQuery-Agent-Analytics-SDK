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

  def test_key_mismatch_between_node_id_and_property(self):
    """The materializer writes node table primary keys from
    ``node.properties`` but writes edge FK columns from
    ``parse_key_segment(node_id)``. If the two disagree, edges
    point at non-existent rows. The validator catches this."""
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    # node_id segment says decision_id=d1; property says d2.
    graph = _graph(
        nodes=[
            _node("sess1:Decision:decision_id=d1", "Decision", decision_id="d2")
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "key_mismatch"]
    assert len(failures) == 1
    assert failures[0].scope is FallbackScope.NODE
    assert failures[0].expected == "d2"
    assert failures[0].observed == "d1"

  def test_key_mismatch_catches_unescaped_comma_in_value(self):
    """``_build_key_string`` is unescaped, so a property value
    containing ',' truncates at the comma when re-parsed by
    ``parse_key_segment``. Comparing parsed-vs-extracted catches
    that silent corruption before materialization."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    # If a hand-built node_id encodes the comma raw and the property
    # value carries the same comma-containing string, parse_key_segment
    # truncates at ',' so parsed != property.
    graph = _graph(
        nodes=[
            _node(
                "sess1:Decision:decision_id=a,b", "Decision", decision_id="a,b"
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "key_mismatch"]
    assert len(failures) == 1
    assert failures[0].observed == "a"  # truncated at ','
    assert failures[0].expected == "a,b"

  def test_key_mismatch_does_not_fire_when_node_id_lacks_key_segment(self):
    """Short-form node-ids like 'd1' produce no parseable keys —
    that's the materializer's index-fallback path. ``key_mismatch``
    should only fire when the parsed segment actually carries the
    column; otherwise other codes (missing_endpoint_key on the edge
    side) cover it."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(nodes=[_node("d1", "Decision", decision_id="d1")])

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "key_mismatch"]
    assert failures == []

  def _renamed_key_spec(self):
    """Spec where Decision's primary-key logical name 'decisionId'
    maps to the physical column 'decision_id' via the binding. Used
    for tests that exercise the validator's matching of the
    materializer's name→column routing for key columns."""
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    ont_yaml = (
        "ontology: RenamedKeyTest\n"
        "entities:\n"
        "  - name: Decision\n"
        "    keys:\n"
        "      primary: [decisionId]\n"
        "    properties:\n"
        "      - name: decisionId\n"
        "        type: string\n"
        "relationships: []\n"
    )
    bnd_yaml = (
        "binding: renamed_key_test\n"
        "ontology: RenamedKeyTest\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: p\n"
        "  dataset: d\n"
        "entities:\n"
        "  - name: Decision\n"
        "    source: decisions\n"
        "    properties:\n"
        "      - name: decisionId\n"
        "        column: decision_id\n"  # renamed: logical decisionId → physical decision_id
        "relationships: []\n"
    )
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="renamed_key_test_"))
    (tmp / "ont.yaml").write_text(ont_yaml, encoding="utf-8")
    (tmp / "bnd.yaml").write_text(bnd_yaml, encoding="utf-8")
    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
    return resolve(ontology, binding)

  def test_key_mismatch_catches_duplicate_property_names_with_conflicting_values(
      self,
  ):
    """When a key column is set by *two* extracted properties — one
    under the logical name, one under the physical column — both
    route to the same physical column. The materializer iterates
    properties in extraction order, last-wins; if their values
    disagree it silently picks one. The validator must surface
    this conflict directly."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec = self._renamed_key_spec()
    node = ExtractedNode(
        node_id="sess1:Decision:decision_id=d2",
        entity_name="Decision",
        labels=["Decision"],
        properties=[
            ExtractedProperty(name="decision_id", value="d2"),
            ExtractedProperty(name="decisionId", value="d1"),
        ],
    )
    graph = _graph(nodes=[node])

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "key_mismatch"]
    assert len(failures) == 1
    # The detail mentions both property names so the extractor can
    # find and fix the duplicate.
    assert "decision_id" in failures[0].detail
    assert "decisionId" in failures[0].detail

  def test_key_mismatch_compares_against_materializer_routed_value(self):
    """When two properties route to the same key column with the
    *same* value, no conflict — but the materializer-effective
    value is what gets compared against the parsed node-id segment.
    Here both properties say d1, node_id says d2: key_mismatch
    fires once, with expected=d1 (the routed value)."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec = self._renamed_key_spec()
    node = ExtractedNode(
        node_id="sess1:Decision:decision_id=d2",
        entity_name="Decision",
        labels=["Decision"],
        properties=[
            ExtractedProperty(name="decision_id", value="d1"),
            ExtractedProperty(name="decisionId", value="d1"),
        ],
    )
    graph = _graph(nodes=[node])

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "key_mismatch"]
    assert len(failures) == 1
    assert failures[0].expected == "d1"
    assert failures[0].observed == "d2"

  def test_key_mismatch_catches_node_id_entity_segment_disagreement(self):
    """The node_id's entity segment must match ``ExtractedNode.entity_name``.
    An in-graph edge would resolve through ``entity_name`` and pass
    even if the segment lied; the same id seen from a lineage-only
    batch would fail the permissive-mode entity-segment check. The
    validator catches the disagreement at the node so both code
    paths agree on what the id means."""
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    # node_id segment says Outcome but entity_name says Decision.
    graph = _graph(
        nodes=[
            _node("sess1:Outcome:decision_id=d1", "Decision", decision_id="d1")
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [
        f
        for f in report.failures
        if f.code == "key_mismatch" and f.path.endswith(".node_id")
    ]
    assert len(failures) == 1
    assert failures[0].scope is FallbackScope.NODE
    assert failures[0].observed == "Outcome"
    assert failures[0].expected == "Decision"

  def test_key_mismatch_catches_empty_node_id_entity_segment(self):
    """Companion to the permissive-endpoint empty-entity test: an
    empty entity segment (``sess1::decision_id=d1``) is a 3-part
    node_id whose ``observed_entity`` is ``""``. The previous
    truthiness guard skipped this case; the same id from an
    external endpoint would have failed, leaving the two paths
    inconsistent. Both paths now emit ``key_mismatch`` /
    ``wrong_endpoint_entity`` on empty entity segments."""
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[_node("sess1::decision_id=d1", "Decision", decision_id="d1")]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [
        f
        for f in report.failures
        if f.code == "key_mismatch" and f.path.endswith(".node_id")
    ]
    assert len(failures) == 1
    assert failures[0].scope is FallbackScope.NODE
    assert failures[0].observed == ""
    assert failures[0].expected == "Decision"

  def test_parse_key_segment_preserves_colon_in_value(self):
    """``parse_key_segment`` must split the node_id on ``:`` at
    most twice so a primary-key value containing a literal ``:``
    survives the parse. Otherwise ``parts[-1]`` is the trailing
    fragment and the whole segment fails the ``=`` check."""
    from bigquery_agent_analytics._ontology_routing import parse_key_segment

    # Single key value with embedded ':'.
    assert parse_key_segment("sess1:Decision:decision_id=a:b") == {
        "decision_id": "a:b"
    }
    # Compound key segment, value contains ':'.
    assert parse_key_segment("sess1:Decision:decision_id=a:b,other=c") == {
        "decision_id": "a:b",
        "other": "c",
    }
    # Short-form fallback id still parses to {} (untouched).
    assert parse_key_segment("d1") == {}
    # Plain key still works.
    assert parse_key_segment("sess1:Decision:decision_id=d1") == {
        "decision_id": "d1"
    }

  def test_key_mismatch_does_not_fire_for_colon_in_value(self):
    """Companion to :func:`test_parse_key_segment_preserves_colon_in_value`:
    a node whose primary-key property value contains ``:`` must
    validate clean against a node_id that encodes the same value
    raw. Without the ``maxsplit=2`` fix the parsed segment would be
    ``{}`` and ``key_mismatch`` wouldn't fire here, but downstream
    edges would crash with ``missing_endpoint_key`` / corrupt FKs."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "sess1:Decision:decision_id=a:b", "Decision", decision_id="a:b"
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "key_mismatch"]
    assert failures == []

  def test_key_mismatch_does_not_fire_for_equals_in_value(self):
    """``parse_key_segment`` splits each ``k=v`` pair on the *first*
    ``=``, so a value like ``a=b`` round-trips cleanly through
    ``_build_key_string`` → ``parse_key_segment``. The validator
    must not falsely flag this as key_mismatch — only commas
    truncate."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "sess1:Decision:decision_id=a=b", "Decision", decision_id="a=b"
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    failures = [f for f in report.failures if f.code == "key_mismatch"]
    assert failures == []


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

  def test_logical_name_wins_on_column_collision(self):
    """Edge case: one property's ``column`` happens to equal another
    property's ``logical_name``. Validator and materializer must
    pick the same property — logical-name lookup wins on collision
    per the shared two-pass build_property_lookup. Without the
    shared helper, the materializer's earlier single-loop build
    would let property order decide the winner; this test asserts
    the validator and materializer agree.

    Setup: prop A has logical_name='x' / column='x_col'.
            prop B has logical_name='y' / column='x' (collides).
    An extracted property with name='x' must route to prop A, not
    prop B, in both validator and materializer.
    """
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph
    from bigquery_agent_analytics.ontology_materializer import _route_node
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    ont_yaml = (
        "ontology: CollisionTest\n"
        "entities:\n"
        "  - name: Thing\n"
        "    keys:\n"
        "      primary: [thing_id]\n"
        "    properties:\n"
        "      - name: thing_id\n"
        "        type: string\n"
        # Property A: logical_name='x', column='x_col'.
        "      - name: x\n"
        "        type: string\n"
        # Property B: logical_name='y', column='x' — collides
        # with property A's logical_name.
        "      - name: y\n"
        "        type: string\n"
        "relationships: []\n"
    )
    bnd_yaml = (
        "binding: collision\n"
        "ontology: CollisionTest\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: p\n"
        "  dataset: d\n"
        "entities:\n"
        "  - name: Thing\n"
        "    source: things\n"
        "    properties:\n"
        "      - name: thing_id\n"
        "        column: thing_id\n"
        "      - name: x\n"
        "        column: x_col\n"  # A: logical='x', column='x_col'
        "      - name: y\n"
        "        column: x\n"  # B: logical='y', column='x' (collides)
        "relationships: []\n"
    )
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="collision_"))
    (tmp / "ont.yaml").write_text(ont_yaml, encoding="utf-8")
    (tmp / "bnd.yaml").write_text(bnd_yaml, encoding="utf-8")
    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
    spec = resolve(ontology, binding)
    thing_entity = next(e for e in spec.entities if e.name == "Thing")

    # Extractor emits name='x'. Logical-name 'x' belongs to
    # property A (column='x_col'). Both validator and materializer
    # must resolve to A.
    node = ExtractedNode(
        node_id="sess1:Thing:thing_id=t1",
        entity_name="Thing",
        labels=["Thing"],
        properties=[
            ExtractedProperty(name="thing_id", value="t1"),
            ExtractedProperty(name="x", value="value-from-A"),
        ],
    )
    graph = _graph(nodes=[node])

    # Validator accepts the extraction.
    report = validate_extracted_graph(spec, graph)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

    # Materializer must route 'x' → 'x_col' (property A's
    # physical column), not to 'x' (property B's column).
    row = _route_node(node, thing_entity, session_id="sess1")
    assert row.get("x_col") == "value-from-A", (
        f"materializer routed to the wrong physical column on "
        f"logical-name/column collision; row={row!r}"
    )
    # Property B's column 'x' should be absent from the row
    # because the extraction targeted property A, not B.
    assert (
        "x" not in row
    ), f"materializer also wrote to the colliding column; row={row!r}"


class TestNormalizationAndJsonRoundTrip:
  """Validator-clean values must produce JSON-serializable
  materializer rows. Earlier behavior accepted Python ``bytes``,
  ``date``, and tz-aware ``datetime`` (correctly per the issue's
  type table) but the materializer wrote them raw into the row
  dict, which then failed at ``insert_rows_json`` /
  ``load_table_from_json``. Normalization now bridges that gap."""

  def test_bytes_value_normalizes_to_base64_string(self):
    import json

    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph
    from bigquery_agent_analytics.ontology_materializer import _route_node

    spec = self._bytes_spec()
    entity = next(e for e in spec.entities if e.name == "Blob")

    node = ExtractedNode(
        node_id="sess1:Blob:blob_id=b1",
        entity_name="Blob",
        labels=["Blob"],
        properties=[
            ExtractedProperty(name="blob_id", value="b1"),
            ExtractedProperty(name="payload", value=b"hello world"),
        ],
    )
    graph = _graph(nodes=[node])

    # (a) Validator accepts raw bytes.
    report = validate_extracted_graph(spec, graph)
    assert report.ok is True

    # (b) Materializer normalizes to base64 string and the row
    #     is JSON-serializable.
    row = _route_node(node, entity, session_id="sess1")
    assert isinstance(row["payload"], str)
    json.dumps(row)  # must not raise

  def test_date_value_normalizes_to_iso_string(self):
    import datetime as dt
    import json

    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph
    from bigquery_agent_analytics.ontology_materializer import _route_node

    spec, _, _ = _resolved_spec()
    decision = next(e for e in spec.entities if e.name == "Decision")

    node = ExtractedNode(
        node_id="sess1:Decision:decision_id=d1",
        entity_name="Decision",
        labels=["Decision"],
        properties=[
            ExtractedProperty(name="decision_id", value="d1"),
            ExtractedProperty(
                name="occurred_at",
                value=dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=dt.timezone.utc),
            ),
        ],
    )
    graph = _graph(nodes=[node])

    assert validate_extracted_graph(spec, graph).ok

    row = _route_node(node, decision, session_id="sess1")
    assert isinstance(row["occurred_at"], str)
    assert row["occurred_at"].startswith("2026-05-05")
    json.dumps(row)

  def test_invalid_iso_date_string_now_rejected(self):
    """Earlier regex-only acceptance let '9999-99-99' match
    ``\\d{4}-\\d{2}-\\d{2}``. The validator now uses
    ``datetime.date.fromisoformat`` so genuinely-malformed dates
    fail before BigQuery sees them."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec = self._date_spec()
    node = ExtractedNode(
        node_id="sess1:Event:event_id=e1",
        entity_name="Event",
        labels=["Event"],
        properties=[
            ExtractedProperty(name="event_id", value="e1"),
            # Looks date-shaped but isn't a valid date.
            ExtractedProperty(name="day", value="9999-99-99"),
        ],
    )
    graph = _graph(nodes=[node])

    report = validate_extracted_graph(spec, graph)
    fails = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(fails) == 1

  def test_compact_date_string_rejected(self):
    """``date.fromisoformat`` accepts compact ``20260505`` but
    BigQuery JSON inserts only accept the dashed ``YYYY-MM-DD``
    shape. The validator gates on the BigQuery shape first."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec = self._date_spec()
    node = ExtractedNode(
        node_id="sess1:Event:event_id=e1",
        entity_name="Event",
        labels=["Event"],
        properties=[
            ExtractedProperty(name="event_id", value="e1"),
            ExtractedProperty(name="day", value="20260505"),
        ],
    )
    graph = _graph(nodes=[node])

    report = validate_extracted_graph(spec, graph)
    fails = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(fails) == 1

  def test_week_date_string_rejected(self):
    """``date.fromisoformat`` (Python 3.11+) accepts ``2026-W19-2``
    week dates; BigQuery JSON does not."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec = self._date_spec()
    node = ExtractedNode(
        node_id="sess1:Event:event_id=e1",
        entity_name="Event",
        labels=["Event"],
        properties=[
            ExtractedProperty(name="event_id", value="e1"),
            ExtractedProperty(name="day", value="2026-W19-2"),
        ],
    )
    graph = _graph(nodes=[node])

    report = validate_extracted_graph(spec, graph)
    fails = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(fails) == 1

  def test_compact_timestamp_string_rejected(self):
    """``datetime.fromisoformat`` (3.11+) accepts compact
    ``20260505T120000``; BigQuery JSON requires the dashed/colon
    form."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                occurred_at="20260505T120000Z",
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    fails = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(fails) == 1

  def test_nanosecond_precision_timestamp_rejected(self):
    """BigQuery TIMESTAMP is microsecond precision; nanosecond
    strings like ``2026-05-05T12:00:00.123456789Z`` are rejected at
    INSERT time so the validator must reject them up front."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                occurred_at="2026-05-05T12:00:00.123456789Z",
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    fails = [f for f in report.failures if f.code == "type_mismatch"]
    assert len(fails) == 1

  def test_microsecond_precision_timestamp_accepted(self):
    """Boundary case: 1-6 fractional digits are accepted (BigQuery
    TIMESTAMP supports up to microsecond precision)."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[
            _node(
                "d1",
                "Decision",
                decision_id="d1",
                occurred_at="2026-05-05T12:00:00.123456Z",
            )
        ]
    )

    report = validate_extracted_graph(spec, graph)
    assert report.ok is True

  def test_sdk_type_alias_float64_is_type_checked(self):
    """``float64`` is an alias for ``double`` in
    ``ontology_materializer._DDL_TYPE_MAP``. The validator now
    accepts the alias so a string value on a float64-typed property
    fails type-check (instead of slipping through the unknown-type
    fallback)."""
    from bigquery_agent_analytics.graph_validation import _value_matches_sdk_type

    assert _value_matches_sdk_type(3.14, "float64") is True
    assert _value_matches_sdk_type(42, "float64") is True
    assert _value_matches_sdk_type("not-a-number", "float64") is False

  def test_sdk_type_alias_bool_is_type_checked(self):
    """Same alias coverage for ``bool`` vs ``boolean``."""
    from bigquery_agent_analytics.graph_validation import _value_matches_sdk_type

    assert _value_matches_sdk_type(True, "bool") is True
    assert _value_matches_sdk_type(False, "bool") is True
    assert _value_matches_sdk_type("true", "bool") is False
    assert _value_matches_sdk_type(1, "bool") is False  # int rejected

  def _bytes_spec(self):
    """Minimal spec with a bytes-typed property."""
    import pathlib
    import tempfile

    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    ont_yaml = (
        "ontology: BlobTest\n"
        "entities:\n"
        "  - name: Blob\n"
        "    keys:\n"
        "      primary: [blob_id]\n"
        "    properties:\n"
        "      - name: blob_id\n"
        "        type: string\n"
        "      - name: payload\n"
        "        type: bytes\n"
        "relationships: []\n"
    )
    bnd_yaml = (
        "binding: blob_test\n"
        "ontology: BlobTest\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: p\n"
        "  dataset: d\n"
        "entities:\n"
        "  - name: Blob\n"
        "    source: blobs\n"
        "    properties:\n"
        "      - name: blob_id\n"
        "        column: blob_id\n"
        "      - name: payload\n"
        "        column: payload\n"
        "relationships: []\n"
    )
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="blob_test_"))
    (tmp / "ont.yaml").write_text(ont_yaml, encoding="utf-8")
    (tmp / "bnd.yaml").write_text(bnd_yaml, encoding="utf-8")
    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
    return resolve(ontology, binding)

  def _date_spec(self):
    """Minimal spec with a date-typed property."""
    import pathlib
    import tempfile

    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    ont_yaml = (
        "ontology: DateTest\n"
        "entities:\n"
        "  - name: Event\n"
        "    keys:\n"
        "      primary: [event_id]\n"
        "    properties:\n"
        "      - name: event_id\n"
        "        type: string\n"
        "      - name: day\n"
        "        type: date\n"
        "relationships: []\n"
    )
    bnd_yaml = (
        "binding: date_test\n"
        "ontology: DateTest\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: p\n"
        "  dataset: d\n"
        "entities:\n"
        "  - name: Event\n"
        "    source: events\n"
        "    properties:\n"
        "      - name: event_id\n"
        "        column: event_id\n"
        "      - name: day\n"
        "        column: day\n"
        "relationships: []\n"
    )
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="date_test_"))
    (tmp / "ont.yaml").write_text(ont_yaml, encoding="utf-8")
    (tmp / "bnd.yaml").write_text(bnd_yaml, encoding="utf-8")
    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
    return resolve(ontology, binding)


class TestExternalEndpoints:
  """Lineage-edge batches (``graph.nodes=[]`` with edges referring to
  nodes materialized in earlier passes) need
  ``allow_external_endpoints=True`` to validate without false-failing
  on ``unresolved_endpoint``."""

  def test_external_endpoints_default_strict_mode_fails(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    d_id = "sess1:Decision:decision_id=d1"
    o_id = "sess1:Outcome:outcome_id=o1"
    graph = _graph(
        nodes=[],
        edges=[_edge("e1", "HasOutcome", d_id, o_id, weight=1.0)],
    )

    report = validate_extracted_graph(spec, graph)
    assert any(f.code == "unresolved_endpoint" for f in report.failures)

  def test_external_endpoints_permissive_mode_passes(self):
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    d_id = "sess1:Decision:decision_id=d1"
    o_id = "sess1:Outcome:outcome_id=o1"
    graph = _graph(
        nodes=[],
        edges=[_edge("e1", "HasOutcome", d_id, o_id, weight=1.0)],
    )

    report = validate_extracted_graph(
        spec, graph, allow_external_endpoints=True
    )
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  def test_external_endpoints_still_validates_endpoint_key_format(self):
    """Permissive mode skips the in-graph node lookup but the
    endpoint-key parse still runs — short-form ids that produce
    empty FK columns must still fail."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    graph = _graph(
        nodes=[],
        edges=[_edge("e1", "HasOutcome", "d1", "o1")],
    )

    report = validate_extracted_graph(
        spec, graph, allow_external_endpoints=True
    )
    fails = [f for f in report.failures if f.code == "missing_endpoint_key"]
    assert len(fails) == 2  # decision_id and outcome_id both unparseable

  def test_external_endpoints_catches_wrong_entity_segment(self):
    """Permissive mode skips the in-graph node lookup but the
    node_id itself carries an entity segment
    ('{session}:{entity}:k=v'). An obvious mismatch like a Decision
    node-id where the relationship expects an Outcome endpoint must
    still emit ``wrong_endpoint_entity`` — otherwise lineage-edge
    batches silently route to the wrong table."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    d_id = "sess1:Decision:decision_id=d1"
    # to-endpoint should be Outcome, but we pass a Decision-shaped id.
    wrong_o_id = "sess1:Decision:outcome_id=o1"
    graph = _graph(
        nodes=[],
        edges=[_edge("e1", "HasOutcome", d_id, wrong_o_id, weight=1.0)],
    )

    report = validate_extracted_graph(
        spec, graph, allow_external_endpoints=True
    )
    fails = [f for f in report.failures if f.code == "wrong_endpoint_entity"]
    assert len(fails) == 1
    assert fails[0].observed == "Decision"
    assert fails[0].expected == "Outcome"

  def test_external_endpoints_empty_entity_segment_fails(self):
    """A node-id with an empty entity segment ('sess1::outcome_id=o1')
    matches the documented '{session}:{entity}:k=v' shape's part
    count but violates the requirement that ``entity`` be non-empty.
    parse_key_segment still extracts 'outcome_id=o1' so
    missing_endpoint_key wouldn't fire — the entity-segment check is
    the only thing standing between this id and a silent route to
    the wrong table."""
    from bigquery_agent_analytics.graph_validation import validate_extracted_graph

    spec, _, _ = _resolved_spec()
    d_id = "sess1:Decision:decision_id=d1"
    empty_entity_o_id = "sess1::outcome_id=o1"
    graph = _graph(
        nodes=[],
        edges=[_edge("e1", "HasOutcome", d_id, empty_entity_o_id, weight=1.0)],
    )

    report = validate_extracted_graph(
        spec, graph, allow_external_endpoints=True
    )
    fails = [f for f in report.failures if f.code == "wrong_endpoint_entity"]
    assert len(fails) == 1
    assert fails[0].observed == ""
    assert fails[0].expected == "Outcome"


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
