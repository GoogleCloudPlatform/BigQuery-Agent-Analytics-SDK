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

"""Unit tests for pure helpers in scripts/quality_report.py.

Imports the real functions from quality_report.py. The module-scope side
effects (logging.basicConfig, dotenv) have been moved into _configure_logging()
and _load_dotenv() so the module is safe to import without triggering them.
"""

from datetime import datetime
from datetime import timezone
import json
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

import bigquery_agent_analytics
from bigquery_agent_analytics.categorical_evaluator import CategoricalContextSource
from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult
from bigquery_agent_analytics.trace import ResolvedTraceSelector
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceIdentity
from bigquery_agent_analytics.trace import TraceScope

# Make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from quality_report import _build_agent_stats
from quality_report import _build_scope_context
from quality_report import _classify_failures
from quality_report import _compute_dimension_averages
from quality_report import _compute_multiturn_stats
from quality_report import _count_trace_metrics
from quality_report import _EVAL_SPEC_CACHE  # noqa: E402
from quality_report import _extract_a2a_text
from quality_report import _extract_conversation
from quality_report import _failure_class
from quality_report import _group_by_category
from quality_report import _has_dimension_data
from quality_report import _has_failure_attribution_data
from quality_report import _inject_golden_summary
from quality_report import _is_single_word_routing
from quality_report import _load_eval_spec
from quality_report import generate_quality_report
from quality_report import get_a2a_response
from quality_report import get_user_input
from quality_report import print_quality_report
import quality_report as quality_report_module

# ---------------------------------------------------------------------------
# Lightweight stubs for report objects
# ---------------------------------------------------------------------------


class _FakeSpan:

  def __init__(self, event_type, content, agent=None):
    self.event_type = event_type
    self.content = content
    self.agent = agent


class _FakeTrace:

  def __init__(self, spans):
    self.spans = spans


class _FakeMetric:

  def __init__(self, metric_name, category, parse_error=False):
    self.metric_name = metric_name
    self.category = category
    self.parse_error = parse_error


class _FakeSession:

  def __init__(self, session_id, metrics, details=None):
    self.session_id = session_id
    self.metrics = metrics
    self.details = details or {}


class _FakeReport:

  def __init__(self, session_results):
    self.session_results = session_results


