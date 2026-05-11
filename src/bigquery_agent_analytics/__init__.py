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

"""BigQuery Agent Analytics SDK.

This package provides the consumption-layer SDK for analyzing agent traces
stored in BigQuery, including:

1. **SDK Client** - High-level Python interface abstracting BigQuery SQL.
2. **Trace Reconstruction** - Retrieve and visualize agent conversation DAGs.
3. **Evaluation Engine** - Code-based and LLM-as-judge evaluation at scale.
4. **Feedback & Curation** - Drift detection and question distribution analysis.
5. **Agent Insights** - Multi-stage pipeline for comprehensive session analysis.
6. **Trace-Based Evaluation Harness** - Trajectory metrics and replay.
7. **Long-Horizon Agent Memory** - Cross-session context and semantic search.
8. **BigQuery AI/ML Integration** - AI.GENERATE, AI.EMBED,
   AI.DETECT_ANOMALIES, AI.FORECAST, with legacy ML.* fallbacks.

Quick start::

    from bigquery_agent_analytics import Client

    client = Client(project_id="my-project", dataset_id="analytics")
    trace = client.get_trace("trace-123")
    trace.render()

    # Generate insights report
    report = client.insights(max_sessions=50)
    print(report.summary())
"""

import logging

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

__all__ = []

# --- Telemetry primitives (always available) ---
# Exposed as public API so operators who want SDK labels on a custom
# bigquery.Client configuration can opt in via make_bq_client, and
# advanced users can pass a LabeledBigQueryClient directly.
from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels

__all__.extend(
    [
        "LabeledBigQueryClient",
        "make_bq_client",
        "with_sdk_labels",
    ]
)

