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

"""Ontology-driven graph extraction engine (V4).

Provides ``OntologyGraphManager`` — queries raw ADK telemetry from
``agent_events``, constructs ``AI.GENERATE`` queries using the compiled
ontology schema and prompt, and hydrates the results into typed
``ExtractedGraph`` objects.

This module sits beside the V3 ``ContextGraphManager`` and reuses the
same query patterns (parameterized queries, markdown fence stripping,
lazy client initialization) without modifying V3 code.

Example usage::

    from bigquery_agent_analytics.resolved_spec import load_resolved_graph
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    spec = load_resolved_graph("examples/ymgo_graph_spec.yaml", env="p.d")
    mgr = OntologyGraphManager(
        project_id="my-project",
        dataset_id="analytics",
        spec=spec,
    )
    graph = mgr.extract_graph(session_ids=["sess-1", "sess-2"])
    print(len(graph.nodes), "nodes,", len(graph.edges), "edges")
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
from typing import Optional

from google.cloud import bigquery

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels
from .extracted_models import ExtractedEdge
from .extracted_models import ExtractedGraph
from .extracted_models import ExtractedNode
from .extracted_models import ExtractedProperty
from .ontology_schema_compiler import compile_extraction_prompt
from .ontology_schema_compiler import compile_output_schema
from .resolved_spec import resolve_from_graph_spec
from .resolved_spec import ResolvedEntity
from .resolved_spec import ResolvedGraph


def _ensure_resolved(spec):
  """Accept either ResolvedGraph or legacy GraphSpec, return ResolvedGraph."""
  if isinstance(spec, ResolvedGraph):
    return spec
  return resolve_from_graph_spec(spec)


from .structured_extraction import run_structured_extractors
from .structured_extraction import StructuredExtractor

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

# ------------------------------------------------------------------ #
# SQL Templates                                                        #
# ------------------------------------------------------------------ #


def _escape_sql_literal(value: str) -> str:
  """Escape a Python string for embedding in a BigQuery SQL single-quoted literal.

  Escapes single quotes (``'`` → ``\\'``) and newlines (``\\n`` → ``\\\\n``)
  so that the resulting SQL string literal is syntactically valid.
  """
  return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


_EXTRACT_ONTOLOGY_AI_QUERY = """\
WITH session_transcripts AS (
  SELECT
    base.session_id,
    STRING_AGG(
      COALESCE(
        JSON_EXTRACT_SCALAR(base.content, '$.text_summary'),
        JSON_EXTRACT_SCALAR(base.content, '$.response'),
        JSON_EXTRACT_SCALAR(base.content, '$.text'),
        TO_JSON_STRING(base.content)
      ),
      '\\n---\\n'
      ORDER BY base.timestamp ASC
    ) AS transcript
  FROM `{project}.{dataset}.{table}` AS base
  WHERE base.session_id IN UNNEST(@session_ids)
    AND (
      base.event_type IN (
        'LLM_RESPONSE',
        'TOOL_COMPLETED',
        'AGENT_COMPLETED',
        'HITL_CONFIRMATION_REQUEST_COMPLETED'
      )
      OR base.span_id IN UNNEST(@partial_span_ids)
    )
    AND base.content IS NOT NULL
    AND base.span_id NOT IN UNNEST(@excluded_span_ids)
  GROUP BY base.session_id
)
SELECT
  st.session_id,
  REGEXP_REPLACE(
    REGEXP_REPLACE(
      AI.GENERATE(
        CONCAT(
          '{prompt}',
          '\\n',
          st.transcript
        ),
        endpoint => '{endpoint}',
        output_schema => 'graph_json STRING'
      ).graph_json,
      r'^```(?:json)?\\s*', ''),
    r'\\s*```$', '')
  AS graph_json
FROM session_transcripts AS st
"""

_EXTRACT_PAYLOADS_QUERY = """\
SELECT
  base.span_id,
  base.session_id,
  COALESCE(
    JSON_EXTRACT_SCALAR(base.content, '$.text_summary'),
    JSON_EXTRACT_SCALAR(base.content, '$.response'),
    JSON_EXTRACT_SCALAR(base.content, '$.text'),
    TO_JSON_STRING(base.content)
  ) AS payload_text