class TestIdentityAwareReportMaps:

  @staticmethod
  def _trace(user_id, run):
    now = datetime.now(tz=timezone.utc)
    identity = TraceIdentity(
        session_id="shared", user_id=user_id, root_agent_name="root"
    )
    scope = TraceScope(custom_labels={"run": run})
    return Trace(
        trace_id=f"trace-{run}",
        session_id="shared",
        user_id=user_id,
        identity=identity,
        scope=scope,
        spans=[
            Span(
                event_type="USER_MESSAGE_RECEIVED",
                agent="root",
                timestamp=now,
                content={"text": f"question-{run}"},
                span_id=f"user-{run}",
            ),
            Span(
                event_type="LLM_RESPONSE",
                agent="root",
                timestamp=now,
                content={"response": f"answer-{run}"},
                span_id=f"answer-{run}",
            ),
        ],
        start_time=now,
        end_time=now,
    )

  def test_colliding_resolved_traces_survive_and_match_score_details(self):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    resolved = quality_report_module.resolve_trace_responses(traces)
    indexed = quality_report_module._index_resolved_entries(resolved)

    assert len(indexed) == 2
    assert {entry["user_id"] for entry in indexed.values()} == {
        "alice",
        "bob",
    }

    for trace in traces:
      score = _FakeSession(
          "shared",
          [],
          details={
              "user_id": trace.identity.user_id,
              "root_agent_name": trace.identity.root_agent_name,
              "scope_signature": trace.scope.scope_signature,
          },
      )
      context = quality_report_module._resolved_context_for_score(
          indexed, score
      )
      assert context["user_id"] == trace.identity.user_id
      assert context["scope_signature"] == trace.scope.scope_signature

  def test_session_only_score_does_not_pick_one_collision(self, caplog):
    resolved = quality_report_module.resolve_trace_responses(
        [self._trace("alice", "v0"), self._trace("bob", "v1")]
    )
    indexed = quality_report_module._index_resolved_entries(resolved)

    context = quality_report_module._resolved_context_for_score(
        indexed, _FakeSession("shared", [])
    )

    assert context == {}
    assert "ambiguous" in caplog.text.lower()

  def test_trajectory_selection_skips_unattributed_collision(self, caplog):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    resolved = quality_report_module._index_resolved_entries(
        quality_report_module.resolve_trace_responses(traces)
    )
    report = _FakeReport(
        [
            _FakeSession(
                "shared",
                [_FakeMetric("response_usefulness", "unhelpful")],
            )
        ]
    )

    session_ids, selected = quality_report_module._select_trajectory_population(
        report,
        resolved,
        1,
    )

    assert session_ids == []
    assert selected == {}
    assert "ambiguous" in caplog.text.lower()

  def test_trajectory_selection_keeps_only_the_attributed_score_context(self):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    resolved = quality_report_module._index_resolved_entries(
        quality_report_module.resolve_trace_responses(traces)
    )
    selected_trace = traces[1]
    report = _FakeReport(
        [
            _FakeSession(
                "shared",
                [_FakeMetric("response_usefulness", "unhelpful")],
                details={
                    "user_id": selected_trace.identity.user_id,
                    "root_agent_name": selected_trace.identity.root_agent_name,
                    "scope_signature": selected_trace.scope.scope_signature,
                },
            )
        ]
    )

    session_ids, selected = quality_report_module._select_trajectory_population(
        report,
        resolved,
        1,
    )

    assert session_ids == ["shared"]
    assert list(selected.values()) == [
        resolved[
            (
                "shared",
                "bob",
                "root",
                selected_trace.scope.scope_signature,
            )
        ]
    ]

  def test_built_report_index_reuses_attribution_lookup(self):
    trace = self._trace("alice", "v0")
    resolved = quality_report_module._index_resolved_entries(
        quality_report_module.resolve_trace_responses([trace])
    )
    score = _FakeSession(
        "shared",
        [],
        details={
            "user_id": "alice",
            "root_agent_name": "root",
            "scope_signature": trace.scope.scope_signature,
        },
    )

    def _unexpected_full_scan():
      raise AssertionError("correlation rescanned the full report index")

    resolved.items = _unexpected_full_scan

    assert (
        quality_report_module._resolved_context_for_score(resolved, score)[
            "user_id"
        ]
        == "alice"
    )

  def test_collision_safe_trace_index_retains_both_candidates(self):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]

    indexed = quality_report_module._index_traces(traces)

    assert len(indexed) == 2
    assert {trace.identity.user_id for trace in indexed.values()} == {
        "alice",
        "bob",
    }

  def test_golden_matching_preserves_exact_selector_keys(self, monkeypatch):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    selectors = [
        ResolvedTraceSelector(
            identity=trace.identity,
            scope=trace.scope,
        )
        for trace in traces
    ]
    questions = {
        selectors[0]: "alice question",
        selectors[1]: "bob question",
    }
    golden = [
        {"question": "alice golden", "expected_answer": "alice answer"},
        {"question": "bob golden", "expected_answer": "bob answer"},
    ]

    def fake_embeddings(texts, **kwargs):
      return [[1.0, 0.0] if "alice" in text else [0.0, 1.0] for text in texts]

    from bigquery_agent_analytics import golden_matching

    monkeypatch.setattr(golden_matching, "embed_texts", fake_embeddings)

    contexts, metadata = quality_report_module.match_golden_qa(
        questions, golden
    )

    assert set(contexts) == set(selectors)
    assert set(metadata) == set(selectors)
    assert "alice answer" in contexts[selectors[0]]
    assert "bob answer" in contexts[selectors[1]]

  def test_bigquery_run_passes_selector_keyed_golden_context(self, monkeypatch):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    captured = {}

    class FakeClient:

      def list_traces(self, filter_criteria):
        return traces

      def evaluate_categorical(self, **kwargs):
        captured.update(kwargs)
        results = []
        for trace in traces:
          results.append(
              CategoricalSessionResult(
                  session_id=trace.session_id,
                  identity=trace.identity,
                  scope=trace.scope,
                  metrics=[
                      CategoricalMetricResult(
                          metric_name="response_usefulness",
                          category="meaningful",
                      )
                  ],
                  details={
                      "user_id": trace.identity.user_id,
                      "root_agent_name": trace.identity.root_agent_name,
                      "scope_signature": trace.scope.scope_signature,
                  },
              )
          )
        return CategoricalEvaluationReport(
            dataset="test",
            total_sessions=2,
            session_results=results,
        )

    def fake_match(questions, golden_qa, threshold):
      captured["question_keys"] = set(questions)
      return (
          {
              selector: f"context:{selector.identity.user_id}"
              for selector in questions
          },
          {selector: {"matched": True} for selector in questions},
      )

    monkeypatch.setattr(quality_report_module, "get_client", FakeClient)
    monkeypatch.setattr(quality_report_module, "match_golden_qa", fake_match)

    quality_report_module.run_evaluation(
        model="gemini-test",
        limit=2,
        eval_spec={
            "golden_qa": [
                {"question": "q", "expected_answer": "a"},
            ]
        },
    )

    expected = {
        ResolvedTraceSelector(identity=trace.identity, scope=trace.scope)
        for trace in traces
    }
    assert captured["question_keys"] == expected
    assert set(captured["per_session_context"]) == expected
    assert (
        captured["context_source"]
        is CategoricalContextSource.GOLDEN_EXPECTED_ANSWER
    )

  def test_trace_fetch_limit_counts_resolved_identity_scope_candidates(self):
    resolved = quality_report_module._index_resolved_entries(
        [
            {
                "session_id": "shared",
                "user_id": "alice",
                "root_agent_name": "root",
                "scope_signature": "scope-a",
            },
            {
                "session_id": "shared",
                "user_id": "bob",
                "root_agent_name": "root",
                "scope_signature": "scope-b",
            },
            {
                "session_id": "other",
                "user_id": "carol",
                "root_agent_name": "root",
                "scope_signature": "scope-c",
            },
        ]
    )

    assert quality_report_module._trace_fetch_limit(resolved, ["shared"]) == 2
    assert (
        quality_report_module._trace_fetch_limit(resolved, ["shared", "other"])
        == 3
    )

  def test_explicit_report_limit_reserves_u2_candidates_per_session(self):
    assert quality_report_module._explicit_report_trace_limit(["shared"]) == 64
    assert (
        quality_report_module._explicit_report_trace_limit(
            ["shared", "other", "shared"]
        )
        == 128
    )

  def test_fetch_session_traces_uses_resolved_trace_limit(self, monkeypatch):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    requested_limits = []

    class _FakeClient:

      def __init__(self, **_kwargs):
        pass

      def list_traces(self, filter_criteria):
        requested_limits.append(filter_criteria.limit)
        return traces

    monkeypatch.setattr(bigquery_agent_analytics, "Client", _FakeClient)
    monkeypatch.setattr(
        quality_report_module, "_import_render_timing_tree", lambda: True
    )
    monkeypatch.setattr(quality_report_module, "PROJECT_ID", "project")
    monkeypatch.setattr(quality_report_module, "DATASET_ID", "dataset")

    fetched = quality_report_module._fetch_session_traces(
        ["shared"], max_sessions=1, max_traces=2
    )

    assert requested_limits == [2, 4]
    assert len(fetched) == 2

  def test_fetch_session_traces_preserves_base_filter_bounds(self, monkeypatch):
    alice = self._trace("alice", "v0")
    captured = {}

    class _FakeClient:

      def __init__(self, **_kwargs):
        pass

      def list_traces(self, filter_criteria):
        captured.setdefault("filters", []).append(filter_criteria)
        return [alice]

    monkeypatch.setattr(bigquery_agent_analytics, "Client", _FakeClient)
    monkeypatch.setattr(
        quality_report_module, "_import_render_timing_tree", lambda: True
    )
    monkeypatch.setattr(quality_report_module, "PROJECT_ID", "project")
    monkeypatch.setattr(quality_report_module, "DATASET_ID", "dataset")

    base = quality_report_module._cli_base_trace_filter(
        app_name="skill-evolution-lab",
        custom_labels={"run": "lab_1", "slice": "v0_test"},
        time_period="24h",
    )
    base.limit = 500

    quality_report_module._fetch_session_traces(
        ["shared"], max_sessions=1, max_traces=2, base_filter=base
    )

    fetch_filter = captured["filters"][0]
    # App/label/time bounds survive the delegation to the fetch filter...
    assert fetch_filter.root_agent_name == "skill-evolution-lab"
    assert fetch_filter.custom_labels == {"run": "lab_1", "slice": "v0_test"}
    assert fetch_filter.start_time is not None
    # ...while ONLY the session ids and display limit are replaced...
    assert fetch_filter.session_ids == ["shared"]
    assert fetch_filter.limit == 2
    # ...and the caller's base filter is never mutated.
    assert base.session_ids is None
    assert base.limit == 500

  def test_cli_base_trace_filter_bounds_and_unbounded_none(self):
    base = quality_report_module._cli_base_trace_filter(
        app_name="skill-evolution-lab",
        custom_labels={"run": "lab_1"},
        time_period="24h",
    )
    assert base.root_agent_name == "skill-evolution-lab"
    assert base.custom_labels == {"run": "lab_1"}
    assert base.start_time is not None

    assert (
        quality_report_module._cli_base_trace_filter(
            app_name=None, custom_labels=None, time_period="all"
        )
        is None
    )

  def test_fetch_session_traces_retains_only_report_attribution(
      self, monkeypatch
  ):
    alice = self._trace("alice", "v0")
    bob = self._trace("bob", "v1")
    resolved = quality_report_module._index_resolved_entries(
        quality_report_module.resolve_trace_responses([alice])
    )
    captured = {}

    class _FakeClient:

      def __init__(self, **_kwargs):
        pass

      def list_traces(self, filter_criteria):
        captured["filter"] = filter_criteria
        # The foreign trace is deliberately first, matching the failure mode
        # where a newer same-session scope would otherwise fill the limit.
        return [bob, alice]

    monkeypatch.setattr(bigquery_agent_analytics, "Client", _FakeClient)
    monkeypatch.setattr(
        quality_report_module, "_import_render_timing_tree", lambda: True
    )
    monkeypatch.setattr(quality_report_module, "PROJECT_ID", "project")
    monkeypatch.setattr(quality_report_module, "DATASET_ID", "dataset")

    fetched = quality_report_module._fetch_session_traces(
        ["shared"],
        max_sessions=1,
        max_traces=1,
        resolved_map=resolved,
    )

    assert captured["filter"].limit == 64
    assert list(fetched.values()) == [alice]

  def test_fetch_session_traces_expands_past_64_scope_candidates(
      self, monkeypatch
  ):
    traces = [self._trace("alice", f"v{i:02d}") for i in range(65)]
    resolved = quality_report_module._index_resolved_entries(
        quality_report_module.resolve_trace_responses(traces)
    )
    requested_limits = []

    class _FakeClient:

      def __init__(self, **_kwargs):
        pass

      def list_traces(self, filter_criteria):
        requested_limits.append(filter_criteria.limit)
        return traces[: filter_criteria.limit]

    monkeypatch.setattr(bigquery_agent_analytics, "Client", _FakeClient)
    monkeypatch.setattr(
        quality_report_module, "_import_render_timing_tree", lambda: True
    )
    monkeypatch.setattr(quality_report_module, "PROJECT_ID", "project")
    monkeypatch.setattr(quality_report_module, "DATASET_ID", "dataset")

    fetched = quality_report_module._fetch_session_traces(
        ["shared"],
        max_sessions=1,
        max_traces=1,
        resolved_map=resolved,
    )

    assert requested_limits == [64, 128]
    assert len(fetched) == 65

  def test_complete_report_listing_fails_instead_of_returning_capped_page(
      self, monkeypatch
  ):
    import pytest

    traces = [self._trace("alice", f"v{i:02d}") for i in range(64)]

    class _FakeClient:

      def list_traces(self, filter_criteria):
        return traces[: filter_criteria.limit]

    monkeypatch.setattr(
        quality_report_module, "_REPORT_TRACE_LIMIT_CEILING", 64
    )

    with pytest.raises(
        quality_report_module._ReportTracePopulationTruncatedError,
        match="no partial report",
    ):
      quality_report_module._list_complete_report_traces(
          _FakeClient(),
          bigquery_agent_analytics.TraceFilter(
              session_ids=["shared"],
              limit=64,
          ),
      )

  def test_fetch_session_traces_fails_closed_for_legacy_collision(
      self, monkeypatch, caplog
  ):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    resolved = quality_report_module._index_resolved_entries(
        [{"session_id": "shared", "question": "legacy"}]
    )

    class _FakeClient:

      def __init__(self, **_kwargs):
        pass

      def list_traces(self, filter_criteria):
        del filter_criteria
        return traces

    monkeypatch.setattr(bigquery_agent_analytics, "Client", _FakeClient)
    monkeypatch.setattr(
        quality_report_module, "_import_render_timing_tree", lambda: True
    )
    monkeypatch.setattr(quality_report_module, "PROJECT_ID", "project")
    monkeypatch.setattr(quality_report_module, "DATASET_ID", "dataset")

    fetched = quality_report_module._fetch_session_traces(
        ["shared"],
        max_sessions=1,
        max_traces=1,
        resolved_map=resolved,
    )

    assert fetched == {}
    assert "legacy report context" in caplog.text

  def test_markdown_trajectory_skips_trace_outside_report_attribution(
      self, monkeypatch, caplog
  ):
    alice = self._trace("alice", "v0")
    bob = self._trace("bob", "v1")
    resolved = quality_report_module._index_resolved_entries(
        quality_report_module.resolve_trace_responses([alice])
    )
    written = []
    monkeypatch.setattr(
        quality_report_module,
        "_render_trace",
        lambda _trace: "FOREIGN TRACE",
    )

    quality_report_module._md_write_trajectory_section(
        written.append,
        quality_report_module._index_traces([bob]),
        resolved,
    )

    rendered = "\n".join(written)
    assert "FOREIGN TRACE" not in rendered
    assert "trace outside the resolved report population" in caplog.text

  def test_unique_legacy_context_matches_scoped_trace_both_directions(self):
    trace = self._trace("alice", "v0")
    legacy_context = {
        "session_id": "shared",
        "question": "legacy question",
    }
    resolved = {"shared": legacy_context}
    trajectories = quality_report_module._index_traces([trace])

    assert (
        quality_report_module._trace_for_context(trajectories, legacy_context)
        is trace
    )
    assert (
        quality_report_module._resolved_context_for_trace(resolved, trace)
        is legacy_context
    )

  def test_json_output_correlates_colliding_scores_by_attribution(self):
    traces = [self._trace("alice", "v0"), self._trace("bob", "v1")]
    resolved = quality_report_module._index_resolved_entries(
        quality_report_module.resolve_trace_responses(traces)
    )
    scores = [
        CategoricalSessionResult(
            session_id="shared",
            identity=trace.identity,
            scope=trace.scope,
            context_applied=True,
            context_source=(CategoricalContextSource.GOLDEN_EXPECTED_ANSWER),
            execution_mode="ai_generate",
            details={
                "user_id": trace.identity.user_id,
                "root_agent_name": trace.identity.root_agent_name,
                "scope_signature": trace.scope.scope_signature,
            },
        )
        for trace in traces
    ]
    report = CategoricalEvaluationReport(
        dataset="test",
        total_sessions=2,
        session_results=scores,
    )

    output = quality_report_module._build_json_output(report, resolved)

    assert [row["user_id"] for row in output["sessions"]] == [
        "alice",
        "bob",
    ]
    assert [row["question"] for row in output["sessions"]] == [
        "question-v0",
        "question-v1",
    ]
    assert all(row["context_applied"] is True for row in output["sessions"])
    assert {row["context_source"] for row in output["sessions"]} == {
        "golden_expected_answer"
    }
    assert {row["execution_mode"] for row in output["sessions"]} == {
        "ai_generate"
    }
    suffix = quality_report_module._report_identity_suffix(
        resolved[next(iter(resolved))]
    )
    assert "user=" in suffix
    assert "scope=" in suffix
    coverage_suffix = quality_report_module._report_identity_suffix(
        {
            "scope_signature": None,
            "scope_coverage": tuple(
                trace.scope.scope_signature for trace in traces
            ),
        }
    )
    assert "scope_coverage=" in coverage_suffix
    assert traces[0].scope.scope_signature in coverage_suffix
    assert traces[1].scope.scope_signature in coverage_suffix

  def test_json_to_markdown_retains_colliding_identity_attribution(
      self, tmp_path, monkeypatch
  ):
    scopes = [
        TraceScope(custom_labels={"run": "v0"}),
        TraceScope(custom_labels={"run": "v1"}),
    ]
    sessions = [
        {
            "session_id": "shared",
            "user_id": user_id,
            "root_agent_name": "root",
            "scope_signature": scope.scope_signature,
            "question": f"question-{run}",
            "response": f"answer-{run}",
            "is_a2a": is_a2a,
            "metrics": {
                "response_usefulness": {
                    "category": "unhelpful",
                    "justification": f"reason-{run}",
                },
                "failure_attribution": {
                    "category": failure_class,
                    "justification": f"failure-{run}",
                },
            },
        }
        for user_id, run, scope, is_a2a, failure_class in zip(
            ("alice", "bob"),
            ("v0", "v1"),
            scopes,
            (True, False),
            ("knowledge_gap", "tool_gap"),
        )
    ]
    json_path = tmp_path / "collision.json"
    json_path.write_text(
        json.dumps({"sessions": sessions, "details": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(quality_report_module, "PROJECT_ID", "")
    monkeypatch.setattr(quality_report_module, "DATASET_ID", "local")

    quality_report_module._render_md_from_json(
        str(json_path), SimpleNamespace(model=None, samples=None)
    )
    markdown = json_path.with_suffix(".md").read_text(encoding="utf-8")

    assert "user=alice" in markdown
    assert "user=bob" in markdown
    assert scopes[0].scope_signature in markdown
    assert scopes[1].scope_signature in markdown
    assert "question-v0" in markdown
    assert "question-v1" in markdown
    assert markdown.count("[A2A]") == 1

    knowledge_gap = markdown.split(
        "### Knowledge Gaps (add a fact to existing data)", 1
    )[1].split("### Tool Gaps (build a new tool / data source)", 1)[0]
    tool_gap = markdown.split(
        "### Tool Gaps (build a new tool / data source)", 1
    )[1].split("## Category Distributions", 1)[0]
    assert "question-v0" in knowledge_gap
    assert "question-v1" not in knowledge_gap
    assert "question-v1" in tool_gap
    assert "question-v0" not in tool_gap


# ================================================================== #
# _is_single_word_routing                                             #
# ================================================================== #


class TestIsSingleWordRouting:

  def test_empty_string(self):
    assert _is_single_word_routing("") is True

  def test_none(self):
    assert _is_single_word_routing(None) is True

  def test_single_short_word(self):
    assert _is_single_word_routing("hello") is True

  def test_single_long_word(self):
    # >= 20 chars, single word
    assert _is_single_word_routing("a" * 20) is False

  def test_multi_word(self):
    assert _is_single_word_routing("hello world") is False

  def test_whitespace_only(self):
    assert _is_single_word_routing("   ") is True

  def test_short_word_with_whitespace(self):
    assert _is_single_word_routing("  hi  ") is True


# ================================================================== #
# _extract_a2a_text                                                    #
# ================================================================== #


class TestExtractA2AText:

  def test_artifacts(self):
    payload = {
        "artifacts": [{"parts": [{"kind": "text", "text": "Hello from A2A"}]}]
    }
    text, agent = _extract_a2a_text(payload)
    assert text == "Hello from A2A"
    assert agent is None

  def test_history_fallback(self):
    payload = {
        "history": [
            {
                "role": "agent",
                "parts": [{"kind": "text", "text": "History response"}],
            }
        ]
    }
    text, agent = _extract_a2a_text(payload)
    assert text == "History response"

  def test_metadata_agent_name(self):
    payload = {
        "artifacts": [{"parts": [{"kind": "text", "text": "resp"}]}],
        "metadata": {"adk_app_name": "my_agent"},
    }
    text, agent = _extract_a2a_text(payload)
    assert agent == "my_agent"

  def test_metadata_author_fallback(self):
    payload = {
        "artifacts": [{"parts": [{"kind": "text", "text": "resp"}]}],
        "metadata": {"adk_author": "author_agent"},
    }
    text, agent = _extract_a2a_text(payload)
    assert agent == "author_agent"

  def test_missing_fields(self):
    payload = {}
    text, agent = _extract_a2a_text(payload)
    assert text is None
    assert agent is None

  def test_non_dict_input(self):
    text, agent = _extract_a2a_text("raw string")
    assert text == "raw string"
    assert agent is None

  def test_none_input(self):
    text, agent = _extract_a2a_text(None)
    assert text is None
    assert agent is None

  def test_non_text_parts_skipped(self):
    payload = {"artifacts": [{"parts": [{"kind": "image", "data": "binary"}]}]}
    text, agent = _extract_a2a_text(payload)
    assert text is None

  def test_empty_text_parts_skipped(self):
    payload = {"artifacts": [{"parts": [{"kind": "text", "text": ""}]}]}
    text, agent = _extract_a2a_text(payload)
    assert text is None

  def test_multiple_artifacts_concatenated(self):
    payload = {
        "artifacts": [
            {"parts": [{"kind": "text", "text": "part1"}]},
            {"parts": [{"kind": "text", "text": "part2"}]},
        ]
    }
    text, agent = _extract_a2a_text(payload)
    assert text == "part1 part2"

  def test_user_history_skipped(self):
    payload = {
        "history": [
            {
                "role": "user",
                "parts": [{"kind": "text", "text": "user msg"}],
            },
            {
                "role": "agent",
                "parts": [{"kind": "text", "text": "agent msg"}],
            },
        ]
    }
    text, agent = _extract_a2a_text(payload)
    assert text == "agent msg"


# ================================================================== #
# _build_agent_stats                                                   #
# ================================================================== #


class TestBuildAgentStats:

  def test_mixed_categories(self):
    sessions = [
        _FakeSession("s1", [_FakeMetric("response_usefulness", "meaningful")]),
        _FakeSession("s2", [_FakeMetric("response_usefulness", "unhelpful")]),
        _FakeSession("s3", [_FakeMetric("response_usefulness", "partial")]),
    ]
    report = _FakeReport(sessions)
    resolved = {
        "s1": {"answered_by": "agent_a"},
        "s2": {"answered_by": "agent_a"},
        "s3": {"answered_by": "agent_b"},
    }
    stats = _build_agent_stats(report, resolved)
    assert stats["agent_a"]["total"] == 2
    assert stats["agent_a"]["meaningful"] == 1
    assert stats["agent_a"]["unhelpful"] == 1
    assert stats["agent_b"]["partial"] == 1

  def test_unclassified(self):
    sessions = [
        _FakeSession("s1", [_FakeMetric("response_usefulness", "weird_cat")]),
    ]
    report = _FakeReport(sessions)
    resolved = {"s1": {"answered_by": "agent_a"}}
    stats = _build_agent_stats(report, resolved)
    assert stats["agent_a"]["unclassified"] == 1

  def test_missing_usefulness_metric(self):
    sessions = [
        _FakeSession("s1", [_FakeMetric("task_grounding", "grounded")]),
    ]
    report = _FakeReport(sessions)
    resolved = {"s1": {"answered_by": "agent_a"}}
    stats = _build_agent_stats(report, resolved)
    assert stats["agent_a"]["unclassified"] == 1

  def test_a2a_count(self):
    sessions = [
        _FakeSession("s1", [_FakeMetric("response_usefulness", "meaningful")]),
        _FakeSession("s2", [_FakeMetric("response_usefulness", "meaningful")]),
        _FakeSession("s3", [_FakeMetric("response_usefulness", "meaningful")]),
    ]
    report = _FakeReport(sessions)
    resolved = {
        "s1": {"answered_by": "agent_a", "is_a2a": True},
        "s2": {"answered_by": "agent_a", "is_a2a": False},
        "s3": {"answered_by": "agent_a", "is_a2a": True},
    }
    stats = _build_agent_stats(report, resolved)
    assert stats["agent_a"]["a2a_count"] == 2
    assert stats["agent_a"]["total"] == 3

  def test_empty_input(self):
    report = _FakeReport([])
    stats = _build_agent_stats(report, {})
    assert stats == {}

  def test_unknown_agent_fallback(self):
    sessions = [
        _FakeSession("s1", [_FakeMetric("response_usefulness", "meaningful")]),
    ]
    report = _FakeReport(sessions)
    resolved = {}
    stats = _build_agent_stats(report, resolved)
    assert "unknown" in stats
    assert stats["unknown"]["total"] == 1


# ================================================================== #
# _group_by_category                                                   #
# ================================================================== #


class TestGroupByCategory:

  def test_basic_grouping(self):
    sessions = [
        _FakeSession("s1", [_FakeMetric("response_usefulness", "meaningful")]),
        _FakeSession("s2", [_FakeMetric("response_usefulness", "unhelpful")]),
        _FakeSession("s3", [_FakeMetric("response_usefulness", "partial")]),
    ]
    report = _FakeReport(sessions)
    groups = _group_by_category(report)
    assert len(groups["meaningful"]) == 1
    assert len(groups["unhelpful"]) == 1
    assert len(groups["partial"]) == 1

  def test_unknown_category(self):
    sessions = [
        _FakeSession("s1", [_FakeMetric("response_usefulness", None)]),
    ]
    report = _FakeReport(sessions)
    groups = _group_by_category(report)
    assert len(groups.get("unknown", [])) == 1

  def test_empty_report(self):
    report = _FakeReport([])
    groups = _group_by_category(report)
    assert groups == {
        "unhelpful": [],
        "partial": [],
        "meaningful": [],
        "declined": [],
    }


# ================================================================== #
# get_user_input                                                       #
# ================================================================== #


class TestGetUserInput:

  def test_single_message(self):
    trace = _FakeTrace(
        [
            _FakeSpan("USER_MESSAGE_RECEIVED", {"text": "Hello"}),
        ]
    )
    assert get_user_input(trace) == "Hello"

  def test_multi_turn_returns_last(self):
    trace = _FakeTrace(
        [
            _FakeSpan("USER_MESSAGE_RECEIVED", {"text": "First question"}),
            _FakeSpan("LLM_RESPONSE", {"response": "Answer 1"}),
            _FakeSpan("USER_MESSAGE_RECEIVED", {"text": "Follow-up question"}),
        ]
    )
    assert get_user_input(trace) == "Follow-up question"

  def test_text_summary_preferred(self):
    trace = _FakeTrace(
        [
            _FakeSpan(
                "USER_MESSAGE_RECEIVED",
                {"text_summary": "Summary", "text": "Full text"},
            ),
        ]
    )
    assert get_user_input(trace) == "Summary"

  def test_string_content(self):
    trace = _FakeTrace(
        [
            _FakeSpan("USER_MESSAGE_RECEIVED", "plain string"),
        ]
    )
    assert get_user_input(trace) == "plain string"

  def test_no_user_messages(self):
    trace = _FakeTrace(
        [
            _FakeSpan("LLM_RESPONSE", {"response": "something"}),
        ]
    )
    assert get_user_input(trace) == ""

  def test_empty_spans(self):
    trace = _FakeTrace([])
    assert get_user_input(trace) == ""

  def test_none_content_skipped(self):
    trace = _FakeTrace(
        [
            _FakeSpan("USER_MESSAGE_RECEIVED", None),
        ]
    )
    assert get_user_input(trace) == ""


# ================================================================== #
# get_a2a_response                                                     #
# ================================================================== #


class TestGetA2AResponse:

  def test_dict_content(self):
    payload = {
        "artifacts": [{"parts": [{"kind": "text", "text": "A2A answer"}]}],
        "metadata": {"adk_app_name": "remote"},
    }
    trace = _FakeTrace(
        [
            _FakeSpan("A2A_INTERACTION", payload, agent="fallback_agent"),
        ]
    )
    text, agent = get_a2a_response(trace)
    assert text == "A2A answer"
    assert agent == "remote"

  def test_null_content_returns_no_response(self):
    trace = _FakeTrace(
        [
            _FakeSpan("A2A_INTERACTION", None, agent="remote_agent"),
        ]
    )
    text, agent = get_a2a_response(trace)
    assert text == "(no response)"
    assert agent == "remote_agent"

  def test_empty_dict_returns_no_response(self):
    trace = _FakeTrace(
        [
            _FakeSpan("A2A_INTERACTION", {}, agent="remote_agent"),
        ]
    )
    text, agent = get_a2a_response(trace)
    assert text == "(no response)"
    assert agent == "remote_agent"

  def test_returns_last_a2a_interaction(self):
    payload1 = {
        "artifacts": [{"parts": [{"kind": "text", "text": "First"}]}],
    }
    payload2 = {
        "artifacts": [{"parts": [{"kind": "text", "text": "Second"}]}],
    }
    trace = _FakeTrace(
        [
            _FakeSpan("A2A_INTERACTION", payload1, agent="agent1"),
            _FakeSpan("A2A_INTERACTION", payload2, agent="agent2"),
        ]
    )
    text, agent = get_a2a_response(trace)
    assert text == "Second"

  def test_no_a2a_interactions(self):
    trace = _FakeTrace(
        [
            _FakeSpan("LLM_RESPONSE", {"response": "hi"}),
        ]
    )
    text, agent = get_a2a_response(trace)
    assert text is None
    assert agent is None

  def test_string_content_json(self):
    import json

    payload = {
        "artifacts": [{"parts": [{"kind": "text", "text": "parsed"}]}],
        "metadata": {"adk_app_name": "json_agent"},
    }
    trace = _FakeTrace(
        [
            _FakeSpan("A2A_INTERACTION", json.dumps(payload), agent="fallback"),
        ]
    )
    text, agent = get_a2a_response(trace)
    assert text == "parsed"
    assert agent == "json_agent"

  def test_invalid_json_string(self):
    trace = _FakeTrace(
        [
            _FakeSpan("A2A_INTERACTION", "not json", agent="agent"),
        ]
    )
    text, agent = get_a2a_response(trace)
    assert text == "(no response)"
    assert agent == "agent"


# ================================================================== #
# _build_scope_context                                                #
# ================================================================== #


class TestBuildScopeContext:

  def test_none_spec(self):
    assert _build_scope_context(None) == ""

  def test_empty_spec(self):
    assert _build_scope_context({}) == ""

  def test_scope_free_text(self):
    result = _build_scope_context({"scope": "Handles PTO and benefits only."})
    assert "Handles PTO and benefits only." in result
    assert "OUT OF SCOPE" in result
    assert "declined" in result

  def test_ground_truth_only(self):
    result = _build_scope_context({"ground_truth": "PTO is 20 days/year."})
    assert "GROUND TRUTH" in result
    assert "20 days/year" in result

  def test_scope_and_ground_truth(self):
    result = _build_scope_context(
        {
            "scope": "HR policy questions.",
            "ground_truth": "PTO is 20 days.",
        }
    )
    assert "HR policy questions." in result
    assert "PTO is 20 days." in result

  def test_no_relevant_fields(self):
    # A spec with only golden_qa contributes no scope/ground-truth context.
    assert _build_scope_context({"golden_qa": [{"question": "q"}]}) == ""


# ================================================================== #
# _inject_golden_summary                                              #
# ================================================================== #


class TestInjectGoldenSummary:

  def _report(self, sessions):
    return {"summary": {}, "sessions": sessions}

  def test_no_metadata_is_noop(self):
    report = self._report([{"session_id": "s1"}])
    _inject_golden_summary(report, None)
    assert "golden_eval_summary" not in report["summary"]

  def test_matched_meaningful_and_mismatch(self):
    sessions = [
        {
            "session_id": "s1",
            "question": "q1",
            "response": "good",
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        },
        {
            "session_id": "s2",
            "question": "q2",
            "response": "bad",
            "metrics": {"response_usefulness": {"category": "unhelpful"}},
        },
        {
            "session_id": "s3",
            "question": "q3",
            "response": "x",
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        },
    ]
    meta = {
        "s1": {
            "matched": True,
            "expected_answer": "a1",
            "topic": "pto",
            "similarity": 0.99,
        },
        "s2": {
            "matched": True,
            "expected_answer": "a2",
            "topic": "benefits",
            "similarity": 0.98,
        },
        "s3": {"matched": False, "similarity": 0.4},
    }
    report = self._report(sessions)
    _inject_golden_summary(report, meta)
    gs = report["summary"]["golden_eval_summary"]
    assert gs["matched"] == 2
    assert gs["matched_meaningful"] == 1
    assert gs["matched_unhelpful"] == 1
    assert gs["unmatched"] == 1
    assert len(gs["mismatches"]) == 1
    assert gs["mismatches"][0]["question"] == "q2"
    # Per-session golden_eval is attached.
    assert sessions[0]["golden_eval"]["matched"] is True
    assert sessions[2]["golden_eval"]["matched"] is False

  def test_declined_counts_as_meaningful(self):
    sessions = [
        {
            "session_id": "s1",
            "question": "q",
            "response": "decline",
            "metrics": {"response_usefulness": {"category": "declined"}},
        },
    ]
    meta = {
        "s1": {
            "matched": True,
            "expected_answer": "",
            "topic": "out_of_scope",
            "similarity": 0.99,
        }
    }
    report = self._report(sessions)
    _inject_golden_summary(report, meta)
    gs = report["summary"]["golden_eval_summary"]
    assert gs["matched_meaningful"] == 1
    assert gs["matched_unhelpful"] == 0

  def test_unattributed_collision_does_not_use_legacy_golden_metadata(self):
    sessions = [
        {
            "session_id": "shared",
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        },
        {
            "session_id": "shared",
            "metrics": {"response_usefulness": {"category": "unhelpful"}},
        },
    ]
    report = self._report(sessions)

    _inject_golden_summary(
        report,
        {
            "shared": {
                "matched": True,
                "expected_answer": "must not attach",
                "similarity": 1.0,
            }
        },
    )

    assert [session["golden_eval"] for session in sessions] == [None, None]
    assert report["summary"]["golden_eval_summary"]["total_sessions"] == 0

  def test_attributed_row_does_not_use_legacy_golden_metadata(self):
    scope = TraceScope(custom_labels={"run": "v0"})
    sessions = [
        {
            "session_id": "shared",
            "user_id": "alice",
            "root_agent_name": "root",
            "experiment_id": None,
            "custom_labels": {"run": "v0"},
            "scope_signature": scope.scope_signature,
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        }
    ]
    report = self._report(sessions)

    _inject_golden_summary(
        report,
        {
            "shared": {
                "matched": True,
                "expected_answer": "must not attach",
                "similarity": 1.0,
            }
        },
    )

    assert sessions[0]["golden_eval"] is None
    assert report["summary"]["golden_eval_summary"]["total_sessions"] == 0

  def test_colliding_session_ids_receive_their_exact_golden_match(self):
    alice_scope = TraceScope(custom_labels={"run": "v0"})
    bob_scope = TraceScope(custom_labels={"run": "v1"})
    alice = ResolvedTraceSelector(
        identity=TraceIdentity("shared", "alice", "root"),
        scope=alice_scope,
    )
    bob = ResolvedTraceSelector(
        identity=TraceIdentity("shared", "bob", "root"),
        scope=bob_scope,
    )
    sessions = [
        {
            "session_id": "shared",
            "user_id": "alice",
            "root_agent_name": "root",
            "experiment_id": None,
            "custom_labels": {"run": "v0"},
            "scope_signature": alice_scope.scope_signature,
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        },
        {
            "session_id": "shared",
            "user_id": "bob",
            "root_agent_name": "root",
            "experiment_id": None,
            "custom_labels": {"run": "v1"},
            "scope_signature": bob_scope.scope_signature,
            "metrics": {"response_usefulness": {"category": "unhelpful"}},
        },
    ]
    report = self._report(sessions)

    _inject_golden_summary(
        report,
        {
            alice: {
                "matched": True,
                "expected_answer": "alice answer",
                "topic": "alice",
                "similarity": 1.0,
            },
            bob: {
                "matched": True,
                "expected_answer": "bob answer",
                "topic": "bob",
                "similarity": 1.0,
            },
        },
    )

    assert sessions[0]["golden_eval"]["expected_answer"] == "alice answer"
    assert sessions[1]["golden_eval"]["expected_answer"] == "bob answer"
    assert report["summary"]["golden_eval_summary"]["matched"] == 2

  def test_nullable_identity_dimensions_receive_exact_golden_match(self):
    anonymous_scope = TraceScope(custom_labels={"run": "anonymous"})
    named_scope = TraceScope(custom_labels={"run": "named"})
    anonymous = ResolvedTraceSelector(
        identity=TraceIdentity("shared", None, None),
        scope=anonymous_scope,
    )
    named = ResolvedTraceSelector(
        identity=TraceIdentity("shared", "alice", "root"),
        scope=named_scope,
    )
    sessions = [
        {
            "session_id": "shared",
            "user_id": None,
            "root_agent_name": None,
            "experiment_id": None,
            "custom_labels": {"run": "anonymous"},
            "scope_signature": anonymous_scope.scope_signature,
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        },
        {
            "session_id": "shared",
            "user_id": "alice",
            "root_agent_name": "root",
            "experiment_id": None,
            "custom_labels": {"run": "named"},
            "scope_signature": named_scope.scope_signature,
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        },
    ]
    report = self._report(sessions)

    _inject_golden_summary(
        report,
        {
            anonymous: {
                "matched": True,
                "expected_answer": "anonymous answer",
                "similarity": 1.0,
            },
            named: {
                "matched": True,
                "expected_answer": "named answer",
                "similarity": 1.0,
            },
        },
    )

    assert sessions[0]["golden_eval"]["expected_answer"] == "anonymous answer"
    assert sessions[1]["golden_eval"]["expected_answer"] == "named answer"
    assert report["summary"]["golden_eval_summary"]["matched"] == 2

  @pytest.mark.parametrize("invalid_labels", [False, 0])
  def test_invalid_falsy_labels_do_not_rebuild_exact_selector(
      self, invalid_labels
  ):
    scope = TraceScope()
    selector = ResolvedTraceSelector(
        identity=TraceIdentity("shared", None, None),
        scope=scope,
    )
    sessions = [
        {
            "session_id": "shared",
            "user_id": None,
            "root_agent_name": None,
            "experiment_id": None,
            "custom_labels": invalid_labels,
            "scope_signature": scope.scope_signature,
            "metrics": {"response_usefulness": {"category": "meaningful"}},
        }
    ]
    report = self._report(sessions)

    _inject_golden_summary(
        report,
        {
            selector: {
                "matched": True,
                "expected_answer": "must not attach",
                "similarity": 1.0,
            }
        },
    )

    assert sessions[0]["golden_eval"] is None
    assert report["summary"]["golden_eval_summary"]["total_sessions"] == 0


# ================================================================== #
# _failure_class / _classify_failures                                 #
# ================================================================== #


class TestFailureClass:

  def test_not_a_failure(self):
    assert _failure_class("meaningful", "proper", "correct") is None
    assert _failure_class("declined", "no_tool_needed", "correct") is None

  def test_knowledge_gap(self):
    # Looked it up, didn't fabricate, still couldn't answer -> missing fact.
    assert _failure_class("unhelpful", "proper", "correct") == "knowledge_gap"
    assert (
        _failure_class("unhelpful", "proper", "mostly_correct")
        == "knowledge_gap"
    )

  def test_skill_gap_no_tool(self):
    # Didn't even look up -> skill-fixable.
    assert _failure_class("unhelpful", "none", "correct") == "skill_gap"

  def test_skill_gap_hallucinated(self):
    # Used tool but fabricated -> skill-fixable (should have declined).
    assert _failure_class("unhelpful", "proper", "incorrect") == "skill_gap"

  def test_judge_attribution_wins(self):
    # The judge's failure_attribution overrides the deterministic heuristic.
    assert (
        _failure_class("unhelpful", "proper", "correct", "tool_gap")
        == "tool_gap"
    )
    assert (
        _failure_class("unhelpful", "none", "correct", "knowledge_gap")
        == "knowledge_gap"
    )

  def test_judge_not_a_failure_falls_back(self):
    # An unexpected attribution falls back to the deterministic split.
    assert (
        _failure_class("unhelpful", "proper", "correct", "not_a_failure")
        == "knowledge_gap"
    )


class TestClassifyFailures:

  def _session(self, sid, use, tool, corr, question="q"):
    return {
        "session_id": sid,
        "question": question,
        "metrics": {
            "response_usefulness": {"category": use},
            "tool_usage": {"category": tool},
            "correctness": {"category": corr},
        },
    }

  def test_split_and_addressable_rate(self):
    report = {
        "summary": {"total_sessions": 4, "meaningful": 2, "declined": 0},
        "sessions": [
            self._session("s1", "meaningful", "proper", "correct"),
            self._session("s2", "meaningful", "proper", "correct"),
            self._session(
                "s3", "unhelpful", "proper", "correct", "orthodontia?"
            ),
            self._session("s4", "unhelpful", "none", "correct"),
        ],
    }
    _classify_failures(report)
    s = report["summary"]
    assert s["knowledge_gap"] == 1
    assert s["skill_gap"] == 1
    # 2 meaningful / (4 - 1 knowledge gap) = 66.7%
    assert s["addressable_meaningful_rate"] == 66.7
    assert s["knowledge_gap_questions"] == ["orthodontia?"]
    # Per-session tags applied.
    by_id = {
        x["session_id"]: x.get("failure_class") for x in report["sessions"]
    }
    assert by_id["s3"] == "knowledge_gap"
    assert by_id["s4"] == "skill_gap"
    assert by_id["s1"] is None

  def test_no_failures(self):
    report = {
        "summary": {"total_sessions": 1, "meaningful": 1, "declined": 0},
        "sessions": [self._session("s1", "meaningful", "proper", "correct")],
    }
    _classify_failures(report)
    assert report["summary"]["knowledge_gap"] == 0
    assert report["summary"]["skill_gap"] == 0
    assert report["summary"]["tool_gap"] == 0
    assert report["summary"]["addressable_meaningful_rate"] == 100.0

  def test_tool_gap_via_judge(self):
    # With failure_attribution present, tool gaps are excluded from addressable.
    sess = self._session("s1", "unhelpful", "none", "correct", "tuition?")
    sess["metrics"]["failure_attribution"] = {"category": "tool_gap"}
    report = {
        "summary": {"total_sessions": 2, "meaningful": 1, "declined": 0},
        "sessions": [
            self._session("s0", "meaningful", "proper", "correct"),
            sess,
        ],
    }
    _classify_failures(report)
    s = report["summary"]
    assert s["tool_gap"] == 1
    assert s["skill_gap"] == 0
    assert s["tool_gap_questions"] == ["tuition?"]
    # 1 meaningful / (2 - 1 tool gap) = 100%
    assert s["addressable_meaningful_rate"] == 100.0


# ================================================================== #
# _load_eval_spec                                                     #
# ================================================================== #


class TestLoadEvalSpec:

  def setup_method(self):
    _EVAL_SPEC_CACHE.clear()

  def teardown_method(self):
    _EVAL_SPEC_CACHE.clear()

  def test_explicit_path(self):
    import json as _json

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
      _json.dump({"scope": "HR only"}, f)
      path = f.name
    try:
      result = _load_eval_spec(path)
      assert result == {"scope": "HR only"}
    finally:
      os.unlink(path)

  def test_none_string_disables(self):
    assert _load_eval_spec("none") is None

  def test_missing_explicit_path_raises(self):
    import pytest

    with pytest.raises(FileNotFoundError):
      _load_eval_spec("/nonexistent/eval_spec.json")

  def test_cache_hit(self):
    import json as _json

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
      _json.dump({"cached": True}, f)
      path = f.name
    try:
      first = _load_eval_spec(path)
      second = _load_eval_spec(path)
      assert first is second
    finally:
      os.unlink(path)

  def test_cache_isolates_paths(self):
    import json as _json

    paths = []
    for content in [{"a": 1}, {"b": 2}]:
      with tempfile.NamedTemporaryFile(
          mode="w", suffix=".json", delete=False
      ) as f:
        _json.dump(content, f)
        paths.append(f.name)
    try:
      c1 = _load_eval_spec(paths[0])
      c2 = _load_eval_spec(paths[1])
      assert c1 != c2
      assert c1 == {"a": 1}
      assert c2 == {"b": 2}
    finally:
      for p in paths:
        os.unlink(p)

  def test_auto_discover_returns_none_or_dict(self):
    # With no eval_spec.json in known locations, returns None; otherwise dict.
    result = _load_eval_spec(None)
    assert result is None or isinstance(result, dict)


# ================================================================== #
# _count_trace_metrics                                                #
# ================================================================== #


class TestCountTraceMetrics:

  def test_counts_user_messages_and_tools(self):
    trace = _FakeTrace(
        [
            _FakeSpan("USER_MESSAGE_RECEIVED", {"text": "Q1"}),
            _FakeSpan("LLM_RESPONSE", {"response": "A1"}),
            _FakeSpan("TOOL_COMPLETED", {"tool": "search"}),
            _FakeSpan("USER_MESSAGE_RECEIVED", {"text": "Q2"}),
            _FakeSpan("TOOL_COMPLETED", {"tool": "lookup"}),
        ]
    )
    user_turns, tool_calls = _count_trace_metrics(trace)
    assert user_turns == 2
    assert tool_calls == 2

  def test_empty_trace(self):
    trace = _FakeTrace([])
    user_turns, tool_calls = _count_trace_metrics(trace)
    assert user_turns == 0
    assert tool_calls == 0

  def test_single_turn_no_tools(self):
    trace = _FakeTrace(
        [
            _FakeSpan("USER_MESSAGE_RECEIVED", {"text": "Q"}),
            _FakeSpan("LLM_RESPONSE", {"response": "A"}),
        ]
    )
    user_turns, tool_calls = _count_trace_metrics(trace)
    assert user_turns == 1
    assert tool_calls == 0

  def test_tool_starting_not_counted(self):
    trace = _FakeTrace(
        [
            _FakeSpan("TOOL_STARTING", {"tool": "search"}),
            _FakeSpan("TOOL_COMPLETED", {"tool": "search"}),
        ]
    )
    _, tool_calls = _count_trace_metrics(trace)
    assert tool_calls == 1

  def test_tool_error_counted(self):
    trace = _FakeTrace(
        [
            _FakeSpan("TOOL_STARTING", {"tool": "search"}),
            _FakeSpan("TOOL_ERROR", {"error": "timeout"}),
            _FakeSpan("TOOL_STARTING", {"tool": "lookup"}),
            _FakeSpan("TOOL_COMPLETED", {"tool": "lookup"}),
        ]
    )
    _, tool_calls = _count_trace_metrics(trace)
    assert tool_calls == 2


# ================================================================== #
# _compute_dimension_averages                                         #
# ================================================================== #


class TestComputeDimensionAverages:

  def test_basic_averages(self):
    sessions = [
        _FakeSession(
            "s1",
            [
                _FakeMetric("correctness", "correct"),
                _FakeMetric("tool_usage", "proper"),
                _FakeMetric("specificity", "specific"),
                _FakeMetric("scope_compliance", "compliant"),
                _FakeMetric("first_time_right", "correct"),
            ],
        ),
        _FakeSession(
            "s2",
            [
                _FakeMetric("correctness", "incorrect"),
                _FakeMetric("tool_usage", "none"),
                _FakeMetric("specificity", "vague"),
                _FakeMetric("scope_compliance", "non_compliant"),
                _FakeMetric("first_time_right", "correction_needed"),
            ],
        ),
    ]
    report = _FakeReport(sessions)
    avgs = _compute_dimension_averages(report)
    assert avgs["correctness"] == 1.0  # (2+0)/2
    assert avgs["tool_usage"] == 1.0
    assert avgs["specificity"] == 1.0
    assert avgs["scope_compliance"] == 1.0
    assert avgs["first_time_right"] == 1.0

  def test_all_perfect(self):
    sessions = [
        _FakeSession(
            "s1",
            [
                _FakeMetric("correctness", "correct"),
                _FakeMetric("specificity", "specific"),
            ],
        ),
    ]
    report = _FakeReport(sessions)
    avgs = _compute_dimension_averages(report)
    assert avgs["correctness"] == 2.0
    assert avgs["specificity"] == 2.0

  def test_empty_report(self):
    report = _FakeReport([])
    avgs = _compute_dimension_averages(report)
    assert all(v == 0 for v in avgs.values())

  def test_missing_dimensions(self):
    sessions = [
        _FakeSession(
            "s1",
            [_FakeMetric("response_usefulness", "meaningful")],
        ),
    ]
    report = _FakeReport(sessions)
    avgs = _compute_dimension_averages(report)
    # Non-dimension metrics should not contribute
    assert avgs["correctness"] == 0

  def test_parse_error_skipped(self):
    sessions = [
        _FakeSession(
            "s1",
            [
                _FakeMetric("correctness", "correct"),
                _FakeMetric("correctness", "incorrect", parse_error=True),
            ],
        ),
    ]
    report = _FakeReport(sessions)
    avgs = _compute_dimension_averages(report)
    assert avgs["correctness"] == 2.0

  def test_unknown_category_skipped(self):
    sessions = [
        _FakeSession(
            "s1",
            [
                _FakeMetric("correctness", "correct"),
                _FakeMetric("correctness", "bogus_value"),
            ],
        ),
    ]
    report = _FakeReport(sessions)
    avgs = _compute_dimension_averages(report)
    assert avgs["correctness"] == 2.0

  def test_tool_usage_no_tool_needed_scores_full(self):
    # A correct decline / direct answer where no tool was needed must score 2
    # on tool_usage, not be penalised as a Tool Usage failure (PR #174 P1).
    sessions = [
        _FakeSession("s1", [_FakeMetric("tool_usage", "no_tool_needed")]),
    ]
    avgs = _compute_dimension_averages(_FakeReport(sessions))
    assert avgs["tool_usage"] == 2.0

  def test_tool_usage_no_tool_needed_does_not_drag_average(self):
    # Mixed batch: one proper tool use, one no-tool-needed decline. Both are
    # correct outcomes, so the Tool Usage average must stay at 2.0.
    sessions = [
        _FakeSession("s1", [_FakeMetric("tool_usage", "proper")]),
        _FakeSession("s2", [_FakeMetric("tool_usage", "no_tool_needed")]),
    ]
    avgs = _compute_dimension_averages(_FakeReport(sessions))
    assert avgs["tool_usage"] == 2.0


# ================================================================== #
# _has_dimension_data                                                 #
# ================================================================== #


class TestHasDimensionData:

  def test_unscored_dimensions_are_not_data(self):
    # --dimensions primary scores no dimension metrics → all-zero averages.
    # These must not be treated as real "everything failed" data.
    avgs = _compute_dimension_averages(
        _FakeReport(
            [
                _FakeSession(
                    "s1", [_FakeMetric("response_usefulness", "meaningful")]
                )
            ]
        )
    )
    assert avgs == {d: 0 for d in avgs}
    assert _has_dimension_data(avgs) is False

  def test_scored_dimensions_are_data(self):
    avgs = _compute_dimension_averages(
        _FakeReport([_FakeSession("s1", [_FakeMetric("tool_usage", "proper")])])
    )
    assert _has_dimension_data(avgs) is True

  def test_empty_dict(self):
    assert _has_dimension_data({}) is False


# ================================================================== #
# _compute_multiturn_stats                                            #
# ================================================================== #


class TestComputeMultiturnStats:

  def test_basic_stats(self):
    resolved = {
        "s1": {"user_turns": 3, "tool_calls": 2},
        "s2": {"user_turns": 1, "tool_calls": 4},
    }
    stats = _compute_multiturn_stats(resolved)
    assert stats["avg_user_turns"] == 2.0
    assert stats["avg_tool_calls"] == 3.0
    assert stats["multi_turn_sessions"] == 1

  def test_empty_map(self):
    result = _compute_multiturn_stats({})
    assert result == {
        "avg_user_turns": 0,
        "avg_tool_calls": 0,
        "multi_turn_sessions": 0,
    }

  def test_all_single_turn(self):
    resolved = {
        "s1": {"user_turns": 1, "tool_calls": 0},
        "s2": {"user_turns": 1, "tool_calls": 1},
    }
    stats = _compute_multiturn_stats(resolved)
    assert stats["avg_user_turns"] == 1.0
    assert stats["multi_turn_sessions"] == 0

  def test_missing_keys_default_zero(self):
    resolved = {"s1": {}, "s2": {"user_turns": 2}}
    stats = _compute_multiturn_stats(resolved)
    assert stats["avg_user_turns"] == 1.0  # (0+2)/2

  def test_corrections_stats_present_for_multiturn(self):
    resolved = {
        "s1": {
            "user_turns": 3,
            "tool_calls": 2,
            "corrections": 1,
            "verifications": 0,
        },
        "s2": {
            "user_turns": 1,
            "tool_calls": 1,
            "corrections": 0,
            "verifications": 0,
        },
    }
    stats = _compute_multiturn_stats(resolved)
    assert stats["multi_turn_sessions"] == 1
    assert "correction_rate" in stats
    assert "verification_rate" in stats
    assert stats["correction_rate"] == 50.0  # 1 of 2 sessions
    assert stats["avg_corrections"] == 0.5  # 1 total / 2 sessions

  def test_corrections_stats_absent_when_all_single_turn(self):
    resolved = {
        "s1": {
            "user_turns": 1,
            "tool_calls": 0,
            "corrections": 0,
            "verifications": 0,
        },
    }
    stats = _compute_multiturn_stats(resolved)
    assert stats["multi_turn_sessions"] == 0
    assert "correction_rate" not in stats


# ---------------------------------------------------------------------------
# _extract_conversation
# ---------------------------------------------------------------------------


class _FakeConvSpan:
  """Minimal span stub for conversation extraction tests."""

  def __init__(self, event_type, content=None, agent=None):
    self.event_type = event_type
    self.content = content
    self.agent = agent


class TestExtractConversation:

  def test_single_turn(self):
    spans = [
        _FakeConvSpan("USER_MESSAGE_RECEIVED", {"text": "Hello"}),
        _FakeConvSpan("LLM_RESPONSE", {"response": "call:transfer_to_agent"}),
        _FakeConvSpan(
            "LLM_RESPONSE", {"response": "Hi there! How can I help?"}
        ),
    ]
    trace = type("T", (), {"spans": spans})()
    conv = _extract_conversation(trace)
    assert len(conv) == 2
    assert conv[0] == {"role": "user", "text": "Hello"}
    assert conv[1]["role"] == "agent"
    assert "Hi there" in conv[1]["text"]

  def test_multi_turn(self):
    spans = [
        _FakeConvSpan("USER_MESSAGE_RECEIVED", {"text": "What is PTO?"}),
        _FakeConvSpan("LLM_RESPONSE", {"response": "call:policy_agent"}),
        _FakeConvSpan("LLM_RESPONSE", {"response": "20 days per year."}),
        _FakeConvSpan("USER_MESSAGE_RECEIVED", {"text": "Are you sure?"}),
        _FakeConvSpan("LLM_RESPONSE", {"response": "Yes, verified."}),
    ]
    trace = type("T", (), {"spans": spans})()
    conv = _extract_conversation(trace)
    assert len(conv) == 4
    assert conv[0]["text"] == "What is PTO?"
    assert conv[1]["text"] == "20 days per year."
    assert conv[2]["text"] == "Are you sure?"
    assert conv[3]["text"] == "Yes, verified."

  def test_empty_trace(self):
    trace = type("T", (), {"spans": []})()
    assert _extract_conversation(trace) == []

  def test_routing_response_skipped(self):
    spans = [
        _FakeConvSpan("USER_MESSAGE_RECEIVED", {"text": "Hello"}),
        _FakeConvSpan("LLM_RESPONSE", {"response": "call:agent_x"}),
    ]
    trace = type("T", (), {"spans": spans})()
    conv = _extract_conversation(trace)
    # Only user turn, no agent response (routing was skipped)
    assert len(conv) == 1
    assert conv[0]["role"] == "user"

  def test_no_user_messages(self):
    spans = [
        _FakeConvSpan("LLM_RESPONSE", {"response": "orphaned response"}),
    ]
    trace = type("T", (), {"spans": spans})()
    assert _extract_conversation(trace) == []


# ---------------------------------------------------------------------------
# Public API (generate_quality_report / print_quality_report)
# ---------------------------------------------------------------------------


class TestPublicAPI:

  def test_generate_quality_report_is_callable(self):
    assert callable(generate_quality_report)
    import inspect

    sig = inspect.signature(generate_quality_report)
    assert "session_ids" in sig.parameters
    assert "model" in sig.parameters

  def test_print_quality_report_minimal(self, capsys):
    report = {
        "summary": {
            "total_sessions": 5,
            "meaningful": 3,
            "declined": 1,
            "partial": 1,
            "unhelpful": 0,
            "meaningful_rate": 80.0,
            "dimension_averages": {"correctness": 1.8},
        },
        "sessions": [],
    }
    print_quality_report(report)
    out = capsys.readouterr().out
    assert "80.0%" in out
    assert "correctness" in out


# ---------------------------------------------------------------------------
# TraceFilter custom_tags JSON path
# Regression guard for the $.labels -> $.custom_tags fix in trace.py: a wrong
# JSON path makes --label filtering silently return nothing, with no error.
# ---------------------------------------------------------------------------


class TestCustomTagsJsonPath:

  def test_custom_labels_uses_custom_tags_json_path(self):
    from bigquery_agent_analytics import TraceFilter

    where, _params = TraceFilter(
        custom_labels={"version": "v1"}
    ).to_sql_conditions()
    assert "$.custom_tags." in where
    assert "$.labels." not in where


# ---------------------------------------------------------------------------
# Failure-attribution gating (_has_failure_attribution_data)
# The failure-cause taxonomy must only render when the metrics that drive it
# were actually scored; otherwise it would default every failure to skill_gap.
# ---------------------------------------------------------------------------


class TestHasFailureAttributionData:

  @staticmethod
  def _report(metric_names):
    """Build a minimal report stub with one session scored on metric_names."""

    metrics = [_FakeMetric(m, "n/a") for m in metric_names]
    return _FakeReport([_FakeSession("s1", metrics)])

  def test_true_with_failure_attribution(self):
    report = self._report(["response_usefulness", "failure_attribution"])
    assert _has_failure_attribution_data(report) is True

  def test_true_with_tool_usage_and_correctness(self):
    report = self._report(["response_usefulness", "tool_usage", "correctness"])
    assert _has_failure_attribution_data(report) is True

  def test_false_with_primary_only(self):
    # --dimensions primary: only the 2 primary metrics scored.
    report = self._report(["response_usefulness", "task_grounding"])
    assert _has_failure_attribution_data(report) is False

  def test_false_with_tool_usage_alone(self):
    # tool_usage without correctness is not enough for the 2-way fallback.
    report = self._report(["response_usefulness", "tool_usage"])
    assert _has_failure_attribution_data(report) is False


class TestEnrichmentBaseFilter:
  """Enrichment bounds mirror the evaluator's effective selector (#385 P2)."""

  def _args(self, **kw):
    import argparse

    defaults = dict(
        app_name="skill-evolution-lab",
        time_period="24h",
        session=None,
        session_ids_file=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)

  def test_window_mode_keeps_time_bound(self):
    base = quality_report_module._enrichment_base_filter(
        self._args(), {"run": "lab_1"}
    )
    assert base.start_time is not None
    assert base.root_agent_name == "skill-evolution-lab"
    assert base.custom_labels == {"run": "lab_1"}

  def test_explicit_sessions_drop_time_bound_keep_scope(self):
    for kw in (
        {"session": "sess-1"},
        {"session_ids_file": "ids.json"},
    ):
      base = quality_report_module._enrichment_base_filter(
          self._args(**kw), {"run": "lab_1"}
      )
      assert base.start_time is None
      assert base.root_agent_name == "skill-evolution-lab"
      assert base.custom_labels == {"run": "lab_1"}