# --- SDK Client & Core ---
try:
  from .client import Client
  from .evaluators import CodeEvaluator
  from .evaluators import EvaluationReport
  from .evaluators import LLMAsJudge
  from .evaluators import SessionScore
  from .feedback import AnalysisConfig
  from .feedback import DriftReport
  from .feedback import QuestionDistribution
  from .formatter import format_output
  from .insights import InsightsConfig
  from .insights import InsightsReport
  from .insights import SessionFacet
  from .serialization import serialize
  from .trace import ContentPart
  from .trace import EventType
  from .trace import ObjectRef
  from .trace import Span
  from .trace import Trace
  from .trace import TraceFilter
  from .views import ViewManager

  __all__.extend(
      [
          "Client",
          "Trace",
          "Span",
          "ContentPart",
          "EventType",
          "ObjectRef",
          "TraceFilter",
          "ViewManager",
          "CodeEvaluator",
          "LLMAsJudge",
          "EvaluationReport",
          "SessionScore",
          "DriftReport",
          "QuestionDistribution",
          "AnalysisConfig",
          "format_output",
          "InsightsReport",
          "InsightsConfig",
          "serialize",
          "SessionFacet",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import SDK client components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# Trace Evaluator
try:
  from .trace_evaluator import BigQueryTraceEvaluator
  from .trace_evaluator import EvaluationResult
  from .trace_evaluator import TraceReplayRunner
  from .trace_evaluator import TrajectoryMetrics

  __all__.extend(
      [
          "BigQueryTraceEvaluator",
          "EvaluationResult",
          "TraceReplayRunner",
          "TrajectoryMetrics",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import trace evaluator components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# Memory Service
try:
  from .memory_service import BigQueryMemoryService
  from .memory_service import BigQuerySessionMemory
  from .memory_service import ContextManager
  from .memory_service import Episode
  from .memory_service import UserProfileBuilder

  __all__.extend(
      [
          "BigQueryMemoryService",
          "BigQuerySessionMemory",
          "ContextManager",
          "Episode",
          "UserProfileBuilder",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import memory service components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# AI/ML Integration
try:
  from .ai_ml_integration import AnomalyDetector
  from .ai_ml_integration import BatchEvaluator
  from .ai_ml_integration import BigQueryAIClient
  from .ai_ml_integration import EmbeddingSearchClient
  from .ai_ml_integration import LatencyForecast

  __all__.extend(
      [
          "BigQueryAIClient",
          "EmbeddingSearchClient",
          "AnomalyDetector",
          "BatchEvaluator",
          "LatencyForecast",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import AI/ML integration components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# Multi-Trial
try:
  from .multi_trial import MultiTrialReport
  from .multi_trial import TrialResult
  from .multi_trial import TrialRunner

  __all__.extend(
      [
          "TrialRunner",
          "TrialResult",
          "MultiTrialReport",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import multi-trial components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# Grader Pipeline
try:
  from .grader_pipeline import AggregateVerdict
  from .grader_pipeline import BinaryStrategy
  from .grader_pipeline import GraderPipeline
  from .grader_pipeline import GraderResult
  from .grader_pipeline import MajorityStrategy
  from .grader_pipeline import ScoringStrategy
  from .grader_pipeline import WeightedStrategy

  __all__.extend(
      [
          "AggregateVerdict",
          "BinaryStrategy",
          "GraderPipeline",
          "GraderResult",
          "MajorityStrategy",
          "ScoringStrategy",
          "WeightedStrategy",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import grader pipeline components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# Eval Suite
try:
  from .eval_suite import EvalCategory
  from .eval_suite import EvalSuite
  from .eval_suite import EvalTaskDef
  from .eval_suite import SuiteHealth

  __all__.extend(
      [
          "EvalCategory",
          "EvalSuite",
          "EvalTaskDef",
          "SuiteHealth",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import eval suite components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# Eval Validator
try:
  from .eval_validator import EvalValidator
  from .eval_validator import ValidationWarning

  __all__.extend(
      [
          "EvalValidator",
          "ValidationWarning",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import eval validator components: %s. "
      "Ensure required dependencies are installed.",
      e,
  )

# Event Semantics
try:
  from .event_semantics import ALL_KNOWN_EVENT_TYPES
  from .event_semantics import ERROR_SQL_PREDICATE
  from .event_semantics import EVENT_FAMILIES
  from .event_semantics import extract_response_text
  from .event_semantics import is_error_event
  from .event_semantics import is_hitl_event
  from .event_semantics import is_tool_event
  from .event_semantics import RESPONSE_EVENT_TYPES
  from .event_semantics import tool_outcome

  __all__.extend(
      [
          "is_error_event",
          "extract_response_text",
          "is_tool_event",
          "tool_outcome",
          "is_hitl_event",
          "ERROR_SQL_PREDICATE",
          "RESPONSE_EVENT_TYPES",
          "EVENT_FAMILIES",
          "ALL_KNOWN_EVENT_TYPES",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import event semantics: %s.",
      e,
  )

# Context Graph
try:
  from .context_graph import BizNode
  from .context_graph import Candidate
  from .context_graph import ContextGraphConfig
  from .context_graph import ContextGraphManager
  from .context_graph import DecisionPoint
  from .context_graph import WorldChangeAlert
  from .context_graph import WorldChangeReport

  __all__.extend(
      [
          "BizNode",
          "Candidate",
          "ContextGraphConfig",
          "ContextGraphManager",
          "DecisionPoint",
          "WorldChangeAlert",
          "WorldChangeReport",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import context graph components: %s.",
      e,
  )

# Categorical Evaluator
try:
  from .categorical_evaluator import CategoricalEvaluationConfig
  from .categorical_evaluator import CategoricalEvaluationReport
  from .categorical_evaluator import CategoricalMetricCategory
  from .categorical_evaluator import CategoricalMetricDefinition
  from .categorical_evaluator import CategoricalMetricResult
  from .categorical_evaluator import CategoricalSessionResult

  __all__.extend(
      [
          "CategoricalEvaluationConfig",
          "CategoricalEvaluationReport",
          "CategoricalMetricCategory",
          "CategoricalMetricDefinition",
          "CategoricalMetricResult",
          "CategoricalSessionResult",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import categorical evaluator components: %s.",
      e,
  )

# Categorical Views
try:
  from .categorical_views import CategoricalViewManager

  __all__.append("CategoricalViewManager")
except ImportError as e:
  logger.debug(
      "Could not import categorical views: %s.",
      e,
  )

# Ontology Models
try:
  from .ontology_models import ExtractedEdge
  from .ontology_models import ExtractedGraph
  from .ontology_models import ExtractedNode
  from .ontology_models import ExtractedProperty

  __all__.extend(
      [
          "ExtractedEdge",
          "ExtractedGraph",
          "ExtractedNode",
          "ExtractedProperty",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import ontology model components: %s. "
      "Ensure pyyaml is installed.",
      e,
  )

# Resolved Spec
try:
  from .resolved_spec import load_resolved_graph

  __all__.append("load_resolved_graph")
except ImportError as e:
  logger.debug(
      "Could not import resolved spec: %s.",
      e,
  )

# Ontology Schema Compiler
try:
  from .ontology_schema_compiler import compile_extraction_prompt
  from .ontology_schema_compiler import compile_output_schema

  __all__.extend(
      [
          "compile_extraction_prompt",
          "compile_output_schema",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import ontology schema compiler: %s.",
      e,
  )

# Ontology Graph Manager
try:
  from .ontology_graph import OntologyGraphManager

  __all__.append("OntologyGraphManager")
except ImportError as e:
  logger.debug(
      "Could not import ontology graph manager: %s.",
      e,
  )

# Ontology Materializer
try:
  from .ontology_materializer import MaterializationResult
  from .ontology_materializer import OntologyMaterializer
  from .ontology_materializer import TableStatus

  __all__.extend(
      ["MaterializationResult", "OntologyMaterializer", "TableStatus"]
  )
except ImportError as e:
  logger.debug(
      "Could not import ontology materializer: %s.",
      e,
  )

# Ontology Property Graph Compiler
try:
  from .ontology_property_graph import can_use_upstream_compiler
  from .ontology_property_graph import compile_ddl_via_upstream
  from .ontology_property_graph import compile_property_graph_ddl
  from .ontology_property_graph import OntologyPropertyGraphCompiler

  __all__.extend(
      [
          "OntologyPropertyGraphCompiler",
          "can_use_upstream_compiler",
          "compile_ddl_via_upstream",
          "compile_property_graph_ddl",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import ontology property graph compiler: %s.",
      e,
  )

# Ontology Orchestrator
try:
  from .ontology_orchestrator import build_ontology_graph
  from .ontology_orchestrator import compile_lineage_gql
  from .ontology_orchestrator import compile_showcase_gql

  __all__.extend(
      [
          "build_ontology_graph",
          "compile_lineage_gql",
          "compile_showcase_gql",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import ontology orchestrator: %s.",
      e,
  )

# V5: Structured Extraction
try:
  from .structured_extraction import extract_bka_decision_event
  from .structured_extraction import run_structured_extractors
  from .structured_extraction import StructuredExtractionResult
  from .structured_extraction import StructuredExtractor

  __all__.extend(
      [
          "StructuredExtractionResult",
          "StructuredExtractor",
          "extract_bka_decision_event",
          "run_structured_extractors",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import structured extraction: %s.",
      e,
  )

# V5: TTL Importer
try:
  from .ttl_importer import import_owl_to_ontology

  __all__.extend(
      [
          "import_owl_to_ontology",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import ttl importer: %s.",
      e,
  )

# V5: Lineage Detection
try:
  from .ontology_graph import detect_lineage_edges

  __all__.append("detect_lineage_edges")
except ImportError as e:
  logger.debug(
      "Could not import lineage detection: %s.",
      e,
  )

# Runtime Spec Adapter (ontology package bridge)
try:
  from .runtime_spec import graph_spec_to_ontology_binding
  from .runtime_spec import LineageEdgeConfig
  from .runtime_spec import resolved_graph_to_ontology_binding

  __all__.extend(
      [
          "LineageEdgeConfig",
          "graph_spec_to_ontology_binding",
          "resolved_graph_to_ontology_binding",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import runtime spec adapter: %s.",
      e,
  )

# BigFrames Evaluator (optional bigframes dependency)
try:
  from .bigframes_evaluator import BigFramesEvaluator

  __all__.append("BigFramesEvaluator")
except ImportError as e:
  logger.debug(
      "Could not import BigFramesEvaluator: %s. "
      "Install bigframes to use this feature.",
      e,
  )

# Graph Validator (issue #76)
try:
  from .graph_validation import FallbackScope
  from .graph_validation import validate_extracted_graph
  from .graph_validation import validate_extracted_graph_from_ontology
  from .graph_validation import ValidationFailure
  from .graph_validation import ValidationReport

  __all__.extend(
      [
          "FallbackScope",
          "ValidationFailure",
          "ValidationReport",
          "validate_extracted_graph",
          "validate_extracted_graph_from_ontology",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import graph validator: %s.",
      e,
  )

# Extractor compilation scaffolding (issue #75 PR 4b.1)
try:
  from .extractor_compilation import AstFailure
  from .extractor_compilation import AstReport
  from .extractor_compilation import AttemptRecord
  from .extractor_compilation import build_ast_diagnostic
  from .extractor_compilation import build_compile_result_diagnostic
  from .extractor_compilation import build_gate_diagnostic
  from .extractor_compilation import build_plan_parse_diagnostic
  from .extractor_compilation import build_resolution_prompt
  from .extractor_compilation import build_retry_prompt
  from .extractor_compilation import build_runtime_extractor_registry
  from .extractor_compilation import build_smoke_diagnostic
  from .extractor_compilation import check_thresholds
  from .extractor_compilation import compile_extractor
  from .extractor_compilation import compile_with_llm
  from .extractor_compilation import CompileMeasurement
  from .extractor_compilation import CompileResult
  from .extractor_compilation import CompileSource
  from .extractor_compilation import compute_fingerprint
  from .extractor_compilation import discover_bundles
  from .extractor_compilation import DiscoveryResult
  from .extractor_compilation import EventTypeCounts
  from .extractor_compilation import FallbackOutcome
  from .extractor_compilation import FieldMapping
  from .extractor_compilation import LLMClient
  from .extractor_compilation import load_bundle
  from .extractor_compilation import LoadedBundle
  from .extractor_compilation import LoadFailure
  from .extractor_compilation import Manifest
  from .extractor_compilation import measure_compile
  from .extractor_compilation import OutcomeCallback
  from .extractor_compilation import parse_resolved_extractor_plan_json
  from .extractor_compilation import PlanParseError
  from .extractor_compilation import PlanResolver
  from .extractor_compilation import render_extractor_source
  from .extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
  from .extractor_compilation import ResolvedExtractorPlan
  from .extractor_compilation import RetryCompileResult
  from .extractor_compilation import revalidate_compiled_extractors
  from .extractor_compilation import RevalidationReport
  from .extractor_compilation import RevalidationThresholds
  from .extractor_compilation import run_smoke_test
  from .extractor_compilation import run_with_fallback
  from .extractor_compilation import SmokeTestReport
  from .extractor_compilation import SpanHandlingRule
  from .extractor_compilation import ThresholdCheckResult
  from .extractor_compilation import validate_source
  from .extractor_compilation import WrappedRegistry

  __all__.extend(
      [
          "AstFailure",
          "AstReport",
          "AttemptRecord",
          "CompileMeasurement",
          "CompileResult",
          "CompileSource",
          "DiscoveryResult",
          "EventTypeCounts",
          "FallbackOutcome",
          "FieldMapping",
          "LoadFailure",
          "LoadedBundle",
          "OutcomeCallback",
          "WrappedRegistry",
          "LLMClient",
          "Manifest",
          "PlanParseError",
          "PlanResolver",
          "RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA",
          "ResolvedExtractorPlan",
          "RetryCompileResult",
          "RevalidationReport",
          "RevalidationThresholds",
          "SmokeTestReport",
          "SpanHandlingRule",
          "ThresholdCheckResult",
          "build_ast_diagnostic",
          "build_compile_result_diagnostic",
          "build_gate_diagnostic",
          "build_plan_parse_diagnostic",
          "build_resolution_prompt",
          "build_retry_prompt",
          "build_smoke_diagnostic",
          "check_thresholds",
          "compile_extractor",
          "compile_with_llm",
          "compute_fingerprint",
          "discover_bundles",
          "load_bundle",
          "measure_compile",
          "parse_resolved_extractor_plan_json",
          "build_runtime_extractor_registry",
          "render_extractor_source",
          "revalidate_compiled_extractors",
          "run_smoke_test",
          "run_with_fallback",
          "validate_source",
      ]
  )
except ImportError as e:
  logger.debug(
      "Could not import extractor compilation scaffolding: %s.",
      e,
  )
