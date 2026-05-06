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

"""Tests for diagnostic builders (issue #75 PR 4b.2.2.c.1).

The diagnostic strings these builders produce will get embedded
in retry prompts (PR 4b.2.2.c.2). They have to be:

* **Actionable** — surface the stable failure ``code`` and the
  source location / dotted path so the LLM can grep its own
  output.
* **Bounded** — capped at the first ten entries per category;
  tracebacks reduced to their last informative line.
* **Deterministic** — same input report → byte-identical output.

Tests assert on exact strings where the input is hand-built, and
spot-check structure / boundedness for the higher-volume cases.
"""

from __future__ import annotations

import pytest

# ------------------------------------------------------------------ #
# build_plan_parse_diagnostic                                         #
# ------------------------------------------------------------------ #


class TestPlanParseDiagnostic:

  def test_root_path_renders_as_root(self):
    from bigquery_agent_analytics.extractor_compilation import build_plan_parse_diagnostic
    from bigquery_agent_analytics.extractor_compilation import PlanParseError

    err = PlanParseError(
        code="invalid_json", path="", message="payload is not valid JSON"
    )
    assert (
        build_plan_parse_diagnostic(err)
        == "PlanParseError [code=invalid_json] at <root>: payload is not valid JSON"
    )

  def test_simple_path(self):
    from bigquery_agent_analytics.extractor_compilation import build_plan_parse_diagnostic
    from bigquery_agent_analytics.extractor_compilation import PlanParseError

    err = PlanParseError(
        code="missing_required_field",
        path="event_type",
        message="required field 'event_type' is missing",
    )
    assert build_plan_parse_diagnostic(err) == (
        "PlanParseError [code=missing_required_field] at event_type: "
        "required field 'event_type' is missing"
    )

  def test_dotted_path(self):
    from bigquery_agent_analytics.extractor_compilation import build_plan_parse_diagnostic
    from bigquery_agent_analytics.extractor_compilation import PlanParseError

    err = PlanParseError(
        code="wrong_type",
        path="key_field.source_path[1]",
        message="path segment must be a string, got int",
    )
    assert "key_field.source_path[1]" in build_plan_parse_diagnostic(err)


# ------------------------------------------------------------------ #
# build_ast_diagnostic                                                #
# ------------------------------------------------------------------ #


class TestAstDiagnostic:

  def test_clean_report_returns_passthrough_message(self):
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic

    assert build_ast_diagnostic(AstReport()) == (
        "AST validation passed (no diagnostic to render)."
    )

  def test_single_failure_with_line_and_col(self):
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic

    report = AstReport(
        failures=(
            AstFailure(
                code="disallowed_import",
                detail="import from 'os' not allowlisted",
                line=3,
                col=0,
            ),
        )
    )
    diag = build_ast_diagnostic(report)
    assert diag == (
        "AST validation failed (1 issue):\n"
        "  line 3 col 0: disallowed_import: import from 'os' not allowlisted"
    )

  def test_failure_without_col(self):
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic

    report = AstReport(
        failures=(
            AstFailure(
                code="disallowed_while", detail="while loops banned", line=5
            ),
        )
    )
    assert "line 5: disallowed_while" in build_ast_diagnostic(report)

  def test_failure_without_line(self):
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic

    report = AstReport(
        failures=(
            AstFailure(code="disallowed_async", detail="no async allowed"),
        )
    )
    assert "<no-line>: disallowed_async" in build_ast_diagnostic(report)

  def test_multiple_failures_all_listed_in_walk_order(self):
    """``ast.walk`` order is what the validator produces; the
    diagnostic preserves it so the LLM sees failures roughly
    top-to-bottom in its own source."""
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic

    report = AstReport(
        failures=(
            AstFailure(code="disallowed_import", detail="import os", line=1),
            AstFailure(code="disallowed_name", detail="eval used", line=10),
            AstFailure(
                code="disallowed_attribute", detail="dunder __class__", line=15
            ),
        )
    )
    diag = build_ast_diagnostic(report)
    assert "AST validation failed (3 issues):" in diag
    # Order preserved.
    pos_import = diag.index("disallowed_import")
    pos_name = diag.index("disallowed_name")
    pos_attr = diag.index("disallowed_attribute")
    assert pos_import < pos_name < pos_attr

  def test_truncation_at_ten_failures(self):
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic

    failures = tuple(
        AstFailure(code="disallowed_name", detail=f"name #{i}", line=i)
        for i in range(1, 16)  # 15 failures
    )
    diag = build_ast_diagnostic(AstReport(failures=failures))
    assert "AST validation failed (15 issues):" in diag
    assert "name #1" in diag
    assert "name #10" in diag
    # Truncation notice
    assert "... and 5 more (truncated)" in diag
    # Failures 11-15 are NOT listed verbatim
    assert "name #11" not in diag
    assert "name #15" not in diag