FROM `{project}.{dataset}.{table}` AS base
WHERE base.session_id IN UNNEST(@session_ids)
  AND base.event_type IN (
    'LLM_RESPONSE',
    'TOOL_COMPLETED',
    'AGENT_COMPLETED',
    'HITL_CONFIRMATION_REQUEST_COMPLETED'
  )
  AND base.content IS NOT NULL
ORDER BY base.timestamp ASC
"""

_FETCH_RAW_EVENTS_QUERY = """\
SELECT
  base.span_id,
  base.session_id,
  base.event_type,
  base.timestamp,
  TO_JSON_STRING(base.content) AS content_json
FROM `{project}.{dataset}.{table}` AS base
WHERE base.session_id IN UNNEST(@session_ids)
  AND base.content IS NOT NULL
ORDER BY base.timestamp ASC
"""

# ------------------------------------------------------------------ #
# Hydration                                                            #
# ------------------------------------------------------------------ #


def _hydrate_graph(
    spec: ResolvedGraph,
    raw_rows: list[dict],
) -> ExtractedGraph:
  """Hydrate raw AI.GENERATE JSON rows into an ``ExtractedGraph``.

  Each row contains ``session_id`` and ``graph_json`` (a JSON string
  with ``nodes`` and ``edges`` arrays).  One row per session is
  expected from the session-aggregated AI.GENERATE query.

  Node IDs are key-based: ``{session_id}:{entity_name}:{key1=v1,...}``,
  matching the edge reference scheme so that ``edge.from_node_id``
  resolves to a hydrated node's ``node_id``.  Key-based IDs require
  *all* primary key columns to be present; if any are missing (or the
  entity is unknown to the spec), the fallback index-based ID
  ``{session_id}:{entity_name}:{idx}`` is used instead.

  Duplicate nodes (same ``node_id``) are deduplicated: the last
  occurrence wins, keeping the most recent/complete data.

  Edge IDs: ``{session_id}:{relationship_name}:{idx}``.

  Args:
      spec: The ``ResolvedGraph`` used for extraction.
      raw_rows: List of dicts with ``session_id`` and
          ``graph_json`` keys.

  Returns:
      A merged ``ExtractedGraph`` with all nodes and edges.
  """
  entity_map = {e.name: e for e in spec.entities}
  seen_nodes: dict[str, ExtractedNode] = {}
  all_edges: list[ExtractedEdge] = []

  for row in raw_rows:
    session_id = row.get("session_id", "")
    raw_json = row.get("graph_json", "")

    if not raw_json:
      continue

    try:
      data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
      logger.debug("Could not parse graph JSON for session %s", session_id)
      continue

    if not isinstance(data, dict):
      logger.debug(
          "Expected dict from graph JSON, got %s for session %s",
          type(data).__name__,
          session_id,
      )
      continue

    # Hydrate nodes.
    for idx, raw_node in enumerate(data.get("nodes", [])):
      entity_name = raw_node.get("entity_name", "")
      entity_spec = entity_map.get(entity_name)
      labels = list(entity_spec.labels) if entity_spec else [entity_name]

      node_id = _build_node_id(
          raw_node, entity_name, entity_spec, session_id, idx
      )
      props = []
      for key, value in raw_node.items():
        if key == "entity_name":
          continue
        props.append(ExtractedProperty(name=key, value=value))

      seen_nodes[node_id] = ExtractedNode(
          node_id=node_id,
          entity_name=entity_name,
          labels=labels,
          properties=props,
      )

    # Hydrate edges.
    for idx, raw_edge in enumerate(data.get("edges", [])):
      rel_name = raw_edge.get("relationship_name", "")
      edge_id = f"{session_id}:{rel_name}:{idx}"

      from_node_id = _build_edge_node_ref(raw_edge, "from", session_id)
      to_node_id = _build_edge_node_ref(raw_edge, "to", session_id)

      props = []
      skip_keys = {
          "relationship_name",
          "from_entity_name",
          "to_entity_name",
          "from_keys",
          "to_keys",
      }
      for key, value in raw_edge.items():
        if key in skip_keys:
          continue
        props.append(ExtractedProperty(name=key, value=value))

      all_edges.append(
          ExtractedEdge(
              edge_id=edge_id,
              relationship_name=rel_name,
              from_node_id=from_node_id,
              to_node_id=to_node_id,
              properties=props,
          )
      )

  return ExtractedGraph(
      name=spec.name,
      nodes=list(seen_nodes.values()),
      edges=all_edges,
  )


def _build_key_string(keys_obj: dict) -> str:
  """Build a sorted ``k1=v1,k2=v2`` string from a key-value dict."""
  if isinstance(keys_obj, dict) and keys_obj:
    return ",".join(f"{k}={v}" for k, v in sorted(keys_obj.items()))
  return ""


def _build_node_id(
    raw_node: dict,
    entity_name: str,
    entity_spec: Optional[ResolvedEntity],
    session_id: str,
    idx: int,
) -> str:
  """Build a deterministic node ID from primary key values.

  Uses the entity's primary key columns (from the spec) to extract
  key values from the raw node dict, producing IDs like
  ``{session_id}:{entity_name}:{key1=val1,key2=val2}``.

  Falls back to ``{session_id}:{entity_name}:{idx}`` when:
  - The entity is unknown to the spec
  - Primary key values are missing from the raw node
  """
  if entity_spec is not None:
    keys_obj = {}
    for col in entity_spec.key_columns:
      if col not in raw_node:
        # Missing key column — fall back to index-based ID.
        return f"{session_id}:{entity_name}:{idx}"
      keys_obj[col] = raw_node[col]
    key_str = _build_key_string(keys_obj)
    if key_str:
      return f"{session_id}:{entity_name}:{key_str}"
  return f"{session_id}:{entity_name}:{idx}"


def _build_edge_node_ref(
    raw_edge: dict,
    direction: str,
    session_id: str,
) -> str:
  """Build a node reference string from an edge's key data.

  Uses ``from_keys``/``to_keys`` object to construct a deterministic
  reference like ``{session_id}:{entity_name}:{key1=val1,key2=val2}``.

  This matches the ID scheme used by ``_build_node_id``, so edge
  references resolve to hydrated node IDs in the same graph.
  """
  entity_name = raw_edge.get(f"{direction}_entity_name", "")
  keys_obj = raw_edge.get(f"{direction}_keys", {})
  key_str = _build_key_string(keys_obj)
  if key_str:
    return f"{session_id}:{entity_name}:{key_str}"
  return f"{session_id}:{entity_name}:unknown"


# ------------------------------------------------------------------ #
# OntologyGraphManager                                                 #
# ------------------------------------------------------------------ #


class OntologyGraphManager:
  """Configuration-driven graph extraction engine.

  Queries raw ADK telemetry from ``agent_events``, constructs
  ``AI.GENERATE`` queries using the compiled ontology schema and
  prompt, and hydrates the results into ``ExtractedGraph`` objects.

  Args:
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.
      spec: A validated ``ResolvedGraph`` (from ``load_graph_spec``).
      table_id: Source telemetry table name.
      endpoint: AI.GENERATE model endpoint.
      location: BigQuery location.
      bq_client: Optional pre-configured BigQuery client.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      spec: ResolvedGraph,
      table_id: str = "agent_events",
      endpoint: str = "gemini-2.5-flash",
      location: Optional[str] = None,
      bq_client: Optional[bigquery.Client] = None,
      extractors: Optional[dict[str, StructuredExtractor]] = None,
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.spec = _ensure_resolved(spec)
    self.table_id = table_id
    self.endpoint = endpoint
    self.location = location
    self._bq_client = bq_client
    self._warned_unlabeled_client = False
    self.extractors = extractors or {}
    # Audit handle for the C2.c compiled-bundle path. Stays
    # ``None`` for manager instances built via the legacy
    # ``extractors=`` constructor parameter; set to the
    # :class:`WrappedRegistry` produced by
    # :meth:`from_bundles_root`. ``self.extractors`` is always
    # the dict the runtime invokes — the registry is the audit
    # object next to it.
    self.runtime_registry: Optional["WrappedRegistry"] = None

  @classmethod
  def from_ontology_binding(
      cls,
      project_id: str,
      dataset_id: str,
      ontology: "Ontology",
      binding: "Binding",
      lineage_config: Optional[dict] = None,
      table_id: str = "agent_events",
      endpoint: str = "gemini-2.5-flash",
      location: Optional[str] = None,
      bq_client: Optional[bigquery.Client] = None,
      extractors: Optional[dict[str, StructuredExtractor]] = None,
  ) -> "OntologyGraphManager":
    """Create from upstream Ontology + Binding.

    Converts the separated ontology/binding pair into a ``ResolvedGraph``
    via the resolver, then constructs the manager.

    Note: ``project_id`` and ``dataset_id`` control where the manager
    reads telemetry from (``{project}.{dataset}.{table_id}``). This
    may differ from ``binding.target`` which controls where
    materialized data is written. For the materializer and compiler,
    use their ``from_ontology_binding()`` methods which derive
    project/dataset from the binding target automatically.

    Args:
        project_id: GCP project where telemetry is stored.
        dataset_id: BigQuery dataset where telemetry is stored.
        ontology: Upstream Ontology object.
        binding: Upstream Binding object.
        lineage_config: Optional lineage session column config.
        table_id: Source telemetry table name.
        endpoint: AI.GENERATE model endpoint.
        location: BigQuery location.
        bq_client: Optional pre-configured BigQuery client.
        extractors: Optional structured extractor registry.

    Returns:
        A configured ``OntologyGraphManager``.
    """
    from .resolved_spec import resolve

    spec = resolve(ontology, binding, lineage_config=lineage_config)
    return cls(
        project_id=project_id,
        dataset_id=dataset_id,
        spec=spec,
        table_id=table_id,
        endpoint=endpoint,
        location=location,
        bq_client=bq_client,
        extractors=extractors,
    )

  @classmethod
  def from_bundles_root(
      cls,
      project_id: str,
      dataset_id: str,
      ontology: "Ontology",
      binding: "Binding",
      bundles_root: pathlib.Path,
      expected_fingerprint: str,
      fallback_extractors: dict[str, StructuredExtractor],
      lineage_config: Optional[dict] = None,
      table_id: str = "agent_events",
      endpoint: str = "gemini-2.5-flash",
      location: Optional[str] = None,
      bq_client: Optional[bigquery.Client] = None,
      event_type_allowlist: Optional[tuple[str, ...]] = None,
      on_outcome: Optional["OutcomeCallback"] = None,
  ) -> "OntologyGraphManager":
    """Construct from ontology + binding *and* a compiled-bundle root.

    The C2.c.2 entry point that actually puts compiled
    extractors on the runtime path. Wires C2.a's
    :func:`discover_bundles` and C2.b's
    :func:`run_with_fallback` together via C2.c.1's
    :func:`build_runtime_extractor_registry`, then constructs an
    :class:`OntologyGraphManager` whose ``extractors`` dict is
    the wrapped registry — so existing call sites that read
    ``self.extractors`` and pass them through to
    :func:`run_structured_extractors` pick up the
    compiled-with-fallback behavior with no other changes.

    The resulting manager exposes:

    * ``manager.extractors`` — the dict the runtime invokes
      (wrapped closures for event_types with both a compiled
      bundle and a fallback; original fallback callables
      otherwise).
    * ``manager.runtime_registry`` — the full
      :class:`WrappedRegistry` from C2.c.1, including
      ``discovery``, ``bundles_without_fallback``, and
      ``fallbacks_without_bundle`` for rollout-coverage
      telemetry.

    Args:
      project_id: GCP project where telemetry is stored.
      dataset_id: BigQuery dataset where telemetry is stored.
      ontology: Upstream :class:`Ontology` object.
      binding: Upstream :class:`Binding` object.
      bundles_root: Directory containing compiled bundles.
        Forwarded to :func:`discover_bundles`.
      expected_fingerprint: Fingerprint the runtime computed
        from its active inputs. Bundles whose manifest
        fingerprint doesn't match are skipped at discovery.
      fallback_extractors: ``event_type -> handwritten
        callable``. Required: C2.b's safety contract uses the
        fallback as the authoritative baseline against which
        the compiled output is compared. Validated at build
        time (non-str keys, empty-string keys, non-callable
        values, and callables that can't accept ``(event,
        spec)`` are rejected with ``TypeError``).
      lineage_config: Optional lineage session column config
        forwarded to :func:`resolve`.
      table_id: Source telemetry table name.
      endpoint: ``AI.GENERATE`` model endpoint for the
        non-compiled extraction path.
      location: BigQuery location.
      bq_client: Optional pre-configured BigQuery client.
      event_type_allowlist: Optional. Filters both compiled
        bundles and fallback extractors to the named event
        types; ``None`` considers everything.
      on_outcome: Optional ``(event_type, outcome) -> None``
        invoked from inside each wrapped extractor on every
        event including ``compiled_unchanged`` outcomes —
        denominator metric for compiled-vs-fallback rates.
        Callback exceptions propagate.

    Returns:
      A configured :class:`OntologyGraphManager` with
      ``runtime_registry`` set.
    """
    from .extractor_compilation import build_runtime_extractor_registry
    from .resolved_spec import resolve

    spec = resolve(ontology, binding, lineage_config=lineage_config)

    registry = build_runtime_extractor_registry(
        bundles_root=bundles_root,
        expected_fingerprint=expected_fingerprint,
        fallback_extractors=fallback_extractors,
        resolved_graph=spec,
        event_type_allowlist=event_type_allowlist,
        on_outcome=on_outcome,
    )

    manager = cls(
        project_id=project_id,
        dataset_id=dataset_id,
        spec=spec,
        table_id=table_id,
        endpoint=endpoint,
        location=location,
        bq_client=bq_client,
        extractors=registry.extractors,
    )
    manager.runtime_registry = registry
    return manager

  @property
  def bq_client(self) -> bigquery.Client:
    """Lazily initializes the BigQuery client."""
    if self._bq_client is None:
      self._bq_client = make_bq_client(self.project_id, location=self.location)
    elif isinstance(self._bq_client, bigquery.Client) and not isinstance(
        self._bq_client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._bq_client

  def _resolve_endpoint(self) -> str:
    """Resolve the endpoint to a full Vertex AI URL.

    Reuses the same resolution logic as V3
    ``ContextGraphManager._resolve_endpoint``.

    Raises:
        ValueError: If the endpoint looks like a legacy BQ ML
            model reference.
    """
    ep = self.endpoint
    if ep.startswith("https://"):
      return ep
    if ep.count(".") >= 2:
      raise ValueError(
          f"Legacy BQ ML model reference '{ep}' is not supported "
          f"for AI.GENERATE. Use a Vertex AI model name "
          f"(e.g. 'gemini-2.5-flash') or a full endpoint URL."
      )
    return (
        f"https://aiplatform.googleapis.com/v1/projects/"
        f"{self.project_id}/locations/global/publishers/google/"
        f"models/{ep}"
    )

  def get_extraction_sql(
      self,
      session_ids: Optional[list[str]] = None,
  ) -> str:
    """Return the AI.GENERATE extraction SQL (for inspection).

    Args:
        session_ids: Ignored (the SQL uses a query parameter).
            Included for API symmetry.

    Returns:
        The formatted SQL string.
    """
    prompt = compile_extraction_prompt(self.spec)
    schema_hint = compile_output_schema(self.spec)
    full_prompt = prompt + "\n\nReturn a single JSON object:\n" + schema_hint
    return _EXTRACT_ONTOLOGY_AI_QUERY.format(
        prompt=_escape_sql_literal(full_prompt),
        endpoint=self._resolve_endpoint(),
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

  def extract_graph(
      self,
      session_ids: list[str],
      use_ai_generate: bool = True,
  ) -> ExtractedGraph:
    """Extract a typed graph from agent telemetry.

    Args:
        session_ids: Sessions to extract from.
        use_ai_generate: If True, runs AI.GENERATE server-side.
            If False, fetches raw payloads (stub graph returned).

    Returns:
        An ``ExtractedGraph`` with nodes and edges.
    """
    # Run structured extractors first if any are registered.
    structured_nodes: list[ExtractedNode] = []
    structured_edges: list[ExtractedEdge] = []
    excluded_span_ids: list[str] = []
    partial_span_ids: list[str] = []
    partial_hint = ""

    if self.extractors and use_ai_generate:
      raw_events = self._fetch_raw_events(session_ids)
      if raw_events:
        structured_result = run_structured_extractors(
            raw_events,
            self.extractors,
            self.spec,
        )
        structured_nodes = structured_result.nodes
        structured_edges = structured_result.edges
        excluded_span_ids = list(structured_result.fully_handled_span_ids)
        partial_span_ids = list(structured_result.partially_handled_span_ids)

        # Build prompt hint for partially-handled spans so the LLM
        # focuses on unstructured content and avoids re-extracting
        # facts already captured structurally.
        if partial_span_ids:
          entity_names = sorted({n.entity_name for n in structured_nodes})
          partial_hint = (
              "Note: the following entity types were already "
              "extracted from structured events — focus on "
              "unstructured content only: " + ", ".join(entity_names) + ".\n"
          )

    if use_ai_generate:
      ai_graph = self._extract_via_ai_generate(
          session_ids,
          excluded_span_ids,
          partial_span_ids,
          partial_hint,
      )
    else:
      ai_graph = self._extract_payloads(session_ids)

    # Merge: structured nodes/edges + AI-extracted, dedup by node_id.
    if structured_nodes or structured_edges:
      seen_nodes: dict[str, ExtractedNode] = {}
      for node in structured_nodes:
        seen_nodes[node.node_id] = node
      for node in ai_graph.nodes:
        if node.node_id not in seen_nodes:
          seen_nodes[node.node_id] = node
      all_edges = structured_edges + ai_graph.edges
      return ExtractedGraph(
          name=self.spec.name,
          nodes=list(seen_nodes.values()),
          edges=all_edges,
      )

    return ai_graph

  def _fetch_raw_events(self, session_ids: list[str]) -> list[dict]:
    """Fetch raw events with full content for structured extraction.

    Unlike ``_EXTRACT_PAYLOADS_QUERY`` (V4), this returns all event
    types and preserves ``event_type`` and full ``content`` JSON so
    that registered structured extractors can match on event type and
    access typed fields.
    """
    query = _FETCH_RAW_EVENTS_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter(
                "session_ids",
                "STRING",
                session_ids,
            ),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="ontology-build")
    try:
      job = self.bq_client.query(query, job_config=job_config)
      events = []
      for row in job.result():
        event = dict(row)
        # Parse content_json back into a dict for extractors.
        raw_content = event.pop("content_json", None)
        if raw_content:
          try:
            event["content"] = json.loads(raw_content)
          except (json.JSONDecodeError, TypeError):
            event["content"] = raw_content
        events.append(event)
      return events
    except Exception as e:
      logger.warning("Failed to fetch raw events: %s", e)
      return []

  def _extract_via_ai_generate(
      self,
      session_ids: list[str],
      excluded_span_ids: Optional[list[str]] = None,
      partial_span_ids: Optional[list[str]] = None,
      partial_hint: str = "",
  ) -> ExtractedGraph:
    """Server-side extraction using AI.GENERATE with output_schema.

    The transcript CTE includes events matching the V4 allowlist OR
    events whose span_id is in ``partial_span_ids`` (custom event
    types that were partially handled by structured extractors and
    still need LLM processing for unstructured content).
    """
    prompt = compile_extraction_prompt(self.spec)
    schema_hint = compile_output_schema(self.spec)
    full_prompt = prompt
    if partial_hint:
      full_prompt += "\n" + partial_hint
    full_prompt += "\n\nReturn a single JSON object:\n" + schema_hint

    query = _EXTRACT_ONTOLOGY_AI_QUERY.format(
        prompt=_escape_sql_literal(full_prompt),
        endpoint=self._resolve_endpoint(),
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter(
                "session_ids",
                "STRING",
                session_ids,
            ),
            bigquery.ArrayQueryParameter(
                "excluded_span_ids",
                "STRING",
                excluded_span_ids or [],
            ),
            bigquery.ArrayQueryParameter(
                "partial_span_ids",
                "STRING",
                partial_span_ids or [],
            ),
        ]
    )
    job_config = with_sdk_labels(
        job_config, feature="ontology-build", ai_function="ai-generate"
    )

    try:
      job = self.bq_client.query(query, job_config=job_config)
      rows = [dict(row) for row in job.result()]
    except Exception as e:
      logger.warning("AI.GENERATE ontology extraction failed: %s", e)
      return ExtractedGraph(name=self.spec.name)

    return _hydrate_graph(self.spec, rows)

  def _extract_payloads(self, session_ids: list[str]) -> ExtractedGraph:
    """Fetch raw payloads without AI extraction (stub fallback)."""
    query = _EXTRACT_PAYLOADS_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="ontology-build")

    try:
      job = self.bq_client.query(query, job_config=job_config)
      rows = list(job.result())
    except Exception as e:
      logger.warning("Payload extraction failed: %s", e)
      return ExtractedGraph(name=self.spec.name)

    # Return raw payloads as untyped nodes for client-side processing.
    nodes = []
    for idx, row in enumerate(rows):
      nodes.append(
          ExtractedNode(
              node_id=f"{row.get('session_id', '')}:{row.get('span_id', '')}:payload:{idx}",
              entity_name="raw_payload",
              labels=["raw_payload"],
              properties=[
                  ExtractedProperty(
                      name="payload_text",
                      value=row.get("payload_text", ""),
                  ),
                  ExtractedProperty(
                      name="session_id",
                      value=row.get("session_id", ""),
                  ),
                  ExtractedProperty(
                      name="span_id",
                      value=row.get("span_id", ""),
                  ),
              ],
          )
      )

    return ExtractedGraph(
        name=self.spec.name,
        nodes=nodes,
    )


