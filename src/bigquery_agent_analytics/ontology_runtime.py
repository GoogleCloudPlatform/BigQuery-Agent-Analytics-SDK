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

"""Read-side runtime for ontology + binding + concept index
(issue #58 reader follow-on to PR #92's emission side).

PR #92 ships the **emission** path: `gm compile
--emit-concept-index` writes a deterministic concept-index
table plus an `__meta` sibling table carrying the
``compile_fingerprint`` / ``compile_id`` provenance. This
module ships the **reader** path:

* :class:`OntologyRuntime` — public façade. Loads
  ``Ontology + Binding`` from YAML files (or accepts already-
  loaded models) plus an optional :class:`ConceptIndexLookup`
  wired to the emitted BigQuery table. Read-only accessors
  walk the loaded models for entity / relationship / SKOS
  metadata without re-parsing.
* :class:`ConceptIndexLookup` — BigQuery-backed accessor over
  the emitted concept index. **Fingerprint-strict**: verifies
  the table's ``__meta`` row matches the runtime's active
  ontology + binding before returning any row, and every
  per-query SQL includes ``WHERE compile_fingerprint = @fp``
  as defense in depth. Stale provenance produces no confident
  match.
* :class:`EntityResolver` — Protocol an extraction / resolution
  pipeline depends on. Two reference implementations:
  :class:`ExactEntityResolver` (in-memory match on
  ``entity_name``) and :class:`LabelSynonymResolver`
  (BQ-backed match against the concept index's
  ``label`` / ``synonym`` / ``notation`` rows). **No embedding,
  LLM, or fuzzy matching in this slice** — those are explicit
  non-goals for the reader v1; future PRs can layer fuzzier
  resolvers on top of the same Protocol without changing the
  runtime surface.

Trust contract — same discipline as Phase C compiled
extractors: stale provenance must never produce a confident
match. The fingerprint check runs at three points:

1. :meth:`OntologyRuntime.from_files` (and
   :meth:`OntologyRuntime.from_models`) verifies the
   ``__meta`` row against locally-computed
   ``compile_fingerprint(ontology_fp, binding_fp,
   compiler_version)`` when a ``concept_index_table`` is
   supplied. Mismatch raises
   :class:`FingerprintMismatchError` before any reader is
   returned.
2. :meth:`ConceptIndexLookup.verify` is the same gate
   exposed as a method so callers can re-check explicitly
   (e.g. before a long batch).
3. Every :meth:`ConceptIndexLookup.lookup_by_*` query
   includes ``WHERE compile_fingerprint = @expected_fp``.
   Even if the table is swapped or partially-corrupted
   mid-flight, rows with a stale fingerprint can't surface.

Out of scope (deferred):

* **Embedding / LLM-backed resolvers.** Future PRs can layer
  fuzzier matching on top of the :class:`EntityResolver`
  Protocol without touching the runtime surface.
* **Cross-language fallback.** ``lookup_by_label`` filters
  by language when asked; no automatic "if French missed,
  try English."
* **Result ranking by user signals.** The reader returns
  candidates ordered by the concept-index emission's stable
  sort (`scheme`, `entity_name`, `label_kind`, ...). Ranking
  by usage / recency / context belongs in the consumer.
* **Mutation.** Read-only by design. The emission side
  (`gm compile --emit-concept-index`) is the writer.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Optional, Protocol, runtime_checkable, Union

from bigquery_ontology import Binding
from bigquery_ontology import Entity
from bigquery_ontology import load_binding
from bigquery_ontology import load_ontology
from bigquery_ontology import Ontology
from bigquery_ontology import Relationship
from bigquery_ontology._fingerprint import compile_fingerprint
from bigquery_ontology._fingerprint import compile_id
from bigquery_ontology._fingerprint import fingerprint_model

__all__ = [
    "ConceptIndexError",
    "ConceptIndexLookup",
    "ConceptIndexRowView",
    "EntityResolver",
    "ExactEntityResolver",
    "FingerprintMismatchError",
    "LabelSynonymResolver",
    "MetaTableEmptyError",
    "MetaTableMissingError",
    "OntologyRuntime",
    "ResolverCandidate",
]

# Label-kind priority order used by the concept-index emission
# (PR #92 / RFC §6 finding 6). Lower index = higher priority.
# Re-stated here so the reader's ranking matches the emission's
# documented contract.
_LABEL_KIND_PRIORITY: tuple[str, ...] = (
    "name",
    "pref",
    "alt",
    "hidden",
    "synonym",
    "notation",
)

# Columns the lookup expects on the main concept-index table.
# Matches `_MAIN_COLUMNS` in
# ``bigquery_ontology/graph_ddl_compiler.py``. Re-stated as the
# explicit projection so a table-schema drift fails at SELECT
# time with a clear column-not-found rather than corrupting
# the row view.
_MAIN_COLUMNS: tuple[str, ...] = (
    "entity_name",
    "label",
    "label_kind",
    "notation",
    "scheme",
    "language",
    "is_abstract",
    "compile_id",
    "compile_fingerprint",
)


# ------------------------------------------------------------------ #
# Errors                                                              #
# ------------------------------------------------------------------ #


class ConceptIndexError(Exception):
  """Base class for concept-index reader failures.

  Subclasses name the specific failure mode so callers can
  ``except FingerprintMismatchError`` to surface a clean
  operator message vs. ``except ConceptIndexError`` to
  blanket-catch.
  """


class FingerprintMismatchError(ConceptIndexError):
  """The concept-index table's ``__meta`` ``compile_fingerprint``
  doesn't equal the value the runtime computed from its
  locally-loaded ontology + binding. The table was compiled
  from a different ontology + binding (or a different
  compiler version); fail-closed.
  """

  def __init__(
      self,
      *,
      table_id: str,
      expected_compile_fingerprint: str,
      actual_compile_fingerprint: str,
      compiler_version: str,
  ) -> None:
    super().__init__(
        f"concept index {table_id!r}: compile_fingerprint mismatch — "
        f"expected {expected_compile_fingerprint!r} from locally-loaded "
        f"ontology + binding (compiler_version={compiler_version!r}), "
        f"got {actual_compile_fingerprint!r} on the table's __meta row"
    )
    self.table_id = table_id
    self.expected_compile_fingerprint = expected_compile_fingerprint
    self.actual_compile_fingerprint = actual_compile_fingerprint
    self.compiler_version = compiler_version


class MetaTableMissingError(ConceptIndexError):
  """The concept-index ``__meta`` sibling table doesn't exist
  or isn't queryable. Without it, the reader has no
  fingerprint to compare against and must fail-closed."""

  def __init__(self, *, table_id: str, detail: str) -> None:
    super().__init__(
        f"concept index {table_id!r}: __meta sibling table is not "
        f"queryable ({detail})"
    )
    self.table_id = table_id


class MetaTableEmptyError(ConceptIndexError):
  """The ``__meta`` sibling table exists but contains zero
  rows. PR #92's emission writes exactly one meta row per
  table; an empty meta table indicates manual tampering."""

  def __init__(self, *, table_id: str) -> None:
    super().__init__(
        f"concept index {table_id!r}: __meta sibling table is empty"
    )
    self.table_id = table_id


# ------------------------------------------------------------------ #
# Data types                                                          #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class ConceptIndexRowView:
  """Read-only view over one row of the emitted concept index.

  Fields match PR #92's main-table schema 1:1. Returned by
  :meth:`ConceptIndexLookup.lookup_by_*` and used internally
  to build :class:`ResolverCandidate` instances.
  """

  entity_name: str
  label: str
  label_kind: str
  notation: Optional[str]
  scheme: Optional[str]
  language: Optional[str]
  is_abstract: bool
  compile_id: str
  compile_fingerprint: str


@dataclasses.dataclass(frozen=True)
class ResolverCandidate:
  """One match returned by an :class:`EntityResolver`.

  Carries the matched label + its kind + the row's full
  provenance so a consumer can both surface the human-readable
  match (``matched_label``) and audit-trail the decision
  (``compile_id`` / ``compile_fingerprint``). Multiple
  candidates for the same ``entity_name`` are legal — a
  concept reached via both ``name`` and ``synonym`` matches
  produces two candidates; the caller decides whether to
  collapse on ``entity_name`` or rank by ``matched_label_kind``.
  """

  entity_name: str
  matched_label: str
  matched_label_kind: str
  notation: Optional[str]
  scheme: Optional[str]
  language: Optional[str]
  is_abstract: bool
  compile_id: str
  compile_fingerprint: str


# ------------------------------------------------------------------ #
# Resolver protocol                                                   #
# ------------------------------------------------------------------ #


@runtime_checkable
class EntityResolver(Protocol):
  """Map a free-text query to a list of
  :class:`ResolverCandidate` matches.

  Reference implementations: :class:`ExactEntityResolver`
  (in-memory match on ``entity_name``) and
  :class:`LabelSynonymResolver` (BigQuery-backed match against
  the emitted concept-index rows). Fuzzier resolvers
  (embedding-backed, LLM-backed) can implement this same
  Protocol in future PRs without touching :class:`OntologyRuntime`.
  """

  def resolve(self, query: str, *, limit: int = 10) -> list[ResolverCandidate]:
    ...


# ------------------------------------------------------------------ #
# Concept-index lookup (BigQuery-backed)                              #
# ------------------------------------------------------------------ #


class ConceptIndexLookup:
  """BigQuery-backed accessor over an emitted concept-index
  table.

  Constructed lazily — call :meth:`verify` (or any
  ``lookup_*`` method) to actually issue a query.
  :class:`OntologyRuntime.from_files` calls
  :meth:`verify` once at construction so the fingerprint
  mismatch surfaces eagerly.

  Every ``lookup_*`` query includes ``WHERE compile_fingerprint
  = @expected_compile_fingerprint`` as defense in depth: even
  if the table is swapped between :meth:`verify` and the
  query, rows from a stale compile can't surface in the
  result.

  Construction parameters:

  * ``bq_client`` — anything implementing the
    ``google.cloud.bigquery.Client`` query API (tests pass
    in-memory fakes).
  * ``table_id`` — ``project.dataset.table``. The
    ``__meta`` sibling is read at ``table_id + '__meta'``,
    matching PR #92's emission convention.
  * ``expected_compile_fingerprint`` — full 64-hex sha256
    computed locally via :func:`compile_fingerprint` from the
    runtime's ontology + binding + compiler_version.
  * ``compiler_version`` — recorded for error messages.
  """

  def __init__(
      self,
      *,
      bq_client: Any,
      table_id: str,
      expected_compile_fingerprint: str,
      compiler_version: str,
  ) -> None:
    self._bq_client = bq_client
    self._table_id = table_id
    self._meta_table_id = table_id + "__meta"
    self._expected_compile_fingerprint = expected_compile_fingerprint
    self._compiler_version = compiler_version
    self._verified = False

  # -------------------------------------------------------- #
  # Properties                                               #
  # -------------------------------------------------------- #

  @property
  def table_id(self) -> str:
    return self._table_id

  @property
  def expected_compile_fingerprint(self) -> str:
    return self._expected_compile_fingerprint

  # -------------------------------------------------------- #
  # Verification                                             #
  # -------------------------------------------------------- #

  def verify(self) -> None:
    """Read the ``__meta`` sibling row and check it matches
    the locally-computed compile_fingerprint. Idempotent —
    re-running is a no-op after the first success.

    Raises:
      MetaTableMissingError: the ``__meta`` table doesn't
        exist or the query failed.
      MetaTableEmptyError: the ``__meta`` table has zero rows.
      FingerprintMismatchError: the ``__meta`` row's
        ``compile_fingerprint`` differs from
        ``expected_compile_fingerprint``.
    """
    if self._verified:
      return
    sql = f"SELECT compile_fingerprint " f"FROM `{self._meta_table_id}` LIMIT 1"
    try:
      rows = list(self._bq_client.query(sql).result())
    except Exception as exc:  # noqa: BLE001 — record + raise
      raise MetaTableMissingError(
          table_id=self._table_id,
          detail=f"{type(exc).__name__}: {exc}",
      ) from exc
    if not rows:
      raise MetaTableEmptyError(table_id=self._table_id)
    actual = rows[0]["compile_fingerprint"]
    if actual != self._expected_compile_fingerprint:
      raise FingerprintMismatchError(
          table_id=self._table_id,
          expected_compile_fingerprint=self._expected_compile_fingerprint,
          actual_compile_fingerprint=actual,
          compiler_version=self._compiler_version,
      )
    self._verified = True

  # -------------------------------------------------------- #
  # Public lookup API                                        #
  # -------------------------------------------------------- #

  def lookup_by_label(
      self,
      label: str,
      *,
      case_insensitive: bool = True,
      label_kinds: Optional[tuple[str, ...]] = None,
      language: Optional[str] = None,
      limit: int = 100,
  ) -> list[ConceptIndexRowView]:
    """Return rows whose ``label`` column matches *label*.

    Args:
      label: The query string. Compared with the emitted
        ``label`` column.
      case_insensitive: Compare via ``LOWER(label) =
        LOWER(@label)``. Defaults to ``True`` because
        operator queries rarely care about case.
      label_kinds: Optional filter on ``label_kind`` (``name``
        / ``pref`` / ``alt`` / ``hidden`` / ``synonym`` /
        ``notation``). When ``None``, every kind is returned.
      language: Optional filter on ``language``. ``None`` is
        the default-language label or notation; use
        ``language="fr"`` to limit to French.
      limit: Cap on returned rows. Default 100 — for typical
        resolver queries this is enough; the caller can bump
        it for sweep-style operations.
    """
    self.verify()
    predicate = (
        "LOWER(label) = LOWER(@label)" if case_insensitive else "label = @label"
    )
    where_clauses = [
        "compile_fingerprint = @expected_fp",
        predicate,
    ]
    params: dict[str, Any] = {
        "label": label,
        "expected_fp": self._expected_compile_fingerprint,
    }
    if label_kinds is not None:
      where_clauses.append("label_kind IN UNNEST(@label_kinds)")
      params["label_kinds"] = list(label_kinds)
    if language is not None:
      where_clauses.append("language = @language")
      params["language"] = language
    return self._run_lookup(where_clauses, params, limit=limit)

  def lookup_by_entity_name(
      self,
      entity_name: str,
      *,
      label_kinds: Optional[tuple[str, ...]] = None,
      limit: int = 100,
  ) -> list[ConceptIndexRowView]:
    """Return every row for an entity (every
    ``(label, label_kind, language, scheme)`` membership).

    Useful for "show me all the labels this concept has" —
    the inverse direction from
    :meth:`lookup_by_label`."""
    self.verify()
    where_clauses = [
        "compile_fingerprint = @expected_fp",
        "entity_name = @entity_name",
    ]
    params: dict[str, Any] = {
        "entity_name": entity_name,
        "expected_fp": self._expected_compile_fingerprint,
    }
    if label_kinds is not None:
      where_clauses.append("label_kind IN UNNEST(@label_kinds)")
      params["label_kinds"] = list(label_kinds)
    return self._run_lookup(where_clauses, params, limit=limit)

  def lookup_by_notation(
      self,
      notation: str,
      *,
      limit: int = 100,
  ) -> list[ConceptIndexRowView]:
    """Return rows whose ``notation`` column equals
    *notation*.

    Notation matches are exact (no case folding) — notations
    are display tokens like ``"ACME-7"`` and codes like
    ``"4350"``; tolerating case would risk collapsing
    distinct values."""
    self.verify()
    where_clauses = [
        "compile_fingerprint = @expected_fp",
        "notation = @notation",
    ]
    params: dict[str, Any] = {
        "notation": notation,
        "expected_fp": self._expected_compile_fingerprint,
    }
    return self._run_lookup(where_clauses, params, limit=limit)

  # -------------------------------------------------------- #
  # Helpers                                                  #
  # -------------------------------------------------------- #

  def _run_lookup(
      self,
      where_clauses: list[str],
      params: dict[str, Any],
      *,
      limit: int,
  ) -> list[ConceptIndexRowView]:
    """Common SELECT path for every ``lookup_*`` method.

    Builds the SQL from a fixed column projection + the
    caller's WHERE clauses + an explicit ``LIMIT``. Parameter
    binding via the BigQuery client's query-parameters API
    keeps user input out of the SQL string.
    """
    columns = ", ".join(_MAIN_COLUMNS)
    sql = (
        f"SELECT {columns} FROM `{self._table_id}` "
        f"WHERE {' AND '.join(where_clauses)} "
        f"LIMIT @row_limit"
    )
    params_with_limit = dict(params)
    params_with_limit["row_limit"] = int(limit)
    job_config = self._build_query_config(params_with_limit)
    rows = self._bq_client.query(sql, job_config=job_config).result()
    return [
        ConceptIndexRowView(
            entity_name=row["entity_name"],
            label=row["label"],
            label_kind=row["label_kind"],
            notation=row["notation"],
            scheme=row["scheme"],
            language=row["language"],
            is_abstract=bool(row["is_abstract"]),
            compile_id=row["compile_id"],
            compile_fingerprint=row["compile_fingerprint"],
        )
        for row in rows
    ]

  def _build_query_config(self, params: dict[str, Any]) -> Any:
    """Construct a ``bigquery.QueryJobConfig`` for the given
    parameter dict. Tests pass a fake client whose ``query``
    method ignores ``job_config``; production uses the real
    client's parameter binding."""
    try:
      from google.cloud import bigquery  # type: ignore
    except Exception:
      # Test environments without google-cloud-bigquery
      # installed still work because the fake client's
      # ``query()`` doesn't read ``job_config``.
      return None

    parameters = []
    for name, value in params.items():
      if isinstance(value, list):
        parameters.append(bigquery.ArrayQueryParameter(name, "STRING", value))
      elif isinstance(value, int):
        parameters.append(bigquery.ScalarQueryParameter(name, "INT64", value))
      else:
        parameters.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return bigquery.QueryJobConfig(query_parameters=parameters)


