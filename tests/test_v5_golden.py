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

"""Golden tests for V5 Context Graph: TTL import, structured extraction,
lineage detection, DDL session columns, materializer ownership, and
lineage GQL generation.
"""

from __future__ import annotations

import json
import os

import pytest

from bigquery_agent_analytics.ontology_graph import detect_lineage_edges
from bigquery_agent_analytics.ontology_materializer import _route_edge
from bigquery_agent_analytics.ontology_materializer import compile_relationship_ddl
from bigquery_agent_analytics.ontology_models import _validate_graph_spec
from bigquery_agent_analytics.ontology_models import BindingSpec
from bigquery_agent_analytics.ontology_models import EntitySpec
from bigquery_agent_analytics.ontology_models import ExtractedEdge
from bigquery_agent_analytics.ontology_models import ExtractedGraph
from bigquery_agent_analytics.ontology_models import ExtractedNode
from bigquery_agent_analytics.ontology_models import ExtractedProperty
from bigquery_agent_analytics.ontology_models import GraphSpec
from bigquery_agent_analytics.ontology_models import KeySpec
from bigquery_agent_analytics.ontology_models import load_graph_spec
from bigquery_agent_analytics.ontology_models import load_graph_spec_from_string
from bigquery_agent_analytics.ontology_models import PropertySpec
from bigquery_agent_analytics.ontology_models import RelationshipSpec
from bigquery_agent_analytics.ontology_orchestrator import compile_lineage_gql
from bigquery_agent_analytics.resolved_spec import resolve_from_graph_spec
from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
from bigquery_agent_analytics.structured_extraction import merge_extraction_results
from bigquery_agent_analytics.structured_extraction import run_structured_extractors
from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

# ------------------------------------------------------------------ #
# Fixture Paths                                                        #
# ------------------------------------------------------------------ #

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_TTL_PATH = os.path.join(_FIXTURES_DIR, "yamo_sample.ttl")
_MIXED_EVENTS_PATH = os.path.join(_FIXTURES_DIR, "mixed_events.json")
_LINEAGE_SESSIONS_PATH = os.path.join(_FIXTURES_DIR, "lineage_sessions.json")


def _load_mixed_events() -> list[dict]:
  """Load the mixed_events.json fixture."""
  with open(_MIXED_EVENTS_PATH, encoding="utf-8") as f:
    return json.load(f)


def _load_lineage_sessions() -> dict[str, dict]:
  """Load the lineage_sessions.json fixture."""
  with open(_LINEAGE_SESSIONS_PATH, encoding="utf-8") as f:
    return json.load(f)


def _demo_spec() -> GraphSpec:
  """Load the existing YMGO demo spec."""
  demo_path = os.path.join(
      os.path.dirname(__file__),
      "..",
      "examples",
      "ymgo_graph_spec.yaml",
  )
  return load_graph_spec(demo_path, env="p.d")


def _hydrate_lineage_graph(session_data: dict) -> ExtractedGraph:
  """Build an ExtractedGraph from a lineage session dict."""
  nodes = []
  for raw in session_data.get("nodes", []):
    props = [
        ExtractedProperty(name=p["name"], value=p["value"])
        for p in raw.get("properties", [])
    ]
    nodes.append(
        ExtractedNode(
            node_id=raw["node_id"],
            entity_name=raw["entity_name"],
            labels=raw.get("labels", [raw["entity_name"]]),
            properties=props,
        )
    )
  edges = []
  for raw in session_data.get("edges", []):
    props = [
        ExtractedProperty(name=p["name"], value=p["value"])
        for p in raw.get("properties", [])
    ]
    edges.append(
        ExtractedEdge(
            edge_id=raw["edge_id"],
            relationship_name=raw["relationship_name"],
            from_node_id=raw["from_node_id"],
            to_node_id=raw["to_node_id"],
            properties=props,
        )
    )
  return ExtractedGraph(name="lineage_test", nodes=nodes, edges=edges)


def _make_entity(name, props=None, keys=None, source="p.d.t", labels=None):
  """Shortcut for building an EntitySpec in tests."""
  props = props or [PropertySpec(name="eid", type="string")]
  keys = keys or ["eid"]
  labels = labels or [name]
  return EntitySpec(
      name=name,
      binding=BindingSpec(source=source),
      keys=KeySpec(primary=keys),
      properties=props,
      labels=labels,
  )


def _make_lineage_spec() -> GraphSpec:
  """Build a GraphSpec with a lineage self-edge for testing."""
  entity = _make_entity(
      name="sup_YahooAdUnit",
      props=[
          PropertySpec(name="adUnitId", type="string"),
          PropertySpec(name="adUnitName", type="string"),
          PropertySpec(name="adUnitPosition", type="string"),
          PropertySpec(name="adUnitSize", type="string"),
      ],
      keys=["adUnitId"],
      source="p.d.yahoo_ad_units",
  )
  lineage_rel = RelationshipSpec(
      name="sup_YahooAdUnitEvolvedFrom",
      from_entity="sup_YahooAdUnit",
      to_entity="sup_YahooAdUnit",
      binding=BindingSpec(
          source="p.d.ad_unit_lineage",
          from_columns=["adUnitId"],
          to_columns=["adUnitId"],
          from_session_column="from_session_id",
          to_session_column="to_session_id",
      ),
      properties=[
          PropertySpec(name="from_session_id", type="string"),
          PropertySpec(name="to_session_id", type="string"),
          PropertySpec(name="event_time", type="timestamp"),
          PropertySpec(name="changed_properties", type="string"),
      ],
  )
  return GraphSpec(
      name="lineage_test",
      entities=[entity],
      relationships=[lineage_rel],
  )


