# Phase 1: Foundation — ExtractedModels + ResolvedSpec

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the two foundational modules that all subsequent migration phases depend on: `extracted_models.py` (move runtime extraction models out of `ontology_models.py`) and `resolved_spec.py` (the new internal runtime model built from upstream `Ontology` + `Binding`).

**Architecture:** Phase 1 is purely additive — zero consumers change. PR 1a moves 4 Pydantic models to a new file with re-exports for backward compat. PR 1b defines frozen dataclasses for the resolved runtime view and a builder function that produces output physically equivalent to the current `graph_spec_from_ontology_binding()` pipeline. Both PRs must leave all 1684 existing tests passing.

**Tech Stack:** Python 3.10+, Pydantic v2, dataclasses (frozen), pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/bigquery_agent_analytics/extracted_models.py` | CREATE | `ExtractedProperty`, `ExtractedNode`, `ExtractedEdge`, `ExtractedGraph` |
| `src/bigquery_agent_analytics/ontology_models.py` | MODIFY | Remove extracted model classes, add re-exports from `extracted_models` |
| `src/bigquery_agent_analytics/resolved_spec.py` | CREATE | `ResolvedProperty`, `ResolvedEntity`, `ResolvedRelationship`, `ResolvedGraph`, `resolve()` builder |
| `tests/test_extracted_models.py` | CREATE | Verify extracted models importable from both old and new paths |
| `tests/test_resolved_spec.py` | MODIFY | Add physical-layout equivalence tests for `resolve()` vs `graph_spec_from_ontology_binding()` |

---

## Task 1: Extract `ExtractedGraph` models to `extracted_models.py`

**Files:**
- Create: `src/bigquery_agent_analytics/extracted_models.py`
- Modify: `src/bigquery_agent_analytics/ontology_models.py:168-213`
- Create: `tests/test_extracted_models.py`

- [ ] **Step 1: Create `extracted_models.py` with the 4 model classes**

Copy the 4 Pydantic models verbatim from `ontology_models.py`. No changes to field names, types, or defaults.

```python
# src/bigquery_agent_analytics/extracted_models.py
"""Runtime containers for AI-extracted graph instances.

These models represent the output of the extraction pipeline — nodes,
edges, and property values extracted from agent telemetry by AI or
structured extractors. They are SDK-specific and have no upstream
equivalent in the ``bigquery_ontology`` package.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import Field


class ExtractedProperty(BaseModel):
  """A single property value on an extracted node or edge."""

  name: str = Field(description="Property name.")
  value: Any = Field(description="Property value.")


class ExtractedNode(BaseModel):
  """A node instance extracted from agent telemetry."""

  node_id: str = Field(description="Unique node identifier.")
  entity_name: str = Field(description="Entity type from the spec.")
  labels: list[str] = Field(default_factory=list, description="Node labels.")
  properties: list[ExtractedProperty] = Field(
      default_factory=list, description="Property values."
  )


class ExtractedEdge(BaseModel):
  """An edge instance extracted from agent telemetry."""

  edge_id: str = Field(description="Unique edge identifier.")
  relationship_name: str = Field(
      description="Relationship type from the spec."
  )
  from_node_id: str = Field(description="Source node ID.")
  to_node_id: str = Field(description="Target node ID.")
  properties: list[ExtractedProperty] = Field(
      default_factory=list, description="Edge property values."
  )


class ExtractedGraph(BaseModel):
  """A complete graph instance extracted from agent telemetry."""

  name: str = Field(description="Graph name from the spec.")
  nodes: list[ExtractedNode] = Field(
      default_factory=list, description="Extracted nodes."
  )
  edges: list[ExtractedEdge] = Field(
      default_factory=list, description="Extracted edges."
  )
```

- [ ] **Step 2: Replace classes in `ontology_models.py` with re-exports**

In `src/bigquery_agent_analytics/ontology_models.py`, replace lines 168-213 (the 4 class definitions) with re-exports. This preserves backward compatibility — any code that does `from .ontology_models import ExtractedGraph` still works.

Replace the block from `class ExtractedProperty(BaseModel):` through the end of `class ExtractedGraph(BaseModel):` with:

```python
# ------------------------------------------------------------------ #
# Extracted Models (re-exported from extracted_models.py)              #
# ------------------------------------------------------------------ #

from .extracted_models import ExtractedEdge
from .extracted_models import ExtractedGraph
from .extracted_models import ExtractedNode
from .extracted_models import ExtractedProperty
```

Also remove the `Any` import from the top of `ontology_models.py` if it was only used by `ExtractedProperty.value`. Check: `Any` is imported at line 42 (`from typing import Any, Optional`). It is still used by `load_from_ontology_binding` at the bottom of the file, so keep the import.

- [ ] **Step 3: Write a test verifying both import paths work**

```python
# tests/test_extracted_models.py
"""Verify extracted models are importable from both old and new paths."""

from __future__ import annotations


class TestExtractedModelsImport:

  def test_import_from_new_module(self):
    from bigquery_agent_analytics.extracted_models import (
        ExtractedEdge,
        ExtractedGraph,
        ExtractedNode,
        ExtractedProperty,
    )
    assert ExtractedGraph is not None
    assert ExtractedNode is not None
    assert ExtractedEdge is not None
    assert ExtractedProperty is not None

  def test_import_from_old_module(self):
    """Backward compat: old import path still works."""
    from bigquery_agent_analytics.ontology_models import (
        ExtractedEdge,
        ExtractedGraph,
        ExtractedNode,
        ExtractedProperty,
    )
    assert ExtractedGraph is not None

  def test_import_from_package_root(self):
    """Package-level import still works."""
    from bigquery_agent_analytics import (
        ExtractedEdge,
        ExtractedGraph,
        ExtractedNode,
        ExtractedProperty,
    )
    assert ExtractedGraph is not None

  def test_same_class_from_both_paths(self):
    """Old and new import paths resolve to the exact same class."""
    from bigquery_agent_analytics.extracted_models import (
        ExtractedGraph as New,
    )
    from bigquery_agent_analytics.ontology_models import (
        ExtractedGraph as Old,
    )
    assert New is Old

  def test_extracted_graph_round_trip(self):
    """Basic construction and serialization still works."""
    from bigquery_agent_analytics.extracted_models import (
        ExtractedEdge,
        ExtractedGraph,
        ExtractedNode,
        ExtractedProperty,
    )
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
```

- [ ] **Step 4: Run the new test**

Run: `python -m pytest tests/test_extracted_models.py -v`
Expected: 5 tests PASS

- [ ] **Step 5: Run the full test suite to confirm zero regressions**

Run: `python -m pytest tests/ tests/bigquery_ontology/ -x -q`
Expected: 1684 tests pass (or same count as before this change), 0 failures

- [ ] **Step 6: Commit**

```bash
git add src/bigquery_agent_analytics/extracted_models.py \
        src/bigquery_agent_analytics/ontology_models.py \
        tests/test_extracted_models.py
git commit -m "refactor: extract ExtractedGraph models to extracted_models.py

Move ExtractedProperty, ExtractedNode, ExtractedEdge, ExtractedGraph
from ontology_models.py to their own module. Re-exports in
ontology_models.py preserve backward compatibility. These SDK-specific
runtime models have no upstream equivalent and will survive the
GraphSpec deletion in Phase 3.

Part of #38 (Phase 1a)."
```

---

## Task 2: Define `ResolvedSpec` dataclasses

**Files:**
- Create: `src/bigquery_agent_analytics/resolved_spec.py`

- [ ] **Step 1: Create `resolved_spec.py` with frozen dataclasses**

These dataclasses mirror the physical layout that `graph_spec_from_ontology_binding()` currently produces in `GraphSpec`, but with explicit field names that distinguish logical vs physical.

```python
# src/bigquery_agent_analytics/resolved_spec.py
"""Resolved runtime specification built from Ontology + Binding.

