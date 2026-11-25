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

"""The default labeling rubric for ``failure_judge``.

Adapted from the AgentForensics labeling guide (RFC #435), keeping only
what holds for any tool-using agent. Benchmark-specific guidance (how to
treat a known-bad reference answer, sub-answers, harness quirks) belongs
in the caller's ``rubric=`` or in a dossier's ``context``. Every example
is synthetic.
"""

DEFAULT_RUBRIC = """\
# How to label a failed run

## Rules at a glance

1. The run failed. Your job is to say why, not whether. If the final
   response seems to give the expected answer, look at its form: a
   sentence around the value, extra values, units, separators or
   rounding the task did not ask for make an exact-match scorer reject
   it, and that is finalization. Do not answer "unknown" because the
   value looks right.
2. Give one primary category, spelled exactly as listed.
3. Attach it to the first failing step: the earliest step whose own
   action introduced a defect that the agent never repaired and that
   changed the outcome. Not the step where the symptom first shows, and
   not the last step by default.
4. If no step is at fault and the run just ended badly (no final answer,
   the call limit, an answer in the wrong form, an outside abort), the
   target is the gap after the last step: target_kind "gap_after_span"
   and first_error_step null.
5. If two categories fit, take the first "yes" in the ordered questions
   below. Use "unknown" only after walking all seven, and say why.

## Finding the first failing step

The first failing step is the earliest step k such that (1) step k's own
action (its tool call, arguments or code) introduces or commits a
defect, (2) the agent never repairs it, and (3) it changed the outcome.

- Own action, not first symptom. A wrong table picked at step 4 whose
  effect prints at step 6 is step 4.
- An omission anchors on the first step that proceeds without the
  missing piece, usually the first computation. Mention the step where
  the cue was visible in the rationale ("cue at step 2"). The step that
  only delivered a partial view is not at fault.
- A slip the agent repairs within a step or two is not a failing step:
  a name error fixed by the next call, a guessed column name corrected,
  a timeout followed by a faster rewrite.
- A defect copied into every retry is anchored where it first appears.

Typical anchors: task/planning, the first step that commits to the plan
without the requirement; wrong source, the step that loads or selects
the wrong object; execution/computation, the step that first contains
the faulty code; incomplete evidence, the step that acts on the partial
evidence; turn-waste, the first step of the loop; finalization, the gap
after the last step; tool blockers, the first step whose status is
ERROR, else the gap.

## Ordered questions (the tie-break)

Find the earliest defect, then ask in order. The first "yes" wins.

Q1. Was the failure forced from outside the agent: a model API error
    (rate limit, 5xx), a tool or sandbox fault on code any reasonable
    agent would write, or a tool the task needs that is not offered?
    The agent's own tracebacks, error strings and timeouts do not count.
    -> tool blockers
Q2. Does the task state a requirement (a filter, a definition, a time
    window, an aggregation level, a unit, a rounding or output format)
    that the agent's plan ignores or contradicts? -> task/planning
Q3. Did the agent rely on a file, table, column, period or record set
    other than the one the task calls for, when the right one was
    available? -> wrong source
Q4. Did the agent commit to a computation or an answer on evidence it
    could see was partial (a preview, a cut output, one of several
    parts), or stop before reading something it needed?
    -> incomplete evidence
Q5. Were plan, source and evidence fine, but the code or arithmetic
    wrong (types, join keys, name matching, rounding, a silent empty
    result)? -> execution/computation
Q6. Did the agent loop (about four or more consecutive steps adding no
    new information) before the correct value was in hand, with no
    earlier concrete defect, and did the loop use up the budget?
    -> turn-waste
Q7. Is the final response the failure: the correct value was already in
    hand but the response is missing, a sentence instead of the value,
    or in the wrong form; or the run ended without a final response
    although every step was reasonable? -> finalization
None of the above after an honest pass -> unknown, with the reason.

## Boundary tests

- task/planning vs execution/computation: would a flawless
  implementation of the agent's own plan have given the right answer?
  Yes: execution/computation. No: task/planning.
- task/planning vs incomplete evidence: a requirement the plan never
  contained is task/planning; a partial read of the right material is
  incomplete evidence. A condition stated in the task and ignored is
  task/planning even if a cue was on screen.
- wrong source vs incomplete evidence: a different object is wrong
  source; part of the right one is incomplete evidence.
- wrong source vs execution/computation: the wrong object is wrong
  source, even when buggy selection code picked it; the right object
  transformed wrongly is execution/computation.
- execution/computation vs turn-waste: if each step tries something new
  against a concrete bug, it is execution/computation however long the
  run; re-running the same thing with no change of plan is turn-waste.
- finalization vs any upstream category: was the correct value already
  shown by a tool? Yes: finalization. No: the upstream defect. A wrong
  value in a clean form is never finalization.
- tool blockers vs execution/computation: would any reasonable agent
  have hit it? The agent's own errors are execution/computation (or
  nothing, if repaired).
- The answer was never shown: if the code ends without printing and the
  final response gives a value no tool showed, label an earlier concrete
  defect if there is one; otherwise incomplete evidence at the gap, with
  execution/computation as runner-up.

## Observability and confidence

Prefer a defect you can see in the dossier (the code, the arguments,
the printed values, the task's own words) over one you could only infer
from the expected answer or outside knowledge. "clear" means you can
point at the step and say what is wrong with it, and no other category
is a reasonable reading. Otherwise use "judgement call" and name the
runner-up category.

## Examples (synthetic)

- The task asks for the median over 2020-2022. Step 3 computes the
  median over every year in the file. -> task/planning, step 3, clear.
- The task names the monthly sheet. Step 2 loads the yearly sheet and
  every later step uses it. -> wrong source, step 2, clear.
- Step 2 previews the first 5 rows of a 400-row file; step 3 answers by
  summing those 5 rows. -> incomplete evidence, step 3, clear (cue at
  step 2).
- Step 4 joins two tables on a name column whose spellings differ, and
  half the rows drop silently. -> execution/computation, step 4, clear.
- Step 5 prints the correct value 41.7; the final response says "The
  answer is approximately 42 units." -> finalization, gap, clear.
- Every model call after step 3 fails with a rate-limit error and the
  run ends without an answer. -> tool blockers, gap, clear.
"""