# ------------------------------------------------------------------ #
# Resolvers                                                           #
# ------------------------------------------------------------------ #


class ExactEntityResolver:
  """Resolve by exact ``entity_name`` match.

  Reads only from the runtime's loaded :class:`Ontology`; no
  BigQuery roundtrip. Returns at most one candidate per
  query — `entity_name` is unique within an ontology.

  Useful when callers already know the entity_name they want
  but need the canonical :class:`ResolverCandidate` shape
  (e.g. to feed a downstream pipeline that consumes the
  Protocol).
  """

  def __init__(
      self,
      runtime: "OntologyRuntime",
      *,
      case_insensitive: bool = False,
  ) -> None:
    self._runtime = runtime
    self._case_insensitive = case_insensitive

  def resolve(self, query: str, *, limit: int = 10) -> list[ResolverCandidate]:
    """Return at most one candidate whose ``entity_name``
    matches *query*. ``limit`` is accepted for Protocol
    symmetry but never returns more than one row (exact match
    on a unique name)."""
    if not query:
      return []
    entity = self._runtime.entity(
        query, case_insensitive=self._case_insensitive
    )
    if entity is None:
      return []
    # Without a concept index there's no compile_fingerprint
    # to record. The candidate's provenance fields fall back
    # to the runtime's locally-computed values.
    return [
        ResolverCandidate(
            entity_name=entity.name,
            matched_label=entity.name,
            matched_label_kind="name",
            notation=self._runtime.notation_for(entity.name),
            scheme=None,
            language=None,
            is_abstract=entity.abstract,
            compile_id=self._runtime.compile_id,
            compile_fingerprint=self._runtime.compile_fingerprint,
        )
    ]


