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

"""Backward-compatibility module mapping for evaluators."""

from typing import Optional
from .system_evaluator import *
from .performance_evaluator import *
from .utils import _parse_json_from_text, _extract_json_from_text, strip_markdown_fences
class LLMAsJudge(PerformanceEvaluator):
  """Legacy LLMAsJudge subclass preserving pre-built factories for backwards compatibility."""

  def __init__(self, name: str = "llm_judge", model: Optional[str] = None, threshold: float = 0.5, *args, **kwargs):
    super().__init__(
        project_id=kwargs.get("project_id", "proj"),
        dataset_id=kwargs.get("dataset_id", "ds"),
        llm_judge_model=model,
    )
    self._name = name
    self._threshold = threshold

  @property
  def name(self) -> str:
    return self._name

  @property
  def _criteria(self) -> list:
    class _JudgeCriterion:
      def __init__(self, name: str, threshold: float):
        self.name = name
        self.threshold = threshold
    
    name_map = {
        "correctness_judge": "correctness",
        "hallucination_judge": "faithfulness",
        "sentiment_judge": "sentiment",
    }
    criterion_name = name_map.get(self.name, "correctness")
    return [_JudgeCriterion(name=criterion_name, threshold=self._threshold)]

  @staticmethod
  def correctness(threshold: float = 0.5, model: Optional[str] = None) -> LLMAsJudge:
    return LLMAsJudge(name="correctness_judge", project_id="proj", dataset_id="ds", llm_judge_model=model, threshold=threshold)

  @staticmethod
  def hallucination(threshold: float = 0.5, model: Optional[str] = None) -> LLMAsJudge:
    return LLMAsJudge(name="hallucination_judge", project_id="proj", dataset_id="ds", llm_judge_model=model, threshold=threshold)

  @staticmethod
  def sentiment(threshold: float = 0.5, model: Optional[str] = None) -> LLMAsJudge:
    return LLMAsJudge(name="sentiment_judge", project_id="proj", dataset_id="ds", llm_judge_model=model, threshold=threshold)
