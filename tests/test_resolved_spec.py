"""Physical-layout equivalence tests for the resolve() builder."""

from __future__ import annotations

import textwrap

from bigquery_agent_analytics.resolved_spec import resolve
from bigquery_agent_analytics.resolved_spec import ResolvedGraph
from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string

_ONTOLOGY = textwrap.dedent(
    """\
  ontology: finance
  entities:
    - name: Account
      keys:
        primary: [account_id]
      properties:
        - name: account_id
          type: string
        - name: opened_at
          type: timestamp
    - name: Security
      keys:
        primary: [security_id]
      properties:
        - name: security_id
          type: string
  relationships:
    - name: HOLDS
      from: Account
      to: Security
      properties:
        - name: quantity
          type: double
"""
)

_BINDING = textwrap.dedent(
    """\
  binding: finance-bq-prod
  ontology: finance
  target:
    backend: bigquery
    project: my-proj
    dataset: finance
  entities:
    - name: Account
      source: raw.accounts
      properties:
        - {name: account_id, column: acct_id}
        - {name: opened_at, column: created_ts}
    - name: Security
      source: ref.securities
      properties:
        - {name: security_id, column: cusip}
  relationships:
    - name: HOLDS
      source: raw.holdings
      from_columns: [account_id]
      to_columns: [security_id]
      properties:
        - {name: quantity, column: qty}
"""
)


def _load():
  ont = load_ontology_from_string(_ONTOLOGY)
  bnd = load_binding_from_string(_BINDING, ontology=ont)
  return ont, bnd


