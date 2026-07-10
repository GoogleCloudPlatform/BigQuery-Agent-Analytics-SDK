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

"""PR-time regression for the release-notes renderer (#356 review).

The renderer previously lived inline in the tag-only workflow, so
placeholder drift would first fail AFTER the release tag was pushed.
"""

import pathlib
import re
import sys

from bigquery_agent_analytics_tracing.otlp import config_artifacts

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import render_release_notes

PUBLIC_IMAGE = "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver"


def _render():
  # public_image is REQUIRED and comes from the workflow's authoritative
  # env var — the renderer holds no duplicate coordinate (#356 review).
  return render_release_notes.render(
      version="0.2.0",
      digest="sha256:" + "e" * 64,
      public_image=PUBLIC_IMAGE,
  )


def test_render_leaves_no_placeholders():
  body = _render()
  assert not re.search(r"\{[a-z_]+\}", body), "unrendered placeholder left"


def test_render_pins_the_image_by_digest():
  body = _render()
  assert (
      "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver:0.2.0@sha256:"
      + "e" * 64
      in body
  )


def test_codex_version_comes_from_the_canonical_constant():
  # Never a hand-copied string: the compatibility pin lives in
  # config_artifacts and the notes must always agree with it.
  assert config_artifacts.CODEX_MIN_VERSION in _render()


def test_demo_link_is_absolute_and_tag_pinned():
  body = _render()
  assert (
      "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
      "/tree/tracing-v0.2.0/demo/hero_story" in body
  )


def test_lifecycle_commands_include_token_fill_and_confirmed_teardown():
  body = _render()
  assert "gcloud secrets versions access" in body
  assert "teardown" in body and "--confirm" in body


def test_curated_changelog_names_the_tracing_prs_only():
  body = _render()
  for issue in ("#316", "#324", "#317", "#340", "#342", "#343"):
    assert issue in body


def test_render_rejects_a_malformed_public_image():
  import pytest

  with pytest.raises(ValueError):
    render_release_notes.render(
        version="0.2.0",
        digest="sha256:" + "e" * 64,
        public_image="not a registry/path",
    )


def _token_fill_snippet():
  """The exact bash between the token-fill markers, executed as-is."""
  body = _render()
  start = body.index("# --token-fill-start--")
  end = body.index("# --token-fill-end--")
  return body[start:end]


def test_token_fill_snippet_replaces_placeholders_in_both_files(tmp_path):
  import subprocess

  for name in ("codex.config.toml", "claude-code.managed-settings.json"):
    (tmp_path / name).write_text("Authorization=Bearer <token>\n")
  result = subprocess.run(
      ["bash", "-c", 'TOKEN="real-secret"\n' + _token_fill_snippet()],
      cwd=tmp_path,
      env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
      capture_output=True,
      text=True,
  )
  assert result.returncode == 0, result.stderr
  for name in ("codex.config.toml", "claude-code.managed-settings.json"):
    content = (tmp_path / name).read_text()
    assert "<token>" not in content
    assert "real-secret" in content


def test_token_fill_snippet_fails_on_missing_artifact(tmp_path):
  import subprocess

  (tmp_path / "codex.config.toml").write_text("<token>")
  # claude-code.managed-settings.json deliberately absent
  result = subprocess.run(
      ["bash", "-c", 'TOKEN="real-secret"\n' + _token_fill_snippet()],
      cwd=tmp_path,
      env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
      capture_output=True,
      text=True,
  )
  assert result.returncode != 0
  assert "missing artifact" in result.stdout + result.stderr


def test_token_fill_snippet_fails_on_empty_token(tmp_path):
  import subprocess

  result = subprocess.run(
      ["bash", "-c", 'TOKEN=""\n' + _token_fill_snippet()],
      cwd=tmp_path,
      env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
      capture_output=True,
      text=True,
  )
  assert result.returncode != 0


def test_lifecycle_defines_its_variables_and_fills_both_artifacts():
  body = _render()
  # Variables are defined before first use (#356: bootstrap prints a URL
  # but never exports $URL).
  assert "PROJECT=" in body and "DATASET=" in body and "URL=$(" in body
  # BOTH generated artifacts get the token fill, with a guard against an
  # empty token and an assertion that no placeholder survives.
  assert "claude-code.managed-settings.json" in body
  assert "codex.config.toml" in body
  assert 'test -n "$TOKEN"' in body or '[ -n "$TOKEN" ]' in body
  assert "--token-fill-start--" in body  # executable guard block present
