# Context Graph V5 — Implementation Design

Status: approved
Scope: BigQuery-only V5 demo on the current SDK runtime. Offline TTL
import to runtime GraphSpec, mixed structured + AI extraction without
duplication, and additive concrete-entity temporal lineage.

Excludes: ontology/binding contract migration (`docs/ontology/*.md`),
Spanner backend, BKA runtime, and MAKO synchronization. Those are
follow-up epics listed in Part III.

Target: April 15, 2026 working session (Haiyuan, Pufan, Mikul, Umang).

Tracking: [haiyuan-eng-google/BigQuery-Agent-Analytics-SDK#83](https://github.com/haiyuan-eng-google/BigQuery-Agent-Analytics-SDK/issues/83)

---

## Part I: Current State Analysis

### What exists today (V4)

The V4 pipeline is a BigQuery-only, session-scoped, YAML-driven flow:

```
YAML GraphSpec ──> AI.GENERATE extraction ──> Materialize ──> Property Graph
```

| Module | File | Responsibility |
|--------|------|----------------|
| Spec models | `ontology_models.py` | Pydantic `GraphSpec` with embedded `BindingSpec` per entity/relationship |
| Schema compiler | `ontology_schema_compiler.py` | Compiles `GraphSpec` into AI.GENERATE prompt + `output_schema` JSON |
| Graph extractor | `ontology_graph.py` | Queries `agent_events`, runs AI.GENERATE, hydrates `ExtractedGraph` |
| Materializer | `ontology_materializer.py` | Creates BigQuery tables, routes nodes/edges, delete-then-insert |
| DDL transpiler | `ontology_property_graph.py` | Generates `CREATE PROPERTY GRAPH` DDL from spec |
| Orchestrator | `ontology_orchestrator.py` | Ties phases 1-5 into `build_ontology_graph()` |

#### Key design constraints baked into V4

1. **Combined spec shape.** `GraphSpec` carries both logical schema and
   physical bindings in a single YAML. The draft `docs/ontology/*.md`
   design separates these into `*.ontology.yaml` + `*.binding.yaml`, but
   no code implements that separation yet.

2. **Session-scoped identity.** Node IDs are `{session_id}:{entity}:{keys}`
   (`ontology_graph.py:155`). Node table KEY is `(primary_keys..., session_id)`
   (`ontology_property_graph.py:101`). Edge KEY includes `session_id`
   (`ontology_property_graph.py:194-200`). GQL showcase filters by
   `session_id` (`ontology_orchestrator.py:144`). Materializer deletes
   by `session_id` (`ontology_materializer.py:283-286`).

3. **AI-only extraction.** Two paths exist: `AI.GENERATE` (server-side,
   session-level transcript aggregation via `STRING_AGG`,
   `ontology_graph.py:76-98`) and a raw-payload fallback that returns
   untyped `raw_payload` nodes (`ontology_graph.py:465-514`). No
   structured fast-path.

4. **Runtime type subset.** Schema compiler supports 8 types: `string`,
   `int64`, `double`, `float64`, `bool`/`boolean`, `timestamp`, `date`,
   `bytes` (`ontology_schema_compiler.py:54-64`). Materializer DDL maps
   the same 8 (`ontology_materializer.py:70-81`). The ontology design
   docs define 11 types (adding `numeric`, `time`, `datetime`, `json`).

### What V5 adds (this design)

Three new capabilities, all additive to the V4 pipeline:

| Capability | V4 | V5 |
|------------|----|----|
| Ontology input | Hand-authored YAML | OWL/Turtle import to YAML |
| Extraction | AI.GENERATE only | Structured + AI.GENERATE (dedupe-safe) |
| Temporal scope | Single-session snapshots | Cross-session lineage via concrete self-edges |

### What V5 does NOT change

- `GraphSpec` combined shape (ontology + binding in one YAML)
- Session-scoped node identity (`{session_id}:{entity}:{keys}`)
- Node table KEY columns
- `_build_node_id()` logic
- Delete-then-insert idempotency pattern (lineage edges use the same
  pattern with destination-session ownership)
- BigQuery-only execution (no Spanner)

### What V5 does change (beyond new files)

- `BindingSpec` gains two optional fields (`from_session_column`,
  `to_session_column`) — backward-compatible, defaults to `None`
- `compile_edge_table_clause()` uses session column overrides when present
- `_route_edge()` sets session ownership from `to_session_column` for
  lineage edges
- `OntologyGraphManager` gains an optional `extractors` parameter

---

## Part II: V5 Demo Implementation Plan

### Step 0: Golden Fixtures

**Goal:** Lock expected pipeline behavior in tests before changing code.

#### Fixture set

| Fixture | Contents | Purpose |
|---------|----------|---------|
| `tests/fixtures/yamo_sample.ttl` | ~5 OWL classes, ~10 properties, 2 SKOS concept schemes | TTL import test input |
| `tests/fixtures/yamo_sample_import.yaml` | Expected unresolved import output | Import phase golden output |
| `tests/fixtures/yamo_sample_resolved.yaml` | Expected resolved `GraphSpec` YAML | Resolve phase golden output |
| `tests/fixtures/mixed_events.json` | Array of agent events: 3 BKA-typed structured + 3 raw unstructured + 1 partial (typed fields + free-text reasoning) | Structured extraction test |
| `tests/fixtures/mixed_extraction_expected.json` | Expected `ExtractedGraph` JSON | Extraction golden output |
| `tests/fixtures/lineage_sessions.json` | Two sessions where `sup_YahooAdUnit` entity evolves (name change, position change) | Temporal lineage test |
| `tests/fixtures/lineage_expected.json` | Expected lineage edges + materialized rows | Lineage golden output |

#### Test structure

```python
# tests/test_v5_golden.py

class TestTTLImportGolden:
    """Step 1 golden tests — import + resolve."""

    def test_import_produces_expected_artifact(self):
        result = ttl_import("tests/fixtures/yamo_sample.ttl", ...)
        assert result == load_expected("tests/fixtures/yamo_sample_import.yaml")

    def test_resolve_produces_valid_graph_spec(self):
        resolved = ttl_resolve("tests/fixtures/yamo_sample_import.yaml", ...)
        spec = load_graph_spec_from_string(resolved)  # must not raise
        assert spec == load_expected("tests/fixtures/yamo_sample_resolved.yaml")

class TestMixedExtractionGolden:
    """Step 2 golden tests — structured + AI extraction."""

    def test_structured_events_handled(self): ...
    def test_unhandled_events_reach_ai(self): ...
    def test_partial_span_included_in_transcript(self): ...

class TestLineageGolden:
    """Step 3 golden tests — temporal lineage edges."""

    def test_lineage_edges_materialized(self): ...
    def test_session_scoped_delete_preserves_lineage(self): ...
```

**Acceptance criteria:**
- Pre-existing V4 baseline tests stay green (no regressions)
- New V5 golden fixtures are committed and their test skeletons are
  authored, but the V5 tests are expected to **fail** initially — they
  define the target behavior for Steps 1-3 and start passing as each
  step lands
- All fixtures committed before any pipeline code changes

---

### Step 1: Offline TTL Import (Two-Phase)

**Goal:** Import YAMO Turtle files into a runtime-ready `GraphSpec` YAML
that `load_graph_spec()` accepts, without requiring the draft
`*.ontology.yaml` + `*.binding.yaml` contract.

#### Why two phases

`load_graph_spec()` calls `_validate_graph_spec()` which checks that every
key column exists in entity properties (`ontology_models.py:251-259`) and
that relationship endpoints reference declared entities
(`ontology_models.py:261-272`). If the importer emits `FILL_IN` for
missing keys (per `owl-import.md` section 10), `load_graph_spec()` will raise
`ValueError` at load time. "Runtime-ready GraphSpec" and "unresolved
placeholders" must be distinct artifacts.

#### Architecture

```
                                     *.import.yaml
OWL/TTL file ──> ttl_import() ──────> (unresolved artifact)
                                     + ImportReport (JSON)
                                           │
                                    user edits / defaults dict
                                           │
                                           ▼
                                     ttl_resolve() ──> GraphSpec YAML
                                                       (load_graph_spec() ready)
```

#### Phase 1: Import (`ttl_import`)

**New file:** `src/bigquery_agent_analytics/ttl_importer.py`

```python
def ttl_import(
    ttl_path: str,
    include_namespaces: list[str],
    dataset_template: str = "{{ env }}",
) -> TTLImportResult:
    """Parse OWL/Turtle and emit an unresolved import artifact.

    Args:
        ttl_path: Path to .ttl or .owl file.
        include_namespaces: IRI prefixes to include (per owl-import.md section 4).
        dataset_template: Template for binding.source (default: {{ env }}).

    Returns:
        TTLImportResult with .yaml_text and .report.
    """
```

**`TTLImportResult` model:**

```python
@dataclass
class TTLImportResult:
    yaml_text: str        # unresolved *.import.yaml content
    report: ImportReport  # structured drop/placeholder summary

@dataclass
class ImportReport:
    classes_mapped: int
    properties_mapped: int
    relationships_mapped: int
    classes_excluded: dict[str, int]     # namespace -> count
    properties_excluded: dict[str, int]
    placeholders: list[PlaceholderInfo]   # location + reason
    type_warnings: list[TypeWarning]      # unsupported type mappings
    drops: list[DropInfo]                 # OWL features we can't map
```

**Artifact boundary (execution risk mitigation):**

The unresolved output uses suffix `*.import.yaml` and a different
top-level structure. The file includes an `ontology_import:` metadata
block above the `graph:` block. `load_graph_spec_from_string()` reads
`data["graph"]` (`ontology_models.py:333`), so the YAML parses
successfully — but `_validate_graph_spec()` then rejects `FILL_IN`
values in key columns (`ontology_models.py:251-259`), producing a clear
`ValueError` pointing at the unresolved placeholder. This is the single
designed failure mode.

```yaml
# yamo_sample.import.yaml — NOT a valid GraphSpec
ontology_import:
  status: unresolved
  source_file: yamo_v2.2.ttl
  import_timestamp: "2026-04-15T10:00:00Z"
  placeholders_remaining: 3

graph:
  name: YAMO_Context_Graph

  entities:
    - name: AdUnit
      description: "A specific ad slot"
      # no owl:hasKey in OWL source
      # candidate data properties: ad_unit_id, external_ref
      binding:
        source: "{{ env }}.ad_units"
      keys:
        primary: [FILL_IN]
      properties:
        - name: ad_unit_id
          type: string
        # ...
```

The `ontology_import:` block is ignored by the YAML loader (it only reads
`data["graph"]`), but makes the file visually and programmatically
distinct from a resolved GraphSpec. Tooling can check for
`data.get("ontology_import", {}).get("status") == "unresolved"` to
produce a friendlier error before validation runs.

**Type compatibility gate:**

The current runtime type subset is:

| Runtime type | Schema compiler (`_TYPE_MAP`) | Materializer (`_DDL_TYPE_MAP`) |
|--------------|------------------------------|--------------------------------|
| `string` | `STRING` | `STRING` |
| `int64` | `INTEGER` | `INT64` |
| `double` | `NUMBER` | `FLOAT64` |
| `float64` | `NUMBER` | `FLOAT64` |
| `bool` | `BOOLEAN` | `BOOL` |
| `boolean` | `BOOLEAN` | `BOOL` |
| `timestamp` | `STRING` | `TIMESTAMP` |
| `date` | `STRING` | `DATE` |
| `bytes` | `STRING` | `BYTES` |

OWL import may produce `numeric`, `time`, `datetime`, or `json` (from
`owl-import.md` section 6 type mapping). The importer applies these
narrowing rules:

| OWL / ontology type | Runtime mapping | Rationale |
|---------------------|-----------------|-----------|
| `numeric` | `double` | Acceptable precision loss for demo |
| `time` | `string` | No BQ DDL support in materializer |
| `datetime` | `timestamp` | Semantically close |
| `json` | `string` | Serialized as text |

Any XSD type not in the OWL mapping table (`owl-import.md` section 6) maps
to `string` with a warning in the `ImportReport.type_warnings` list.

#### Phase 2: Resolve (`ttl_resolve`)

```python
def ttl_resolve(
    import_yaml_path: str,
    defaults: dict[str, Any] | None = None,
) -> str:
    """Resolve placeholders in an import artifact.

    Args:
        import_yaml_path: Path to *.import.yaml.
        defaults: Optional dict of {placeholder_location: value}
            for programmatic resolution.

    Returns:
        Runtime-ready GraphSpec YAML string.

    Raises:
        ValueError: If unresolved FILL_IN placeholders remain.
    """
```

The resolve phase:

1. Reads the `*.import.yaml` artifact
2. Applies user edits or defaults dict to replace `FILL_IN` values
3. Strips the `ontology_import:` metadata block
4. Validates via `load_graph_spec_from_string()` — must succeed
5. Returns clean `GraphSpec` YAML

**Demo workflow:** For the April 15 demo, commit the pre-resolved YAML
(`examples/yamo_resolved_graph_spec.yaml`) so the demo does not require
interactive placeholder repair. The import + resolve phases are shown as
prior-step outputs.

**Acceptance criteria:**
- `ttl_import("yamo.ttl")` produces `*.import.yaml` + `ImportReport`
- `load_graph_spec()` raises on the unresolved artifact (by design)
- `ttl_resolve()` produces YAML that `load_graph_spec()` accepts
- No type outside `{string, int64, double, float64, bool, boolean, timestamp, date, bytes}` reaches `_bq_schema_type()` or `_ddl_type()`
- Type narrowing decisions logged in `ImportReport.type_warnings`
- Golden fixture test passes

---

### Step 2: Structured Extractor Registry (Dedupe-Safe)

**Goal:** Route known event types directly to `ExtractedNode`/`ExtractedEdge`
without LLM extraction, falling back to AI.GENERATE for unstructured
events. Prevent double-extraction of structured events.

#### Why deduplication matters

The current AI path uses session-level transcript aggregation:

```sql
-- ontology_graph.py:76-98
WITH session_transcripts AS (
  SELECT session_id,
    STRING_AGG(COALESCE(...), '\n---\n' ORDER BY timestamp) AS transcript
  FROM agent_events
  WHERE session_id IN UNNEST(@session_ids)
    AND event_type IN ('LLM_RESPONSE', 'TOOL_COMPLETED', ...)
  GROUP BY session_id
)
SELECT session_id, AI.GENERATE(CONCAT(prompt, transcript), ...) ...
```

If structured-handled events remain in the `STRING_AGG` transcript, the
LLM will re-extract the same facts, producing duplicate nodes/edges. The
structured fast path must be **additive without duplication**.

#### Data contracts

**New file:** `src/bigquery_agent_analytics/structured_extraction.py`

```python
@dataclass
class StructuredExtractionResult:
    """Result from a structured extractor."""
    nodes: list[ExtractedNode]
    edges: list[ExtractedEdge]
    fully_handled_span_ids: set[str]      # exclude from AI transcript
    partially_handled_span_ids: set[str]  # include with extraction hint

StructuredExtractor = Callable[
    [dict, GraphSpec],  # (event_dict, spec)
    StructuredExtractionResult,
]
```

**Handling semantics:**

| Category | Meaning | AI transcript behavior |
|----------|---------|----------------------|
| `fully_handled_span_ids` | All semantic content extracted structurally | Excluded from `STRING_AGG` |
| `partially_handled_span_ids` | Typed fields extracted, but span also has free-text reasoning | Included in `STRING_AGG` with prompt hint listing already-extracted facts |
| Neither | Unstructured event | Included normally |

#### Modified extraction flow

Changes to `OntologyGraphManager`:

```python
class OntologyGraphManager:
    def __init__(
        self,
        ...
        extractors: dict[str, StructuredExtractor] | None = None,
    ) -> None:
        ...
        self.extractors = extractors or {}
```

**New `extract_graph` flow:**

```
1. Query raw events (all spans for sessions)
         │
         ▼
2. For each event, check event_type against self.extractors
    ├── Match found ──> Run extractor ──> Collect nodes, edges,
    │                                      fully/partially_handled_span_ids
    └── No match ──> Skip (will be handled by AI)
         │
         ▼
3. Build AI.GENERATE transcript CTE with exclusion filter:
   WHERE base.span_id NOT IN UNNEST(@fully_handled_span_ids)
         │
         ▼
4. For partially_handled spans, prepend to AI prompt:
   "The following facts were already extracted from typed events,
    focus on unstructured content: [list of extracted entity names]"
         │
         ▼
5. Run AI.GENERATE on filtered transcript
         │
         ▼
6. Merge: structured nodes/edges + AI-extracted nodes/edges
   (dedup by node_id — structured wins on conflict)
```

**Modified SQL template** (`_EXTRACT_ONTOLOGY_AI_QUERY`):

```sql
WITH session_transcripts AS (
  SELECT
    base.session_id,
    STRING_AGG(
      COALESCE(...),
      '\n---\n'
      ORDER BY base.timestamp ASC
    ) AS transcript
  FROM `{project}.{dataset}.{table}` AS base
  WHERE base.session_id IN UNNEST(@session_ids)
    AND base.event_type IN (...)
    AND base.content IS NOT NULL
    AND base.span_id NOT IN UNNEST(@excluded_span_ids)  -- NEW
  GROUP BY base.session_id
)
...
```

When `self.extractors` is empty (backward-compatible default),
`@excluded_span_ids` is an empty array and the query is identical to V4.

#### Example structured extractor

```python
def extract_bka_decision_event(
    event: dict,
    spec: GraphSpec,
) -> StructuredExtractionResult:
    """Extract a BKA-typed decision event into DecisionPoint + CandidateEdge."""
    content = event.get("content", {})
    if not isinstance(content, dict) or "decision_id" not in content:
        return StructuredExtractionResult([], [], set(), set())

    session_id = event["session_id"]
    span_id = event["span_id"]

    # Build DecisionPoint node
    dp_node = ExtractedNode(
        node_id=f"{session_id}:mako_DecisionPoint:decision_id={content['decision_id']}",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[
            ExtractedProperty(name="decision_id", value=content["decision_id"]),
            ExtractedProperty(name="decision_type", value=content.get("decision_type", "")),
        ],
    )

    nodes = [dp_node]
    edges = []

    # Check if the event also has free-text reasoning
    has_free_text = bool(content.get("reasoning_text"))

    if has_free_text:
        return StructuredExtractionResult(
            nodes=nodes,
            edges=edges,
            fully_handled_span_ids=set(),
            partially_handled_span_ids={span_id},
        )
    else:
        return StructuredExtractionResult(
            nodes=nodes,
            edges=edges,
            fully_handled_span_ids={span_id},
            partially_handled_span_ids=set(),
        )
```

**Acceptance criteria:**
- Structured events produce nodes/edges without LLM round-trip
- `fully_handled_span_ids` are excluded from AI transcript (`STRING_AGG`)
- `partially_handled_span_ids` are included with extraction hint in prompt
- Sessions with zero structured events produce identical results to V4
- Golden fixture test passes for mixed-event fixture (including partial case)
- No changes to `OntologyGraphManager` public API when `extractors` is `None`

---

### Step 3: Concrete-Entity Temporal Lineage

**Goal:** Track how the same business entity evolves across sessions,
without changing node key semantics or the delete-then-insert pattern.

#### Why not a generic EVOLVED_FROM

`RelationshipSpec` requires concrete `from_entity` and `to_entity` names
(`ontology_models.py:121-124`). `_validate_graph_spec()` checks that both
are declared entities (`ontology_models.py:261-272`). A generic
`EVOLVED_FROM: Entity -> Entity` cannot pass validation because `Entity`
is not a declared entity name.

Additionally, a single generic edge table would need columns from every
entity's primary key, making the schema unbounded.

#### Design: per-entity self-edge relationships

Add concrete lineage relationship types as self-edges on specific entity
types. Each lineage relationship:

- Points from one session's version to the same entity in another session
- Is owned by the **destination session** for delete-safety
- Lives in a separate edge table

#### Required contract extension: BindingSpec session columns

The current DDL compiler hardcodes endpoint session key construction
(`ontology_property_graph.py:202-208`):

```python
# Current V4 — always uses the edge's own session_id for both endpoints
src_key_str = ", ".join([*from_cols, "session_id"])
dst_key_str = ", ".join([*to_cols, "session_id"])
```

For lineage edges, SOURCE needs `from_session_id` (the prior session)
and DESTINATION needs `to_session_id` (the current session), both
mapping to the node table's `session_id` key. The current code cannot
express this — it always appends the edge's own `session_id` column
for both sides.

**Solution: extend `BindingSpec` with optional session column overrides.**

Change in `ontology_models.py`:

```python
class BindingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(...)
    from_columns: Optional[list[str]] = Field(default=None, ...)
    to_columns: Optional[list[str]] = Field(default=None, ...)
    # NEW: optional session column overrides for cross-session edges
    from_session_column: Optional[str] = Field(
        default=None,
        description=(
            "Edge column to use as session key for SOURCE endpoint. "
            "When set, this column maps to the source node's session_id "
            "key instead of the edge's own session_id. "
            "Used for cross-session lineage edges."
        ),
    )
    to_session_column: Optional[str] = Field(
        default=None,
        description=(
            "Edge column to use as session key for DESTINATION endpoint. "
            "When set, this column maps to the destination node's "
            "session_id key instead of the edge's own session_id. "
            "Used for cross-session lineage edges."
        ),
    )
```

When both are `None` (the default), behavior is identical to V4: the
edge's own `session_id` column is used for both endpoints. This is a
backward-compatible extension — existing YAML specs are unaffected.

**YAML spec additions** (added to `ymgo_graph_spec.yaml`):

```yaml
relationships:
  # ... existing CandidateEdge, ForCandidate ...

  - name: sup_YahooAdUnitEvolvedFrom
    description: "Tracks evolution of a YahooAdUnit across sessions."
    from_entity: sup_YahooAdUnit
    to_entity: sup_YahooAdUnit
    binding:
      source: "{{ env }}.sup_yahoo_ad_unit_lineage"
      from_columns: [adUnitId]
      to_columns: [adUnitId]
      from_session_column: from_session_id   # NEW
      to_session_column: to_session_id       # NEW
    properties:
      - name: from_session_id
        type: string
        description: "Session where the prior version appeared."
      - name: to_session_id
        type: string
        description: "Session where the evolved version appeared."
      - name: event_time
        type: timestamp
        description: "When the evolution was detected."
      - name: changed_properties
        type: string
        description: "Comma-separated list of properties that changed."
```

#### DDL compiler changes

Modify `compile_edge_table_clause()` in `ontology_property_graph.py`
to use the session column overrides when present:

```python
# ontology_property_graph.py — compile_edge_table_clause()

# Session key columns for SOURCE and DESTINATION endpoints.
# Default: edge's own session_id (V4 behavior).
# Override: binding.from_session_column / to_session_column (V5 lineage).
src_session_col = rel.binding.from_session_column or "session_id"
dst_session_col = rel.binding.to_session_column or "session_id"

# SOURCE KEY uses src_session_col mapped to node's session_id key
src_key_str = ", ".join([*from_cols, src_session_col])
src_ref_str = ", ".join([*src.keys.primary, "session_id"])

# DESTINATION KEY uses dst_session_col mapped to node's session_id key
dst_key_str = ", ".join([*to_cols, dst_session_col])
dst_ref_str = ", ".join([*tgt.keys.primary, "session_id"])
```

When `from_session_column` and `to_session_column` are both `None`, this
produces identical DDL to V4. When set, the generated DDL maps the
override columns to the node's `session_id` key.

#### Materializer changes

Modify `_route_edge()` in `ontology_materializer.py` to set the edge's
`session_id` column to the destination session value when
`to_session_column` is set:

```python
# ontology_materializer.py — _route_edge()

# Determine session_id for delete-scoped ownership.
# For lineage edges with to_session_column, session_id = to_session value.
# For normal edges, session_id = the session being processed (V4 behavior).
if rel.binding.to_session_column and rel.binding.to_session_column in row:
    row["session_id"] = row[rel.binding.to_session_column]
else:
    row["session_id"] = session_id
```

This ensures lineage edges are owned by the destination session for
delete-then-insert idempotency.

#### Physical table schema

```sql
CREATE TABLE IF NOT EXISTS `project.dataset.sup_yahoo_ad_unit_lineage` (
  adUnitId STRING,          -- shared key (same business entity)
  from_session_id STRING,   -- source session (maps to SOURCE KEY)
  to_session_id STRING,     -- destination session (maps to DESTINATION KEY)
  event_time TIMESTAMP,
  changed_properties STRING,
  session_id STRING,        -- = to_session_id (for delete-scoped ownership)
  extracted_at TIMESTAMP
)
```

#### Lineage edge ownership and delete-safety

The materializer deletes by `session_id` (`ontology_materializer.py:283-286`):

```sql
DELETE FROM `table` WHERE session_id IN UNNEST(@session_ids)
```

Lineage edges span two sessions. The `session_id` column on the lineage
edge table is set to `to_session_id` (the destination) via the
materializer change above. This means:

- Re-running extraction for session B deletes and re-creates lineage
  edges where B is the destination
- Re-running extraction for session A does NOT delete lineage edges
  where A is the source and B is the destination
- This matches the semantics: "session B discovered that entity X
  evolved from session A"

#### Lineage detection logic

**New function** in `ontology_graph.py`:

```python
def detect_lineage_edges(
    current_graph: ExtractedGraph,
    current_session_id: str,
    prior_graphs: dict[str, ExtractedGraph],
    lineage_entity_types: list[str],
    spec: GraphSpec,
) -> list[ExtractedEdge]:
    """Detect evolution edges between current and prior session graphs.

    For each entity type in lineage_entity_types, find nodes that share
    the same primary key across sessions and have different property
    values.

    Args:
        current_graph: Graph extracted from the current session.
        current_session_id: The current session ID.
        prior_graphs: Dict of {session_id: ExtractedGraph} for prior sessions.
        lineage_entity_types: Entity names to check for evolution.
        spec: GraphSpec for entity key lookups.

    Returns:
        List of ExtractedEdge instances for the lineage relationships.
    """
```

The detection is purely additive:

1. For each entity type in `lineage_entity_types`, collect current-session
   nodes keyed by primary key values
2. For each prior session, collect prior-session nodes for the same entity
   type
3. For each shared primary key: diff property values
4. If any property changed, emit a lineage edge with `changed_properties`
   listing the changed fields
5. If no property changed, no lineage edge (identical across sessions)

#### Property Graph DDL for lineage

With the `BindingSpec` extension, the DDL compiler produces:

```sql
`project.dataset.sup_yahoo_ad_unit_lineage` AS sup_YahooAdUnitEvolvedFrom
  KEY (adUnitId, from_session_id, to_session_id, session_id)
  SOURCE KEY (adUnitId, from_session_id)
    REFERENCES sup_YahooAdUnit (adUnitId, session_id)
  DESTINATION KEY (adUnitId, to_session_id)
    REFERENCES sup_YahooAdUnit (adUnitId, session_id)
  LABEL sup_YahooAdUnitEvolvedFrom
  PROPERTIES (
    event_time,
    changed_properties,
    extracted_at
  )
```

`SOURCE KEY (adUnitId, from_session_id)` maps to the source node's
`(adUnitId, session_id)` key. `DESTINATION KEY (adUnitId, to_session_id)`
maps to the destination node's `(adUnitId, session_id)` key. This allows
GQL traversal across sessions.

#### Validation additions

`_validate_graph_spec()` needs two new checks for session column
overrides:

1. If `from_session_column` is set, it must name a declared property
   on the relationship
2. If `to_session_column` is set, same rule
3. Both must be set together or both absent (like `from_columns`/`to_columns`)

#### GQL showcase: cross-session lineage traversal

New showcase template in `ontology_orchestrator.py`:

```sql
GRAPH `{graph_ref}`
MATCH
  (prior:sup_YahooAdUnit)-[ev:sup_YahooAdUnitEvolvedFrom]->(current:sup_YahooAdUnit)
WHERE current.session_id = @session_id
RETURN
  prior.adUnitId,
  prior.adUnitName AS prior_name,
  prior.adUnitPosition AS prior_position,
  ev.from_session_id,
  ev.changed_properties,
  ev.event_time,
  current.adUnitName AS current_name,
  current.adUnitPosition AS current_position
ORDER BY ev.event_time DESC
LIMIT @result_limit
```

**Acceptance criteria:**
- Existing session-scoped pipeline works unchanged (all V4 tests pass) —
  `from_session_column` / `to_session_column` default to `None`,
  producing identical DDL and materialization behavior
- Lineage edges materialized in separate table per entity type
- `_build_node_id()` and node table KEY columns unmodified
- DDL compiler uses `from_session_column` / `to_session_column` for
  SOURCE KEY / DESTINATION KEY when present
- Materializer sets `session_id = to_session_column` value for
  delete-scoped ownership
- Session-scoped delete of session B removes lineage edges owned by B
  (destination) but not lineage edges where B is the source
- GQL traversal across sessions works for entities with lineage
- Golden fixture test passes for lineage fixture

---

### Step 4: V5 Demo Notebook

**File:** `examples/ontology_graph_v5_demo.ipynb`

#### Demo flow

```
Cell 1: Environment setup
  - Project/dataset configuration
  - Import SDK modules

Cell 2: TTL Import (Step 1 — show, don't run live)
  - Display the YAMO sample TTL fixture
  - Show ttl_import() output: *.import.yaml + ImportReport
  - Show ttl_resolve() output: resolved GraphSpec YAML
  - Load the pre-committed resolved YAML for runtime use

Cell 3: Mixed Extraction (Step 2)
  - Register structured extractors for BKA-typed decision events
  - Run extract_graph() with mixed structured + AI.GENERATE
  - Display extraction stats: N structured, M AI-extracted, K partial
  - Show that structured events are excluded from AI transcript

Cell 4: Materialization
  - Create tables and materialize extracted graph
  - Display row counts per entity/relationship table

Cell 5: Temporal Lineage (Step 3)
  - Run extraction for a second session with evolved entities
  - Detect and materialize lineage edges
  - Display lineage edge details: which properties changed

Cell 6: Property Graph + GQL
  - Create Property Graph (including lineage edge tables)
  - Run forward traversal GQL (V4 showcase)
  - Run cross-session lineage GQL (V5 showcase)
  - Display results side by side

Cell 7: Summary
  - Architecture diagram
  - Performance comparison: structured vs AI extraction time
  - What's next: link to follow-up epics
```

**Acceptance criteria:**
- Notebook runs end-to-end against a live BQ dataset
- All seven cells produce visible output
- No interactive placeholder repair required (pre-committed resolved YAML)
- Mixed extraction demonstrably faster for known event types
- Cross-session lineage GQL returns meaningful results

---

## Execution Risks

### Risk 1: Import/Resolve Artifact Boundary

**Risk:** If the unresolved import artifact looks too similar to a runtime
`GraphSpec`, someone will feed it into `load_graph_spec()` by mistake.

**Mitigation:** Use `*.import.yaml` suffix and `ontology_import:` metadata
block. The YAML parses (the loader reads `data["graph"]`), but
`_validate_graph_spec()` rejects `FILL_IN` values in key columns with a
clear `ValueError`. The `ontology_import:` block makes the file visually
distinct, and tooling can check
`data.get("ontology_import", {}).get("status") == "unresolved"` to
produce a friendlier error before validation runs.

### Risk 2: Partial Span Handling

**Risk:** Binary `fully_handled` / `not handled` under-extracts when a
structured extractor handles only part of a span's semantics (e.g.,
typed BKA fields extracted, but free-text reasoning in the same span
only the LLM would catch).

