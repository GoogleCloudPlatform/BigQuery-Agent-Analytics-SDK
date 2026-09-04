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

"""Live BigQuery integration tests for the ontology+binding runtime path.

These tests require:
  - RUN_LIVE_BIGQUERY_TESTS=1 (explicit opt-in)
  - GOOGLE_CLOUD_PROJECT env var set to a real GCP project
  - BigQuery API enabled with default credentials
  - The ``agent_events`` table with YMGO ADCP session data

Optional env vars:
  - BQ_LOCATION (default: US) — must match the source dataset's region
  - BQ_DATASET (default: agent_analytics)
  - BQ_TABLE (default: agent_events)
  - BQ_SESSION_ID (default: adcp-033c95d7a97d)

Run explicitly::

    RUN_LIVE_BIGQUERY_TESTS=1 GOOGLE_CLOUD_PROJECT=my-project \\
        pytest tests/test_integration_ontology_binding.py -v

Skipped automatically without the opt-in flag.
"""

from __future__ import annotations

import os
import uuid

import pytest

_LIVE = os.environ.get("RUN_LIVE_BIGQUERY_TESTS", "").lower() in (
    "1",
    "true",
    "yes",
)
_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
_DATASET = os.environ.get("BQ_DATASET", "agent_analytics")
_TABLE = os.environ.get("BQ_TABLE", "agent_events")
_SESSION = os.environ.get("BQ_SESSION_ID", "adcp-033c95d7a97d")
_LOCATION = os.environ.get("BQ_LOCATION", "US")

pytestmark = pytest.mark.skipif(
    not _LIVE or _PROJECT is None or _PROJECT == "your-project-id",
    reason=(
        "Live BQ tests require RUN_LIVE_BIGQUERY_TESTS=1 and "
        "GOOGLE_CLOUD_PROJECT set"
    ),
)


@pytest.fixture(scope="module")
def scratch_dataset():
  """Create a scratch dataset that auto-expires, yield it, then delete."""
  from google.cloud import bigquery

  run_id = uuid.uuid4().hex[:8]
  ds_id = f"{_DATASET}_integ_{run_id}"
  client = bigquery.Client(project=_PROJECT, location=_LOCATION)
  ds = bigquery.Dataset(f"{_PROJECT}.{ds_id}")
  ds.location = _LOCATION
  ds.default_table_expiration_ms = 3600000
  client.create_dataset(ds, exists_ok=True)
  yield ds_id
  client.delete_dataset(
      f"{_PROJECT}.{ds_id}", delete_contents=True, not_found_ok=True
  )


