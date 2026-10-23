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

"""The graph managers keep every name and label inside its SQL token.

``ContextGraphManager`` formats its events ``table_id``, the six table
names and the graph name in ``ContextGraphConfig`` (or a method's
``graph_name`` override) into backtick-quoted paths, ``max_hops`` into
GQL path quantifiers, and the config's ``entity_types`` into a quoted
AI.GENERATE prompt. ``OntologyGraphManager`` formats its ``table_id`` the
same way. A backtick, quote or backslash in any of them ended its token
early, so the rest became SQL: a ``CROSS JOIN`` on another dataset's
table, or a scalar subquery inside the prompt. The managers now check
their project, dataset and events table when built and when assigned
(the events table may be a wildcard read, but not in property-graph DDL),
the config checks its table and graph names, method overrides are
checked where they are used, ``max_hops`` must be a whole number, and the
entity types are escaped.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.context_graph import ContextGraphConfig
from bigquery_agent_analytics.context_graph import ContextGraphManager
from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
from tests.test_client_project_dataset_ids import _NOT_A_DATASET_ID
from tests.test_client_project_dataset_ids import _NOT_A_PROJECT_ID
from tests.test_client_table_id import _NOT_A_TABLE_NAME
from tests.test_client_table_id import _NOT_A_TABLE_SOURCE
from tests.test_client_table_id import _WILDCARDS
from tests.test_drift_production_table import _TABLE_NAMES
from tests.test_judge_sql_literals import _read_quoted
from tests.test_ontology_graph import _simple_spec

# The review's payloads.
_GRAPH_TABLE_PAYLOAD = (
    "agent_events` AS base CROSS JOIN `p.private.secrets` AS injected, --"
)
_ONTOLOGY_TABLE_PAYLOAD = (
    "agent_events` AS base CROSS JOIN `p.private.secrets` AS injected --"
)
_SUBQUERY_ENTITY = (
    "Product', (SELECT STRING_AGG(secret) FROM `p.private.secrets`), 'Budget"
)

# Entity-type lists whose joined text left its literal, or changed it.
_ENTITY_TYPES = {
    "subquery": [_SUBQUERY_ENTITY],
    "apostrophe": ["Children's Products"],
    "trailing_backslash": ["Budget\\"],
    "line_break": ["Budget',\n(SELECT 1), '"],
    "carriage_return": ["Budget" + chr(13)],
    "tab": ["Bud" + chr(9) + "get"],
    "nul": ["Budget" + chr(0)],
    "delete": ["Budget" + chr(0x7F)],
    "several": ["Product", "Budget's", "Audience\\"],
}

# String values that are not one table name, wildcards included. (The
# config's fields are strings, so pydantic refuses other types itself.)
_NOT_A_STRICT_NAME = {
    name: value
    for name, value in _NOT_A_TABLE_NAME.items()
    if isinstance(value, str)
}
# Graph-name overrides fall back to the config's name when empty.
_NOT_A_GRAPH_OVERRIDE = {
    name: value for name, value in _NOT_A_STRICT_NAME.items() if value
}

_CONFIG_NAMES = [
    "biz_nodes_table",
    "cross_links_table",
    "decision_points_table",
    "candidates_table",
    "made_decision_edges_table",
    "candidate_edges_table",
    "graph_name",
]

_TABLE_ID_ERROR = "^table_id must be one table name"
_GRAPH_NAME_ERROR = "^graph_name must be one table name"
_MAX_HOPS_ERROR = "^max_hops must be"

# What follows the entity-types literal in the extraction prompt.
_THEN_RETURN = ",\n            'Return a JSON array of objects"
_DEFAULT_ENTITY_TYPES = (
    "Entity types: Product, Targeting, Campaign, Budget, Audience,"
    " Creative, Placement. "
)


def _bq_client():
  """Fake BigQuery client whose every query returns no rows."""
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  return bq


def _sent_sql(bq):
  return [call.args[0] for call in bq.query.call_args_list]


def _client(bq):
  return Client(
      project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
  )


def _context_graph(bq=None, **options):
  return ContextGraphManager(
      project_id="p", dataset_id="d", client=bq or _bq_client(), **options
  )


def _ontology_graph(bq=None, **options):
  return OntologyGraphManager(
      project_id="p",
      dataset_id="d",
      spec=_simple_spec(),
      bq_client=bq or _bq_client(),
      **options,
  )


def _ai_generate_sql(bq):
  [sql] = [sql for sql in _sent_sql(bq) if "AI.GENERATE(" in sql]
  return sql


def _entity_types_literal(sql):
  """The prompt's entity-types literal, decoded, and what follows it."""
  value, end = _read_quoted(sql, sql.index("'Entity types: "))
  return value, sql[end:]