A ``ResolvedGraph`` is the internal runtime currency of the SDK. It
fuses an upstream ``Ontology`` (logical schema) with a ``Binding``
(physical mapping) into a single resolved view where:

  - Sources are fully qualified (``project.dataset.table``).
  - Property names are physical column names (from the binding).
  - Key columns are remapped to physical column names.
  - Labels are derived from ``extends`` chains.
  - Lineage session columns are carried as SDK-specific config.
  - Metadata columns (``session_id``, ``extracted_at``) are declared.

The ``resolve()`` builder is the single place where ontology/binding
impedance matching happens. All downstream consumers read resolved
fields without reimplementing the mapping logic.
"""

from __future__ import annotations

import dataclasses
from typing import Optional


@dataclasses.dataclass(frozen=True)
class ResolvedProperty:
  """One property in the resolved runtime view.

  ``column`` is the physical column name (from the binding).
  ``logical_name`` is the ontology property name (may differ from
  column when the binding renames). ``sdk_type`` is the SDK type
  string (e.g. ``"string"``, ``"int64"``, ``"timestamp"``).
  """

  column: str
  logical_name: str
  sdk_type: str
  description: str = ""


@dataclasses.dataclass(frozen=True)
class ResolvedEntity:
  """One entity in the resolved runtime view.

  ``source`` is the fully qualified BigQuery table reference.
  ``key_columns`` are physical column names for the primary key.
  ``labels`` are derived from the entity name and ``extends`` chain.
  ``properties`` are in ontology declaration order.
  ``metadata_columns`` lists runtime columns the SDK injects
  (default: ``session_id``, ``extracted_at``).
  """

  name: str
  source: str
  key_columns: tuple[str, ...]
  labels: tuple[str, ...]
  properties: tuple[ResolvedProperty, ...]
  description: str = ""
  extends: Optional[str] = None
  metadata_columns: tuple[str, ...] = ("session_id", "extracted_at")


@dataclasses.dataclass(frozen=True)
class ResolvedRelationship:
  """One relationship in the resolved runtime view.

  ``from_columns`` / ``to_columns`` are the binding's endpoint join
  columns. ``from_session_column`` / ``to_session_column`` are the
  SDK-specific lineage session overrides (None if not configured).
  ``properties`` are in ontology declaration order.
  """

  name: str
  source: str
  from_entity: str
  to_entity: str
  from_columns: tuple[str, ...]
  to_columns: tuple[str, ...]
  properties: tuple[ResolvedProperty, ...]
  description: str = ""
  from_session_column: Optional[str] = None
  to_session_column: Optional[str] = None
  metadata_columns: tuple[str, ...] = ("session_id", "extracted_at")


@dataclasses.dataclass(frozen=True)
class ResolvedGraph:
  """Complete resolved runtime specification.

  Built once from ``Ontology`` + ``Binding`` via ``resolve()``.
  All downstream SDK modules consume this — extraction, materialization,
  DDL compilation, GQL generation.
  """

  name: str
  entities: tuple[ResolvedEntity, ...]
  relationships: tuple[ResolvedRelationship, ...]
```

- [ ] **Step 2: Run a quick import check**

Run: `python -c "from bigquery_agent_analytics.resolved_spec import ResolvedGraph, ResolvedEntity, ResolvedRelationship, ResolvedProperty; print('OK')"` 
Expected: prints `OK`

- [ ] **Step 3: Commit dataclass definitions**

```bash
git add src/bigquery_agent_analytics/resolved_spec.py
git commit -m "feat: define ResolvedSpec frozen dataclasses

ResolvedGraph, ResolvedEntity, ResolvedRelationship, ResolvedProperty
are the internal runtime currency that will replace GraphSpec. Fields
use explicit names (column vs logical_name) to avoid the ambiguity
that GraphSpec had (PropertySpec.name was the column name, not the
ontology name). metadata_columns carries session_id/extracted_at so
all consumers read from one source of truth.

Part of #38 (Phase 1b — dataclass definitions)."
```

---

## Task 3: Implement `resolve()` builder

**Files:**
- Modify: `src/bigquery_agent_analytics/resolved_spec.py`
- Modify: `tests/test_resolved_spec.py`

- [ ] **Step 1: Write the failing test for `resolve()`**

This test builds an `Ontology` + `Binding` from YAML (using the upstream loaders), calls `resolve()`, and asserts the resolved output matches the physical layout the current `graph_spec_from_ontology_binding()` pipeline produces.

```python
# tests/test_resolved_spec.py
"""Physical-layout equivalence tests for the resolve() builder.

