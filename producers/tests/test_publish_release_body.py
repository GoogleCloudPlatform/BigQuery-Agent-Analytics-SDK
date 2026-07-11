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

"""The complete publish-time body re-anchor path, composed and tested
(#356 round 7): anchor-wheel digest extraction -> renderer -> atomic
gh release edit with --notes-file --draft=false --latest=false."""

import pathlib
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import publish_release_body

VERSION = "0.2.0"
PUBLIC_IMAGE = "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver"
DIGEST = "sha256:" + "e" * 64


def _anchor_wheel(tmp_path):
  wheel = (
      tmp_path / f"bigquery_agent_analytics_tracing-{VERSION}-py3-none-any.whl"
  )
  ref = f"{PUBLIC_IMAGE}:{VERSION}@{DIGEST}"
  with zipfile.ZipFile(wheel, "w") as zf:
    zf.writestr(
        "bigquery_agent_analytics_tracing/otlp/_release.py",
        f'RELEASE_IMAGE = "{ref}"\n',
    )
  return wheel


def test_composes_extract_render_and_atomic_publish(tmp_path):
  calls = []

  def fake_gh(argv):
    calls.append(argv)
    return 0

  _anchor_wheel(tmp_path)
  rc = publish_release_body.publish(
      version=VERSION,
      public_image=PUBLIC_IMAGE,
      anchor_dir=tmp_path,
      tag="tracing-v0.2.0",
      repo="GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK",
      out_dir=tmp_path,
      run_gh=fake_gh,
  )
  assert rc == 0
  body = (tmp_path / "release_body.md").read_text()
  assert f"{PUBLIC_IMAGE}:{VERSION}@{DIGEST}" in body
  (argv,) = calls
  assert argv[:3] == ["gh", "release", "edit"]
  assert "tracing-v0.2.0" in argv
  assert "--notes-file" in argv and "--draft=false" in argv
  assert "--latest=false" in argv


def test_fails_when_anchor_wheel_is_absent(tmp_path):
  with pytest.raises(FileNotFoundError):
    publish_release_body.publish(
        version=VERSION,
        public_image=PUBLIC_IMAGE,
        anchor_dir=tmp_path,
        tag="t",
        repo="o/r",
        out_dir=tmp_path,
        run_gh=lambda argv: 0,
    )


def test_propagates_gh_failure(tmp_path):
  _anchor_wheel(tmp_path)
  rc = publish_release_body.publish(
      version=VERSION,
      public_image=PUBLIC_IMAGE,
      anchor_dir=tmp_path,
      tag="t",
      repo="o/r",
      out_dir=tmp_path,
      run_gh=lambda argv: 3,
  )
  assert rc == 3
