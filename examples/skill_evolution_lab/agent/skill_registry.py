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

"""Minimal REST client for the Gemini Enterprise Agent Platform Skill Registry.

The registry is the versioned store for the agent's SKILL.md: creating a skill
makes revision 1 (V0), and each update is a new immutable revision (V1, ...).
This demo keeps a local SKILL.md as the working copy AND mirrors it to the
registry; reset() restores both to V0.

We use the REST API (with an ADC bearer token) rather than the agentplatform
Python client so the demo runs without pinning google-cloud-aiplatform >= 1.154
or a preview ADK build. Endpoints follow:
https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/skill-registry/create-manage

Skill Registry regions: us-central1, europe-west4, us-east5 (not global).
"""

from __future__ import annotations

import base64
import io
import logging
import os
import subprocess
import time
import zipfile

import requests

logger = logging.getLogger("skill_registry")

# The registry is regional; default to us-central1.
DEFAULT_LOCATION = "us-central1"
_API = "https://{loc}-aiplatform.googleapis.com/v1beta1"


class SkillRegistry:
  """Thin REST wrapper for create / get / update / revisions / delete."""

  # Access tokens are valid ~1h; cache for 50 min so a multi-minute LRO poll
  # doesn't spawn a `gcloud print-access-token` subprocess every 3 seconds.
  _TOKEN_TTL = 3000

  def __init__(self, project: str, location: str = DEFAULT_LOCATION):
    self.project = project
    self.location = location
    self.base = (
        f"{_API.format(loc=location)}/projects/{project}/locations/{location}"
    )
    self._cached_token = None
    self._token_expiry = 0.0

  # -- auth ---------------------------------------------------------------

  def _token(self) -> str:
    env_token = os.environ.get("SKILL_REGISTRY_TOKEN")
    if env_token:
      return env_token
    if self._cached_token and time.monotonic() < self._token_expiry:
      return self._cached_token
    out = subprocess.run(
        ["gcloud", "auth", "application-default", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    )
    self._cached_token = out.stdout.strip()
    self._token_expiry = time.monotonic() + self._TOKEN_TTL
    return self._cached_token

  def _headers(self) -> dict:
    return {
        "Authorization": f"Bearer {self._token()}",
        "Content-Type": "application/json",
    }

  # -- LRO ----------------------------------------------------------------

  def _wait(self, operation: dict, timeout: int = 300) -> dict:
    """Poll a long-running operation to completion (create/update/delete)."""
    name = operation.get("name", "")
    if not name or operation.get("done"):
      return operation
    op_id = name.split("/operations/")[-1]
    url = f"{self.base}/operations/{op_id}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      time.sleep(3)
      resp = requests.get(url, headers=self._headers(), timeout=60)
      resp.raise_for_status()
      op = resp.json()
      if op.get("done"):
        if op.get("error"):
          raise RuntimeError(f"Operation failed: {op['error']}")
        logger.info("Operation %s done.", op_id)
        return op
    raise TimeoutError(f"Operation {op_id} did not finish in {timeout}s")

  # -- payload ------------------------------------------------------------

  @staticmethod
  def _zip_b64(skill_dir: str) -> str:
    """Zip a skill directory (must contain SKILL.md) -> single-line base64."""
    if not os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
      raise FileNotFoundError(f"{skill_dir} must contain a SKILL.md")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
      for root, _, files in os.walk(skill_dir):
        for fn in files:
          path = os.path.join(root, fn)
          zf.write(path, os.path.relpath(path, skill_dir))
    return base64.b64encode(buf.getvalue()).decode("ascii")

  # -- operations ---------------------------------------------------------

  def create(
      self, skill_id: str, skill_dir: str, *, display_name=None, description=""
  ) -> dict:
    """CreateSkill (LRO). Returns the created skill (revision 1 = V0)."""
    body = {
        "displayName": display_name or skill_id,
        "description": description,
        "zippedFilesystem": self._zip_b64(skill_dir),
    }
    resp = requests.post(
        f"{self.base}/skills?skillId={skill_id}",
        headers=self._headers(),
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    logger.info("CreateSkill %s submitted; waiting...", skill_id)
    return self._wait(resp.json())

  def update(
      self, skill_id: str, skill_dir: str, *, display_name=None, description=""
  ) -> dict:
    """UpdateSkill (LRO) -> a new immutable revision (e.g. V1)."""
    body = {
        "displayName": display_name or skill_id,
        "description": description,
        "zippedFilesystem": self._zip_b64(skill_dir),
    }
    mask = "displayName,description,zippedFilesystem"
    resp = requests.patch(
        f"{self.base}/skills/{skill_id}?updateMask={mask}",
        headers=self._headers(),
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    logger.info("UpdateSkill %s submitted; waiting...", skill_id)
    return self._wait(resp.json())

  def get(self, skill_id: str) -> dict:
    resp = requests.get(
        f"{self.base}/skills/{skill_id}", headers=self._headers(), timeout=60
    )
    resp.raise_for_status()
    return resp.json()

  def list_revisions(self, skill_id: str) -> list:
    resp = requests.get(
        f"{self.base}/skills/{skill_id}/revisions",
        headers=self._headers(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("skillRevisions", [])

  def exists(self, skill_id: str) -> bool:
    resp = requests.get(
        f"{self.base}/skills/{skill_id}", headers=self._headers(), timeout=60
    )
    if resp.status_code == 404:
      return False
    # Anything else non-2xx (401/403/5xx) is a real error, not "missing" --
    # surface it instead of silently treating it as absent.
    resp.raise_for_status()
    return True

  def delete(self, skill_id: str) -> dict:
    """DeleteSkill (LRO). The skill_id is reserved for 24h afterward."""
    resp = requests.delete(
        f"{self.base}/skills/{skill_id}", headers=self._headers(), timeout=60
    )
    resp.raise_for_status()
    logger.info("DeleteSkill %s submitted; waiting...", skill_id)
    return self._wait(resp.json())
