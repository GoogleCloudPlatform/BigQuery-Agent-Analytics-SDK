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


def _render():
  return render_release_notes.render(
      version="0.2.0",
      digest="sha256:" + "e" * 64,
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