# ------------------------------------------------------------------ #
# build_smoke_diagnostic                                              #
# ------------------------------------------------------------------ #


class TestSmokeDiagnostic:

  def _empty_smoke_report(self):
    """Build a clean-passing SmokeTestReport (1 event, no
    failures, 1 nonempty result, floor=1)."""
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    return SmokeTestReport(
        events_processed=1,
        events_with_exception=0,
        exceptions=(),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=1,
        min_nonempty_results=1,
        validation_failures=(),
    )

  def test_clean_report_returns_passthrough_message(self):
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic

    assert build_smoke_diagnostic(self._empty_smoke_report()) == (
        "Smoke test passed (no diagnostic to render)."
    )

  def test_per_event_exceptions_reduce_to_last_line(self):
    """Multi-line tracebacks render only their last informative
    line (the exception type + message)."""
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    multiline_tb = (
        "Traceback (most recent call last):\n"
        '  File "<x>", line 5, in extract_bka\n'
        "    decision_id = content.get('decision_id')\n"
        "AttributeError: 'NoneType' object has no attribute 'get'\n"
    )
    report = SmokeTestReport(
        events_processed=2,
        events_with_exception=1,
        exceptions=(multiline_tb,),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=1,
        min_nonempty_results=1,
        validation_failures=(),
    )
    diag = build_smoke_diagnostic(report)
    # Last line of the traceback shows up
    assert "AttributeError: 'NoneType' object has no attribute 'get'" in diag
    # Earlier scaffolding lines are filtered out
    assert "Traceback (most recent call last):" not in diag
    assert 'File "<x>"' not in diag

  def test_wrong_return_types_section(self):
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    report = SmokeTestReport(
        events_processed=2,
        events_with_exception=0,
        exceptions=(),
        events_with_wrong_return_type=1,
        wrong_return_types=(
            "extractor returned 'dict', expected StructuredExtractionResult",
        ),
        events_with_nonempty_result=1,
        min_nonempty_results=1,
        validation_failures=(),
    )
    diag = build_smoke_diagnostic(report)
    assert "Wrong return types (1 of 2 events):" in diag
    assert "extractor returned 'dict'" in diag

  def test_min_nonempty_floor_section(self):
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    report = SmokeTestReport(
        events_processed=3,
        events_with_exception=0,
        exceptions=(),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=0,
        min_nonempty_results=1,
        validation_failures=(),
    )
    diag = build_smoke_diagnostic(report)
    assert (
        "Non-empty floor: 0 of 3 events produced non-empty output; "
        "required >= 1." in diag
    )

  def test_graph_validator_failures_section(self):
    """``[scope] code at path: detail`` per failure — same shape
    as the validator's own ``ValidationFailure.__str__`` style so
    the LLM can grep its own response."""
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure

    failures = (
        ValidationFailure(
            scope=FallbackScope.NODE,
            code="missing_key",
            path="nodes[0].properties.<key:decision_id>",
            detail="primary-key column 'decision_id' is missing or empty",
        ),
        ValidationFailure(
            scope=FallbackScope.FIELD,
            code="type_mismatch",
            path="nodes[0].properties[1].value",
            detail="value 42 is not a valid string",
        ),
    )
    report = SmokeTestReport(
        events_processed=1,
        events_with_exception=0,
        exceptions=(),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=1,
        min_nonempty_results=1,
        validation_failures=failures,
    )
    diag = build_smoke_diagnostic(report)
    assert "#76 graph validator failures (2):" in diag
    assert (
        "[node] missing_key at nodes[0].properties.<key:decision_id>:" in diag
    )
    assert "[field] type_mismatch at nodes[0].properties[1].value:" in diag

  def test_all_sections_combined(self):
    """A report with every category populated renders all four
    sections in order: exceptions, wrong types, non-empty floor,
    validator failures."""
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure

    report = SmokeTestReport(
        events_processed=4,
        events_with_exception=1,
        exceptions=("RuntimeError: oops",),
        events_with_wrong_return_type=1,
        wrong_return_types=(
            "extractor returned 'list', expected StructuredExtractionResult",
        ),
        events_with_nonempty_result=0,
        min_nonempty_results=1,
        validation_failures=(
            ValidationFailure(
                scope=FallbackScope.EDGE,
                code="unresolved_endpoint",
                path="edges[0].from_node_id",
                detail="from_node_id refers to no node",
            ),
        ),
    )
    diag = build_smoke_diagnostic(report)
    pos_exc = diag.index("Per-event exceptions")
    pos_wrong = diag.index("Wrong return types")
    pos_floor = diag.index("Non-empty floor")
    pos_validator = diag.index("graph validator failures")
    assert pos_exc < pos_wrong < pos_floor < pos_validator

  def test_truncation_at_ten_validator_failures(self):
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure

    failures = tuple(
        ValidationFailure(
            scope=FallbackScope.FIELD,
            code="type_mismatch",
            path=f"nodes[0].properties[{i}].value",
            detail=f"failure #{i}",
        )
        for i in range(15)
    )
    report = SmokeTestReport(
        events_processed=1,
        events_with_exception=0,
        exceptions=(),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=1,
        min_nonempty_results=1,
        validation_failures=failures,
    )
    diag = build_smoke_diagnostic(report)
    assert "graph validator failures (15):" in diag
    assert "failure #0" in diag
    assert "failure #9" in diag
    assert "... and 5 more (truncated)" in diag
    assert "failure #10" not in diag

  def test_empty_traceback_renders_placeholder(self):
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    report = SmokeTestReport(
        events_processed=1,
        events_with_exception=1,
        exceptions=("",),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=1,
        min_nonempty_results=1,
        validation_failures=(),
    )
    diag = build_smoke_diagnostic(report)
    assert "<empty traceback>" in diag