class TestEntityTypes:
  """The entity types stay inside their prompt literal."""

  @pytest.mark.parametrize(
      "entity_types", list(_ENTITY_TYPES.values()), ids=list(_ENTITY_TYPES)
  )
  @pytest.mark.parametrize("via", ["Client.context_graph", "manager"])
  def test_entity_types_stay_one_string_literal(self, via, entity_types):
    bq = _bq_client()
    config = ContextGraphConfig(entity_types=entity_types)
    if via == "manager":
      manager = _context_graph(bq, config=config)
    else:
      manager = _client(bq).context_graph(config)
    manager.extract_biz_nodes(session_ids=["s1"])
    value, rest = _entity_types_literal(_ai_generate_sql(bq))
    assert value == f"Entity types: {', '.join(entity_types)}. "
    assert rest.startswith(_THEN_RETURN)

  def test_default_entity_types_render_unchanged(self):
    bq = _bq_client()
    _client(bq).context_graph().extract_biz_nodes(session_ids=["s1"])
    assert f"'{_DEFAULT_ENTITY_TYPES}'{_THEN_RETURN}" in _ai_generate_sql(bq)


class TestContextGraphTableId:
  """The context graph's events table is checked like Client's."""

  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_SOURCE.values()),
      ids=list(_NOT_A_TABLE_SOURCE),
  )
  def test_refused_when_built_and_when_assigned(self, table_id):
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      _context_graph(table_id=table_id)
    manager = _context_graph()
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      manager.table_id = table_id
    assert manager.table_id == "agent_events"

  def test_review_reassignment_never_reaches_the_query(self):
    bq = _bq_client()
    manager = _client(bq).context_graph()
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      manager.table_id = _GRAPH_TABLE_PAYLOAD
    manager.extract_biz_nodes(session_ids=["s1"])
    sent = _sent_sql(bq)
    assert not any("CROSS JOIN" in sql for sql in sent)
    assert "FROM `p.d.agent_events` AS base" in _ai_generate_sql(bq)

  @pytest.mark.parametrize("table_id", _TABLE_NAMES + _WILDCARDS)
  def test_table_and_wildcard_reads_reach_the_extraction(self, table_id):
    bq = _bq_client()
    manager = _context_graph(bq, table_id=table_id)
    manager.extract_biz_nodes(session_ids=["s1"])
    manager.extract_decision_points(session_ids=["s1"])
    reads = [sql for sql in _sent_sql(bq) if "AI.GENERATE(" in sql]
    assert len(reads) == 2
    assert all(f"FROM `p.d.{table_id}` AS base" in sql for sql in reads)

  @pytest.mark.parametrize("table_id", _WILDCARDS)
  @pytest.mark.parametrize(
      "build",
      [
          lambda m: m.get_property_graph_ddl(),
          lambda m: m.get_decision_property_graph_ddl(),
          lambda m: m.create_property_graph(),
          lambda m: m.create_property_graph(include_decisions=True),
      ],
      ids=[
          "get_property_graph_ddl",
          "get_decision_property_graph_ddl",
          "create_property_graph",
          "create_property_graph-decisions",
      ],
  )
  def test_graph_ddl_refuses_wildcard(self, build, table_id):
    # A property graph's node and edge tables are real tables.
    bq = _bq_client()
    manager = _context_graph(bq, table_id=table_id)
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      build(manager)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("table_id", _TABLE_NAMES)
  def test_table_name_reaches_the_graph_ddl(self, table_id):
    manager = _context_graph(table_id=table_id)
    assert f"`p.d.{table_id}` AS TechNode" in manager.get_property_graph_ddl()