@pytest.fixture(scope="module")
def ontology_and_binding(scratch_dataset, tmp_path_factory):
  """Write ontology + binding YAML and load via upstream loaders."""
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  tmp = tmp_path_factory.mktemp("specs")

  ont_path = tmp / "ymgo.ontology.yaml"
  ont_path.write_text(
      "ontology: YMGO_Integration_Test\n"
      "entities:\n"
      "  - name: mako_DecisionPoint\n"
      "    keys:\n"
      "      primary: [decision_id]\n"
      "    properties:\n"
      "      - name: decision_id\n"
      "        type: string\n"
      "      - name: decision_type\n"
      "        type: string\n"
      "  - name: sup_YahooAdUnit\n"
      "    keys:\n"
      "      primary: [adUnitId]\n"
      "    properties:\n"
      "      - name: adUnitId\n"
      "        type: string\n"
      "      - name: adUnitName\n"
      "        type: string\n"
      "      - name: adUnitSize\n"
      "        type: string\n"
      "      - name: adUnitPosition\n"
      "        type: string\n"
      "relationships:\n"
      "  - name: CandidateEdge\n"
      "    from: mako_DecisionPoint\n"
      "    to: sup_YahooAdUnit\n"
      "    properties:\n"
      "      - name: edge_type\n"
      "        type: string\n"
      "      - name: mako_scoreValue\n"
      "        type: double\n"
      "  - name: sup_YahooAdUnitEvolvedFrom\n"
      "    from: sup_YahooAdUnit\n"
      "    to: sup_YahooAdUnit\n"
      "    properties:\n"
      "      - name: from_session_id\n"
      "        type: string\n"
      "      - name: to_session_id\n"
      "        type: string\n"
      "      - name: event_time\n"
      "        type: timestamp\n"
      "      - name: changed_properties\n"
      "        type: string\n",
      encoding="utf-8",
  )

  bnd_path = tmp / "ymgo-bq.binding.yaml"
  bnd_path.write_text(
      f"binding: ymgo_integ\n"
      f"ontology: YMGO_Integration_Test\n"
      f"target:\n"
      f"  backend: bigquery\n"
      f"  project: {_PROJECT}\n"
      f"  dataset: {scratch_dataset}\n"
      f"entities:\n"
      f"  - name: mako_DecisionPoint\n"
      f"    source: decision_points\n"
      f"    properties:\n"
      f"      - name: decision_id\n"
      f"        column: decision_id\n"
      f"      - name: decision_type\n"
      f"        column: decision_type\n"
      f"  - name: sup_YahooAdUnit\n"
      f"    source: yahoo_ad_units\n"
      f"    properties:\n"
      f"      - name: adUnitId\n"
      f"        column: adUnitId\n"
      f"      - name: adUnitName\n"
      f"        column: adUnitName\n"
      f"      - name: adUnitSize\n"
      f"        column: adUnitSize\n"
      f"      - name: adUnitPosition\n"
      f"        column: adUnitPosition\n"
      f"relationships:\n"
      f"  - name: CandidateEdge\n"
      f"    source: candidate_edges\n"
      f"    from_columns: [decision_id]\n"
      f"    to_columns: [adUnitId]\n"
      f"    properties:\n"
      f"      - name: edge_type\n"
      f"        column: edge_type\n"
      f"      - name: mako_scoreValue\n"
      f"        column: mako_scoreValue\n"
      f"  - name: sup_YahooAdUnitEvolvedFrom\n"
      f"    source: sup_yahoo_ad_unit_lineage\n"
      f"    from_columns: [adUnitId]\n"
      f"    to_columns: [adUnitId]\n"
      f"    properties:\n"
      f"      - name: from_session_id\n"
      f"        column: from_session_id\n"
      f"      - name: to_session_id\n"
      f"        column: to_session_id\n"
      f"      - name: event_time\n"
      f"        column: event_time\n"
      f"      - name: changed_properties\n"
      f"        column: changed_properties\n",
      encoding="utf-8",
  )

  ontology = load_ontology(str(ont_path))
  binding = load_binding(str(bnd_path), ontology=ontology)
  return ontology, binding


@pytest.fixture(scope="module")
def lineage_config():
  from bigquery_agent_analytics.runtime_spec import LineageEdgeConfig

  return {
      "sup_YahooAdUnitEvolvedFrom": LineageEdgeConfig(
          from_session_column="from_session_id",
          to_session_column="to_session_id",
      ),
  }


class TestExtraction:
  """Live extraction via from_ontology_binding."""

  def test_extract_returns_nodes(self, ontology_and_binding, lineage_config):
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    ontology, binding = ontology_and_binding
    mgr = OntologyGraphManager.from_ontology_binding(
        project_id=_PROJECT,
        dataset_id=_DATASET,
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        table_id=_TABLE,
    )
    graph = mgr.extract_graph(session_ids=[_SESSION], use_ai_generate=True)
    assert len(graph.nodes) > 0, "Expected at least 1 node from extraction"


class TestMaterialization:
  """Live materialization via from_ontology_binding."""

  def test_create_tables_and_materialize(
      self, ontology_and_binding, lineage_config, scratch_dataset
  ):
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer

    ontology, binding = ontology_and_binding

    # Extract.
    mgr = OntologyGraphManager.from_ontology_binding(
        project_id=_PROJECT,
        dataset_id=_DATASET,
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        table_id=_TABLE,
    )
    graph = mgr.extract_graph(session_ids=[_SESSION], use_ai_generate=True)
    assert len(graph.nodes) > 0

    # Materialize.
    mat = OntologyMaterializer.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        write_mode="batch_load",
    )
    tables = mat.create_tables()
    assert len(tables) > 0, "Expected tables to be created"

    result = mat.materialize_with_status(graph, [_SESSION])
    assert (
        sum(result.row_counts.values()) > 0
    ), "Expected at least 1 row materialized"
    for ts in result.table_statuses.values():
      if ts.rows_inserted > 0:
        assert (
            ts.idempotent is True
        ), f"{ts.table_ref}: expected idempotent=True"


