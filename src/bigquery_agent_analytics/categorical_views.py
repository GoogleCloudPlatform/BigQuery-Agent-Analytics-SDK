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

"""Dashboard views over the categorical evaluation results table.

Creates standard (non-materialized) BigQuery views that deduplicate
the append-only ``categorical_results`` table and provide pre-aggregated
dashboard summaries.

All downstream views read from the dedup base view
(``categorical_results_latest``), never from the raw table.  This
ensures retries and overlapping micro-batch runs do not inflate counts.

Example usage::

    from bigquery_agent_analytics.categorical_views import CategoricalViewManager

    vm = CategoricalViewManager(
        project_id="my-project",
        dataset_id="analytics",
    )
    vm.create_all_views()
    print(vm.get_view_sql("categorical_results_latest"))
"""

from __future__ import annotations

import logging
from typing import Optional

from google.cloud import bigquery

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels
from .categorical_evaluator import DEFAULT_RESULTS_TABLE

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

# ------------------------------------------------------------------ #
# View SQL Templates                                                    #
# ------------------------------------------------------------------ #

# Keyed by (view_suffix, SQL body).  The base dedup view must be
# created first; all others reference it.

_CATEGORICAL_VIEW_DEFS: dict[str, str] = {
    "categorical_results_latest": """\
CREATE OR REPLACE VIEW `{project}.{dataset}.{prefix}categorical_results_latest` AS
WITH ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY session_id, metric_name, COALESCE(prompt_version, '')
      ORDER BY created_at DESC, raw_response DESC
    ) AS _rn
  FROM `{project}.{dataset}.{results_table}`
)
SELECT * EXCEPT(_rn) FROM ranked WHERE _rn = 1
""",
    "categorical_daily_counts": """\
CREATE OR REPLACE VIEW `{project}.{dataset}.{prefix}categorical_daily_counts` AS
SELECT
  DATE(created_at) AS eval_date,
  metric_name,
  category,
  execution_mode,
  COUNT(*) AS session_count
FROM `{project}.{dataset}.{prefix}categorical_results_latest`
GROUP BY 1, 2, 3, 4
""",
    "categorical_hourly_counts": """\
CREATE OR REPLACE VIEW `{project}.{dataset}.{prefix}categorical_hourly_counts` AS
SELECT
  TIMESTAMP_TRUNC(created_at, HOUR) AS eval_hour,
  metric_name,
  category,
  COUNT(*) AS session_count
FROM `{project}.{dataset}.{prefix}categorical_results_latest`
GROUP BY 1, 2, 3
""",
    "categorical_operational_metrics": """\
CREATE OR REPLACE VIEW `{project}.{dataset}.{prefix}categorical_operational_metrics` AS
SELECT
  DATE(created_at) AS eval_date,
  execution_mode,
  endpoint,
  COUNTIF(parse_error) AS parse_errors,
  COUNTIF(NOT passed_validation AND NOT parse_error) AS validation_failures,
  COUNTIF(execution_mode = 'api_fallback') AS fallback_count,
  COUNT(*) AS total,
  SAFE_DIVIDE(COUNTIF(parse_error), COUNT(*)) AS parse_error_rate,
  SAFE_DIVIDE(COUNTIF(execution_mode = 'api_fallback'), COUNT(*)) AS fallback_rate
FROM `{project}.{dataset}.{prefix}categorical_results_latest`
GROUP BY 1, 2, 3
""",
}

# Creation order matters: base view first, then dependents.
_VIEW_CREATION_ORDER = [
    "categorical_results_latest",
    "categorical_daily_counts",
    "categorical_hourly_counts",
    "categorical_operational_metrics",
]


# ------------------------------------------------------------------ #
# CategoricalViewManager                                                #
# ------------------------------------------------------------------ #


class CategoricalViewManager:
  """Manages dashboard BigQuery views over categorical evaluation results.

  Args:
      project_id: Google Cloud project ID.
      dataset_id: BigQuery dataset containing results.
      results_table: Source table name (default ``categorical_results``).
      view_prefix: Optional prefix for view names (e.g. ``"adk_"``).
      location: BigQuery location (e.g. ``"US"`` or ``"us-central1"``).
      bq_client: Optional pre-configured BigQuery client.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      results_table: str = DEFAULT_RESULTS_TABLE,
      view_prefix: str = "",
      location: Optional[str] = None,
      bq_client: Optional[bigquery.Client] = None,
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.results_table = results_table
    self.view_prefix = view_prefix
    self.location = location
    self._bq_client = bq_client
    self._warned_unlabeled_client = False

  @property
  def bq_client(self) -> bigquery.Client:
    if self._bq_client is None:
      self._bq_client = make_bq_client(self.project_id, location=self.location)
    elif isinstance(self._bq_client, bigquery.Client) and not isinstance(
        self._bq_client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._bq_client

  def available_views(self) -> list[str]:
    """Returns the list of available view names (without prefix)."""
    return list(_VIEW_CREATION_ORDER)

  def get_view_sql(self, view_name: str) -> str:
    """Returns the SQL for a single view.

    Args:
        view_name: One of the available view names.

    Returns:
        The CREATE OR REPLACE VIEW SQL statement.

    Raises:
        KeyError: If the view_name is not recognized.
    """
    if view_name not in _CATEGORICAL_VIEW_DEFS:
      raise KeyError(
          f"Unknown view '{view_name}'. " f"Available: {self.available_views()}"
      )
    template = _CATEGORICAL_VIEW_DEFS[view_name]
    return template.format(
        project=self.project_id,
        dataset=self.dataset_id,
        results_table=self.results_table,
        prefix=self.view_prefix,
    )

  def create_view(self, view_name: str) -> None:
    """Creates (or replaces) a single view.

    Args:
        view_name: The view to create.
    """
    sql = self.get_view_sql(view_name)
    full_name = f"{self.view_prefix}{view_name}"
    logger.info(
        "Creating view %s.%s.%s",
        self.project_id,
        self.dataset_id,
        full_name,
    )
    job_config = with_sdk_labels(
        bigquery.QueryJobConfig(), feature="eval-categorical"
    )
    self.bq_client.query(sql, job_config=job_config).result()
    logger.info("View %s created successfully.", full_name)

  def create_all_views(self) -> dict[str, str]:
    """Creates all categorical dashboard views in dependency order.

    Returns:
        A dict mapping view_name to prefixed view name for each
        created view.
    """
    created = {}
    for view_name in _VIEW_CREATION_ORDER:
      try:
        self.create_view(view_name)
        created[view_name] = f"{self.view_prefix}{view_name}"
      except Exception as e:
        logger.error(
            "Failed to create view %s: %s",
            view_name,
            e,
            exc_info=True,
        )
    return created