@pytest.mark.parametrize("field", _CONFIG_NAMES)
class TestContextGraphConfig:
  """The config's table and graph names are checked when set."""

  @pytest.mark.parametrize(
      "value",
      list(_NOT_A_STRICT_NAME.values()),
      ids=list(_NOT_A_STRICT_NAME),
  )
  def test_refused_when_built_and_when_assigned(self, field, value):
    with pytest.raises(ValueError, match=f"{field} must be one table name"):
      ContextGraphConfig(**{field: value})
    config = ContextGraphConfig()
    before = getattr(config, field)
    with pytest.raises(ValueError, match=f"{field} must be one table name"):
      setattr(config, field, value)
    assert getattr(config, field) == before

  @pytest.mark.parametrize("value", _TABLE_NAMES)
  def test_accepts_table_name(self, field, value):
    config = ContextGraphConfig(**{field: value})
    assert getattr(config, field) == value
    config = ContextGraphConfig()
    setattr(config, field, value)
    assert getattr(config, field) == value


# Every method that takes a graph_name override, and how to call it.
_GRAPH_NAME_METHODS = {
    "get_property_graph_ddl": lambda m, g: m.get_property_graph_ddl(g),
    "get_decision_property_graph_ddl": (
        lambda m, g: m.get_decision_property_graph_ddl(g)
    ),
    "create_property_graph": lambda m, g: m.create_property_graph(g),
    "get_reasoning_chain_gql": (
        lambda m, g: m.get_reasoning_chain_gql(graph_name=g)
    ),
    "explain_decision": lambda m, g: m.explain_decision(graph_name=g),
    "explain_decision-audit": (
        lambda m, g: m.explain_decision(session_id="s1", graph_name=g)
    ),
    "get_causal_chain_gql": (
        lambda m, g: m.get_causal_chain_gql("s1", graph_name=g)
    ),
    "traverse_causal_chain": (
        lambda m, g: m.traverse_causal_chain("s1", graph_name=g)
    ),
    "reconstruct_trace_gql": (
        lambda m, g: m.reconstruct_trace_gql("s1", graph_name=g, span_ids=["a"])
    ),
    "get_eu_audit_gql": lambda m, g: m.get_eu_audit_gql(graph_name=g),
    "get_dropped_candidates_gql": (
        lambda m, g: m.get_dropped_candidates_gql(graph_name=g)
    ),
}

# Every method whose GQL formats a max_hops override. (The EU audit query
# takes max_hops but its template has no path quantifier.)
_MAX_HOPS_METHODS = {
    "get_reasoning_chain_gql": lambda m, h: m.get_reasoning_chain_gql(
        max_hops=h
    ),
    "get_causal_chain_gql": lambda m, h: m.get_causal_chain_gql(
        "s1", max_hops=h
    ),
    "traverse_causal_chain": (
        lambda m, h: m.traverse_causal_chain("s1", max_hops=h)
    ),
    "explain_decision": lambda m, h: m.explain_decision(max_hops=h),
}

_NOT_A_MAX_HOPS = {
    "gql_string": "20}](injected)",
    "numeric_string": "5",
    "true": True,
    "fractional": 2.5,
    "nan": float("nan"),
    "list": [5],
}


@pytest.mark.parametrize(
    "call", list(_GRAPH_NAME_METHODS.values()), ids=list(_GRAPH_NAME_METHODS)
)
class TestGraphNameOverride:
  """A method's graph_name override is checked where it is used."""

  @pytest.mark.parametrize(
      "graph_name",
      list(_NOT_A_GRAPH_OVERRIDE.values()),
      ids=list(_NOT_A_GRAPH_OVERRIDE),
  )
  def test_refused_before_any_query(self, call, graph_name):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_GRAPH_NAME_ERROR):
      call(_context_graph(bq), graph_name)
    assert _sent_sql(bq) == []

  def test_valid_override_is_used(self, call):
    bq = _bq_client()
    result = call(_context_graph(bq), "team_graph-2")
    texts = _sent_sql(bq) + ([result] if isinstance(result, str) else [])
    assert any("`p.d.team_graph-2`" in text for text in texts)