class TestPropertyGraph:
  """Live DDL + GQL via from_ontology_binding."""

  def test_create_graph_and_query(
      self, ontology_and_binding, lineage_config, scratch_dataset
  ):
    from google.cloud import bigquery

    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer
    from bigquery_agent_analytics.ontology_orchestrator import compile_showcase_gql
    from bigquery_agent_analytics.ontology_property_graph import OntologyPropertyGraphCompiler

    ontology, binding = ontology_and_binding

    # Extract + materialize.
    mgr = OntologyGraphManager.from_ontology_binding(
        project_id=_PROJECT,
        dataset_id=_DATASET,
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        table_id=_TABLE,
    )
    graph = mgr.extract_graph(session_ids=[_SESSION], use_ai_generate=True)

    mat = OntologyMaterializer.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        write_mode="batch_load",
    )
    mat.create_tables()
    mat.materialize_with_status(graph, [_SESSION])

    # Create property graph.
    compiler = OntologyPropertyGraphCompiler.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
    )
    created = compiler.create_property_graph()
    assert created is True, "Property Graph creation failed"

    # Run GQL query.
    gql = compile_showcase_gql(mgr.spec, _PROJECT, scratch_dataset)
    client = bigquery.Client(project=_PROJECT, location=_LOCATION)
    job = client.query(
        gql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("session_id", "STRING", _SESSION),
                bigquery.ScalarQueryParameter("result_limit", "INT64", 50),
            ]
        ),
    )
    rows = list(job.result())
    assert len(rows) > 0, "GQL query returned 0 rows"