**Mitigation:** The `StructuredExtractionResult` contract distinguishes
`fully_handled_span_ids` (exclude from transcript) from
`partially_handled_span_ids` (include with extraction hint). Step 0's
mixed-event fixture includes at least one partial-handling test case to
drive this design before registry code is written.

### Risk 3: Lineage DDL Self-Reference

**Risk:** BigQuery Property Graph `SOURCE KEY ... REFERENCES` syntax for
self-referential edges (same node table for both SOURCE and DESTINATION)
may have undocumented restrictions.

**Mitigation:** Validate the self-referential DDL against BigQuery before
committing to this design. If self-reference is not supported, fall back
to a dedicated lineage node type (e.g., `sup_YahooAdUnit_Version` with
`version_id` key) and point edges between versions.

---

## Part III: Long-Term Production Epics

These are follow-up issues, each with their own design docs and acceptance
criteria. None are blockers for the V5 demo. Dependency graph:

```
B1 (ontology + binding compiler, flat only)
├── B2 (full OWL importer emitting *.ontology.yaml)
├── B3 (Spanner backend)
│   └── B5 (MAKO governance bridge)
├── B4 (BKA ingestion contract)
└── B6 (inheritance lowering in compiler)
```

### Epic B1: Ontology + Binding Compiler (Flat Only)

