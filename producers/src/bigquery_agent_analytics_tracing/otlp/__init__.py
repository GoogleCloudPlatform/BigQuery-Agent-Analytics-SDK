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

"""OTel-native OTLP receiver storage + decode layer for BQAA (issue #316).

- PR 1: BigQuery schema package — native ``otel_*`` tables, ``agent_events_otlp``
  projection contract, read-time dedup views (``schema`` / ``projection``).
- PR 2: OTLP decode + envelope library — decoded OTLP logs/metrics to envelope
  v1, ``source_position``, per-signal idempotency, dead-letter envelopes
  (``envelope`` / ``decode``).
- PR 3: receiver request handling — auth, decode dispatch, Pub/Sub + DLQ
  routing (``receiver``); WSGI entrypoint in ``app``.

See ``docs/otlp_receiver_design.md``.
"""

from __future__ import annotations

from bigquery_agent_analytics_tracing.otlp.decode import decode_logs_request
from bigquery_agent_analytics_tracing.otlp.decode import decode_metrics_request
from bigquery_agent_analytics_tracing.otlp.envelope import canonical_json
from bigquery_agent_analytics_tracing.otlp.envelope import dead_letter_envelope
from bigquery_agent_analytics_tracing.otlp.envelope import dead_letter_key
from bigquery_agent_analytics_tracing.otlp.envelope import ENVELOPE_VERSION
from bigquery_agent_analytics_tracing.otlp.envelope import log_idempotency_key
from bigquery_agent_analytics_tracing.otlp.envelope import make_envelope
from bigquery_agent_analytics_tracing.otlp.envelope import metric_idempotency_key
from bigquery_agent_analytics_tracing.otlp.envelope import otlp_attrs_to_dict
from bigquery_agent_analytics_tracing.otlp.envelope import raw_preservation
from bigquery_agent_analytics_tracing.otlp.envelope import request_hash
from bigquery_agent_analytics_tracing.otlp.envelope import SourcePosition
from bigquery_agent_analytics_tracing.otlp.envelope import span_idempotency_key
from bigquery_agent_analytics_tracing.otlp.projection import agent_events_otlp_columns
from bigquery_agent_analytics_tracing.otlp.projection import DEDUP_TABLES
from bigquery_agent_analytics_tracing.otlp.projection import dedup_view_sql
from bigquery_agent_analytics_tracing.otlp.projection import missing_agent_events_columns
from bigquery_agent_analytics_tracing.otlp.receiver import authenticate
from bigquery_agent_analytics_tracing.otlp.receiver import decode_body
from bigquery_agent_analytics_tracing.otlp.receiver import DecodeError
from bigquery_agent_analytics_tracing.otlp.receiver import handle_export
from bigquery_agent_analytics_tracing.otlp.receiver import Publisher
from bigquery_agent_analytics_tracing.otlp.receiver import ReceiverConfig
from bigquery_agent_analytics_tracing.otlp.receiver import ReceiverResult
from bigquery_agent_analytics_tracing.otlp.receiver import route_envelopes
from bigquery_agent_analytics_tracing.otlp.receiver import SIGNAL_PATHS
from bigquery_agent_analytics_tracing.otlp.schema import METRIC_TABLES
from bigquery_agent_analytics_tracing.otlp.schema import NATIVE_TABLES
from bigquery_agent_analytics_tracing.otlp.schema import OTEL_SCHEMA_VERSION
from bigquery_agent_analytics_tracing.otlp.schema import table_labels

__all__ = [
    # schema (PR 1)
    "OTEL_SCHEMA_VERSION",
    "NATIVE_TABLES",
    "METRIC_TABLES",
    "table_labels",
    "agent_events_otlp_columns",
    "missing_agent_events_columns",
    "dedup_view_sql",
    "DEDUP_TABLES",
    # envelope + decode (PR 2)
    "ENVELOPE_VERSION",
    "SourcePosition",
    "request_hash",
    "canonical_json",
    "otlp_attrs_to_dict",
    "log_idempotency_key",
    "metric_idempotency_key",
    "span_idempotency_key",
    "raw_preservation",
    "make_envelope",
    "dead_letter_envelope",
    "dead_letter_key",
    "decode_logs_request",
    "decode_metrics_request",
    # receiver (PR 3)
    "ReceiverConfig",
    "ReceiverResult",
    "Publisher",
    "DecodeError",
    "authenticate",
    "decode_body",
    "route_envelopes",
    "handle_export",
    "SIGNAL_PATHS",
]