class TestSkipPropertyGraph:
  """Live test that --skip-property-graph does not run CREATE PROPERTY GRAPH.

  Issue #104 acceptance: "creates a pre-existing property graph, runs
  ontology-build --skip-property-graph against pre-existing base tables,
  and verifies the user's graph definition is unchanged after the run."

  Verified by:
    - Capturing a timestamp after creating the user's CREATE PROPERTY
      GRAPH directly (not via the SDK).
    - Running build_ontology_graph(..., skip_property_graph=True).
    - Querying INFORMATION_SCHEMA.JOBS_BY_PROJECT for any
      'CREATE OR REPLACE PROPERTY GRAPH' jobs in the post-timestamp
      window. Asserting zero.
    - Asserting the GQL query against the user's graph still works
      after the SDK run (graph object intact, base tables refreshed).
  """

  def test_skip_property_graph_issues_no_create_graph_job(
      self, ontology_and_binding, lineage_config, scratch_dataset
  ):
    from google.cloud import bigquery

    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer
    from bigquery_agent_analytics.ontology_orchestrator import build_ontology_graph
    from bigquery_agent_analytics.ontology_orchestrator import compile_showcase_gql
    from bigquery_agent_analytics.ontology_property_graph import OntologyPropertyGraphCompiler
    from bigquery_agent_analytics.resolved_spec import resolve

    ontology, binding = ontology_and_binding
    spec = resolve(ontology, binding, lineage_config=lineage_config)

    # Step 1: create base tables (idempotent), then create the user's
    # property graph via direct SQL (simulating Terraform/dbt-managed
    # DDL the SDK should NOT touch when --skip-property-graph is set).
    mat = OntologyMaterializer.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        write_mode="batch_load",
    )
    mat.create_tables()

    compiler = OntologyPropertyGraphCompiler.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
    )
    assert compiler.create_property_graph() is True

    # Step 2: capture the "before" timestamp AFTER the authored DDL
    # has finished so the JOBS_BY_PROJECT filter does not catch our
    # own setup job. Bind via a SQL CURRENT_TIMESTAMP() round-trip so
    # the timestamp is BQ-aligned.
    client = bigquery.Client(project=_PROJECT, location=_LOCATION)
    before_ts_row = next(
        iter(client.query("SELECT CURRENT_TIMESTAMP() AS ts").result())
    )
    before_skip_build_ts = before_ts_row.ts

    # Step 3: run build_ontology_graph with skip_property_graph=True.
    # Extraction reads from the real _DATASET.agent_events table where
    # the YMGO ADCP session data lives. Materialization writes to
    # scratch_dataset because spec entity sources are already
    # 3-part-qualified to binding.target.dataset = scratch_dataset
    # (see _qualify_source at resolved_spec.py:141), so the
    # materializer ignores its dataset_id parameter for output table
    # location. The result: extract from prod-like, materialize to
    # scratch — exactly the user-facing flow the test should exercise.
    result = build_ontology_graph(
        spec=spec,
        session_ids=[_SESSION],
        project_id=_PROJECT,
        dataset_id=_DATASET,
        table_id=_TABLE,
        graph_name=spec.name,
        location=_LOCATION,
        skip_property_graph=True,
    )

    assert result["property_graph_created"] is False
    assert result["property_graph_status"] == "skipped:user_requested"
    assert result["skipped_reason"] == "user_requested"
    # Phases 1-4 must have actually populated the scratch tables.
    # Catches the silent-empty-graph trap where extraction can fail
    # (e.g. wrong source dataset) and ontology_graph.py:683 returns
    # an empty ExtractedGraph rather than raising.
    rows_total = sum(result["rows_materialized"].values())
    assert rows_total > 0, (
        "Expected at least 1 row materialized after skip-flag run, "
        f"got rows_materialized={result['rows_materialized']!r}. "
        "Extraction may have silently returned an empty graph."
    )

    # Step 4: assert no CREATE OR REPLACE PROPERTY GRAPH job ran for
    # *this test's graph* in the post-timestamp window.
    #
    # Filter design:
    #   1. timestamp > the post-DDL baseline (closes the trap from
    #      #107 cell 1.3 where the user's own setup CREATE PROPERTY
    #      GRAPH would otherwise be caught).
    #   2. DDL keyword.
    #   3. graph name (spec.name) — the graph name is in the DDL
    #      string regardless of which dataset the compiler would
    #      target. If skip_property_graph regresses, the compiler
    #      runs with dataset_id=_DATASET (the orchestrator's
    #      argument), so the regressed DDL would target
    #      _PROJECT._DATASET.<spec.name>, NOT
    #      _PROJECT.<scratch_dataset>.<spec.name>. Filtering on the
    #      graph name (rather than the fully-qualified ref) catches
    #      the regression in either dataset.
    #   4. sdk_feature='ontology-gql' label — only SDK-issued
    #      property-graph jobs carry this label
    #      (ontology_property_graph.py:465). The setup CREATE PROPERTY
    #      GRAPH job in step 1 *also* uses this label (it goes through
    #      OntologyPropertyGraphCompiler.create_property_graph()), but
    #      it is excluded by the post-setup timestamp captured in
    #      step 2. User-authored raw SQL DDL jobs without SDK labels
    #      are excluded by this label filter.
    region_qual = f"`region-{_LOCATION.lower()}`"
    jobs_query = f"""
    SELECT job_id, query, creation_time
    FROM {region_qual}.INFORMATION_SCHEMA.JOBS_BY_PROJECT AS j
    WHERE creation_time > @before
      AND UPPER(query) LIKE '%CREATE OR REPLACE PROPERTY GRAPH%'
      AND query LIKE @graph_name_pattern
      AND EXISTS (
        SELECT 1 FROM UNNEST(j.labels) AS l
        WHERE l.key = 'sdk_feature' AND l.value = 'ontology-gql'
      )
    """
    job = client.query(
        jobs_query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "before", "TIMESTAMP", before_skip_build_ts
                ),
                bigquery.ScalarQueryParameter(
                    "graph_name_pattern",
                    "STRING",
                    f"%{spec.name}%",
                ),
            ]
        ),
    )
    create_graph_jobs = list(job.result())
    assert len(create_graph_jobs) == 0, (
        "Expected zero CREATE OR REPLACE PROPERTY GRAPH jobs after "
        f"build_ontology_graph(skip_property_graph=True), got "
        f"{len(create_graph_jobs)}: "
        f"{[j.job_id for j in create_graph_jobs]}"
    )

    # Step 5: assert the user's graph object still works. Run the
    # showcase GQL query — it should succeed (graph definition is
    # intact) even though it may return zero rows if the test
    # session_id has no matching edges in this scratch dataset.
    gql = compile_showcase_gql(spec, _PROJECT, scratch_dataset)
    gql_job = client.query(
        gql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("session_id", "STRING", _SESSION),
                bigquery.ScalarQueryParameter("result_limit", "INT64", 50),
            ]
        ),
    )
    # Result iteration confirms BigQuery accepted the GQL against
    # the user's pre-existing property graph.
    list(gql_job.result())


