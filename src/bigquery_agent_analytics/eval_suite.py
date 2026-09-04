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

"""Eval suite lifecycle management.

Manages collections of evaluation tasks with lifecycle operations
including tagging, graduation from capability to regression,
saturation detection, and balance checking.

Example usage::

    from bigquery_agent_analytics import (
        EvalCategory, EvalSuite, EvalTaskDef,
    )

    suite = EvalSuite(name="my_agent_evals")
    suite.add_task(EvalTaskDef(
        task_id="t1",
        session_id="sess-123",
        description="Test basic search",
        expected_trajectory=[{"tool_name": "search", "args": {}}],
    ))

    health = suite.check_health()
    print(health.warnings)
"""

from __future__ import annotations

from enum import Enum
import json
import logging
from typing import Any, Optional

from pydantic import BaseModel
from pydantic import Field

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Data Models                                                          #
# ------------------------------------------------------------------ #


class EvalCategory(str, Enum):
  """Category of an evaluation task."""

  CAPABILITY = "capability"
  REGRESSION = "regression"


class EvalTaskDef(BaseModel):
  """Definition of a single evaluation task."""

  task_id: str = Field(description="Unique task identifier.")
  session_id: str = Field(description="Session ID to evaluate.")
  description: str = Field(
      default="",
      description="Human-readable task description.",
  )
  category: EvalCategory = Field(
      default=EvalCategory.CAPABILITY,
      description="Task category.",
  )
  expected_trajectory: Optional[list[dict[str, Any]]] = Field(
      default=None,
      description="Expected tool call sequence.",
  )
  expected_response: Optional[str] = Field(
      default=None,
      description="Expected final response.",
  )
  thresholds: dict[str, float] = Field(
      default_factory=dict,
      description="Metric thresholds for pass/fail.",
  )
  tags: list[str] = Field(
      default_factory=list,
      description="Arbitrary tags for filtering.",
  )
  is_positive_case: bool = Field(
      default=True,
      description="False for negative test cases.",
  )


class SuiteHealth(BaseModel):
  """Health metrics for an eval suite."""

  total_tasks: int = Field(default=0)
  capability_tasks: int = Field(default=0)
  regression_tasks: int = Field(default=0)
  positive_cases: int = Field(default=0)
  negative_cases: int = Field(default=0)
  saturated_task_ids: list[str] = Field(
      default_factory=list,
      description="Tasks at 100% pass rate.",
  )
  balance_ratio: float = Field(
      default=0.0,
      description="Positive / total ratio.",
  )
  warnings: list[str] = Field(
      default_factory=list,
      description="Health warnings.",
  )


# ------------------------------------------------------------------ #
# EvalSuite                                                            #
# ------------------------------------------------------------------ #


