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

"""OTel-native OTLP receiver storage layer for BQAA (issue #316).

PR 1 of the receiver: the BigQuery schema package — native ``otel_*`` tables as
source of truth, BQAA ``agent_events_otlp`` projection contract, and read-time
dedup views. See ``docs/otlp_receiver_design.md``.
"""

from __future__ import annotations

from bigquery_agent_analytics_tracing.otlp.projection import agent_events_otlp_columns
from bigquery_agent_analytics_tracing.otlp.projection import DEDUP_TABLES
from bigquery_agent_analytics_tracing.otlp.projection import dedup_view_sql
from bigquery_agent_analytics_tracing.otlp.projection import missing_agent_events_columns
from bigquery_agent_analytics_tracing.otlp.schema import METRIC_TABLES
from bigquery_agent_analytics_tracing.otlp.schema import NATIVE_TABLES
from bigquery_agent_analytics_tracing.otlp.schema import OTEL_SCHEMA_VERSION
from bigquery_agent_analytics_tracing.otlp.schema import table_labels

__all__ = [
    "OTEL_SCHEMA_VERSION",
    "NATIVE_TABLES",
    "METRIC_TABLES",
    "table_labels",
    "agent_events_otlp_columns",
    "missing_agent_events_columns",
    "dedup_view_sql",
    "DEDUP_TABLES",
]
