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

import os
import sys
import tempfile

# Make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from quality_report import _AGENT_CONFIG_CACHE  # noqa: E402
from quality_report import _build_agent_stats
from quality_report import _build_scope_context
from quality_report import _extract_a2a_text
from quality_report import _group_by_category
from quality_report import _is_single_word_routing
from quality_report import _load_agent_config
from quality_report import get_a2a_response
from quality_report import get_user_input

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

  def __init__(self, metric_name, category):
    self.metric_name = metric_name
    self.category = category


class _FakeSession:

  def __init__(self, session_id, metrics):
    self.session_id = session_id
    self.metrics = metrics


class _FakeReport:

  def __init__(self, session_results):
    self.session_results = session_results


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

  def test_none_config(self):
    assert _build_scope_context(None) == ""

  def test_empty_config(self):
    assert _build_scope_context({}) == ""

  def test_no_oos_topics(self):
    config = {
        "scope_decisions": [
            {"topic": "billing", "decision": "in_scope"},
        ]
    }
    assert _build_scope_context(config) == ""

  def test_single_oos_topic(self):
    config = {
        "scope_decisions": [
            {"topic": "weather", "decision": "out_of_scope"},
        ]
    }
    result = _build_scope_context(config)
    assert "weather" in result
    assert "OUT OF SCOPE" in result

  def test_multiple_oos_topics(self):
    config = {
        "scope_decisions": [
            {"topic": "weather", "decision": "out_of_scope"},
            {"topic": "sports", "decision": "out_of_scope"},
            {"topic": "billing", "decision": "in_scope"},
        ]
    }
    result = _build_scope_context(config)
    assert "weather" in result
    assert "sports" in result
    assert "billing" not in result

  def test_missing_decision_field(self):
    config = {
        "scope_decisions": [
            {"topic": "weather"},
        ]
    }
    assert _build_scope_context(config) == ""


# ================================================================== #
# _load_agent_config                                                  #
# ================================================================== #


class TestLoadAgentConfig:

  def setup_method(self):
    _AGENT_CONFIG_CACHE.clear()

  def teardown_method(self):
    _AGENT_CONFIG_CACHE.clear()

  def test_explicit_path(self):
    import json as _json

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
      _json.dump({"scope_decisions": [{"topic": "t1"}]}, f)
      path = f.name
    try:
      result = _load_agent_config(path)
      assert result == {"scope_decisions": [{"topic": "t1"}]}
    finally:
      os.unlink(path)

  def test_missing_explicit_path_raises(self):
    import pytest

    with pytest.raises(FileNotFoundError):
      _load_agent_config("/nonexistent/config.json")

  def test_cache_hit(self):
    import json as _json

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
      _json.dump({"cached": True}, f)
      path = f.name
    try:
      first = _load_agent_config(path)
      second = _load_agent_config(path)
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
      c1 = _load_agent_config(paths[0])
      c2 = _load_agent_config(paths[1])
      assert c1 != c2
      assert c1 == {"a": 1}
      assert c2 == {"b": 2}
    finally:
      for p in paths:
        os.unlink(p)

  def test_auto_discover_returns_none(self):
    # With no config file in known locations, should return None
    result = _load_agent_config(None)
    # May return None or a config if one exists in the repo
    # Just verify it doesn't raise
    assert result is None or isinstance(result, dict)