Implement the draft design docs (`docs/ontology/*.md`) as code.

**Scope:**
- `*.ontology.yaml` parser with validation per `ontology.md` section 10 (all 13
  rules: unique names, key integrity, type validation, `extra="forbid"`)
- `*.binding.yaml` parser with validation per `binding.md` section 9 (all 13
  rules: name resolution, property coverage, type compatibility)
- Resolver: cross-check ontology + binding names, substitute derived
  expressions, resolve endpoints per `compilation.md` section 3
- Emitter: produce `CREATE PROPERTY GRAPH` DDL for BigQuery and Spanner
  per `compilation.md` section 4
- `gm validate` CLI command per `cli.md` section 4
- `gm compile` CLI command per `cli.md` section 5
- Migrate runtime from `GraphSpec` (combined) to ontology + binding
  (separated)

**Explicit acceptance criterion:** `extends` on entities or relationships
is rejected at compile time with a clear error message. This is specified
in `compilation.md:8`: "v0 compiles flat ontologies only." Inheritance
lowering is Epic B6's scope.

**Key design decisions to make:**
- How to handle the transition period where both `GraphSpec` (V4/V5) and
  `*.ontology.yaml` + `*.binding.yaml` (B1) coexist
- Whether `gm validate` replaces or wraps `load_graph_spec()`