# ------------------------------------------------------------------ #
# build_gate_diagnostic dispatcher                                    #
# ------------------------------------------------------------------ #


class TestBuildGateDiagnostic:

  def test_dispatches_parse(self):
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic
    from bigquery_agent_analytics.extractor_compilation import build_plan_parse_diagnostic
    from bigquery_agent_analytics.extractor_compilation import PlanParseError

    err = PlanParseError(code="invalid_json", path="", message="bad json")
    assert build_gate_diagnostic("parse", err) == build_plan_parse_diagnostic(
        err
    )

  def test_dispatches_ast(self):
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic

    report = AstReport(
        failures=(AstFailure(code="disallowed_name", detail="eval", line=2),)
    )
    assert build_gate_diagnostic("ast", report) == build_ast_diagnostic(report)

  def test_dispatches_smoke(self):
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    report = SmokeTestReport(
        events_processed=1,
        events_with_exception=1,
        exceptions=("RuntimeError: x",),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=0,
        min_nonempty_results=1,
        validation_failures=(),
    )
    assert build_gate_diagnostic("smoke", report) == build_smoke_diagnostic(
        report
    )

  def test_unknown_kind_raises(self):
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic

    with pytest.raises(ValueError, match="unknown gate kind"):
      build_gate_diagnostic("unknown", None)

  def test_payload_type_mismatch_raises(self):
    """Pass a smoke report under kind='ast' — the dispatcher
    raises a clear TypeError naming the expected type."""
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    report = SmokeTestReport(
        events_processed=1,
        events_with_exception=0,
        exceptions=(),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=1,
        min_nonempty_results=1,
        validation_failures=(),
    )
    with pytest.raises(TypeError, match="kind='ast' expects AstReport"):
      build_gate_diagnostic("ast", report)
