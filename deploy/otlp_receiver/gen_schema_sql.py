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

"""Emit the OTel-native BigQuery DDL for a dataset (issue #316, PR 5).

Single source of truth: the DDL is generated from the schema package, so what
``setup.sh`` creates always matches what the writer expects.

Usage::

    python gen_schema_sql.py <dataset> [--enable-spans] > schema.sql
    bq query --use_legacy_sql=false --project_id=<project> < schema.sql
"""

from __future__ import annotations

import argparse

from bigquery_agent_analytics_tracing.otlp import ddl
from bigquery_agent_analytics_tracing.otlp import sql


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("dataset", help="BigQuery dataset (e.g. agent_analytics)")
  parser.add_argument("--enable-spans", action="store_true")
  parser.add_argument(
      "--merge-only",
      action="store_true",
      help="emit only the agent_events_otlp MERGE (for the scheduled query)",
  )
  args = parser.parse_args()

  if args.merge_only:
    print(sql.agent_events_otlp_merge_sql(args.dataset))
    return
  print(ddl.create_all_sql(args.dataset, enable_spans=args.enable_spans))


if __name__ == "__main__":
  main()