### Epic B2: Full OWL/Turtle Importer (`gm import-owl`)

Implement `owl-import.md` fully, emitting `*.ontology.yaml` (not
`GraphSpec`).

**Scope:**
- Full `gm import-owl` CLI command per `cli.md` section 6
- Namespace filtering per `owl-import.md` section 4
- Complete OWL mapping table per `owl-import.md` section 5
- Deterministic output per `owl-import.md` section 14
- Drop surfacing: structured annotations + YAML comments per
  `owl-import.md` section 13
- Placeholder resolution workflow per `owl-import.md` section 11
- YAMO v2.2 scale target: 36 classes, 112 properties, 31 SKOS schemes

**Depends on:** Epic B1 (needs `*.ontology.yaml` contract).

**Relationship to V5 Step 1:** The V5 demo importer is a bridge — it
emits the current `GraphSpec` shape. Epic B2 replaces it with the
production importer emitting the new contract.

### Epic B3: Spanner Graph Backend

**Scope:**
- Spanner target in `*.binding.yaml` per `binding.md` section 3:
  `backend: spanner`, `instance:`, `database:`
- Spanner DDL emission in compiler per `compilation.md` section 4
- Type compatibility validation: `time` and `datetime` rejected on
  Spanner targets per `binding.md` section 8