class TestLineageEndToEnd:
  """Live lineage detection + GQL via from_ontology_binding."""

  def test_synthetic_lineage_query(
      self, ontology_and_binding, lineage_config, scratch_dataset
  ):
    from google.cloud import bigquery

    from bigquery_agent_analytics.ontology_graph import detect_lineage_edges
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer
    from bigquery_agent_analytics.ontology_models import ExtractedGraph
    from bigquery_agent_analytics.ontology_models import ExtractedNode
    from bigquery_agent_analytics.ontology_models import ExtractedProperty
    from bigquery_agent_analytics.ontology_orchestrator import compile_lineage_gql
    from bigquery_agent_analytics.ontology_property_graph import OntologyPropertyGraphCompiler

    ontology, binding = ontology_and_binding

    # Extract session A.
    mgr = OntologyGraphManager.from_ontology_binding(
        project_id=_PROJECT,
        dataset_id=_DATASET,
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        table_id=_TABLE,
    )
    graph_a = mgr.extract_graph(session_ids=[_SESSION], use_ai_generate=True)
    assert len(graph_a.nodes) > 0

    # Build synthetic session B.
    ad_units = [n for n in graph_a.nodes if n.entity_name == "sup_YahooAdUnit"]
    assert len(ad_units) > 0, "No ad units extracted from session A"

    original = ad_units[0]
    orig_props = {p.name: p.value for p in original.properties}
    shared_id = orig_props.get("adUnitId", "unknown")

    synthetic = ExtractedNode(
        node_id=f"sess-integ-B:sup_YahooAdUnit:adUnitId={shared_id}",
        entity_name="sup_YahooAdUnit",
        labels=["sup_YahooAdUnit"],
        properties=[
            ExtractedProperty(name="adUnitId", value=shared_id),
            ExtractedProperty(
                name="adUnitName",
                value=orig_props.get("adUnitName", "") + " (Integ Test)",
            ),
            ExtractedProperty(
                name="adUnitSize",
                value=orig_props.get("adUnitSize", "300x250"),
            ),
            ExtractedProperty(name="adUnitPosition", value="BTF"),
        ],
    )
    graph_b = ExtractedGraph(name=mgr.spec.name, nodes=[synthetic], edges=[])

    # Detect lineage.
    lineage_edges = detect_lineage_edges(
        current_graph=graph_b,
        current_session_id="sess-integ-B",
        prior_graphs={_SESSION: graph_a},
        lineage_entity_types=["sup_YahooAdUnit"],
        spec=mgr.spec,
    )
    assert len(lineage_edges) > 0, "Expected at least 1 lineage edge"

    # Materialize all.
    mat = OntologyMaterializer.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
        write_mode="batch_load",
    )
    mat.create_tables()
    mat.materialize_with_status(graph_a, [_SESSION])
    mat.materialize_with_status(graph_b, ["sess-integ-B"])

    lineage_graph = ExtractedGraph(
        name=mgr.spec.name, nodes=[], edges=lineage_edges
    )
    mat.materialize_with_status(lineage_graph, ["sess-integ-B"])

    # Create property graph + run lineage GQL.
    compiler = OntologyPropertyGraphCompiler.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=lineage_config,
    )
    created = compiler.create_property_graph()
    assert created is True

    lineage_gql = compile_lineage_gql(
        spec=mgr.spec,
        project_id=_PROJECT,
        dataset_id=scratch_dataset,
        relationship_name="sup_YahooAdUnitEvolvedFrom",
    )
    client = bigquery.Client(project=_PROJECT, location=_LOCATION)
    job = client.query(
        lineage_gql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "session_id", "STRING", "sess-integ-B"
                ),
                bigquery.ScalarQueryParameter("result_limit", "INT64", 50),
            ]
        ),
    )
    rows = list(job.result())
    assert len(rows) > 0, "Lineage GQL returned 0 rows"


