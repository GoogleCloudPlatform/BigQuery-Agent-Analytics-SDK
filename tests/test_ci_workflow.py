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

"""Tests that CI runs the BQCA sync_generation gate in a required check.

`validate_contracts.py --profile bqca --previous-binding PATH` is the only
check that the BQCA binding's `sync_generation` never moves back. A binding
that sets it back to an earlier sync, whose committed receipt it still holds,
passes the validator on its own; only the comparison with the previous
binding refuses it. A failing step blocks a merge only when its job is a
status check that main's ruleset requires, so the gate has to run in one of
those jobs, with nothing there that can skip it.
"""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
VALIDATOR = "dashboards/looker_studio/tools/validate_contracts.py"
GATE_FLAG = "--previous-binding"

# The status checks that main's ruleset requires: "Format check" and one
# "Test (Python 3.x)" per entry of the test job's matrix.
REQUIRED_CHECK = "Format check"
REQUIRED_CHECK_PREFIX = "Test ("


def _is_required(check_name):
  return check_name == REQUIRED_CHECK or check_name.startswith(
      REQUIRED_CHECK_PREFIX
  )


def _is_gate(step):
  run = step.get("run") or ""
  return VALIDATOR in run and GATE_FLAG in run


def _gate_jobs():
  """Maps the check name of each ci.yml job that runs the gate to the job."""
  jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
  return {
      # A job's check run takes the job's `name`, or else its id.
      job.get("name", job_id): job
      for job_id, job in jobs.items()
      if any(_is_gate(step) for step in job.get("steps") or [])
  }


def _required_gate_jobs():
  required = {
      name: job for name, job in _gate_jobs().items() if _is_required(name)
  }
  assert required, "no required status check runs the sync_generation gate"
  return required


def test_a_required_status_check_runs_the_sync_generation_gate():
  gate_jobs = _gate_jobs()

  assert gate_jobs, f"no ci.yml job runs {VALIDATOR} {GATE_FLAG}"
  assert any(_is_required(name) for name in gate_jobs), (
      f"only {sorted(gate_jobs)} run {VALIDATOR} {GATE_FLAG}, and none of"
      f" them is a required status check ({REQUIRED_CHECK!r} or"
      f" {REQUIRED_CHECK_PREFIX!r}...), so a failing sync_generation gate"
      " does not block a merge"
  )


def test_the_gate_job_checks_out_the_full_history():
  """The previous binding is read at an earlier commit, so it must exist."""
  for name, job in _required_gate_jobs().items():
    steps = job["steps"]
    gate = next(index for index, step in enumerate(steps) if _is_gate(step))
    checkouts = [
        step
        for step in steps[:gate]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts, f"{name!r} runs the gate before any checkout"
    for checkout in checkouts:
      assert str((checkout.get("with") or {}).get("fetch-depth")) == "0", (
          f"{name!r} must check out with fetch-depth: 0: the gate reads the"
          " binding at the merge base with the base branch, or at the commit"
          " before the push, and a shallow clone has neither"
      )


def test_nothing_in_the_required_job_can_skip_or_soften_the_gate():
  """A job skipped by `if:` or a failed `needs:` passes its required check."""
  for name, job in _required_gate_jobs().items():
    assert "needs" not in job, f"{name!r} must not wait on other jobs"
    assert "if" not in job, f"{name!r} must not be conditional"
    assert not job.get("continue-on-error"), name
    for step in job["steps"]:
      if _is_gate(step):
        assert "if" not in step, f"{name!r} must not make the gate conditional"
        assert not step.get("continue-on-error"), name


def test_the_gate_compares_with_the_base_branch_or_the_commit_before_a_push():
  """A pull request compares with its merge base, a push with `before`."""
  for name, job in _required_gate_jobs().items():
    for step in job["steps"]:
      if not _is_gate(step):
        continue
      env = step.get("env") or {}

      assert env.get("BASE_REF") == "${{ github.base_ref }}", name
      assert env.get("BEFORE") == "${{ github.event.before }}", name
      assert "$BASE_REF" in step["run"] and "$BEFORE" in step["run"], name