class LabelSynonymResolver:
  """Resolve via the concept-index ``label`` / ``synonym`` /
  ``notation`` rows. **Requires** the runtime to have an
  attached :class:`ConceptIndexLookup` — fuzzier in-memory
  resolution against ``Entity.synonyms`` isn't shipped as a
  separate codepath because the concept-index emission is
  already the canonical SKOS-aware materialization.

  Candidates are returned in the concept index's stable sort
  order, then re-sorted by :data:`_LABEL_KIND_PRIORITY` so a
  ``name`` match outranks a ``synonym`` match for the same
  query.
  """

  def __init__(self, runtime: "OntologyRuntime") -> None:
    if runtime.concept_index is None:
      raise ValueError(
          "LabelSynonymResolver requires runtime.concept_index; "
          "construct OntologyRuntime with a concept_index_table."
      )
    self._runtime = runtime
    self._lookup = runtime.concept_index

  def resolve(
      self,
      query: str,
      *,
      limit: int = 10,
      label_kinds: Optional[tuple[str, ...]] = None,
      language: Optional[str] = None,
      case_insensitive: bool = True,
  ) -> list[ResolverCandidate]:
    """Return candidates whose label matches *query*.

    Ranking: rows are pulled in the emitted sort order, then
    re-sorted by ``_LABEL_KIND_PRIORITY`` so ``name`` matches
    come before ``pref`` before ``alt`` before ``hidden``
    before ``synonym`` before ``notation``. Within a kind,
    the emission's deterministic order is preserved.

    The ``limit`` parameter caps the number of rows the BQ
    query fetches AND the number of candidates returned, so a
    caller asking for 10 doesn't pay for materializing 1000."""
    if not query:
      return []
    rows = self._lookup.lookup_by_label(
        query,
        case_insensitive=case_insensitive,
        label_kinds=label_kinds,
        language=language,
        limit=limit,
    )
    candidates = [
        ResolverCandidate(
            entity_name=row.entity_name,
            matched_label=row.label,
            matched_label_kind=row.label_kind,
            notation=row.notation,
            scheme=row.scheme,
            language=row.language,
            is_abstract=row.is_abstract,
            compile_id=row.compile_id,
            compile_fingerprint=row.compile_fingerprint,
        )
        for row in rows
    ]
    candidates.sort(key=_candidate_priority_key)
    return candidates[:limit]