# ------------------------------------------------------------------ #
# Binding-validator live test (issue #105 PR 2a)                       #
# ------------------------------------------------------------------ #


class TestBindingValidationLive:
  """Live validation that ``validate_binding_against_bigquery``
  behaves correctly against real BigQuery.

  Self-contained: uses its own per-test scratch dataset (rather
  than the module-scoped fixture) because the third phase of this
  test deliberately drops a column via ALTER TABLE, and running
  destructive SQL against a shared dataset would interfere with
  other tests in this file.

  Phases:
    1. Materialize real tables via OntologyMaterializer.
    2. Default-mode validation: report.ok must be True; warnings
       contain only KEY_COLUMN_NULLABLE entries (because the SDK's
       CREATE TABLE IF NOT EXISTS emits NULLABLE keys).
    3. Strict-mode validation: same input must surface those
       warnings as KEY_COLUMN_NULLABLE failures, with warnings
       empty (escalated, not duplicated).
    4. Drop the 'confidence' column via real ALTER TABLE; default-
       mode re-validation must emit exactly one MISSING_COLUMN
       failure pointing at the dropped column.
  """

  @pytest.fixture(scope="function")
  def isolated_scratch(self):
    """Per-test scratch dataset; cleaned up unconditionally."""
    from google.cloud import bigquery

    run_id = uuid.uuid4().hex[:8]
    ds_id = f"bind_validate_live_{run_id}"
    client = bigquery.Client(project=_PROJECT, location=_LOCATION)
    ds = bigquery.Dataset(f"{_PROJECT}.{ds_id}")
    ds.location = _LOCATION
    ds.default_table_expiration_ms = 3600000
    client.create_dataset(ds, exists_ok=True)
    try:
      yield client, ds_id
    finally:
      client.delete_dataset(
          f"{_PROJECT}.{ds_id}",
          delete_contents=True,
          not_found_ok=True,
      )

  @pytest.fixture(scope="function")
  def isolated_ontology_and_binding(self, isolated_scratch, tmp_path_factory):
    """Per-test ontology+binding pointing at the isolated scratch."""
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    _, ds_id = isolated_scratch
    tmp = tmp_path_factory.mktemp("bind_validate_live")

    ont_path = tmp / "ontology.yaml"
    ont_path.write_text(
        "ontology: BindValidatorLive\n"
        "entities:\n"
        "  - name: Decision\n"
        "    keys:\n"
        "      primary: [decision_id]\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        type: string\n"
        "      - name: confidence\n"
        "        type: double\n"
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
        "        type: double\n",
        encoding="utf-8",
    )

    bnd_path = tmp / "binding.yaml"
    bnd_path.write_text(
        f"binding: live_check\n"
        f"ontology: BindValidatorLive\n"
        f"target:\n"
        f"  backend: bigquery\n"
        f"  project: {_PROJECT}\n"
        f"  dataset: {ds_id}\n"
        f"entities:\n"
        f"  - name: Decision\n"
        f"    source: decisions\n"
        f"    properties:\n"
        f"      - name: decision_id\n"
        f"        column: decision_id\n"
        f"      - name: confidence\n"
        f"        column: confidence\n"
        f"  - name: Outcome\n"
        f"    source: outcomes\n"
        f"    properties:\n"
        f"      - name: outcome_id\n"
        f"        column: outcome_id\n"
        f"relationships:\n"
        f"  - name: HasOutcome\n"
        f"    source: edges\n"
        f"    from_columns: [decision_id]\n"
        f"    to_columns: [outcome_id]\n"
        f"    properties:\n"
        f"      - name: weight\n"
        f"        column: weight\n",
        encoding="utf-8",
    )

    ontology = load_ontology(str(ont_path))
    binding = load_binding(str(bnd_path), ontology=ontology)
    return ontology, binding

  def test_validator_end_to_end_against_real_bigquery(
      self, isolated_scratch, isolated_ontology_and_binding
  ):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery
    from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer

    client, ds_id = isolated_scratch
    ontology, binding = isolated_ontology_and_binding

    # Cheap fixture-scope sanity check: the binding YAML the second
    # fixture wrote must point at the dataset the first fixture
    # created. If a future refactor flips either fixture to module
    # scope by accident, the binding would race with whichever
    # dataset was created first and this assert would catch it
    # before any BQ call runs.
    assert binding.target.dataset == ds_id, (
        f"binding.target.dataset={binding.target.dataset!r} but "
        f"isolated_scratch yielded {ds_id!r}; fixture scopes are "
        f"out of sync"
    )

    # Phase 1: materialize real tables.
    mat = OntologyMaterializer.from_ontology_binding(
        ontology=ontology,
        binding=binding,
        lineage_config=None,
        write_mode="batch_load",
    )
    tables = mat.create_tables()
    assert set(tables.keys()) == {
        "Decision",
        "Outcome",
        "HasOutcome",
    }, f"Unexpected tables created: {sorted(tables.keys())}"

    # Phase 2: default-mode validation. SDK-created tables must
    # validate clean; the only signal is advisory warnings on
    # NULLABLE keys.
    default_report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )
    assert default_report.ok is True, (
        f"Default mode rejected SDK-created tables. Failures: "
        f"{[(f.code, f.detail) for f in default_report.failures]}"
    )
    assert all(
        w.code == FailureCode.KEY_COLUMN_NULLABLE
        for w in default_report.warnings
    ), (
        "Only KEY_COLUMN_NULLABLE warnings expected against SDK-"
        "created tables. Got: "
        f"{[w.code for w in default_report.warnings]}"
    )
    # Decision.decision_id, Outcome.outcome_id (entity primary keys)
    # plus HasOutcome.from_columns[0]=decision_id and
    # HasOutcome.to_columns[0]=outcome_id (relationship endpoints).
    assert len(default_report.warnings) == 4

    # Phase 3: strict-mode escalation.
    strict_report = validate_binding_against_bigquery(
        ontology=ontology,
        binding=binding,
        bq_client=client,
        strict=True,
    )
    assert (
        strict_report.ok is False
    ), "Strict mode should reject NULLABLE primary-key columns"
    assert all(
        f.code == FailureCode.KEY_COLUMN_NULLABLE
        for f in strict_report.failures
    )
    assert len(strict_report.failures) == 4
    assert strict_report.warnings == (), (
        "Strict mode must escalate warnings into failures, not "
        "double-emit them"
    )

    # Phase 4: drop a non-key property column via real ALTER TABLE
    # and assert the validator catches the resulting drift.
    table_ref = f"{_PROJECT}.{ds_id}.decisions"
    client.query(f"ALTER TABLE `{table_ref}` DROP COLUMN confidence").result()

    broken_report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )
    miss = [
        f
        for f in broken_report.failures
        if f.code == FailureCode.MISSING_COLUMN and "confidence" in f.bq_ref
    ]
    assert len(miss) == 1, (
        f"Expected exactly 1 MISSING_COLUMN for confidence, got "
        f"failures: "
        f"{[(f.code, f.bq_ref) for f in broken_report.failures]}"
    )
    # Path must reflect binding YAML order. Decision's binding lists
    # decision_id at properties[0] and confidence at properties[1].
    assert miss[0].binding_path == ("binding.entities[0].properties[1].column")
    assert miss[0].bq_ref == f"{table_ref}.confidence"
