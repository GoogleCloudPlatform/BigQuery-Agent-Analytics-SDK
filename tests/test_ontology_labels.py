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

"""Tests for SDK label wiring across the three ontology_* modules.

Grouped by file so each module's feature-label assignment is asserted
independently:

- ontology_graph.py  -> feature="ontology-build" (extraction pipeline)
- ontology_materializer.py  -> feature="ontology-build" (tables + loads)
- ontology_property_graph.py -> feature="ontology-gql" (graph DDL)

The AI.GENERATE extraction path in ontology_graph.py is the only site
that also carries sdk_ai_function="ai-generate". Every class in the
bundle accepts an injected client and implements the warn-once pattern
from Phase 1 / 2a / 2c / 2d / 2e.
"""

import logging
from unittest.mock import MagicMock

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
import pytest

from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
from bigquery_agent_analytics.ontology_materializer import OntologyMaterializer
from bigquery_agent_analytics.ontology_property_graph import OntologyPropertyGraphCompiler
from bigquery_agent_analytics.resolved_spec import ResolvedEntity
from bigquery_agent_analytics.resolved_spec import ResolvedGraph
from bigquery_agent_analytics.resolved_spec import ResolvedProperty


def _mock_bq_client():
  client = MagicMock()
  job = MagicMock()
  job.result.return_value = []
  client.query.return_value = job
  load_job = MagicMock()
  load_job.result.return_value = None
  client.load_table_from_json.return_value = load_job
  client.get_table.return_value = MagicMock(schema=[])
  client.insert_rows_json.return_value = []
  return client


def _simple_spec() -> ResolvedGraph:
  alpha = ResolvedEntity(
      name="Alpha",
      source="p.d.alpha_table",
      key_columns=("alpha_id",),
      properties=(
          ResolvedProperty(
              column="alpha_id", logical_name="alpha_id", sdk_type="string"
          ),
      ),
      labels=("Alpha",),
  )
  return ResolvedGraph(
      name="test_ontology",
      entities=(alpha,),
      relationships=(),
  )


def _labels_per_call(mock_bq, method="query"):
  out = []
  for call in getattr(mock_bq, method).call_args_list:
    args, kwargs = call
    sql = args[0] if args else kwargs.get("query", "")
    cfg = kwargs.get("job_config")
    labels = dict(cfg.labels) if cfg and cfg.labels else {}
    out.append((sql, labels))
  return out


# ------------------------------------------------------------------ #
# ontology_graph.py                                                     #
# ------------------------------------------------------------------ #


class TestOntologyGraphLabels:

  def test_fetch_raw_events_labels_ontology_build(self):
    mock_bq = _mock_bq_client()
    mgr = OntologyGraphManager(
        project_id="p", dataset_id="d", spec=_simple_spec(), bq_client=mock_bq
    )
    mgr._fetch_raw_events(["sess-1"])
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "ontology-build"
    assert "sdk_ai_function" not in labels

  def test_ai_generate_extraction_labels_ai_generate(self):
    mock_bq = _mock_bq_client()
    mgr = OntologyGraphManager(
        project_id="p", dataset_id="d", spec=_simple_spec(), bq_client=mock_bq
    )
    mgr._extract_via_ai_generate(["sess-1"])
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "ontology-build"
    assert labels.get("sdk_ai_function") == "ai-generate"

  def test_extract_payloads_fallback_labels_ontology_build(self):
    mock_bq = _mock_bq_client()
    mgr = OntologyGraphManager(
        project_id="p", dataset_id="d", spec=_simple_spec(), bq_client=mock_bq
    )
    mgr._extract_payloads(["sess-1"])
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "ontology-build"
    assert "sdk_ai_function" not in labels


# ------------------------------------------------------------------ #
# ontology_materializer.py                                              #
# ------------------------------------------------------------------ #