def _candidate_priority_key(candidate: ResolverCandidate) -> tuple:
  """Sort key for :class:`LabelSynonymResolver`. Lower tuple =
  higher priority. ``label_kind`` not in the priority list
  (future-emitted kinds) sorts last to keep behavior
  deterministic without dropping rows."""
  try:
    kind_rank = _LABEL_KIND_PRIORITY.index(candidate.matched_label_kind)
  except ValueError:
    kind_rank = len(_LABEL_KIND_PRIORITY)
  return (
      kind_rank,
      candidate.entity_name,
      candidate.matched_label,
      candidate.language or "",
      candidate.scheme or "",
  )


# ------------------------------------------------------------------ #
# OntologyRuntime — the public façade                                 #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class OntologyRuntime:
  """Read-side runtime over a validated
  ``Ontology + Binding`` pair plus an optional concept-index
  lookup.

  Public surface lives in this dataclass for two reasons:

  1. Frozen / immutable — concurrent readers can share an
     instance without coordination.
  2. Field-based — callers can pass an already-loaded
     ``Ontology + Binding`` (e.g. from a hot reload path) and
     attach a ``ConceptIndexLookup`` separately.

  See :meth:`from_files` for the YAML-loading factory and
  :meth:`from_models` for the model-passing factory.
  """

  ontology: Ontology
  binding: Binding
  compiler_version: str
  concept_index: Optional[ConceptIndexLookup] = None

  # -------------------------------------------------------- #
  # Factories                                                #
  # -------------------------------------------------------- #

  @classmethod
  def from_files(
      cls,
      *,
      ontology_path: Union[str, pathlib.Path],
      binding_path: Union[str, pathlib.Path],
      compiler_version: str,
      concept_index_table: Optional[str] = None,
      bq_client: Optional[Any] = None,
  ) -> "OntologyRuntime":
    """Load ontology + binding YAML and (optionally) attach a
    BigQuery-backed :class:`ConceptIndexLookup`.

    Args:
      ontology_path: Path to the ontology YAML.
      binding_path: Path to the binding YAML.
      compiler_version: The version string the concept-index
        was emitted with. Used in the fingerprint computation;
        a mismatch surfaces as
        :class:`FingerprintMismatchError`. Recorded directly
        on the runtime so callers (and resolvers) can audit
        which version produced the active index.
      concept_index_table: Optional ``project.dataset.table``
        for the emitted concept index. When supplied, the
        ``__meta`` sibling is verified during construction;
        when ``None``, the runtime is in-memory only and
        BigQuery-backed resolvers will raise on use.
      bq_client: BigQuery client (required iff
        ``concept_index_table`` is set). Tests pass an
        in-memory fake.

    Raises:
      FingerprintMismatchError: ``concept_index_table`` is set
        and its ``__meta`` row's ``compile_fingerprint``
        doesn't match the locally-computed value.
      MetaTableMissingError / MetaTableEmptyError: see
        :meth:`ConceptIndexLookup.verify`.
    """
    ontology = load_ontology(str(ontology_path))
    binding = load_binding(str(binding_path), ontology=ontology)
    return cls.from_models(
        ontology=ontology,
        binding=binding,
        compiler_version=compiler_version,
        concept_index_table=concept_index_table,
        bq_client=bq_client,
    )

  @classmethod
  def from_models(
      cls,
      *,
      ontology: Ontology,
      binding: Binding,
      compiler_version: str,
      concept_index_table: Optional[str] = None,
      bq_client: Optional[Any] = None,
  ) -> "OntologyRuntime":
    """Same shape as :meth:`from_files`, but the caller has
    already loaded the models. Useful for hot-reload paths
    and tests that build models in memory."""
    lookup: Optional[ConceptIndexLookup] = None
    if concept_index_table is not None:
      if bq_client is None:
        raise ValueError(
            "concept_index_table is set but bq_client is None; "
            "supply a bigquery.Client (or test substitute) to "
            "verify the table's compile_fingerprint."
        )
      expected_fp = compile_fingerprint(
          fingerprint_model(ontology),
          fingerprint_model(binding),
          compiler_version,
      )
      lookup = ConceptIndexLookup(
          bq_client=bq_client,
          table_id=concept_index_table,
          expected_compile_fingerprint=expected_fp,
          compiler_version=compiler_version,
      )
      # Verify eagerly so the operator sees a fingerprint
      # mismatch at startup, not on first query.
      lookup.verify()
    return cls(
        ontology=ontology,
        binding=binding,
        compiler_version=compiler_version,
        concept_index=lookup,
    )

  # -------------------------------------------------------- #
  # Provenance accessors                                     #
  # -------------------------------------------------------- #

  @property
  def compile_fingerprint(self) -> str:
    """Locally-computed full 64-hex compile_fingerprint."""
    return compile_fingerprint(
        fingerprint_model(self.ontology),
        fingerprint_model(self.binding),
        self.compiler_version,
    )

  @property
  def compile_id(self) -> str:
    """12-hex display token (truncation of
    ``compile_fingerprint``)."""
    return compile_id(
        fingerprint_model(self.ontology),
        fingerprint_model(self.binding),
        self.compiler_version,
    )

  # -------------------------------------------------------- #
  # Read accessors                                           #
  # -------------------------------------------------------- #

  def entity(
      self, name: str, *, case_insensitive: bool = False
  ) -> Optional[Entity]:
    """Return the entity with the given name, or ``None``."""
    if not name:
      return None
    target = name.lower() if case_insensitive else name
    for ent in self.ontology.entities:
      candidate = ent.name.lower() if case_insensitive else ent.name
      if candidate == target:
        return ent
    return None

  def entities(self) -> tuple[Entity, ...]:
    """Tuple of every entity, in declared order."""
    return tuple(self.ontology.entities)

  def relationship(self, name: str) -> Optional[Relationship]:
    """Return the relationship with the given name, or
    ``None``."""
    if not name:
      return None
    for rel in self.ontology.relationships:
      if rel.name == name:
        return rel
    return None

  def relationships(self) -> tuple[Relationship, ...]:
    """Tuple of every relationship, in declared order."""
    return tuple(self.ontology.relationships)

  # -------------------------------------------------------- #
  # SKOS / label traversal                                   #
  # -------------------------------------------------------- #

  def synonyms_for(self, entity_name: str) -> tuple[str, ...]:
    """Return the entity's declared synonyms (the ``synonyms:``
    field on the YAML). Empty tuple when the entity has none
    or doesn't exist."""
    ent = self.entity(entity_name)
    if ent is None or not ent.synonyms:
      return ()
    return tuple(ent.synonyms)

  def annotations_for(self, entity_name: str) -> dict[str, Any]:
    """Return the entity's annotations dict (the
    ``annotations:`` field). Empty dict when the entity has
    none or doesn't exist."""
    ent = self.entity(entity_name)
    if ent is None or not ent.annotations:
      return {}
    return dict(ent.annotations)

  def labels_for(self, entity_name: str) -> tuple[tuple[str, str], ...]:
    """Return ``(label, label_kind)`` tuples for every label
    declared on the entity.

    Sources:

    * Entity name itself → ``("Name", "name")``.
    * Each ``synonyms`` entry → ``(value, "synonym")``.
    * Annotation keys ``skos:prefLabel`` / ``skos:altLabel`` /
      ``skos:hiddenLabel`` (with or without ``@<lang>`` suffix)
      → ``(value, "pref" / "alt" / "hidden")``.

    Matches the kind taxonomy used by the concept-index
    emission so a caller comparing in-memory labels against
    emitted rows sees the same kind strings.
    """
    ent = self.entity(entity_name)
    if ent is None:
      return ()
    labels: list[tuple[str, str]] = [(ent.name, "name")]
    if ent.synonyms:
      for syn in ent.synonyms:
        labels.append((syn, "synonym"))
    if ent.annotations:
      for key, value in ent.annotations.items():
        kind = _label_kind_for_annotation_key(key)
        if kind is None:
          continue
        if isinstance(value, list):
          for v in value:
            if isinstance(v, str):
              labels.append((v, kind))
        elif isinstance(value, str):
          labels.append((value, kind))
    return tuple(labels)

  def schemes_for(self, entity_name: str) -> tuple[str, ...]:
    """Return concept schemes the entity belongs to (the
    ``skos:inScheme`` annotation values). Empty tuple when the
    entity isn't in any scheme."""
    ent = self.entity(entity_name)
    if ent is None or not ent.annotations:
      return ()
    raw = ent.annotations.get("skos:inScheme")
    if raw is None:
      return ()
    if isinstance(raw, list):
      return tuple(v for v in raw if isinstance(v, str))
    if isinstance(raw, str):
      return (raw,)
    return ()

  def notation_for(self, entity_name: str) -> Optional[str]:
    """Return the entity's ``skos:notation`` value, or
    ``None``. When multiple notations are declared, returns
    the first (matching the emission's first-notation rule)."""
    ent = self.entity(entity_name)
    if ent is None or not ent.annotations:
      return None
    raw = ent.annotations.get("skos:notation")
    if raw is None:
      return None
    if isinstance(raw, list):
      for v in raw:
        if isinstance(v, str):
          return v
      return None
    if isinstance(raw, str):
      return raw
    return None


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _label_kind_for_annotation_key(key: str) -> Optional[str]:
  """Map a SKOS annotation key (with or without ``@<lang>``
  suffix) to its concept-index ``label_kind``.

  Returns ``None`` for keys that don't carry a SKOS label
  (``skos:inScheme``, ``skos:notation``, custom annotations).
  """
  base = key.split("@", 1)[0]
  if base == "skos:prefLabel":
    return "pref"
  if base == "skos:altLabel":
    return "alt"
  if base == "skos:hiddenLabel":
    return "hidden"
  return None
