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

"""Static validation checks for eval suites.

Catches common pitfalls such as ambiguous tasks, class imbalance,
suspicious thresholds, duplicate sessions, and saturated tasks.

Example usage::

    from bigquery_agent_analytics import (
        EvalSuite, EvalValidator,
    )

    suite = EvalSuite(name="my_evals")
    # ... add tasks ...
    warnings = EvalValidator.validate_suite(suite)
    for w in warnings:
        print(f"[{w.severity}] {w.task_id}: {w.message}")
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import logging

from .eval_suite import EvalSuite
from .eval_suite import EvalTaskDef

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


@dataclass
class ValidationWarning:
  """A single validation warning."""

  task_id: str
  check_name: str
  message: str
  severity: str = "warning"  # "error" | "warning" | "info"


class EvalValidator:
  """Static validation checks on eval suites."""

  @staticmethod
  def validate_suite(
      suite: EvalSuite,
      pass_history: dict[str, list[bool]] | None = None,
  ) -> list[ValidationWarning]:
    """Runs all validation checks on a suite.

    Args:
        suite: The eval suite to validate.
        pass_history: Optional pass/fail history for
            saturation detection.

    Returns:
        List of ValidationWarning instances.
    """
    tasks = suite.get_tasks()
    warnings: list[ValidationWarning] = []

    warnings.extend(EvalValidator.check_ambiguity(tasks))
    warnings.extend(EvalValidator.check_balance(tasks))
    warnings.extend(EvalValidator.check_threshold_consistency(tasks))
    warnings.extend(EvalValidator.check_duplicate_sessions(tasks))

    if pass_history:
      warnings.extend(EvalValidator.check_saturation(pass_history))

    return warnings

  @staticmethod
  def check_ambiguity(
      tasks: list[EvalTaskDef],
  ) -> list[ValidationWarning]:
    """Flags tasks missing both expected trajectory and response.

    Args:
        tasks: List of task definitions to check.

    Returns:
        List of warnings for ambiguous tasks.
    """
    warnings: list[ValidationWarning] = []
    for task in tasks:
      if task.expected_trajectory is None and task.expected_response is None:
        warnings.append(
            ValidationWarning(
                task_id=task.task_id,
                check_name="ambiguity",
                message=(
                    "Task has no expected_trajectory and no"
                    " expected_response. Evaluation may be"
                    " unreliable."
                ),
                severity="warning",
            )
        )
    return warnings

  @staticmethod
  def check_balance(
      tasks: list[EvalTaskDef],
  ) -> list[ValidationWarning]:
    """Warns if positive/negative ratio is outside 30-70%.

    Args:
        tasks: List of task definitions to check.

    Returns:
        List of balance warnings.
    """
    warnings: list[ValidationWarning] = []
    if not tasks:
      return warnings

    total = len(tasks)
    positive = sum(1 for t in tasks if t.is_positive_case)
    ratio = positive / total

    if ratio < 0.3:
      warnings.append(
          ValidationWarning(
              task_id="__suite__",
              check_name="balance",
              message=(
                  "Low positive case ratio"
                  f" ({ratio:.0%}). Consider adding more"
                  " positive test cases."
              ),
              severity="warning",
          )
      )
    elif ratio > 0.7:
      warnings.append(
          ValidationWarning(
              task_id="__suite__",
              check_name="balance",
              message=(
                  "High positive case ratio"
                  f" ({ratio:.0%}). Consider adding more"
                  " negative test cases."
              ),
              severity="warning",
          )
      )

    return warnings

  @staticmethod
  def check_threshold_consistency(
      tasks: list[EvalTaskDef],
  ) -> list[ValidationWarning]:
    """Flags tasks with thresholds at exactly 0.0 or 1.0.

    Args:
        tasks: List of task definitions to check.

    Returns:
        List of threshold warnings.
    """
    warnings: list[ValidationWarning] = []
    for task in tasks:
      for metric, threshold in task.thresholds.items():
        if threshold == 0.0:
          warnings.append(
              ValidationWarning(
                  task_id=task.task_id,
                  check_name="threshold_consistency",
                  message=(
                      f"Threshold for '{metric}' is 0.0"
                      " (always passes). Likely a mistake."
                  ),
                  severity="warning",
              )
          )
        elif threshold == 1.0:
          warnings.append(
              ValidationWarning(
                  task_id=task.task_id,
                  check_name="threshold_consistency",
                  message=(
                      f"Threshold for '{metric}' is 1.0"
                      " (requires perfect score). May be"
                      " too strict."
                  ),
                  severity="warning",
              )
          )
    return warnings

  @staticmethod
  def check_duplicate_sessions(
      tasks: list[EvalTaskDef],
  ) -> list[ValidationWarning]:
    """Flags tasks pointing to the same session_id.

    Args:
        tasks: List of task definitions to check.

    Returns:
        List of duplicate session warnings.
    """
    warnings: list[ValidationWarning] = []
    seen: dict[str, list[str]] = {}

    for task in tasks:
      if task.session_id not in seen:
        seen[task.session_id] = []
      seen[task.session_id].append(task.task_id)

    for session_id, task_ids in seen.items():
      if len(task_ids) > 1:
        for task_id in task_ids:
          warnings.append(
              ValidationWarning(
                  task_id=task_id,
                  check_name="duplicate_sessions",
                  message=(
                      f"Session '{session_id}' is shared"
                      " with tasks:"
                      f" {', '.join(task_ids)}."
                  ),
                  severity="info",
              )
          )

    return warnings

  @staticmethod
  def check_saturation(
      pass_history: dict[str, list[bool]],
      min_runs: int = 5,
  ) -> list[ValidationWarning]:
    """Flags tasks at 100% pass rate over recent runs.

    Args:
        pass_history: Mapping of task_id to pass/fail history.
        min_runs: Minimum runs required before checking.

    Returns:
        List of saturation warnings.
    """
    warnings: list[ValidationWarning] = []

    for task_id, history in pass_history.items():
      if len(history) < min_runs:
        continue
      recent = history[-min_runs:]
      if all(recent):
        warnings.append(
            ValidationWarning(
                task_id=task_id,
                check_name="saturation",
                message=(
                    "Task has 100% pass rate over last"
                    f" {min_runs} runs. Consider"
                    " graduating to regression or"
                    " increasing difficulty."
                ),
                severity="info",
            )
        )

    return warnings
