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

"""Ontology-aware validator for extracted graphs.

Implements ``validate_extracted_graph(spec, graph)`` per issue #76.
Checks that an :class:`ExtractedGraph` (output of the extraction
pipeline — LLM ``AI.GENERATE``, hand-written ``structured_extraction``,
or future compiled extractors) conforms to the ontology declared in a
:class:`ResolvedGraph` — not just container shape.

The validator is a sibling to
:func:`bigquery_agent_analytics.binding_validation.
validate_binding_against_bigquery` (issue #105):

    | This validator (#76)         | Binding validator (#105)
    | post-extraction              | pre-extraction
    | ResolvedGraph + ExtractedGraph
    |                              | Ontology + Binding + bq_client

Both expose the same public report ergonomics (``ok`` /
``failures`` / typed codes) but keep separate ``Failure`` types
because their context fields differ: failures here carry
``node_id`` / ``edge_id`` / ``FallbackScope``; binding-validation
failures carry ``binding_path`` / ``bq_ref``.

Failure scopes
==============

Each failure is tagged with the smallest safe unit of replacement
so downstream consumers (notably the compiled-extractor runtime in
#75) know whether to re-extract a single field, a whole node, an
edge, or the whole event:

- ``FIELD`` — property type mismatch, unknown property name. The
  rest of the node is recoverable.
- ``NODE`` — missing key, malformed ``node_id``, unknown
  ``entity_name``. The node's identity is broken; any edge that
  references it must also be re-extracted.
- ``EDGE`` — unresolved endpoint, missing endpoint key, wrong
  endpoint entity type, unknown relationship.
- ``EVENT`` — exists in the enum so #75 C2's compiled-extractor
  runtime can classify event-level fallbacks. **#76 does not emit
  EVENT failures** — that requires per-``event_type`` expectations
  that live in #75's compile-time inputs, not in ``ResolvedGraph``.

Twelve codes ship (NODE/FIELD/EDGE only); see
``docs/ontology/validation.md`` for the full table.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from typing import Any, Optional

from ._ontology_routing import build_name_to_column
from ._ontology_routing import build_property_lookup
from ._ontology_routing import parse_iso_date
from ._ontology_routing import parse_iso_datetime
from ._ontology_routing import parse_key_segment
from .extracted_models import ExtractedEdge
from .extracted_models import ExtractedGraph
from .extracted_models import ExtractedNode
from .extracted_models import ExtractedProperty
from .resolved_spec import ResolvedEntity
from .resolved_spec import ResolvedGraph
from .resolved_spec import ResolvedProperty
from .resolved_spec import ResolvedRelationship

# ------------------------------------------------------------------ #
# Public types                                                         #
# ------------------------------------------------------------------ #


class FallbackScope(str, enum.Enum):
  """Smallest safe unit of replacement for a validation failure."""

  FIELD = "field"  # one property on one node/edge
  NODE = "node"  # whole node + edges that reference it
  EDGE = "edge"  # whole edge
  EVENT = "event"  # whole extractor output for an event (deferred to #75 C2)


@dataclasses.dataclass(frozen=True)
class ValidationFailure:
  """One validation failure.

  ``code`` is a stable string identifier (e.g. ``"unknown_entity"``)
  callers can switch on. ``path`` is a human-readable pointer into
  the ``ExtractedGraph`` (e.g. ``"nodes[3].properties[1].value"``)
  for tooling and error reporting. ``node_id`` / ``edge_id`` /
  ``event_id`` are populated when the failure can be attributed to
  a specific node/edge/extractor-event boundary.
  """

  scope: FallbackScope
  code: str
  path: str
  node_id: Optional[str] = None
  edge_id: Optional[str] = None
  event_id: Optional[str] = None
  detail: str = ""
  observed: Any = None
  expected: Any = None


@dataclasses.dataclass(frozen=True)
class ValidationReport:
  """Result of :func:`validate_extracted_graph`."""

  failures: tuple[ValidationFailure, ...] = ()

  @property
  def ok(self) -> bool:
    return not self.failures

  def by_scope(self, scope: FallbackScope) -> tuple[ValidationFailure, ...]:
    """Return only failures with the given scope."""
    return tuple(f for f in self.failures if f.scope is scope)


# ------------------------------------------------------------------ #
# Type validators (per ResolvedProperty.sdk_type)                      #
# ------------------------------------------------------------------ #

# ISO parsing is centralized in _ontology_routing so the validator
# and the materializer-side normalization agree on what counts as
# a parseable ISO date/timestamp. Earlier versions used regex-only
# acceptance which let strings like '9999-99-99' pass as dates and
# fail at BigQuery INSERT time.


def _accepts_string(value: Any) -> bool:
  return isinstance(value, str)


def _accepts_bytes(value: Any) -> bool:
  return isinstance(value, (bytes, bytearray))


def _accepts_int64(value: Any) -> bool:
  # bool is a subclass of int — explicitly reject it. The issue
  # body calls this out: "bool is explicitly rejected despite
  # being an int subclass."
  return isinstance(value, int) and not isinstance(value, bool)


def _accepts_double(value: Any) -> bool:
  # int and float both accepted. bool not accepted because the
  # extractor should produce a numeric, not a flag.
  if isinstance(value, bool):
    return False
  return isinstance(value, (int, float))


def _accepts_boolean(value: Any) -> bool:
  return isinstance(value, bool)


def _accepts_date(value: Any) -> bool:
  if isinstance(value, datetime.date) and not isinstance(
      value, datetime.datetime
  ):
    return True
  if isinstance(value, str):
    return parse_iso_date(value)
  return False


def _accepts_timestamp(value: Any) -> bool:
  if isinstance(value, datetime.datetime):
    # Tz-aware required per the issue table.
    return value.tzinfo is not None
  if isinstance(value, str):
    return parse_iso_datetime(value)
  return False


# _DDL_TYPE_MAP supports SDK type aliases (``float64`` and ``bool``)
# alongside the canonical names (``double`` and ``boolean``). The
# GraphSpec adapter path can produce property metadata with either
# form, so the validator must accept both — otherwise a property
# typed ``float64`` would skip type-checking entirely (the unknown-
# sdk-type fallback returns True for forward compatibility).
_TYPE_ACCEPTORS: dict[str, Any] = {
    "string": _accepts_string,
    "bytes": _accepts_bytes,
    "int64": _accepts_int64,
    "double": _accepts_double,
    "float64": _accepts_double,  # alias for double
    "boolean": _accepts_boolean,
    "bool": _accepts_boolean,  # alias for boolean
    "date": _accepts_date,
    "timestamp": _accepts_timestamp,
}


def _value_matches_sdk_type(value: Any, sdk_type: str) -> bool:
  acceptor = _TYPE_ACCEPTORS.get(sdk_type)
  if acceptor is None:
    # Unknown SDK type: skip the check rather than guessing. This
    # keeps the validator forward-compatible if _PROPERTY_TYPE_TO_SDK
    # adds a new value before this module is updated.
    return True
  return acceptor(value)


# ------------------------------------------------------------------ #
# Property-name resolution                                             #
# ------------------------------------------------------------------ #


def _build_property_lookup(
    properties: tuple[ResolvedProperty, ...],
) -> dict[str, ResolvedProperty]:
  """Thin wrapper around the shared ``build_property_lookup``.

  Kept as a one-line module-private alias so the rest of this file
  reads naturally. The shared implementation in
  ``_ontology_routing`` is the single source of truth for
  precedence (logical name wins on collision); both this module
  and ``ontology_materializer`` consume the same helper.
  """
  return build_property_lookup(properties)


# ------------------------------------------------------------------ #
# Validation passes                                                    #
# ------------------------------------------------------------------ #


def _validate_properties(
    *,
    extracted_props: list[ExtractedProperty],
    spec_props: tuple[ResolvedProperty, ...],
    base_path: str,
    node_id: Optional[str],
    edge_id: Optional[str],
    event_id: Optional[str],
) -> list[ValidationFailure]:
  """Per-property checks. Used for both nodes and edges."""
  failures: list[ValidationFailure] = []
  prop_lookup = _build_property_lookup(spec_props)

  for j, p in enumerate(extracted_props):
    prop_path = f"{base_path}.properties[{j}]"
    spec_prop = prop_lookup.get(p.name)

    if spec_prop is None:
      failures.append(
          ValidationFailure(
              scope=FallbackScope.FIELD,
              code="unknown_property",
              path=f"{prop_path}.name",
              node_id=node_id,
              edge_id=edge_id,
              event_id=event_id,
              observed=p.name,
              detail=(
                  f"property name {p.name!r} does not match any "
                  f"declared property (tried logical_name and "
                  f"column on every spec property)"
              ),
          )
      )
      continue

    # Type compatibility. Reject lists/dicts as unsupported_type
    # for any sdk_type we know about — ontology v0 doesn't have
    # composite types, and a list/dict on a scalar property would
    # silently corrupt materialization.
    if isinstance(p.value, (list, dict)):
      failures.append(
          ValidationFailure(
              scope=FallbackScope.FIELD,
              code="unsupported_type",
              path=f"{prop_path}.value",
              node_id=node_id,
              edge_id=edge_id,
              event_id=event_id,
              observed=type(p.value).__name__,
              expected=spec_prop.sdk_type,
              detail=(
                  f"composite values (list/dict) are not supported "
                  f"on scalar property {spec_prop.logical_name!r} "
                  f"(sdk_type={spec_prop.sdk_type!r}); ontology v0 "
                  f"models arrays as separate entities + relationships"
              ),
          )
      )
      continue

    if not _value_matches_sdk_type(p.value, spec_prop.sdk_type):
      failures.append(
          ValidationFailure(
              scope=FallbackScope.FIELD,
              code="type_mismatch",
              path=f"{prop_path}.value",
              node_id=node_id,
              edge_id=edge_id,
              event_id=event_id,
              observed=type(p.value).__name__,
              expected=spec_prop.sdk_type,
              detail=(
                  f"property {spec_prop.logical_name!r} expects "
                  f"sdk_type={spec_prop.sdk_type!r}; got "
                  f"{type(p.value).__name__} ({p.value!r})"
              ),
          )
      )

  return failures


def _validate_node(
    *,
    node: ExtractedNode,
    node_index: int,
    spec_entity: ResolvedEntity,
    event_id: Optional[str],
) -> list[ValidationFailure]:
  """Per-node entity-specific checks: missing_node_id, missing key
  columns, and per-property validation. Duplicate-node-id detection
  runs at the graph level (Pass 0 in ``validate_extracted_graph``)
  so it covers unknown-entity nodes too."""
  failures: list[ValidationFailure] = []
  base_path = f"nodes[{node_index}]"

  # node_id presence (uniqueness is checked at graph level).
  if not node.node_id:
    failures.append(
        ValidationFailure(
            scope=FallbackScope.NODE,
            code="missing_node_id",
            path=f"{base_path}.node_id",
            event_id=event_id,
            detail="node_id is empty",
        )
    )

  # Node-id entity segment must match ``ExtractedNode.entity_name``.
  # The documented shape is ``{session}:{entity}:k=v``. An in-graph
  # edge resolves endpoints by ``ExtractedNode.entity_name`` and
  # would pass the wrong_endpoint_entity check even if the node-id
  # segment lies — but the same node id, referenced from a lineage-
  # only batch, fails the permissive-mode entity-segment check
  # (which has no in-graph node to consult). Catch the disagreement
  # at the node so both code paths agree on what the id means.
  # Folded under ``key_mismatch`` per the project's preference to
  # not grow the failure-code surface; the detail makes the
  # specific drift explicit.
  if node.node_id:
    nid_parts = node.node_id.split(":", 2)
    # Only check 3-part ids (the documented shape). Short-form
    # fallbacks like 'd1' have no entity segment to compare. An
    # empty entity segment is a 3-part id that fails the
    # comparison — same rule as the permissive-mode endpoint
    # check, which also rejects 'sess1::outcome_id=o1'.
    if len(nid_parts) >= 3:
      observed_entity = nid_parts[1]
      if observed_entity != node.entity_name:
        failures.append(
            ValidationFailure(
                scope=FallbackScope.NODE,
                code="key_mismatch",
                path=f"{base_path}.node_id",
                node_id=node.node_id,
                event_id=event_id,
                expected=node.entity_name,
                observed=observed_entity,
                detail=(
                    f"node_id entity segment is {observed_entity!r} "
                    f"but ExtractedNode.entity_name is "
                    f"{node.entity_name!r}. The same id seen from a "
                    f"lineage-only batch would fail the permissive-"
                    f"mode wrong_endpoint_entity check; in-graph "
                    f"edges resolve through entity_name and would "
                    f"silently disagree. Fix the extractor to emit "
                    f"a node_id whose entity segment matches the "
                    f"node's entity_name."
                ),
            )
        )

  # The materializer writes the node row's primary-key columns from
  # ``node.properties`` but writes edge FK columns from
  # ``parse_key_segment(node_id)``. If those two sources of truth
  # disagree, the validator will pass but the materialized graph
  # will have edges pointing at non-existent rows. Pre-parse the
  # node-id key segment once so the loop below can compare each
  # key column against the materializer-routed property value.
  parsed_node_keys = parse_key_segment(node.node_id) if node.node_id else {}

  # Mirror the materializer's name→physical-column routing. The
  # materializer iterates ``node.properties`` in extraction order
  # and writes each accepted name to its physical column — so two
  # extracted properties whose names both route to the same key
  # column ('decision_id' and a logical alias 'decisionId') both
  # write that column, last-wins. The validator must use the same
  # logic; otherwise it can validate clean against the first match
  # while the materializer actually writes the second.
  name_to_col = build_name_to_column(spec_entity.properties)
  routed_values_by_col: dict[str, list[tuple[str, Any]]] = {}
  for prop in node.properties:
    physical = name_to_col.get(prop.name)
    if physical is not None:
      routed_values_by_col.setdefault(physical, []).append(
          (prop.name, prop.value)
      )

  for key_col in spec_entity.key_columns:
    routed = routed_values_by_col.get(key_col, [])
    # Effective materializer value = last write wins (matches
    # ``ontology_materializer._route_node``).
    found_value = routed[-1][1] if routed else None

    is_empty = found_value is None or (
        isinstance(found_value, str) and not found_value
    )
    if not routed or is_empty:
      failures.append(
          ValidationFailure(
              scope=FallbackScope.NODE,
              code="missing_key",
              path=f"{base_path}.properties.<key:{key_col}>",
              node_id=node.node_id or None,
              event_id=event_id,
              expected=key_col,
              detail=(
                  f"primary-key column {key_col!r} on entity "
                  f"{spec_entity.name!r} is missing or empty on the "
                  f"extracted node"
              ),
          )
      )
      continue

    # Multiple extracted properties routing to the same key column
    # with disagreeing values is itself a key_mismatch — the
    # materializer would silently pick the last one and the
    # extractor's intent is ambiguous. Surface the conflict
    # directly so the extractor can be fixed.
    distinct_values = {str(v) for _, v in routed}
    if len(distinct_values) > 1:
      observed_pairs = ", ".join(f"{n}={v!r}" for n, v in routed)
      failures.append(
          ValidationFailure(
              scope=FallbackScope.NODE,
              code="key_mismatch",
              path=f"{base_path}.properties.<key:{key_col}>",
              node_id=node.node_id or None,
              event_id=event_id,
              expected=key_col,
              observed=str(found_value),
              detail=(
                  f"primary-key column {key_col!r} on entity "
                  f"{spec_entity.name!r} is set by multiple "
                  f"extracted properties with conflicting values "
                  f"({observed_pairs}). The materializer writes "
                  f"properties in extraction order and last-wins, "
                  f"so this silently picks {found_value!r} — fix "
                  f"the extractor so a key column has exactly one "
                  f"source."
              ),
          )
      )
      continue

    # ``key_mismatch``: parsed node-id key disagrees with the
    # materializer-routed property value. The materializer writes
    # node rows from properties and edge FKs from the parsed
    # node-id, so disagreement silently breaks edges. Also catches
    # keys whose raw values contain ``,`` — ``_build_key_string``
    # is unescaped on commas, so ``parse_key_segment`` truncates
    # at the comma and the parsed value won't equal the property
    # value. (``=`` is split-once, so ``key=a=b`` parses as
    # ``{"key": "a=b"}`` and round-trips cleanly.)
    parsed_value = parsed_node_keys.get(key_col)
    if parsed_value is not None and parsed_value != str(found_value):
      failures.append(
          ValidationFailure(
              scope=FallbackScope.NODE,
              code="key_mismatch",
              path=f"{base_path}.properties.<key:{key_col}>",
              node_id=node.node_id,
              event_id=event_id,
              expected=str(found_value),
              observed=parsed_value,
              detail=(
                  f"primary-key column {key_col!r} on entity "
                  f"{spec_entity.name!r} disagrees between sources: "
                  f"node_id key segment is {parsed_value!r} but the "
                  f"extracted property value is {found_value!r}. "
                  f"The materializer writes node rows from "
                  f"properties and edge FK columns from the parsed "
                  f"node_id segment — disagreement produces edges "
                  f"pointing at non-existent rows. (If a property "
                  f"value contains ',', that also triggers this "
                  f"since the node-id format is unescaped.)"
              ),
          )
      )

  failures.extend(
      _validate_properties(
          extracted_props=node.properties,
          spec_props=spec_entity.properties,
          base_path=base_path,
          node_id=node.node_id or None,
          edge_id=None,
          event_id=event_id,
      )
  )

  return failures


def _validate_edge(
    *,
    edge: ExtractedEdge,
    edge_index: int,
    spec_relationship: ResolvedRelationship,
    spec: ResolvedGraph,
    nodes_by_id: dict[str, ExtractedNode],
    event_id: Optional[str],
    allow_external_endpoints: bool,
) -> list[ValidationFailure]:
  """Per-edge checks: endpoint resolution, endpoint entity match,
  endpoint key presence, and per-property validation.

  When ``allow_external_endpoints`` is True, edges whose endpoints
  are not in the same ``ExtractedGraph`` skip the in-graph
  resolution check. The endpoint-key parse (``missing_endpoint_key``)
  still runs so an unparseable node-id still fails.
  """
  failures: list[ValidationFailure] = []
  base_path = f"edges[{edge_index}]"

  entity_by_name = {e.name: e for e in spec.entities}

  # Endpoint resolution + entity match. If from_node_id maps to a
  # node in the graph, that node's entity_name must match the
  # relationship's from_entity. Same for to_node_id.
  for direction, edge_node_id, expected_entity in (
      ("from_node_id", edge.from_node_id, spec_relationship.from_entity),
      ("to_node_id", edge.to_node_id, spec_relationship.to_entity),
  ):
    if not edge_node_id:
      failures.append(
          ValidationFailure(
              scope=FallbackScope.EDGE,
              code="unresolved_endpoint",
              path=f"{base_path}.{direction}",
              edge_id=edge.edge_id or None,
              event_id=event_id,
              detail=f"{direction} is empty",
          )
      )
      continue

    referenced = nodes_by_id.get(edge_node_id)
    if referenced is None:
      if not allow_external_endpoints:
        # Strict mode: the endpoint must exist in the same
        # ExtractedGraph. Lineage-edge batches that reference
        # nodes materialized in earlier passes should opt into
        # ``allow_external_endpoints=True``.
        failures.append(
            ValidationFailure(
                scope=FallbackScope.EDGE,
                code="unresolved_endpoint",
                path=f"{base_path}.{direction}",
                edge_id=edge.edge_id or None,
                event_id=event_id,
                observed=edge_node_id,
                detail=(
                    f"{direction}={edge_node_id!r} does not match "
                    f"any node in the extracted graph (pass "
                    f"allow_external_endpoints=True for lineage-"
                    f"edge batches)"
                ),
            )
        )
        continue
      # Permissive mode: we don't have the in-graph node, but the
      # node_id itself carries the entity segment ('{session}:
      # {entity}:k=v'). Compare that segment cheaply against
      # expected_entity so obvious mismatches still fail. An empty
      # entity segment ('sess1::outcome_id=o1') also fails — the
      # documented shape requires a non-empty entity. The endpoint-
      # key parse still runs below so missing FK columns also fire
      # per-column.
      parts = edge_node_id.split(":")
      if len(parts) >= 3:
        observed_entity = parts[1]
        if observed_entity != expected_entity:
          failures.append(
              ValidationFailure(
                  scope=FallbackScope.EDGE,
                  code="wrong_endpoint_entity",
                  path=f"{base_path}.{direction}",
                  edge_id=edge.edge_id or None,
                  event_id=event_id,
                  observed=observed_entity,
                  expected=expected_entity,
                  detail=(
                      f"{direction}={edge_node_id!r} carries entity "
                      f"segment {observed_entity!r}, but relationship "
                      f"{spec_relationship.name!r} expects "
                      f"{expected_entity!r} (permissive mode: parsed "
                      f"from node_id since the endpoint node is not "
                      f"in this graph)"
                  ),
              )
          )

    if referenced is not None and referenced.entity_name != expected_entity:
      failures.append(
          ValidationFailure(
              scope=FallbackScope.EDGE,
              code="wrong_endpoint_entity",
              path=f"{base_path}.{direction}",
              edge_id=edge.edge_id or None,
              event_id=event_id,
              observed=referenced.entity_name,
              expected=expected_entity,
              detail=(
                  f"{direction}={edge_node_id!r} resolves to a node "
                  f"of entity {referenced.entity_name!r}, but "
                  f"relationship {spec_relationship.name!r} expects "
                  f"{expected_entity!r}"
              ),
          )
      )

    # Endpoint-key presence. The relationship's from_columns /
    # to_columns must be readable from the edge's node-id segment
    # via the same parser the materializer uses
    # (``parse_key_segment`` in ``_ontology_routing``, shared by
    # both modules). The materializer builds FK column values from
    # the node-id segment, not from the endpoint node's
    # properties — so a node-id like 'd1' that parses to {}
    # silently produces empty FK columns at INSERT time.
    # Validating against the parsed segment closes that silent-
    # corruption gap.
    cols = (
        spec_relationship.from_columns
        if direction == "from_node_id"
        else spec_relationship.to_columns
    )
    parsed_keys = parse_key_segment(edge_node_id)
    for key_col in cols:
      value = parsed_keys.get(key_col, "")
      if not value:
        failures.append(
            ValidationFailure(
                scope=FallbackScope.EDGE,
                code="missing_endpoint_key",
                path=f"{base_path}.{direction}.<key:{key_col}>",
                node_id=edge_node_id,
                edge_id=edge.edge_id or None,
                event_id=event_id,
                expected=key_col,
                observed=edge_node_id,
                detail=(
                    f"endpoint key column {key_col!r} on relationship "
                    f"{spec_relationship.name!r} cannot be read from "
                    f"node_id {edge_node_id!r}: the materializer "
                    f"parses keys from the format "
                    f"'{{session}}:{{entity}}:k1=v1,k2=v2'; this "
                    f"node_id does not match that shape (or the "
                    f"required column is missing from the segment)"
                ),
            )
        )

  # Per-property validation on the edge.
  failures.extend(
      _validate_properties(
          extracted_props=edge.properties,
          spec_props=spec_relationship.properties,
          base_path=base_path,
          node_id=None,
          edge_id=edge.edge_id or None,
          event_id=event_id,
      )
  )

  return failures


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #


def validate_extracted_graph(
    spec: ResolvedGraph,
    graph: ExtractedGraph,
    *,
    allow_external_endpoints: bool = False,
) -> ValidationReport:
  """Validate an extracted graph against a resolved spec.

  Returns a :class:`ValidationReport` with NODE/FIELD/EDGE-scope
  failures. ``report.ok`` is True iff ``failures`` is empty.

  Args:
      spec: The runtime-facing :class:`ResolvedGraph` (output of
          :func:`bigquery_agent_analytics.resolved_spec.resolve`).
      graph: The :class:`ExtractedGraph` to validate.
      allow_external_endpoints: When False (default), edges whose
          ``from_node_id`` / ``to_node_id`` does not match any node
          in ``graph.nodes`` produce ``unresolved_endpoint``. When
          True, those edges are accepted on the assumption that the
          referenced node was materialized in an earlier pass — set
          this for lineage-edge batches where ``graph.nodes`` is
          empty by design. The endpoint-key parse
          (``missing_endpoint_key``) still runs in both modes, so
          unparseable node-ids still fail.

  Returns:
      A :class:`ValidationReport`.
  """
  failures: list[ValidationFailure] = []

  entity_by_name = {e.name: e for e in spec.entities}
  relationship_by_name = {r.name: r for r in spec.relationships}

  # Pass 0: global duplicate-node-id detection. Runs across every
  # node — including unknown-entity nodes — because the issue's
  # contract is "node_id must be unique within the graph," and a
  # node with an unknown entity still has a node_id that downstream
  # consumers will trip over. Doing this in Pass 1 alongside
  # entity-specific checks would skip the unknown-entity branch.
  nodes_by_id: dict[str, ExtractedNode] = {}
  duplicates_seen: set[str] = set()
  for i, node in enumerate(graph.nodes):
    if not node.node_id:
      continue  # missing_node_id is reported below
    if node.node_id in nodes_by_id and node.node_id not in duplicates_seen:
      failures.append(
          ValidationFailure(
              scope=FallbackScope.NODE,
              code="duplicate_node_id",
              path=f"nodes[{i}].node_id",
              node_id=node.node_id,
              observed=node.node_id,
              detail=f"duplicate node_id {node.node_id!r}",
          )
      )
      duplicates_seen.add(node.node_id)
    else:
      nodes_by_id.setdefault(node.node_id, node)

  # Pass 1: per-node validation. nodes_by_id is now populated; the
  # duplicate-detection responsibility has moved out of
  # _validate_node(), so it only runs entity-specific checks.
  for i, node in enumerate(graph.nodes):
    spec_entity = entity_by_name.get(node.entity_name)
    if spec_entity is None:
      failures.append(
          ValidationFailure(
              scope=FallbackScope.NODE,
              code="unknown_entity",
              path=f"nodes[{i}].entity_name",
              node_id=node.node_id or None,
              observed=node.entity_name,
              detail=(
                  f"entity_name {node.entity_name!r} is not declared "
                  f"in the resolved spec"
              ),
          )
      )
      continue

    failures.extend(
        _validate_node(
            node=node,
            node_index=i,
            spec_entity=spec_entity,
            event_id=None,
        )
    )

  # Pass 2: edges. Resolve endpoints against nodes_by_id.
  for j, edge in enumerate(graph.edges):
    spec_rel = relationship_by_name.get(edge.relationship_name)
    if spec_rel is None:
      failures.append(
          ValidationFailure(
              scope=FallbackScope.EDGE,
              code="unknown_relationship",
              path=f"edges[{j}].relationship_name",
              edge_id=edge.edge_id or None,
              observed=edge.relationship_name,
              detail=(
                  f"relationship_name {edge.relationship_name!r} is "
                  f"not declared in the resolved spec"
              ),
          )
      )
      continue

    failures.extend(
        _validate_edge(
            edge=edge,
            edge_index=j,
            spec_relationship=spec_rel,
            spec=spec,
            nodes_by_id=nodes_by_id,
            event_id=None,
            allow_external_endpoints=allow_external_endpoints,
        )
    )

  return ValidationReport(failures=tuple(failures))


def validate_extracted_graph_from_ontology(
    ontology,
    binding,
    graph: ExtractedGraph,
    *,
    allow_external_endpoints: bool = False,
) -> ValidationReport:
  """Adapter: ``resolve(ontology, binding)`` then delegate.

  For callers holding upstream ``Ontology`` + ``Binding`` instead
  of a :class:`ResolvedGraph` (e.g. authoring-time validation
  before binding is finalized).
  """
  from .resolved_spec import resolve

  return validate_extracted_graph(
      resolve(ontology, binding),
      graph,
      allow_external_endpoints=allow_external_endpoints,
  )