These tests verify that resolve(ontology, binding) produces the same
physical layout (sources, columns, keys) as the current
graph_spec_from_ontology_binding() pipeline. This is NOT a semantic
round-trip — the original logical ontology property names are
intentionally replaced by physical column names.
"""

from __future__ import annotations

import textwrap

from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string

from bigquery_agent_analytics.resolved_spec import resolve
from bigquery_agent_analytics.resolved_spec import ResolvedGraph


_ONTOLOGY = textwrap.dedent("""\
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
""")

_BINDING = textwrap.dedent("""\
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
""")


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
    assert entity_map["Account"].source == "my-proj.finance.raw.accounts"
    assert entity_map["Security"].source == "my-proj.finance.ref.securities"

  def test_entity_key_columns_are_physical(self):
    """Key columns must be binding column names, not ontology names."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    entity_map = {e.name: e for e in graph.entities}
    assert entity_map["Account"].key_columns == ("acct_id",)
    assert entity_map["Security"].key_columns == ("cusip",)

  def test_entity_properties_are_physical(self):
    """Property columns must be binding column names."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    entity_map = {e.name: e for e in graph.entities}
    acct_cols = [p.column for p in entity_map["Account"].properties]
    assert acct_cols == ["acct_id", "created_ts"]

  def test_entity_properties_preserve_logical_names(self):
    """logical_name must be the ontology property name."""
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
    assert holds.source == "my-proj.finance.raw.holdings"

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
    lineage = {"HOLDS": LineageEdgeConfig(
        from_session_column="src_sid",
        to_session_column="dst_sid",
    )}
    graph = resolve(ont, bnd, lineage_config=lineage)
    holds = graph.relationships[0]
    assert holds.from_session_column == "src_sid"
    assert holds.to_session_column == "dst_sid"

  def test_resolve_is_deterministic(self):
    ont, bnd = _load()
    g1 = resolve(ont, bnd)
    g2 = resolve(ont, bnd)
    assert g1 == g2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_resolved_spec.py::TestResolveBuilder -v`
Expected: FAIL — `resolve` not defined yet

- [ ] **Step 3: Implement `resolve()` and `LineageEdgeConfig` in `resolved_spec.py`**

Add to the bottom of `src/bigquery_agent_analytics/resolved_spec.py`:

```python
@dataclasses.dataclass(frozen=True)
class LineageEdgeConfig:
  """Cross-session lineage configuration for a relationship edge."""

  from_session_column: str
  to_session_column: str


# -- Type mapping: upstream PropertyType -> SDK type string ------------

_PROPERTY_TYPE_TO_SDK: dict[str, str] = {
    "string": "string",
    "bytes": "bytes",
    "integer": "int64",
    "double": "double",
    "numeric": "double",
    "boolean": "boolean",
    "date": "date",
    "time": "string",
    "datetime": "timestamp",
    "timestamp": "timestamp",
    "json": "string",
}


# -- Source qualification -----------------------------------------------

def _qualify_source(
    raw_source: str, project: str, dataset: str
) -> str:
  """Qualify a binding source to a fully-qualified BQ table reference.

  Rules (matching runtime_spec._resolve_source):
  * 2+ dots -> used verbatim (already fully qualified).
  * 1 dot   -> ``{project}.{raw_source}`` (dataset.table).
  * 0 dots  -> ``{project}.{dataset}.{raw_source}`` (bare table).
  """
  dot_count = raw_source.count(".")
  if dot_count >= 2:
    return raw_source
  if dot_count == 1:
    return f"{project}.{raw_source}"
  return f"{project}.{dataset}.{raw_source}"


# -- Builder ------------------------------------------------------------

def resolve(
    ontology,
    binding,
    lineage_config: dict[str, LineageEdgeConfig] | None = None,
) -> ResolvedGraph:
  """Build a ``ResolvedGraph`` from an upstream Ontology + Binding.

  This is the single place where ontology/binding impedance matching
  happens. All downstream SDK modules should consume the resolved
  output rather than re-implementing the mapping.

  Args:
      ontology: A validated ``bigquery_ontology.Ontology``.
      binding: A validated ``bigquery_ontology.Binding`` referencing
          this ontology.
      lineage_config: Optional dict mapping relationship names to
          ``LineageEdgeConfig`` for cross-session lineage edges.

  Returns:
      A frozen ``ResolvedGraph`` ready for consumption by SDK runtime.

  Raises:
      ValueError: If a bound entity/relationship is not found in the
          ontology, or if an entity has no primary key.
  """
  lineage_config = lineage_config or {}
  project = binding.target.project
  dataset = binding.target.dataset

  ont_entity_map = {e.name: e for e in ontology.entities}
  ont_rel_map = {r.name: r for r in ontology.relationships}

  # -- Entities --------------------------------------------------------
  resolved_entities: list[ResolvedEntity] = []
  for eb in binding.entities:
    ont_entity = ont_entity_map.get(eb.name)
    if ont_entity is None:
      raise ValueError(
          f"Binding references entity {eb.name!r} which is not "
          f"defined in ontology {ontology.ontology!r}."
      )

    col_map: dict[str, str] = {bp.name: bp.column for bp in eb.properties}

    if ont_entity.keys is None or ont_entity.keys.primary is None:
      raise ValueError(
          f"Entity {eb.name!r} has no primary key defined."
      )
    key_columns = tuple(
        col_map.get(k, k) for k in ont_entity.keys.primary
    )

    labels: tuple[str, ...]
    if ont_entity.extends:
      labels = (ont_entity.name, ont_entity.extends)
    else:
      labels = (ont_entity.name,)

    properties: list[ResolvedProperty] = []
    for prop in ont_entity.properties:
      if prop.expr is not None:
        continue
      sdk_type = _PROPERTY_TYPE_TO_SDK.get(prop.type.value, "string")
      properties.append(ResolvedProperty(
          column=col_map.get(prop.name, prop.name),
          logical_name=prop.name,
          sdk_type=sdk_type,
          description=prop.description or "",
      ))

    resolved_entities.append(ResolvedEntity(
        name=ont_entity.name,
        source=_qualify_source(eb.source, project, dataset),
        key_columns=key_columns,
        labels=labels,
        properties=tuple(properties),
        description=ont_entity.description or "",
        extends=ont_entity.extends,
    ))

  # -- Relationships ---------------------------------------------------
  resolved_rels: list[ResolvedRelationship] = []
  for rb in binding.relationships:
    ont_rel = ont_rel_map.get(rb.name)
    if ont_rel is None:
      raise ValueError(
          f"Binding references relationship {rb.name!r} which is not "
          f"defined in ontology {ontology.ontology!r}."
      )

    col_map = {bp.name: bp.column for bp in rb.properties}

    properties = []
    for prop in ont_rel.properties:
      if prop.expr is not None:
        continue
      sdk_type = _PROPERTY_TYPE_TO_SDK.get(prop.type.value, "string")
      properties.append(ResolvedProperty(
          column=col_map.get(prop.name, prop.name),
          logical_name=prop.name,
          sdk_type=sdk_type,
          description=prop.description or "",
      ))

    lineage = lineage_config.get(rb.name)
    from_session: str | None = None
    to_session: str | None = None
    if lineage is not None:
      from_session = lineage.from_session_column
      to_session = lineage.to_session_column

    resolved_rels.append(ResolvedRelationship(
        name=ont_rel.name,
        source=_qualify_source(rb.source, project, dataset),
        from_entity=ont_rel.from_,
        to_entity=ont_rel.to,
        from_columns=tuple(rb.from_columns),
        to_columns=tuple(rb.to_columns),
        properties=tuple(properties),
        description=ont_rel.description or "",
        from_session_column=from_session,
        to_session_column=to_session,
    ))

  return ResolvedGraph(
      name=ontology.ontology,
      entities=tuple(resolved_entities),
      relationships=tuple(resolved_rels),
  )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_resolved_spec.py::TestResolveBuilder -v`
Expected: 14 tests PASS

- [ ] **Step 5: Run the full test suite to confirm zero regressions**

Run: `python -m pytest tests/ tests/bigquery_ontology/ -x -q`
Expected: all tests pass (original count + 14 new)

- [ ] **Step 6: Commit**

```bash
git add src/bigquery_agent_analytics/resolved_spec.py \
        tests/test_resolved_spec.py
git commit -m "feat: implement resolve() builder for ResolvedSpec

resolve(ontology, binding, lineage_config) -> ResolvedGraph builds
the internal runtime view from upstream models. Physical-layout
equivalence tested against the current graph_spec_from_ontology_binding
pipeline: same source qualification, same column remapping, same
key column resolution, same label derivation.

LineageEdgeConfig carries SDK-specific session column overrides.
metadata_columns defaults to (session_id, extracted_at) on both
entities and relationships.

Part of #38 (Phase 1b — resolve builder)."
```

---

## Task 4: Cross-validate `resolve()` against `graph_spec_from_ontology_binding()`

**Files:**
- Modify: `tests/test_resolved_spec.py`

This task adds a direct comparison test proving the new `resolve()` builder produces a physically equivalent layout to the existing `graph_spec_from_ontology_binding()` function. This is the key safety net for Phase 2.

- [ ] **Step 1: Write the cross-validation test**

```python
# Add to tests/test_resolved_spec.py

class TestCrossValidation:
  """Prove resolve() matches graph_spec_from_ontology_binding() output."""

  def test_entity_sources_match(self):
    """Fully-qualified sources must be identical."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import (
        graph_spec_from_ontology_binding,
    )
    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_sources = {e.name: e.source for e in graph.entities}
    spec_sources = {e.name: e.binding.source for e in spec.entities}
    assert resolved_sources == spec_sources

  def test_entity_key_columns_match(self):
    """Physical key columns must be identical."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import (
        graph_spec_from_ontology_binding,
    )
    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_keys = {
        e.name: e.key_columns for e in graph.entities
    }
    spec_keys = {
        e.name: tuple(e.keys.primary) for e in spec.entities
    }
    assert resolved_keys == spec_keys

  def test_entity_property_columns_match(self):
    """Physical property column names must be identical."""
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import (
        graph_spec_from_ontology_binding,
    )
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
    from bigquery_agent_analytics.runtime_spec import (
        graph_spec_from_ontology_binding,
    )
    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_sources = {r.name: r.source for r in graph.relationships}
    spec_sources = {
        r.name: r.binding.source for r in spec.relationships
    }
    assert resolved_sources == spec_sources

  def test_relationship_endpoint_columns_match(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import (
        graph_spec_from_ontology_binding,
    )
    spec = graph_spec_from_ontology_binding(ont, bnd)

    for rr, sr in zip(graph.relationships, spec.relationships):
      assert rr.from_columns == tuple(sr.binding.from_columns)
      assert rr.to_columns == tuple(sr.binding.to_columns)

  def test_entity_labels_match(self):
    ont, bnd = _load()
    graph = resolve(ont, bnd)
    from bigquery_agent_analytics.runtime_spec import (
        graph_spec_from_ontology_binding,
    )
    spec = graph_spec_from_ontology_binding(ont, bnd)

    resolved_labels = {e.name: e.labels for e in graph.entities}
    spec_labels = {e.name: tuple(e.labels) for e in spec.entities}
    assert resolved_labels == spec_labels
```

- [ ] **Step 2: Run cross-validation tests**

Run: `python -m pytest tests/test_resolved_spec.py::TestCrossValidation -v`
Expected: 6 tests PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ tests/bigquery_ontology/ -x -q`
Expected: all tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_resolved_spec.py
git commit -m "test: cross-validate resolve() against graph_spec_from_ontology_binding

Physical-layout equivalence tests prove that resolve(ontology, binding)
produces identical sources, key columns, property columns, endpoint
columns, and labels as the current graph_spec_from_ontology_binding()
pipeline. This is the safety net for Phase 2 consumer migration.

Part of #38 (Phase 1b — cross-validation)."
```

---

## Completion Checklist

After all 4 tasks:
- [ ] `src/bigquery_agent_analytics/extracted_models.py` exists with 4 Pydantic models
- [ ] `src/bigquery_agent_analytics/ontology_models.py` re-exports from `extracted_models`
- [ ] `src/bigquery_agent_analytics/resolved_spec.py` exists with 5 frozen dataclasses + `resolve()` builder
- [ ] `tests/test_extracted_models.py` has 5 tests (import paths + basic construction)
- [ ] `tests/test_resolved_spec.py` has 20 tests (14 builder + 6 cross-validation)
- [ ] All original 1684 tests still pass
- [ ] Zero consumers of `GraphSpec` have changed