@pytest.mark.parametrize(
    "call", list(_MAX_HOPS_METHODS.values()), ids=list(_MAX_HOPS_METHODS)
)
class TestMaxHops:
  """``max_hops`` reaches the GQL quantifier only as a whole number."""

  @pytest.mark.parametrize(
      "max_hops", list(_NOT_A_MAX_HOPS.values()), ids=list(_NOT_A_MAX_HOPS)
  )
  def test_refused_before_any_query(self, call, max_hops):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_MAX_HOPS_ERROR):
      call(_context_graph(bq), max_hops)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "max_hops,rendered",
      [(5, "5"), (5.0, "5"), (Decimal("5"), "5"), (None, "20")],
      ids=["int", "whole_float", "decimal", "config_default"],
  )
  def test_renders_whole_number(self, call, max_hops, rendered):
    bq = _bq_client()
    result = call(_context_graph(bq), max_hops)
    texts = _sent_sql(bq) + ([result] if isinstance(result, str) else [])
    assert any(f"{{1,{rendered}}}" in text for text in texts)


class TestOntologyGraphTableId:
  """The ontology graph's events table is checked like Client's."""

  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_SOURCE.values()),
      ids=list(_NOT_A_TABLE_SOURCE),
  )
  def test_refused_when_built_and_when_assigned(self, table_id):
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      _ontology_graph(table_id=table_id)
    manager = _ontology_graph()
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      manager.table_id = table_id
    assert manager.table_id == "agent_events"

  def test_review_payload_never_reaches_the_query(self):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      _ontology_graph(bq, table_id=_ONTOLOGY_TABLE_PAYLOAD)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("table_id", _TABLE_NAMES + _WILDCARDS)
  def test_table_and_wildcard_reads_reach_the_extraction(self, table_id):
    bq = _bq_client()
    manager = _ontology_graph(bq, table_id=table_id)
    manager.extract_graph(session_ids=["s1"])
    path = f"FROM `p.d.{table_id}` AS base"
    assert path in manager.get_extraction_sql()
    assert path in _ai_generate_sql(bq)


_MANAGERS = {
    "ContextGraphManager": _context_graph,
    "OntologyGraphManager": _ontology_graph,
}


@pytest.mark.parametrize("build", list(_MANAGERS.values()), ids=list(_MANAGERS))
class TestManagerIds:
  """Both managers check project and dataset IDs like Client."""

  @pytest.mark.parametrize(
      "field,value",
      [
          *[("dataset_id", value) for value in _NOT_A_DATASET_ID.values()],
          *[("project_id", value) for value in _NOT_A_PROJECT_ID.values()],
      ],
      ids=[
          *[f"dataset_id-{name}" for name in _NOT_A_DATASET_ID],
          *[f"project_id-{name}" for name in _NOT_A_PROJECT_ID],
      ],
  )
  def test_refused_when_built_and_when_assigned(self, build, field, value):
    bq = _bq_client()
    with pytest.raises(ValueError, match=f"^{field} must be one"):
      type(build(bq))(
          **{
              "project_id": "p",
              "dataset_id": "d",
              field: value,
              **_options(build, bq),
          }
      )
    manager = build(bq)
    before = getattr(manager, field)
    with pytest.raises(ValueError, match=f"^{field} must be one"):
      setattr(manager, field, value)
    assert getattr(manager, field) == before

  @pytest.mark.parametrize(
      "field,value,path",
      [
          ("dataset_id", "my-ds", "`p.my-ds.agent_events`"),
          (
              "project_id",
              "example.com:my-project",
              "`example.com:my-project.d.agent_events`",
          ),
      ],
      ids=["dataset", "project"],
  )
  def test_valid_assignment_reaches_the_query(self, build, field, value, path):
    bq = _bq_client()
    manager = build(bq)
    setattr(manager, field, value)
    if isinstance(manager, ContextGraphManager):
      manager.extract_decision_points(session_ids=["s1"])
    else:
      manager.extract_graph(session_ids=["s1"])
    assert path in _ai_generate_sql(bq)


def _options(build, bq):
  """The constructor options a manager needs besides its IDs."""
  if build is _context_graph:
    return {"client": bq}
  return {"spec": _simple_spec(), "bq_client": bq}