class EvalSuite:
  """Manages a collection of evaluation tasks with lifecycle ops.

  Supports adding/removing tasks, filtering by category or tags,
  checking suite health, graduating tasks from capability to
  regression, and serializing to/from JSON.
  """

  def __init__(
      self,
      name: str,
      tasks: list[EvalTaskDef] | None = None,
  ) -> None:
    """Initializes the eval suite.

    Args:
        name: Name of the suite.
        tasks: Optional initial list of tasks.
    """
    self.name = name
    self._tasks: dict[str, EvalTaskDef] = {}
    if tasks:
      for task in tasks:
        self._tasks[task.task_id] = task

  def add_task(self, task: EvalTaskDef) -> None:
    """Adds a task to the suite.

    Args:
        task: The task definition to add.

    Raises:
        ValueError: If a task with the same ID already exists.
    """
    if task.task_id in self._tasks:
      raise ValueError(f"Task '{task.task_id}' already exists in suite.")
    self._tasks[task.task_id] = task

  def remove_task(self, task_id: str) -> None:
    """Removes a task from the suite.

    Args:
        task_id: The ID of the task to remove.

    Raises:
        KeyError: If the task does not exist.
    """
    if task_id not in self._tasks:
      raise KeyError(f"Task '{task_id}' not found in suite.")
    del self._tasks[task_id]

  def get_tasks(
      self,
      category: EvalCategory | None = None,
      tags: list[str] | None = None,
  ) -> list[EvalTaskDef]:
    """Returns tasks matching the given filters.

    Args:
        category: Filter by category (None = all).
        tags: Filter by tags (tasks must have ALL tags).

    Returns:
        List of matching tasks.
    """
    results = list(self._tasks.values())

    if category is not None:
      results = [t for t in results if t.category == category]

    if tags:
      tag_set = set(tags)
      results = [t for t in results if tag_set.issubset(set(t.tags))]

    return results

  def check_health(
      self,
      pass_history: dict[str, list[bool]] | None = None,
  ) -> SuiteHealth:
    """Computes health metrics for the suite.

    Args:
        pass_history: Optional mapping of task_id to list of
            recent pass/fail booleans for saturation detection.

    Returns:
        SuiteHealth with metrics and warnings.
    """
    tasks = list(self._tasks.values())
    total = len(tasks)

    if total == 0:
      return SuiteHealth(
          warnings=["Suite is empty."],
      )

    capability = sum(1 for t in tasks if t.category == EvalCategory.CAPABILITY)
    regression = sum(1 for t in tasks if t.category == EvalCategory.REGRESSION)
    positive = sum(1 for t in tasks if t.is_positive_case)
    negative = total - positive

    balance_ratio = positive / total if total > 0 else 0.0

    # Saturation detection
    saturated: list[str] = []
    if pass_history:
      for task_id, history in pass_history.items():
        if task_id in self._tasks and history and all(history):
          saturated.append(task_id)

    # Warnings
    warnings: list[str] = []
    if balance_ratio < 0.3:
      warnings.append(
          f"Low positive case ratio ({balance_ratio:.0%})."
          " Consider adding more positive test cases."
      )
    if balance_ratio > 0.7:
      warnings.append(
          f"High positive case ratio ({balance_ratio:.0%})."
          " Consider adding more negative test cases."
      )
    if saturated:
      warnings.append(
          f"{len(saturated)} task(s) saturated at 100% pass"
          " rate. Consider graduating or replacing them."
      )

    # Check for missing expectations
    missing_expectations = [
        t.task_id
        for t in tasks
        if (t.expected_trajectory is None and t.expected_response is None)
    ]
    if missing_expectations:
      warnings.append(
          f"{len(missing_expectations)} task(s) missing both"
          " expected_trajectory and expected_response."
      )

    return SuiteHealth(
        total_tasks=total,
        capability_tasks=capability,
        regression_tasks=regression,
        positive_cases=positive,
        negative_cases=negative,
        saturated_task_ids=saturated,
        balance_ratio=balance_ratio,
        warnings=warnings,
    )

  def graduate_to_regression(self, task_id: str) -> None:
    """Moves a task from CAPABILITY to REGRESSION.

    Args:
        task_id: The task to graduate.

    Raises:
        KeyError: If the task does not exist.
        ValueError: If the task is already REGRESSION.
    """
    if task_id not in self._tasks:
      raise KeyError(f"Task '{task_id}' not found in suite.")
    task = self._tasks[task_id]
    if task.category == EvalCategory.REGRESSION:
      raise ValueError(f"Task '{task_id}' is already REGRESSION.")
    task.category = EvalCategory.REGRESSION

  def auto_graduate(
      self,
      pass_history: dict[str, list[bool]],
      threshold_runs: int = 10,
  ) -> list[str]:
    """Auto-graduates tasks that consistently pass.

    Tasks that have passed all of the last ``threshold_runs``
    trials are moved from CAPABILITY to REGRESSION.

    Args:
        pass_history: Mapping of task_id to pass/fail history.
        threshold_runs: Minimum consecutive passes required.

    Returns:
        List of graduated task IDs.
    """
    graduated: list[str] = []

    for task_id, history in pass_history.items():
      if task_id not in self._tasks:
        continue
      task = self._tasks[task_id]
      if task.category != EvalCategory.CAPABILITY:
        continue
      if len(history) < threshold_runs:
        continue
      recent = history[-threshold_runs:]
      if all(recent):
        task.category = EvalCategory.REGRESSION
        graduated.append(task_id)

    return graduated

  def to_eval_dataset(
      self,
      category: EvalCategory | None = None,
  ) -> list[dict[str, Any]]:
    """Converts to the eval dataset format for batch evaluation.

    Produces the ``list[dict]`` format accepted by
    ``BigQueryTraceEvaluator.evaluate_batch()``.

    Args:
        category: Filter by category (None = all).

    Returns:
        List of dicts with session_id, expected_trajectory, etc.
    """
    tasks = self.get_tasks(category=category)
    dataset = []
    for t in tasks:
      item: dict[str, Any] = {
          "session_id": t.session_id,
      }
      if t.expected_trajectory is not None:
        item["expected_trajectory"] = t.expected_trajectory
      if t.expected_response is not None:
        item["expected_response"] = t.expected_response
      if t.description:
        item["task_description"] = t.description
      if t.thresholds:
        item["thresholds"] = t.thresholds
      dataset.append(item)
    return dataset

  def to_json(self) -> str:
    """Serializes the suite to a JSON string.

    Returns:
        JSON string representation.
    """
    data = {
        "name": self.name,
        "tasks": [t.model_dump(mode="json") for t in self._tasks.values()],
    }
    return json.dumps(data, indent=2)

  @classmethod
  def from_json(cls, data: str) -> EvalSuite:
    """Deserializes a suite from a JSON string.

    Args:
        data: JSON string to parse.

    Returns:
        EvalSuite instance.
    """
    parsed = json.loads(data)
    tasks = [EvalTaskDef(**task_data) for task_data in parsed.get("tasks", [])]
    return cls(name=parsed["name"], tasks=tasks)
