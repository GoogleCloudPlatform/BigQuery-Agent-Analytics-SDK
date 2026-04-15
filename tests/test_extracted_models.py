# tests/test_extracted_models.py
"""Verify extracted models are importable from both old and new paths."""

from __future__ import annotations


class TestExtractedModelsImport:

  def test_import_from_new_module(self):
    from bigquery_agent_analytics.extracted_models import ExtractedEdge
    from bigquery_agent_analytics.extracted_models import ExtractedGraph
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty

    assert ExtractedGraph is not None
    assert ExtractedNode is not None
    assert ExtractedEdge is not None
    assert ExtractedProperty is not None

  def test_import_from_old_module(self):
    """Backward compat: old import path still works."""
    from bigquery_agent_analytics.ontology_models import ExtractedEdge
    from bigquery_agent_analytics.ontology_models import ExtractedGraph
    from bigquery_agent_analytics.ontology_models import ExtractedNode
    from bigquery_agent_analytics.ontology_models import ExtractedProperty

    assert ExtractedGraph is not None

  def test_import_from_package_root(self):
    """Package-level import still works."""
    from bigquery_agent_analytics import ExtractedEdge
    from bigquery_agent_analytics import ExtractedGraph
    from bigquery_agent_analytics import ExtractedNode
    from bigquery_agent_analytics import ExtractedProperty

    assert ExtractedGraph is not None

  def test_same_class_from_both_paths(self):
    """Old and new import paths resolve to the exact same class."""
    from bigquery_agent_analytics.extracted_models import ExtractedGraph as New
    from bigquery_agent_analytics.ontology_models import ExtractedGraph as Old

    assert New is Old

  def test_extracted_graph_round_trip(self):
    """Basic construction and serialization still works."""
    from bigquery_agent_analytics.extracted_models import ExtractedEdge
    from bigquery_agent_analytics.extracted_models import ExtractedGraph
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty

    graph = ExtractedGraph(
        name="test",
        nodes=[
            ExtractedNode(
                node_id="n1",
                entity_name="Person",
                labels=["Person"],
                properties=[
                    ExtractedProperty(name="name", value="Alice"),
                ],
            )
        ],
        edges=[
            ExtractedEdge(
                edge_id="e1",
                relationship_name="KNOWS",
                from_node_id="n1",
                to_node_id="n2",
            )
        ],
    )
    assert len(graph.nodes) == 1
    assert len(graph.edges) == 1
    assert graph.nodes[0].properties[0].value == "Alice"