class TestResolveBuilder:

  def test_graph_name(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    assert graph.name == "finance"

  def test_entity_count(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    assert len(graph.entities) == 2

  def test_entity_source_fully_qualified(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    entity_map = {e.name: e for e in graph.entities}
    assert entity_map["Account"].source == "my-proj.raw.accounts"
    assert entity_map["Security"].source == "my-proj.ref.securities"

  def test_entity_key_columns_are_physical(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    entity_map = {e.name: e for e in graph.entities}
    assert entity_map["Account"].key_columns == ("acct_id",)
    assert entity_map["Security"].key_columns == ("cusip",)

  def test_entity_properties_are_physical(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    entity_map = {e.name: e for e in graph.entities}
    acct_cols = [p.column for p in entity_map["Account"].properties]
    assert acct_cols == ["acct_id", "created_ts"]

  def test_entity_properties_preserve_logical_names(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    entity_map = {e.name: e for e in graph.entities}
    logical = [p.logical_name for p in entity_map["Account"].properties]
    assert logical == ["account_id", "opened_at"]

  def test_entity_labels_without_extends(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    entity_map = {e.name: e for e in graph.entities}
    assert entity_map["Account"].labels == ("Account",)

  def test_entity_metadata_columns_default(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    assert graph.entities[0].metadata_columns == ("session_id", "extracted_at")

  def test_relationship_source_fully_qualified(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    holds = graph.relationships[0]
    assert holds.source == "my-proj.raw.holdings"

  def test_relationship_endpoint_columns(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    holds = graph.relationships[0]
    assert holds.from_columns == ("account_id",)
    assert holds.to_columns == ("security_id",)

  def test_relationship_properties_are_physical(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    holds = graph.relationships[0]
    assert holds.properties[0].column == "qty"
    assert holds.properties[0].logical_name == "quantity"

  def test_relationship_no_lineage_by_default(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    holds = graph.relationships[0]
    assert holds.from_session_column is None
    assert holds.to_session_column is None

  def test_lineage_config_applied(self):
    from bigquery_agent_analytics.resolved_spec import LineageEdgeConfig

    ont, bnd = _load()
    lineage = {
        "HOLDS": LineageEdgeConfig(
            from_session_column="src_sid",
            to_session_column="dst_sid",
        )
    }
    graph = resolve(ont, bnd, lineage_config=lineage)
    holds = graph.relationships[0]
    assert holds.from_session_column == "src_sid"
    assert holds.to_session_column == "dst_sid"

  def test_resolve_is_deterministic(self):
    ont, bnd = _load()
    g1 = resolve(ont, bnd)
    g2 = resolve(ont, bnd)
    assert g1 == g2


class TestCrossValidation:
  """Prove resolve() matches graph_spec_from_ontology_binding() output."""

  def test_entity_sources_match(self):
    """Fully-qualified sources must be identical."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding

    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_sources = {e.name: e.source for e in graph.entities}
    spec_sources = {e.name: e.binding.source for e in spec.entities}
    assert resolved_sources == spec_sources

  def test_entity_key_columns_match(self):
    """Physical key columns must be identical."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding

    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_keys = {e.name: e.key_columns for e in graph.entities}
    spec_keys = {e.name: tuple(e.keys.primary) for e in spec.entities}
    assert resolved_keys == spec_keys

  def test_entity_property_columns_match(self):
    """Physical property column names must be identical."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding

    spec = graph_spec_from_ontology_binding(ont, bnd)

    for re, se in zip(
        sorted(graph.entities, key=lambda e: e.name),
        sorted(spec.entities, key=lambda e: e.name),
    ):
      resolved_cols = [p.column for p in re.properties]
      spec_cols = [p.name for p in se.properties]
      assert resolved_cols == spec_cols, f"Mismatch on {re.name}"

  def test_relationship_sources_match(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding

    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_sources = {r.name: r.source for r in graph.relationships}
    spec_sources = {r.name: r.binding.source for r in spec.relationships}
    assert resolved_sources == spec_sources

  def test_relationship_endpoint_columns_match(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding

    spec = graph_spec_from_ontology_binding(ont, bnd)

    for rr, sr in zip(graph.relationships, spec.relationships):
      assert rr.from_columns == tuple(sr.binding.from_columns)
      assert rr.to_columns == tuple(sr.binding.to_columns)

  def test_entity_labels_match(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding

    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_labels = {e.name: e.labels for e in graph.entities}
    spec_labels = {e.name: tuple(e.labels) for e in spec.entities}
    assert resolved_labels == spec_labels


class TestDerivedPropertyRejection:
  """resolve() must reject derived properties, matching runtime_spec behavior."""

  def test_entity_derived_property_raises(self):
    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: Person
          keys: {primary: [pid]}
          properties:
            - {name: pid, type: string}
            - {name: first, type: string}
            - {name: last, type: string}
            - name: full
              type: string
              expr: "first || ' ' || last"
    """
        )
    )
    bnd = load_binding_from_string(
        textwrap.dedent(
            """\
      binding: b
      ontology: test
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Person
          source: t
          properties:
            - {name: pid, column: pid}
            - {name: first, column: first_name}
            - {name: last, column: last_name}
    """
        ),
        ontology=ont,
    )

    import pytest

    with pytest.raises(ValueError, match="derived expression"):
      resolve(ont, bnd)

  def test_relationship_derived_property_raises(self):
    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: A
          keys: {primary: [id]}
          properties: [{name: id, type: string}]
      relationships:
        - name: R
          from: A
          to: A
          properties:
            - {name: val, type: string}
            - name: derived
              type: string
              expr: "'prefix_' || val"
    """
        )
    )
    bnd = load_binding_from_string(
        textwrap.dedent(
            """\
      binding: b
      ontology: test
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: A
          source: t
          properties: [{name: id, column: id}]
      relationships:
        - name: R
          source: e
          from_columns: [a]
          to_columns: [b]
          properties: [{name: val, column: v}]
    """
        ),
        ontology=ont,
    )

    import pytest

    with pytest.raises(ValueError, match="derived expression"):
      resolve(ont, bnd)


class TestUnmatchedLineageWarning:
  """resolve() must warn on lineage_config keys that don't match any relationship."""

  def test_unmatched_lineage_key_warns(self):
    import io
    import logging

    from bigquery_agent_analytics.resolved_spec import LineageEdgeConfig

    ont, bnd = _load()
    lineage = {
        "NONEXISTENT": LineageEdgeConfig(
            from_session_column="a",
            to_session_column="b",
        )
    }
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    log = logging.getLogger(
        "bigquery_agent_analytics.bigquery_agent_analytics.resolved_spec"
    )
    log.addHandler(handler)
    try:
      resolve(ont, bnd, lineage_config=lineage)
      output = stream.getvalue()
      assert "NONEXISTENT" in output
    finally:
      log.removeHandler(handler)


class TestInheritance:
  """resolve() must honor inherited keys and properties from extends chains."""

  def test_child_entity_inherits_parent_keys(self):
    """A child entity with no keys: block should use the parent's primary key."""
    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: Party
          keys: {primary: [party_id]}
          properties:
            - {name: party_id, type: string}
            - {name: name, type: string}
        - name: Person
          extends: Party
          properties:
            - {name: dob, type: date}
    """
        )
    )
    bnd = load_binding_from_string(
        textwrap.dedent(
            """\
      binding: b
      ontology: test
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Person
          source: persons
          properties:
            - {name: party_id, column: pid}
            - {name: name, column: display_name}
            - {name: dob, column: date_of_birth}
    """
        ),
        ontology=ont,
    )

    graph = resolve(ont, bnd)
    person = graph.entities[0]
    assert person.name == "Person"
    # Key inherited from Party, remapped through binding.
    assert person.key_columns == ("pid",)

  def test_child_entity_inherits_parent_properties(self):
    """A child entity should include inherited properties in the resolved view."""
    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: Party
          keys: {primary: [party_id]}
          properties:
            - {name: party_id, type: string}
            - {name: name, type: string}
        - name: Person
          extends: Party
          properties:
            - {name: dob, type: date}
    """
        )
    )
    bnd = load_binding_from_string(
        textwrap.dedent(
            """\
      binding: b
      ontology: test
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Person
          source: persons
          properties:
            - {name: party_id, column: pid}
            - {name: name, column: display_name}
            - {name: dob, column: date_of_birth}
    """
        ),
        ontology=ont,
    )

    graph = resolve(ont, bnd)
    person = graph.entities[0]
    logical_names = [p.logical_name for p in person.properties]
    # Must include inherited (party_id, name) + own (dob).
    assert "party_id" in logical_names
    assert "name" in logical_names
    assert "dob" in logical_names
    assert len(logical_names) == 3

  def test_child_entity_labels_include_parent(self):
    """A child entity's labels should be (child_name, parent_name)."""
    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: Party
          keys: {primary: [party_id]}
          properties:
            - {name: party_id, type: string}
        - name: Person
          extends: Party
          properties:
            - {name: dob, type: date}
    """
        )
    )
    bnd = load_binding_from_string(
        textwrap.dedent(
            """\
      binding: b
      ontology: test
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Person
          source: persons
          properties:
            - {name: party_id, column: pid}
            - {name: dob, column: dob}
    """
        ),
        ontology=ont,
    )

    graph = resolve(ont, bnd)
    person = graph.entities[0]
    assert person.labels == ("Person", "Party")

  def test_child_relationship_inherits_parent_properties(self):
    """A child relationship should include inherited properties."""
    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: ParentRel
          from: A
          to: A
          properties:
            - {name: weight, type: double}
        - name: ChildRel
          extends: ParentRel
          from: A
          to: A
          properties:
            - {name: note, type: string}
    """
        )
    )
    bnd = load_binding_from_string(
        textwrap.dedent(
            """\
      binding: b
      ontology: test
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: A
          source: t
          properties: [{name: id, column: id}]
      relationships:
        - name: ChildRel
          source: edges
          from_columns: [a]
          to_columns: [b]
          properties:
            - {name: weight, column: w}
            - {name: note, column: n}
    """
        ),
        ontology=ont,
    )

    graph = resolve(ont, bnd)
    child_rel = graph.relationships[0]
    logical_names = [p.logical_name for p in child_rel.properties]
    # Must include inherited (weight) + own (note).
    assert "weight" in logical_names
    assert "note" in logical_names
    assert len(logical_names) == 2
