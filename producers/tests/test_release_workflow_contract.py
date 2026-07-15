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

"""Workflow-level contract for the policy-token mint (#356 round 15).

`complete` also covers the already-published idempotent rerun, whose
helper path performs NO Administration:read policy lookup — so the App
token must be minted ONLY for `complete && draft`. Minting it for every
complete state would let missing/rotated App credentials break a
harmless rerun and mask the helper's burn guidance for an
exact-but-public mutable release. These tests pin the workflow wiring
itself (text-level, like the executed regen-locks sections) so a
refactor cannot silently widen the mint condition."""

import pathlib
import re

_WORKFLOW = (
    pathlib.Path(__file__).parent.parent.parent
    / ".github/workflows/release-tracing.yml"
).read_text()


def _policy_token_step() -> str:
  """The mint step's block: from its name line to the next step."""
  start = _WORKFLOW.index("- name: Mint the policy-read token")
  end = _WORKFLOW.index("- name:", start + 1)
  return _WORKFLOW[start:end]


def test_policy_token_is_minted_only_for_a_draft_publication():
  step = _policy_token_step()
  assert "id: policy_token" in step
  assert "if: env.state == 'complete' && env.RELEASE_IS_DRAFT == '1'" in step
  # The condition guards the App-token action itself, not some other
  # step that happens to share the block.
  assert "uses: actions/create-github-app-token@" in step


def test_release_visibility_is_exported_by_reconciliation():
  # The default is NOT-a-draft, flipped to 1 only on a Boolean-checked
  # draft flag — the same fail-closed classification the reconciler
  # args use.
  assert 'echo "RELEASE_IS_DRAFT=0" >> "$GITHUB_ENV"' in _WORKFLOW
  assert 'echo "RELEASE_IS_DRAFT=1" >> "$GITHUB_ENV"' in _WORKFLOW
  default = _WORKFLOW.index('echo "RELEASE_IS_DRAFT=0"')
  draft = _WORKFLOW.index('echo "RELEASE_IS_DRAFT=1"')
  assert default < draft, "the not-a-draft default must be written first"
  # The =1 write sits inside the Boolean draft classification.
  window = _WORKFLOW[draft - 400 : draft]
  assert 'DRAFT_RAW" = "true"' in window.replace("$", "")


def test_publish_step_consumes_the_minted_token_by_env_name():
  assert "ADMIN_GH_TOKEN: ${{ steps.policy_token.outputs.token }}" in _WORKFLOW
  assert "--admin-token-env ADMIN_GH_TOKEN" in _WORKFLOW
  # The mint step precedes the publish helper invocation.
  assert _WORKFLOW.index(
      "- name: Mint the policy-read token"
  ) < _WORKFLOW.index("publish_release_body.py")


def test_mint_condition_is_not_widened_elsewhere():
  # Exactly one App-token mint exists, and no other step carries a
  # bare `env.state == 'complete'` guard that would re-introduce the
  # round-15 defect under a different id.
  assert _WORKFLOW.count("uses: actions/create-github-app-token@") == 1
  for match in re.finditer(r"if: env\.state == 'complete'(.*)", _WORKFLOW):
    assert "env.RELEASE_IS_DRAFT == '1'" in match.group(0)