- Runtime layer for <100ms semantic search (Spanner Graph)

**Depends on:** Epic B1 (needs binding contract with `target.backend`).

### Epic B4: BKA Ingestion Contract

**Scope:**
- Typed Python logging SDK for YMGO orchestrator
- Structured event schema for decision traces
- High-throughput ingestion path: 5M-10M events/day target
- Maps BKA events to ontology entities (per B1 contract)

**Depends on:** Epic B1 (structured events map to ontology entities).

**Relationship to V5 Step 2:** The V5 structured extractors are a
lightweight bridge. Epic B4 replaces ad-hoc extractors with a formal
typed SDK.

### Epic B5: MAKO Governance Bridge

**Scope:**
- Spanner to BigQuery synchronization layer
- Temporal `EVOLVED_FROM` replication from hot (Spanner) to warm
  (BigQuery)
- EU regulatory audit support (DSA/GDPR): immutable decision lineage
- Hot/warm consistency guarantees

**Depends on:** Epic B3 (needs Spanner backend) and Epic B1 (needs
temporal lineage model finalized in the ontology contract).

### Epic B6: Inheritance Lowering in Compiler

**Scope:**
- `extends` support in compilation — currently rejected per
  `compilation.md:8`
- Substitutability: `MATCH (:Parent)` matches all descendants
- Per-label property projections
- Lowering strategies: label-referenced edges, fan-out, union views
- Cross-table identity for inherited entities
- Overlapping sibling handling