# ------------------------------------------------------------------ #
# Lineage Detection                                                    #
# ------------------------------------------------------------------ #


def detect_lineage_edges(
    current_graph: ExtractedGraph,
    current_session_id: str,
    prior_graphs: dict[str, ExtractedGraph],
    lineage_entity_types: list[str],
    spec: ResolvedGraph,
) -> list[ExtractedEdge]:
  """Detect evolution edges between current and prior session graphs.

  For each entity type in ``lineage_entity_types``, find nodes that
  share the same primary key across sessions and have different
  property values.

  Args:
      current_graph: Graph extracted from the current session.
      current_session_id: The current session ID.
      prior_graphs: Dict of ``{session_id: ExtractedGraph}`` for
          prior sessions.
      lineage_entity_types: Entity names to check for evolution.
      spec: ResolvedGraph for entity key lookups.

  Returns:
      List of ``ExtractedEdge`` instances for lineage relationships.
  """
  entity_map = {e.name: e for e in spec.entities}
  lineage_edges: list[ExtractedEdge] = []

  for entity_name in lineage_entity_types:
    entity_spec = entity_map.get(entity_name)
    if entity_spec is None:
      continue

    key_cols = entity_spec.key_columns

    # Index current-session nodes by primary key values.
    current_by_key: dict[str, ExtractedNode] = {}
    for node in current_graph.nodes:
      if node.entity_name != entity_name:
        continue
      prop_map = {p.name: p.value for p in node.properties}
      key_vals = tuple(str(prop_map.get(k, "")) for k in key_cols)
      key_str = ",".join(f"{k}={v}" for k, v in zip(key_cols, key_vals))
      if key_str:
        current_by_key[key_str] = node

    # Compare against each prior session.
    for prior_session_id, prior_graph in prior_graphs.items():
      prior_by_key: dict[str, ExtractedNode] = {}
      for node in prior_graph.nodes:
        if node.entity_name != entity_name:
          continue
        prop_map = {p.name: p.value for p in node.properties}
        key_vals = tuple(str(prop_map.get(k, "")) for k in key_cols)
        key_str = ",".join(f"{k}={v}" for k, v in zip(key_cols, key_vals))
        if key_str:
          prior_by_key[key_str] = node

      # Find shared keys with changed properties.
      for key_str, current_node in current_by_key.items():
        prior_node = prior_by_key.get(key_str)
        if prior_node is None:
          continue

        current_props = {p.name: p.value for p in current_node.properties}
        prior_props = {p.name: p.value for p in prior_node.properties}

        changed = []
        all_prop_names = set(current_props) | set(prior_props)
        for pname in sorted(all_prop_names):
          if pname in key_cols:
            continue
          if current_props.get(pname) != prior_props.get(pname):
            changed.append(pname)

        if not changed:
          continue

        rel_name = f"{entity_name}EvolvedFrom"
        edge_id = (
            f"{current_session_id}:{rel_name}:" f"{prior_session_id}:{key_str}"
        )

        lineage_edges.append(
            ExtractedEdge(
                edge_id=edge_id,
                relationship_name=rel_name,
                from_node_id=prior_node.node_id,
                to_node_id=current_node.node_id,
                properties=[
                    ExtractedProperty(
                        name="from_session_id",
                        value=prior_session_id,
                    ),
                    ExtractedProperty(
                        name="to_session_id",
                        value=current_session_id,
                    ),
                    ExtractedProperty(
                        name="event_time",
                        value=datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                    ),
                    ExtractedProperty(
                        name="changed_properties",
                        value=",".join(changed),
                    ),
                ],
            )
        )

  return lineage_edges
