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


# ------------------------------------------------------------------ #
# build_compile_result_diagnostic                                     #
# ------------------------------------------------------------------ #


class TestCompileResultDiagnostic:
  """Cover every shape ``CompileResult`` can take when ``ok`` is
  False — including the three CompileResult-only failure fields
  (``invalid_identifier`` / ``invalid_event_types`` /
  ``load_error``) that don't surface through any gate's report.

  Most retry-loop failures will be ``invalid_event_types``: parser
  and AST pass, but the LLM declared an ``event_type`` that has no
  matching sample, so ``compile_extractor`` rejects the candidate
  before the smoke gate ever runs.
  """

  def _ok_smoke(self):
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

  def _failing_smoke(self):
    from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

    return SmokeTestReport(
        events_processed=1,
        events_with_exception=1,
        exceptions=("RuntimeError: boom",),
        events_with_wrong_return_type=0,
        wrong_return_types=(),
        events_with_nonempty_result=0,
        min_nonempty_results=1,
        validation_failures=(),
    )

  def test_ok_returns_passthrough_message(self):
    import pathlib

    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult
    from bigquery_agent_analytics.extractor_compilation import Manifest

    manifest = Manifest(
        fingerprint="f" * 64,
        event_types=("bka_decision",),
        module_filename="x.py",
        function_name="extract",
        compiler_package_version="0.0.1",
        template_version="t-1",
        transcript_builder_version="tb-1",
        created_at="2026-05-06T00:00:00Z",
    )
    result = CompileResult(
        manifest=manifest,
        ast_report=AstReport(),
        smoke_report=self._ok_smoke(),
        bundle_dir=pathlib.Path("/tmp/bundle"),
    )
    assert result.ok
    assert build_compile_result_diagnostic(result) == (
        "Compile succeeded (no diagnostic to render)."
    )

  def test_invalid_identifier_renders_compile_error_code(self):
    """The ``invalid_identifier`` field carries the harness's full
    explanation; the diagnostic prepends the stable
    ``CompileError [code=invalid_identifier]:`` prefix the retry
    prompt can grep on."""
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    result = CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_identifier=(
            "module_name='../x' must be a plain Python identifier"
        ),
    )
    assert build_compile_result_diagnostic(result) == (
        "CompileError [code=invalid_identifier]: "
        "module_name='../x' must be a plain Python identifier"
    )

  def test_invalid_event_types_renders_compile_error_code(self):
    """The retry-loop's most common compile-level failure: parser
    and AST pass, but the declared ``event_type`` has no matching
    smoke sample. Surfaces as ``invalid_event_types`` so the LLM
    knows to fix the rule's event_type, not the field mappings."""
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    result = CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_event_types=(
            "declared event_types ['wrong_event'] have no matching sample events"
        ),
    )
    assert build_compile_result_diagnostic(result) == (
        "CompileError [code=invalid_event_types]: "
        "declared event_types ['wrong_event'] have no matching sample events"
    )

  def test_load_error_renders_compile_error_code(self):
    """In-process import path (``isolation=False``) failed to load
    the candidate source. Subprocess mode surfaces this inside the
    smoke report instead, so the in-process path is the one that
    needs a CompileResult-level diagnostic."""
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    result = CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        load_error="SyntaxError: invalid syntax",
    )
    assert build_compile_result_diagnostic(result) == (
        "CompileError [code=load_error]: SyntaxError: invalid syntax"
    )

  def test_ast_failure_falls_through_to_ast_diagnostic(self):
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    ast_report = AstReport(
        failures=(
            AstFailure(
                code="disallowed_import",
                detail="import 'os' not allowlisted",
                line=2,
                col=0,
            ),
        )
    )
    result = CompileResult(
        manifest=None,
        ast_report=ast_report,
        smoke_report=None,
        bundle_dir=None,
    )
    assert build_compile_result_diagnostic(result) == build_ast_diagnostic(
        ast_report
    )

  def test_smoke_failure_falls_through_to_smoke_diagnostic(self):
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import build_smoke_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    smoke = self._failing_smoke()
    result = CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=smoke,
        bundle_dir=None,
    )
    assert build_compile_result_diagnostic(result) == build_smoke_diagnostic(
        smoke
    )

  def test_ast_wins_over_load_error_when_both_populated(self):
    """``compile_extractor`` only sets ``load_error`` *after* AST
    passes, so a real CompileResult never has both populated. But
    a hand-built one with both populated should render the AST
    failure — AST is the earlier pipeline stage, and the compile
    diagnostic's "earliest stage" contract has to hold for the
    public CompileResult shape, not just for canonical pipeline
    output."""
    from bigquery_agent_analytics.extractor_compilation import AstFailure
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_ast_diagnostic
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    ast_report = AstReport(
        failures=(
            AstFailure(
                code="disallowed_name",
                detail="eval not allowlisted",
                line=4,
                col=0,
            ),
        )
    )
    result = CompileResult(
        manifest=None,
        ast_report=ast_report,
        smoke_report=None,
        bundle_dir=None,
        load_error="ImportError: bogus",
    )
    diag = build_compile_result_diagnostic(result)
    assert diag == build_ast_diagnostic(ast_report)
    assert "load_error" not in diag

  def test_compile_level_field_wins_over_smoke_when_both_populated(self):
    """The post-smoke coverage check sets ``invalid_event_types``
    *and* leaves the (passing) smoke_report attached. The
    diagnostic should name the compile-level failure — that's the
    actionable bit; the smoke_report is incidental context."""
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    result = CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=self._ok_smoke(),
        bundle_dir=None,
        invalid_event_types=(
            "declared event_types ['x'] produced no non-empty smoke output"
        ),
    )
    diag = build_compile_result_diagnostic(result)
    assert diag.startswith("CompileError [code=invalid_event_types]:")
    assert "Smoke test" not in diag

  def test_no_field_populated_falls_back_to_unknown(self):
    """Defensive: ``ok=False`` but no failure field is populated
    is a logic-bug shape; rather than render an empty string, the
    diagnostic labels it so the retry loop's feedback isn't
    silently empty."""
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    result = CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
    )
    assert not result.ok
    assert build_compile_result_diagnostic(result) == (
        "CompileError [code=unknown]: compile failed but no "
        "diagnostic field was populated on the CompileResult"
    )


# ------------------------------------------------------------------ #
# build_gate_diagnostic — kind="compile" path                         #
# ------------------------------------------------------------------ #


class TestBuildGateDiagnosticCompileKind:

  def test_dispatches_compile(self):
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_compile_result_diagnostic
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic
    from bigquery_agent_analytics.extractor_compilation import CompileResult

    result = CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_event_types="event_types must be non-empty",
    )
    assert build_gate_diagnostic(
        "compile", result
    ) == build_compile_result_diagnostic(result)

  def test_compile_kind_rejects_non_compile_payload(self):
    """``kind='compile'`` with a bare AstReport (or anything other
    than a CompileResult) raises with the expected-type message,
    so callers can't accidentally route a per-gate report through
    the envelope path."""
    from bigquery_agent_analytics.extractor_compilation import AstReport
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic

    with pytest.raises(TypeError, match="kind='compile' expects CompileResult"):
      build_gate_diagnostic("compile", AstReport())

  def test_unknown_kind_message_lists_compile(self):
    """The error message should advertise the four allowed kinds —
    a caller wiring up the retry loop reads this when they
    misspell ``compile``."""
    from bigquery_agent_analytics.extractor_compilation import build_gate_diagnostic

    with pytest.raises(ValueError, match="'compile'"):
      build_gate_diagnostic("not-a-kind", None)
