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

import pytest

# Make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from quality_report import _build_agent_stats  # noqa: E402
from quality_report import _extract_a2a_text
from quality_report import _group_by_category
from quality_report import _is_single_word_routing

# ---------------------------------------------------------------------------
# Lightweight stubs for report objects
# ---------------------------------------------------------------------------


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
    assert groups == {"unhelpful": [], "partial": [], "meaningful": []}