**Depends on:** Epic B1 (needs flat compiler as foundation).

Explicitly carved out from B1 per `compilation.md:238-240`: "Inheritance
lowering — substitutability, per-label property projections, cross-table
identity, overlapping siblings — is the subject of a separate future
design."

---

## Module Change Summary

| File | Step 0 | Step 1 | Step 2 | Step 3 |
|------|--------|--------|--------|--------|
| `ttl_importer.py` | — | **NEW** | — | — |
| `structured_extraction.py` | — | — | **NEW** | — |
| `ontology_models.py` | — | — | — | Modified (`BindingSpec`: add `from_session_column`, `to_session_column`) |
| `ontology_graph.py` | — | — | Modified (extractor registry, span exclusion) | Modified (lineage detection) |
| `ontology_property_graph.py` | — | — | — | Modified (use session column overrides in `compile_edge_table_clause`) |
| `ontology_materializer.py` | — | — | — | Modified (`_route_edge` session ownership from `to_session_column`) |
| `ontology_orchestrator.py` | — | — | — | Modified (lineage GQL template) |
| `ontology_schema_compiler.py` | — | — | — | — (no changes) |
| `tests/test_v5_golden.py` | **NEW** | Extended | Extended | Extended |
| `tests/fixtures/` | **NEW** (7 files) | — | — | — |
| `examples/yamo_resolved_graph_spec.yaml` | — | **NEW** | — | — |
| `examples/ontology_graph_v5_demo.ipynb` | — | — | — | **NEW** |