class TestOntologyMaterializerLabels:

  def test_create_tables_labels_each_ddl(self):
    mock_bq = _mock_bq_client()
    mat = OntologyMaterializer(
        project_id="p", dataset_id="d", spec=_simple_spec(), bq_client=mock_bq
    )
    mat.create_tables()
    calls = _labels_per_call(mock_bq)
    assert calls, "expected at least one CREATE TABLE dispatch"
    for _sql, labels in calls:
      assert labels.get("sdk_feature") == "ontology-build"
      assert "sdk_ai_function" not in labels

  def test_delete_for_sessions_labeled(self):
    mock_bq = _mock_bq_client()
    mat = OntologyMaterializer(
        project_id="p", dataset_id="d", spec=_simple_spec(), bq_client=mock_bq
    )
    mat._delete_for_sessions("p.d.alpha_table", ["sess-1"])
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "ontology-build"
    assert "sdk_ai_function" not in labels

  def test_load_table_from_json_labeled(self):
    """Load jobs support labels via LoadJobConfig — assert the
    batch-load path stamps feature=ontology-build on the LoadJobConfig."""
    mock_bq = _mock_bq_client()
    mat = OntologyMaterializer(
        project_id="p",
        dataset_id="d",
        spec=_simple_spec(),
        bq_client=mock_bq,
        write_mode="batch_load",
    )
    mat._batch_load_table(
        table_ref="p.d.alpha_table",
        rows=[{"alpha_id": "a1"}],
        session_ids=["sess-1"],
    )
    # load_table_from_json call_args has the load job's config in kwargs
    cfg = mock_bq.load_table_from_json.call_args.kwargs.get("job_config")
    assert cfg is not None
    assert isinstance(cfg, bigquery.LoadJobConfig)
    labels = dict(cfg.labels or {})
    assert labels.get("sdk_feature") == "ontology-build"

  def test_insert_from_staging_labeled(self):
    """The INSERT INTO ... SELECT that follows the staging load also
    gets a labeled QueryJobConfig."""
    mock_bq = _mock_bq_client()
    mat = OntologyMaterializer(
        project_id="p",
        dataset_id="d",
        spec=_simple_spec(),
        bq_client=mock_bq,
        write_mode="batch_load",
    )
    mat._batch_load_table(
        table_ref="p.d.alpha_table",
        rows=[{"alpha_id": "a1"}],
        session_ids=["sess-1"],
    )
    # Post-load: the DELETE runs (via _delete_for_sessions) and then
    # the INSERT INTO ... SELECT. Both should carry ontology-build.
    for _sql, labels in _labels_per_call(mock_bq):
      assert labels.get("sdk_feature") == "ontology-build"


# ------------------------------------------------------------------ #
# ontology_property_graph.py                                            #
# ------------------------------------------------------------------ #


class TestOntologyPropertyGraphLabels:

  def test_create_property_graph_labels_ontology_gql(self):
    mock_bq = _mock_bq_client()
    compiler = OntologyPropertyGraphCompiler(
        project_id="p", dataset_id="d", spec=_simple_spec(), bq_client=mock_bq
    )
    compiler.create_property_graph()
    _sql, labels = _labels_per_call(mock_bq)[0]
    # Property Graph DDL gets its own narrower feature tag because GQL
    # usage is a distinct aggregation dimension from the extraction
    # and materialization work that precedes it.
    assert labels.get("sdk_feature") == "ontology-gql"
    assert "sdk_ai_function" not in labels


# ------------------------------------------------------------------ #
# Warn-once pattern across all three classes                           #
# ------------------------------------------------------------------ #


class TestVanillaClientWarnOnce:

  @pytest.mark.parametrize(
      "factory",
      [
          lambda vanilla: OntologyGraphManager(
              project_id="p",
              dataset_id="d",
              spec=_simple_spec(),
              bq_client=vanilla,
          ),
          lambda vanilla: OntologyMaterializer(
              project_id="p",
              dataset_id="d",
              spec=_simple_spec(),
              bq_client=vanilla,
          ),
          lambda vanilla: OntologyPropertyGraphCompiler(
              project_id="p",
              dataset_id="d",
              spec=_simple_spec(),
              bq_client=vanilla,
          ),
      ],
      ids=[
          "OntologyGraphManager",
          "OntologyMaterializer",
          "OntologyPropertyGraphCompiler",
      ],
  )
  def test_vanilla_client_emits_one_warning(self, caplog, factory):
    vanilla = bigquery.Client(project="p", credentials=AnonymousCredentials())
    obj = factory(vanilla)

    with caplog.at_level(logging.WARNING):
      _ = obj.bq_client
      _ = obj.bq_client
      _ = obj.bq_client

    warnings = [
        r
        for r in caplog.records
        if "SDK telemetry labels will not be applied" in r.message
    ]
    assert len(warnings) == 1