def _resolved_demo_spec():
  """Return _demo_spec() converted to a ResolvedGraph."""
  return resolve_from_graph_spec(_demo_spec())


def _resolved_lineage_spec():
  """Return _make_lineage_spec() converted to a ResolvedGraph."""
  return resolve_from_graph_spec(_make_lineage_spec())


# ------------------------------------------------------------------ #
# TestBindingSpecSessionColumns                                        #
# ------------------------------------------------------------------ #


class TestBindingSpecSessionColumns:
  """BindingSpec from_session_column / to_session_column validation."""

  def test_session_columns_default_none(self):
    """V4 relationships (without session column overrides) default to None."""
    spec = _demo_spec()
    # Check the original V4 relationships, not lineage rels.
    v4_rels = [
        r for r in spec.relationships if r.binding.from_session_column is None
    ]
    assert len(v4_rels) >= 1
    for rel in v4_rels:
      assert rel.binding.from_session_column is None
      assert rel.binding.to_session_column is None

  def test_session_columns_valid(self):
    """Constructing a spec with valid session columns succeeds."""
    entity = _make_entity(
        "A",
        props=[PropertySpec(name="a_id", type="string")],
        keys=["a_id"],
        source="p.d.a",
    )
    rel = RelationshipSpec(
        name="AEvolvedFrom",
        from_entity="A",
        to_entity="A",
        binding=BindingSpec(
            source="p.d.a_lineage",
            from_columns=["a_id"],
            to_columns=["a_id"],
            from_session_column="from_session_id",
            to_session_column="to_session_id",
        ),
        properties=[
            PropertySpec(name="from_session_id", type="string"),
            PropertySpec(name="to_session_id", type="string"),
            PropertySpec(name="changed_properties", type="string"),
        ],
    )
    spec = GraphSpec(name="g", entities=[entity], relationships=[rel])
    spec.entities[0].labels = ["A"]
    # Should not raise.
    _validate_graph_spec(spec)
    assert rel.binding.from_session_column == "from_session_id"
    assert rel.binding.to_session_column == "to_session_id"

  def test_session_columns_must_be_paired(self):
    """from_session_column without to_session_column raises ValueError."""
    entity = _make_entity(
        "A",
        props=[PropertySpec(name="a_id", type="string")],
        keys=["a_id"],
        source="p.d.a",
    )
    rel = RelationshipSpec(
        name="AEvolvedFrom",
        from_entity="A",
        to_entity="A",
        binding=BindingSpec(
            source="p.d.a_lineage",
            from_columns=["a_id"],
            to_columns=["a_id"],
            from_session_column="from_session_id",
            # to_session_column intentionally omitted
        ),
        properties=[
            PropertySpec(name="from_session_id", type="string"),
        ],
    )
    spec = GraphSpec(name="g", entities=[entity], relationships=[rel])
    spec.entities[0].labels = ["A"]
    with pytest.raises(
        ValueError,
        match="from_session_column and to_session_column must both be present",
    ):
      _validate_graph_spec(spec)

  def test_session_columns_must_reference_properties(self):
    """Non-existent property name in from_session_column raises ValueError."""
    entity = _make_entity(
        "A",
        props=[PropertySpec(name="a_id", type="string")],
        keys=["a_id"],
        source="p.d.a",
    )
    rel = RelationshipSpec(
        name="AEvolvedFrom",
        from_entity="A",
        to_entity="A",
        binding=BindingSpec(
            source="p.d.a_lineage",
            from_columns=["a_id"],
            to_columns=["a_id"],
            from_session_column="nonexistent_col",
            to_session_column="also_nonexistent",
        ),
        properties=[
            PropertySpec(name="something_else", type="string"),
        ],
    )
    spec = GraphSpec(name="g", entities=[entity], relationships=[rel])
    spec.entities[0].labels = ["A"]
    with pytest.raises(
        ValueError,
        match="from_session_column.*not found in relationship properties",
    ):
      _validate_graph_spec(spec)


# ------------------------------------------------------------------ #
# TestTTLImport                                                        #
# ------------------------------------------------------------------ #


_rdflib_available = False
try:
  import rdflib as _rdflib_check  # noqa: F401

  _rdflib_available = True
except ImportError:
  pass


@pytest.mark.skipif(
    not _rdflib_available,
    reason="rdflib not installed",
)
class TestTTLImport:
  """Step 1 -- TTL import + resolve."""

  def test_import_produces_unresolved_artifact(self):
    """Importing yamo_sample.ttl produces YAML with ontology_import metadata."""
    from bigquery_agent_analytics.ttl_importer import ttl_import

    result = ttl_import(
        _TTL_PATH,
        include_namespaces=["https://example.com/yamo#"],
    )
    assert "ontology_import:" in result.yaml_text
    assert result.report.classes_mapped == 5
    assert result.report.relationships_mapped == 2

  def test_import_placeholder_for_missing_key(self):
    """DecisionPoint (no owl:hasKey) should have FILL_IN for keys."""
    import yaml

    from bigquery_agent_analytics.ttl_importer import ttl_import

    result = ttl_import(
        _TTL_PATH,
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(result.yaml_text)
    entities = data["graph"]["entities"]
    dp = next(e for e in entities if e["name"] == "DecisionPoint")
    assert "FILL_IN" in dp["keys"]["primary"]
    # Verify placeholder is tracked in the report.
    placeholder_locations = [p.location for p in result.report.placeholders]
    assert any(
        "DecisionPoint" in loc and "keys.primary" in loc
        for loc in placeholder_locations
    )

  def test_import_type_narrowing(self):
    """budget (xsd:decimal) should map to double with a type warning."""
    from bigquery_agent_analytics.ttl_importer import ttl_import

    result = ttl_import(
        _TTL_PATH,
        include_namespaces=["https://example.com/yamo#"],
    )
    budget_warnings = [
        w for w in result.report.type_warnings if w.property_name == "budget"
    ]
    assert len(budget_warnings) == 1
    assert budget_warnings[0].mapped_type == "double"
    assert "decimal" in budget_warnings[0].owl_type

  def test_resolve_produces_valid_graph_spec(self, tmp_path):
    """Resolve with defaults fixing FILL_IN, then load_graph_spec_from_string succeeds."""
    import yaml

    from bigquery_agent_analytics.ttl_importer import ttl_import
    from bigquery_agent_analytics.ttl_importer import ttl_resolve

    result = ttl_import(
        _TTL_PATH,
        include_namespaces=["https://example.com/yamo#"],
    )
    import_file = tmp_path / "yamo.import.yaml"
    import_file.write_text(result.yaml_text, encoding="utf-8")

    # Provide defaults for all FILL_IN placeholders.
    # DecisionPoint has no owl:hasKey, so its PK and the evaluates
    # relationship from_columns both inherited FILL_IN.
    resolved_yaml = ttl_resolve(
        str(import_file),
        defaults={
            "entities[DecisionPoint].keys.primary": ["decision_id"],
            "relationships[evaluates].binding.from_columns": ["decision_id"],
        },
    )
    # The resolved YAML should be loadable as a valid GraphSpec.
    spec = load_graph_spec_from_string(resolved_yaml)
    entity_names = {e.name for e in spec.entities}
    assert "Party" in entity_names
    assert "DecisionPoint" in entity_names
    assert "Campaign" in entity_names

  def test_resolve_rejects_unresolved(self, tmp_path):
    """Resolve without fixing FILL_IN raises ValueError."""
    from bigquery_agent_analytics.ttl_importer import ttl_import
    from bigquery_agent_analytics.ttl_importer import ttl_resolve

    result = ttl_import(
        _TTL_PATH,
        include_namespaces=["https://example.com/yamo#"],
    )
    import_file = tmp_path / "yamo.import.yaml"
    import_file.write_text(result.yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match="Unresolved FILL_IN"):
      ttl_resolve(str(import_file))


# ------------------------------------------------------------------ #
# TestStructuredExtraction                                             #
# ------------------------------------------------------------------ #


class TestStructuredExtraction:
  """Step 2 -- structured extraction."""

  def test_bka_extractor_fully_handled(self):
    """BKA event without reasoning_text is fully handled."""
    events = _load_mixed_events()
    # First event: BKA_DECISION without reasoning_text.
    event = events[0]
    spec = _demo_spec()
    result = extract_bka_decision_event(event, spec)
    assert len(result.nodes) == 1
    assert result.nodes[0].entity_name == "mako_DecisionPoint"
    assert "span-001" in result.fully_handled_span_ids
    assert len(result.partially_handled_span_ids) == 0

  def test_bka_extractor_partially_handled(self):
    """BKA event with reasoning_text is only partially handled."""
    events = _load_mixed_events()
    # Last event (index 6): BKA_DECISION with reasoning_text.
    event = events[6]
    spec = _demo_spec()
    result = extract_bka_decision_event(event, spec)
    assert len(result.nodes) == 1
    assert "span-007" in result.partially_handled_span_ids
    assert len(result.fully_handled_span_ids) == 0

  def test_bka_extractor_ignores_non_matching(self):
    """LLM_RESPONSE event returns empty result from BKA extractor."""
    events = _load_mixed_events()
    # Third event (index 2): LLM_RESPONSE.
    event = events[2]
    spec = _demo_spec()
    result = extract_bka_decision_event(event, spec)
    assert len(result.nodes) == 0
    assert len(result.edges) == 0
    assert len(result.fully_handled_span_ids) == 0

  def test_run_extractors_merges_results(self):
    """run_structured_extractors with mixed events produces merged result."""
    events = _load_mixed_events()
    spec = _demo_spec()
    extractors = {"BKA_DECISION": extract_bka_decision_event}
    result = run_structured_extractors(events, extractors, spec)
    # 4 BKA_DECISION events -> 4 unique decision_ids (dec-100..102, dec-200).
    assert len(result.nodes) == 4
    node_ids = {n.node_id for n in result.nodes}
    assert any("dec-100" in nid for nid in node_ids)
    assert any("dec-200" in nid for nid in node_ids)

  def test_merge_deduplicates_nodes(self):
    """Same node_id from two extractors: last wins."""
    node_v1 = ExtractedNode(
        node_id="sess:mako_DecisionPoint:decision_id=dec-1",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[
            ExtractedProperty(name="decision_id", value="dec-1"),
            ExtractedProperty(name="outcome", value="REJECTED"),
        ],
    )
    node_v2 = ExtractedNode(
        node_id="sess:mako_DecisionPoint:decision_id=dec-1",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[
            ExtractedProperty(name="decision_id", value="dec-1"),
            ExtractedProperty(name="outcome", value="APPROVED"),
        ],
    )
    r1 = StructuredExtractionResult(nodes=[node_v1])
    r2 = StructuredExtractionResult(nodes=[node_v2])
    merged = merge_extraction_results([r1, r2])
    assert len(merged.nodes) == 1
    # Last wins.
    prop_map = {p.name: p.value for p in merged.nodes[0].properties}
    assert prop_map["outcome"] == "APPROVED"


# ------------------------------------------------------------------ #
# TestLineageDetection                                                 #
# ------------------------------------------------------------------ #


class TestLineageDetection:
  """Step 3 -- temporal lineage."""

  def test_detect_lineage_edges_finds_changes(self):
    """Compare sess-A and sess-B, find changed properties."""
    sessions = _load_lineage_sessions()
    spec = _resolved_demo_spec()

    current_graph = _hydrate_lineage_graph(sessions["sess-B"])
    prior_graphs = {
        "sess-A": _hydrate_lineage_graph(sessions["sess-A"]),
    }

    edges = detect_lineage_edges(
        current_graph=current_graph,
        current_session_id="sess-B",
        prior_graphs=prior_graphs,
        lineage_entity_types=["sup_YahooAdUnit"],
        spec=spec,
    )
    assert len(edges) == 1
    edge = edges[0]
    assert edge.relationship_name == "sup_YahooAdUnitEvolvedFrom"
    # Check that changed_properties includes adUnitName and adUnitPosition.
    changed = next(
        p.value for p in edge.properties if p.name == "changed_properties"
    )
    assert "adUnitName" in changed
    assert "adUnitPosition" in changed
    # adUnitSize should NOT be in changed (it stayed 300x250).
    assert "adUnitSize" not in changed

  def test_detect_lineage_edges_no_change(self):
    """Identical nodes across sessions produce no lineage edges."""
    sessions = _load_lineage_sessions()
    spec = _resolved_demo_spec()

    # Use the same session data for both prior and current.
    current_graph = _hydrate_lineage_graph(sessions["sess-A"])
    prior_graphs = {
        "sess-A": _hydrate_lineage_graph(sessions["sess-A"]),
    }

    edges = detect_lineage_edges(
        current_graph=current_graph,
        current_session_id="sess-A",
        prior_graphs=prior_graphs,
        lineage_entity_types=["sup_YahooAdUnit"],
        spec=spec,
    )
    assert len(edges) == 0

  def test_detect_lineage_edges_new_entity(self):
    """Entity only in current session (not in prior) produces no edge."""
    sessions = _load_lineage_sessions()
    spec = _resolved_demo_spec()

    current_graph = _hydrate_lineage_graph(sessions["sess-B"])
    # Prior has no matching entity.
    empty_prior = ExtractedGraph(name="empty")
    prior_graphs = {"sess-X": empty_prior}

    edges = detect_lineage_edges(
        current_graph=current_graph,
        current_session_id="sess-B",
        prior_graphs=prior_graphs,
        lineage_entity_types=["sup_YahooAdUnit"],
        spec=spec,
    )
    assert len(edges) == 0


# ------------------------------------------------------------------ #
# TestDDLSessionColumns                                                #
# ------------------------------------------------------------------ #


class TestDDLSessionColumns:
  """Step 3 -- DDL compiler with session column overrides."""

  def test_edge_ddl_without_session_columns(self):
    """Existing behavior unchanged: edge uses session_id for both endpoints."""
    from bigquery_agent_analytics.ontology_property_graph import compile_edge_table_clause

    spec = _resolved_demo_spec()
    rel = spec.relationships[0]  # CandidateEdge
    clause = compile_edge_table_clause(rel, spec, "proj", "ds")
    assert "SOURCE KEY (decision_id, session_id)" in clause
    assert "REFERENCES mako_DecisionPoint (decision_id, session_id)" in clause

  def test_edge_ddl_with_session_columns(self):
    """Lineage edge uses from_session_id/to_session_id for endpoints."""
    from bigquery_agent_analytics.ontology_property_graph import compile_edge_table_clause

    entity = _make_entity(
        "A",
        props=[PropertySpec(name="a_id", type="string")],
        keys=["a_id"],
        source="p.d.a_table",
    )
    rel = RelationshipSpec(
        name="AEvolvedFrom",
        from_entity="A",
        to_entity="A",
        binding=BindingSpec(
            source="p.d.a_lineage",
            from_columns=["a_id"],
            to_columns=["a_id"],
            from_session_column="from_session_id",
            to_session_column="to_session_id",
        ),
        properties=[
            PropertySpec(name="from_session_id", type="string"),
            PropertySpec(name="to_session_id", type="string"),
            PropertySpec(name="changed_properties", type="string"),
        ],
    )
    old_spec = GraphSpec(name="g", entities=[entity], relationships=[rel])
    spec = resolve_from_graph_spec(old_spec)
    resolved_rel = spec.relationships[0]

    clause = compile_edge_table_clause(resolved_rel, spec, "proj", "ds")
    # SOURCE KEY should use from_session_id, not session_id.
    assert "SOURCE KEY (a_id, from_session_id)" in clause
    assert "REFERENCES A (a_id, session_id)" in clause
    # DESTINATION KEY should use to_session_id.
    assert "DESTINATION KEY (a_id, to_session_id)" in clause


# ------------------------------------------------------------------ #
# TestMaterializerSessionOwnership                                     #
# ------------------------------------------------------------------ #


class TestMaterializerSessionOwnership:
  """Step 3 -- materializer session ownership."""

  def test_route_edge_normal(self):
    """Normal edge: session_id set from the session_id parameter."""
    spec = _resolved_demo_spec()
    rel = spec.relationships[0]  # CandidateEdge
    edge = ExtractedEdge(
        edge_id="sess-1:CandidateEdge:0",
        relationship_name="CandidateEdge",
        from_node_id="sess-1:mako_DecisionPoint:decision_id=d1",
        to_node_id="sess-1:sup_YahooAdUnit:adUnitId=u1",
        properties=[
            ExtractedProperty(name="edge_type", value="SELECTED_CANDIDATE"),
            ExtractedProperty(name="mako_scoreValue", value=0.95),
        ],
    )
    row = _route_edge(edge, rel, spec, "sess-1")
    assert row["session_id"] == "sess-1"

  def test_route_edge_lineage(self):
    """Lineage edge: session_id set from to_session_column value."""
    entity = _make_entity(
        "A",
        props=[PropertySpec(name="a_id", type="string")],
        keys=["a_id"],
        source="p.d.a_table",
    )
    old_rel = RelationshipSpec(
        name="AEvolvedFrom",
        from_entity="A",
        to_entity="A",
        binding=BindingSpec(
            source="p.d.a_lineage",
            from_columns=["a_id"],
            to_columns=["a_id"],
            from_session_column="from_session_id",
            to_session_column="to_session_id",
        ),
        properties=[
            PropertySpec(name="from_session_id", type="string"),
            PropertySpec(name="to_session_id", type="string"),
            PropertySpec(name="changed_properties", type="string"),
        ],
    )
    old_spec = GraphSpec(name="g", entities=[entity], relationships=[old_rel])
    spec = resolve_from_graph_spec(old_spec)
    rel = spec.relationships[0]

    edge = ExtractedEdge(
        edge_id="sess-B:AEvolvedFrom:sess-A:a_id=x1",
        relationship_name="AEvolvedFrom",
        from_node_id="sess-A:A:a_id=x1",
        to_node_id="sess-B:A:a_id=x1",
        properties=[
            ExtractedProperty(name="from_session_id", value="sess-A"),
            ExtractedProperty(name="to_session_id", value="sess-B"),
            ExtractedProperty(name="changed_properties", value="score"),
        ],
    )
    row = _route_edge(edge, rel, spec, "sess-B")
    # session_id should be set from to_session_column value.
    assert row["session_id"] == "sess-B"

  def test_route_node_filters_to_schema(self):
    """Extra AI-emitted properties not in the entity spec are dropped."""
    from bigquery_agent_analytics.ontology_materializer import _route_node

    old_entity = _make_entity(
        "mako_DecisionPoint",
        props=[
            PropertySpec(name="decision_id", type="string"),
            PropertySpec(name="decision_type", type="string"),
        ],
        keys=["decision_id"],
        source="p.d.decision_points",
    )
    old_spec = GraphSpec(name="g", entities=[old_entity], relationships=[])
    resolved = resolve_from_graph_spec(old_spec)
    entity = resolved.entities[0]
    node = ExtractedNode(
        node_id="s1:mako_DecisionPoint:decision_id=d1",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[
            ExtractedProperty(name="decision_id", value="d1"),
            ExtractedProperty(name="decision_type", value="query"),
            # These do NOT belong to this entity — AI hallucinated them.
            ExtractedProperty(name="adUnitName", value="Homepage Banner"),
            ExtractedProperty(name="rejection_id", value="r1"),
        ],
    )
    row = _route_node(node, entity, "s1")
    assert row["decision_id"] == "d1"
    assert row["decision_type"] == "query"
    assert "adUnitName" not in row
    assert "rejection_id" not in row
    assert "session_id" in row

  def test_route_edge_filters_to_schema(self):
    """Extra AI-emitted edge properties not in the rel spec are dropped."""
    spec = _resolved_demo_spec()
    rel = spec.relationships[0]  # CandidateEdge: edge_type, mako_scoreValue
    edge = ExtractedEdge(
        edge_id="s1:CandidateEdge:0",
        relationship_name="CandidateEdge",
        from_node_id="s1:mako_DecisionPoint:decision_id=d1",
        to_node_id="s1:sup_YahooAdUnit:adUnitId=u1",
        properties=[
            ExtractedProperty(name="edge_type", value="SELECTED"),
            ExtractedProperty(name="mako_scoreValue", value=0.9),
            # Extra — does not belong to CandidateEdge schema.
            ExtractedProperty(name="event_time", value="2026-01-01"),
            ExtractedProperty(name="from_session_id", value="s0"),
        ],
    )
    row = _route_edge(edge, rel, spec, "s1")
    assert row["edge_type"] == "SELECTED"
    assert row["mako_scoreValue"] == 0.9
    assert "event_time" not in row
    assert "from_session_id" not in row


# ------------------------------------------------------------------ #
# TestLineageGQL                                                       #
# ------------------------------------------------------------------ #


class TestLineageGQL:
  """Step 3 -- lineage GQL generation."""

  def test_compile_lineage_gql(self):
    """Generates correct GQL with prior/current aliases."""
    entity = _make_entity(
        "sup_YahooAdUnit",
        props=[
            PropertySpec(name="adUnitId", type="string"),
            PropertySpec(name="adUnitName", type="string"),
        ],
        keys=["adUnitId"],
        source="p.d.yahoo_ad_units",
    )
    rel = RelationshipSpec(
        name="sup_YahooAdUnitEvolvedFrom",
        from_entity="sup_YahooAdUnit",
        to_entity="sup_YahooAdUnit",
        binding=BindingSpec(
            source="p.d.ad_unit_lineage",
            from_columns=["adUnitId"],
            to_columns=["adUnitId"],
            from_session_column="from_session_id",
            to_session_column="to_session_id",
        ),
        properties=[
            PropertySpec(name="from_session_id", type="string"),
            PropertySpec(name="to_session_id", type="string"),
            PropertySpec(name="event_time", type="timestamp"),
            PropertySpec(name="changed_properties", type="string"),
        ],
    )
    old_spec = GraphSpec(
        name="lineage_graph",
        entities=[entity],
        relationships=[rel],
    )
    spec = resolve_from_graph_spec(old_spec)

    gql = compile_lineage_gql(
        spec=spec,
        project_id="proj",
        dataset_id="ds",
        relationship_name="sup_YahooAdUnitEvolvedFrom",
    )
    assert "prev" in gql
    assert "cur" in gql
    # Aliases must not use BigQuery GQL reserved keywords.
    assert "(current:" not in gql
    assert "(prior:" not in gql
    assert "sup_YahooAdUnit" in gql
    assert "sup_YahooAdUnitEvolvedFrom" in gql
    assert "prev_adUnitId" in gql or "prev.adUnitId" in gql
    assert "cur_adUnitName" in gql or "cur.adUnitName" in gql
    assert "event_time" in gql
    assert "ORDER BY" in gql

  def test_compile_lineage_gql_rejects_non_self_edge(self):
    """Non-self-edge raises ValueError."""
    a = _make_entity(
        "A",
        props=[PropertySpec(name="a_id", type="string")],
        keys=["a_id"],
        source="p.d.a",
    )
    b = _make_entity(
        "B",
        props=[PropertySpec(name="b_id", type="string")],
        keys=["b_id"],
        source="p.d.b",
    )
    rel = RelationshipSpec(
        name="AToB",
        from_entity="A",
        to_entity="B",
        binding=BindingSpec(
            source="p.d.a_to_b",
            from_columns=["a_id"],
            to_columns=["b_id"],
        ),
    )
    spec = GraphSpec(name="g", entities=[a, b], relationships=[rel])

    with pytest.raises(ValueError, match="not a self-edge"):
      compile_lineage_gql(
          spec=spec,
          project_id="proj",
          dataset_id="ds",
          relationship_name="AToB",
      )


# ------------------------------------------------------------------ #
# Fix validation: query shape, partial hint, DDL PROPERTIES           #
# ------------------------------------------------------------------ #


class TestFetchRawEventsQuery:
  """Verify _FETCH_RAW_EVENTS_QUERY returns event_type and content."""

  def test_fetch_query_includes_event_type_and_content(self):
    """The structured-extraction query must return event_type and
    content_json (full content) so extractors can match and parse."""
    from bigquery_agent_analytics.ontology_graph import _FETCH_RAW_EVENTS_QUERY

    assert "event_type" in _FETCH_RAW_EVENTS_QUERY
    assert "content" in _FETCH_RAW_EVENTS_QUERY
    # Must NOT have the V4 event-type allowlist — all types returned.
    assert "LLM_RESPONSE" not in _FETCH_RAW_EVENTS_QUERY

  def test_fetch_query_no_event_type_filter(self):
    """The raw events query must not filter on event_type, so that
    BKA_DECISION and other custom types reach the extractors."""
    from bigquery_agent_analytics.ontology_graph import _FETCH_RAW_EVENTS_QUERY

    assert "event_type IN" not in _FETCH_RAW_EVENTS_QUERY

  def test_ai_transcript_includes_partial_span_ids(self):
    """The AI transcript CTE must include an OR clause for
    @partial_span_ids so that partially-handled custom event types
    (like BKA_DECISION) are eligible for transcript inclusion."""
    from bigquery_agent_analytics.ontology_graph import _EXTRACT_ONTOLOGY_AI_QUERY

    assert "partial_span_ids" in _EXTRACT_ONTOLOGY_AI_QUERY
    # The OR clause widens eligibility beyond the V4 allowlist.
    assert "OR base.span_id IN UNNEST(@partial_span_ids)" in (
        _EXTRACT_ONTOLOGY_AI_QUERY
    )


class TestPartialHintBuilt:
  """Verify partial_hint is built from partially_handled_span_ids."""

  def test_partial_hint_passed_to_ai(self):
    """When extractors produce partially_handled spans, the AI prompt
    should include a hint about already-extracted entities."""
    from unittest.mock import MagicMock
    from unittest.mock import patch

    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    spec = _resolved_lineage_spec()

    def fake_extractor(event, spec):
      from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

      return StructuredExtractionResult(
          nodes=[
              ExtractedNode(
                  node_id="s1:sup_YahooAdUnit:adUnitId=u1",
                  entity_name="sup_YahooAdUnit",
                  labels=["sup_YahooAdUnit"],
                  properties=[],
              )
          ],
          edges=[],
          fully_handled_span_ids=set(),
          partially_handled_span_ids={"span-partial"},
      )

    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_client.query.return_value = mock_job

    mgr = OntologyGraphManager(
        project_id="p",
        dataset_id="d",
        spec=spec,
        bq_client=mock_client,
        extractors={"BKA_DECISION": fake_extractor},
    )

    # Patch _fetch_raw_events to return a matching event.
    with patch.object(
        mgr,
        "_fetch_raw_events",
        return_value=[
            {
                "span_id": "span-partial",
                "session_id": "s1",
                "event_type": "BKA_DECISION",
                "content": {"decision_id": "d1", "reasoning_text": "why"},
            }
        ],
    ):
      mgr.extract_graph(session_ids=["s1"])

    # The AI query should have been called with the partial hint.
    call_args = mock_client.query.call_args
    sql = call_args[0][0]
    assert "already extracted from structured events" in sql

    # The partial span ID must be passed as a query parameter so the
    # transcript CTE includes it (OR base.span_id IN UNNEST(@partial_span_ids)).
    job_config = call_args[1].get("job_config") or call_args[0][1]
    params = {p.name: p for p in job_config.query_parameters}
    assert "partial_span_ids" in params
    assert "span-partial" in params["partial_span_ids"].values


class TestDDLSessionColumnsInProperties:
  """Verify session column overrides remain in PROPERTIES."""

  def test_lineage_session_cols_in_properties(self):
    """from_session_id and to_session_id must appear in PROPERTIES
    even though they are in the edge KEY, so GQL can query them."""
    from bigquery_agent_analytics.ontology_property_graph import compile_edge_table_clause

    spec = _resolved_lineage_spec()
    rel = next(
        r for r in spec.relationships if r.name == "sup_YahooAdUnitEvolvedFrom"
    )
    clause = compile_edge_table_clause(rel, spec, "p", "d")

    # Session columns must be in PROPERTIES section.
    props_section = clause.split("PROPERTIES")[1]
    assert "from_session_id" in props_section
    assert "to_session_id" in props_section


# ------------------------------------------------------------------ #
# TestMaterializerWriteMode                                            #
# ------------------------------------------------------------------ #


class TestMaterializerWriteMode:
  """Materializer write_mode and status reporting."""

  def test_default_write_mode_is_streaming(self):
    """Default write_mode is 'streaming' for backward compatibility."""
    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer

    spec = _demo_spec()
    mat = OntologyMaterializer(
        project_id="p",
        dataset_id="d",
        spec=spec,
    )
    assert mat.write_mode == "streaming"

  def test_batch_load_write_mode_accepted(self):
    """write_mode='batch_load' is accepted without error."""
    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer

    spec = _demo_spec()
    mat = OntologyMaterializer(
        project_id="p",
        dataset_id="d",
        spec=spec,
        write_mode="batch_load",
    )
    assert mat.write_mode == "batch_load"

  def test_materialize_with_status_returns_result(self):
    """materialize_with_status returns MaterializationResult."""
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.ontology_materializer import MaterializationResult
    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer

    spec = _resolved_demo_spec()
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = None
    mock_client.query.return_value = mock_job
    mock_client.insert_rows_json.return_value = []

    mat = OntologyMaterializer(
        project_id="p",
        dataset_id="d",
        spec=spec,
        bq_client=mock_client,
    )

    graph = ExtractedGraph(
        name="test",
        nodes=[
            ExtractedNode(
                node_id="s1:mako_DecisionPoint:decision_id=d1",
                entity_name="mako_DecisionPoint",
                labels=["mako_DecisionPoint"],
                properties=[
                    ExtractedProperty(name="decision_id", value="d1"),
                    ExtractedProperty(name="decision_type", value="q"),
                ],
            )
        ],
    )

    result = mat.materialize_with_status(graph, ["s1"])
    assert isinstance(result, MaterializationResult)
    assert result.row_counts.get("mako_DecisionPoint", 0) >= 1
    assert len(result.table_statuses) >= 1

  def test_delete_failure_surfaces_non_idempotent(self):
    """When DELETE fails, table status shows non-idempotent."""
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer

    spec = _resolved_demo_spec()
    mock_client = MagicMock()
    mock_job = MagicMock()

    call_count = [0]

    def side_effect(query, **kwargs):
      call_count[0] += 1
      if "DELETE" in query:
        raise Exception("streaming buffer active")
      return mock_job

    mock_job.result.return_value = None
    mock_client.query.side_effect = side_effect
    mock_client.insert_rows_json.return_value = []

    mat = OntologyMaterializer(
        project_id="p",
        dataset_id="d",
        spec=spec,
        bq_client=mock_client,
    )

    graph = ExtractedGraph(
        name="test",
        nodes=[
            ExtractedNode(
                node_id="s1:mako_DecisionPoint:decision_id=d1",
                entity_name="mako_DecisionPoint",
                labels=["mako_DecisionPoint"],
                properties=[
                    ExtractedProperty(name="decision_id", value="d1"),
                ],
            )
        ],
    )

    result = mat.materialize_with_status(graph, ["s1"])
    # Find the table status — cleanup should have failed.
    failed_tables = [
        ts
        for ts in result.table_statuses.values()
        if ts.cleanup_status == "delete_failed"
    ]
    assert len(failed_tables) >= 1
    for ts in failed_tables:
      assert ts.idempotent is False


# ------------------------------------------------------------------ #
# TestUpstreamDDLBridge                                                #
# ------------------------------------------------------------------ #


class TestUpstreamDDLBridge:
  """Upstream DDL compiler bridge (Step 2 migration)."""

  def test_can_use_upstream_flat_spec(self):
    """Flat spec without extends or lineage is compatible."""
    from bigquery_agent_analytics.ontology_property_graph import can_use_upstream_compiler

    entity = _make_entity(
        "A",
        props=[PropertySpec(name="a_id", type="string")],
        keys=["a_id"],
        source="p.d.a_table",
    )
    old_spec = GraphSpec(name="g", entities=[entity], relationships=[])
    spec = resolve_from_graph_spec(old_spec)
    assert can_use_upstream_compiler(spec) is True

  def test_cannot_use_upstream_with_extends(self):
    """Spec with extends is not compatible with upstream v0."""
    from bigquery_agent_analytics.ontology_property_graph import can_use_upstream_compiler

    spec = _resolved_demo_spec()
    # YMGO has sup_YahooAdUnit extends mako_Candidate.
    assert can_use_upstream_compiler(spec) is False

  def test_cannot_use_upstream_with_lineage(self):
    """Spec with session column overrides is not compatible."""
    from bigquery_agent_analytics.ontology_property_graph import can_use_upstream_compiler

    spec = _resolved_lineage_spec()
    assert can_use_upstream_compiler(spec) is False

  def test_compile_via_upstream_flat_spec(self):
    """Flat spec produces valid DDL via upstream compiler."""
    from bigquery_agent_analytics.ontology_property_graph import compile_ddl_via_upstream

    a = _make_entity(
        "Customer",
        props=[
            PropertySpec(name="cid", type="string"),
            PropertySpec(name="name", type="string"),
        ],
        keys=["cid"],
        source="p.d.customers",
    )
    b = _make_entity(
        "Order",
        props=[PropertySpec(name="oid", type="string")],
        keys=["oid"],
        source="p.d.orders",
    )
    rel = RelationshipSpec(
        name="Placed",
        from_entity="Customer",
        to_entity="Order",
        binding=BindingSpec(
            source="p.d.placed",
            from_columns=["cid"],
            to_columns=["oid"],
        ),
    )
    old_spec = GraphSpec(name="shop", entities=[a, b], relationships=[rel])
    spec = resolve_from_graph_spec(old_spec)
    ddl = compile_ddl_via_upstream(spec, "p", "d")
    assert "CREATE PROPERTY GRAPH" in ddl
    assert "NODE TABLES" in ddl
    assert "EDGE TABLES" in ddl
    assert "Customer" in ddl
    assert "Order" in ddl
    assert "Placed" in ddl

  def test_compile_via_upstream_rejects_extends(self):
    """Upstream compiler rejects extends."""
    from bigquery_agent_analytics.ontology_property_graph import compile_ddl_via_upstream

    spec = _resolved_demo_spec()
    with pytest.raises(ValueError, match="extends"):
      compile_ddl_via_upstream(spec, "p", "d")

  def test_compile_via_upstream_rejects_lineage(self):
    """Upstream compiler rejects lineage session column overrides."""
    from bigquery_agent_analytics.ontology_property_graph import compile_ddl_via_upstream

    spec = _resolved_lineage_spec()
    with pytest.raises(ValueError, match="session column overrides"):
      compile_ddl_via_upstream(spec, "p", "d")
