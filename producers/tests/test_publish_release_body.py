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

"""Snapshot-bound publish path (#356 round 12): the release is fetched
BY ID, every asset digest is verified against the anchor immediately
before AND after the ID-based publish edit, and the published release
must be immutable — publication is never bound to the mutable tag."""

import hashlib
import json
import pathlib
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import publish_release_body

VERSION = "0.2.0"
PUBLIC_IMAGE = "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver"
DIGEST = "sha256:" + "e" * 64
TAG = "tracing-v0.2.0"
REPO = "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
RELEASE_ID = 4242

WHEEL = f"bigquery_agent_analytics_tracing-{VERSION}-py3-none-any.whl"
SDIST = f"bigquery_agent_analytics_tracing-{VERSION}.tar.gz"
PLUGIN = f"bigquery-agent-analytics-tracing-claude-code-{VERSION}.tar.gz"


def _anchor(tmp_path):
  """A full anchor: wheel embedding the image reference, sdist, plugin,
  and the SHA256SUMS file."""
  wheel = tmp_path / WHEEL
  ref = f"{PUBLIC_IMAGE}:{VERSION}@{DIGEST}"
  with zipfile.ZipFile(wheel, "w") as zf:
    zf.writestr(
        "bigquery_agent_analytics_tracing/otlp/_release.py",
        f'RELEASE_IMAGE = "{ref}"\n',
    )
  (tmp_path / SDIST).write_bytes(b"sdist-bytes")
  (tmp_path / PLUGIN).write_bytes(b"plugin-bytes")
  (tmp_path / "SHA256SUMS").write_text("manifest lines\n")
  return tmp_path


def _release_json(
    anchor_dir,
    *,
    draft=True,
    immutable=False,
    release_id=RELEASE_ID,
    tag=TAG,
    tamper=(),
    extra=(),
    omit=(),
):
  expected = publish_release_body.expected_asset_digests(VERSION, anchor_dir)
  assets = []
  for i, name in enumerate(sorted(expected)):
    if name in omit:
      continue
    digest = "0" * 64 if name in tamper else expected[name]
    assets.append(
        {
            "name": name,
            "id": 1000 + i,
            "digest": f"sha256:{digest}",
            "state": "uploaded",
        }
    )
  for name in extra:
    assets.append(
        {
            "name": name,
            "id": 9999,
            "digest": "sha256:" + "1" * 64,
            "state": "uploaded",
        }
    )
  return {
      "id": release_id,
      "tag_name": tag,
      "draft": draft,
      "immutable": immutable,
      "assets": assets,
  }


class FakeGh:
  """Serves queued release objects for GET fetches; records every call.

  PATCH calls return ``patch_rc`` with empty output."""

  def __init__(self, fetches, patch_rc=0):
    self.fetches = list(fetches)
    self.patch_rc = patch_rc
    self.calls = []

  def __call__(self, argv):
    self.calls.append(argv)
    if "PATCH" in argv:
      return self.patch_rc, ""
    item = self.fetches.pop(0)
    if isinstance(item, tuple):
      return item
    return 0, json.dumps(item)

  @property
  def patch_calls(self):
    return [c for c in self.calls if "PATCH" in c]


def _publish(tmp_path, gh, out=None):
  return publish_release_body.publish(
      version=VERSION,
      public_image=PUBLIC_IMAGE,
      anchor_dir=tmp_path,
      tag=TAG,
      repo=REPO,
      release_id=RELEASE_ID,
      out_dir=tmp_path,
      run_gh=gh,
      echo=(out.append if out is not None else print),
  )


def test_happy_path_verifies_before_and_after_the_id_based_edit(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=True),
      ]
  )
  rc = _publish(tmp_path, gh)
  assert rc == 0
  body = (tmp_path / "release_body.md").read_text()
  assert f"{PUBLIC_IMAGE}:{VERSION}@{DIGEST}" in body
  (patch,) = gh.patch_calls
  assert f"repos/{REPO}/releases/{RELEASE_ID}" in patch  # by ID, not tag
  assert f"tag_name={TAG}" in patch
  assert f"name=Tracing {TAG}" in patch
  assert "draft=false" in patch
  assert "prerelease=false" in patch
  assert "make_latest=false" in patch
  assert any(a.startswith("body=@") for a in patch)
  # Three gh calls total: fetch, PATCH, re-fetch.
  assert len(gh.calls) == 3


def test_expected_digests_come_from_anchor_bytes(tmp_path):
  anchor = _anchor(tmp_path)
  expected = publish_release_body.expected_asset_digests(VERSION, anchor)
  assert set(expected) == {WHEEL, SDIST, PLUGIN, "SHA256SUMS"}
  assert expected[SDIST] == hashlib.sha256(b"sdist-bytes").hexdigest()


def test_tampered_asset_before_publish_refuses_to_publish(tmp_path):
  # The TOCTOU the reviewer flagged: assets replaced between
  # reconciliation and publish must abort BEFORE the edit.
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh([_release_json(anchor, tamper=(WHEEL,))])
  rc = _publish(tmp_path, gh, out)
  assert rc != 0
  assert not gh.patch_calls
  assert any("snapshot changed" in line for line in out)


def test_extra_asset_before_publish_refuses_to_publish(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh([_release_json(anchor, extra=("evil.bin",))])
  assert _publish(tmp_path, gh) != 0
  assert not gh.patch_calls


def test_missing_asset_before_publish_refuses_to_publish(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh([_release_json(anchor, omit=(PLUGIN,))])
  assert _publish(tmp_path, gh) != 0
  assert not gh.patch_calls


def test_wrong_release_id_or_tag_refuses_to_publish(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh([_release_json(anchor, release_id=999)])
  assert _publish(tmp_path, gh) != 0
  gh = FakeGh([_release_json(anchor, tag="tracing-v9.9.9")])
  assert _publish(tmp_path, gh) != 0


def test_post_publish_tamper_is_detected(tmp_path):
  # Assets swapped between the PATCH and the re-fetch.
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=True, tamper=(SDIST,)),
      ]
  )
  rc = _publish(tmp_path, gh, out)
  assert rc != 0
  assert any("post-publish" in line for line in out)


def test_mutable_published_release_fails_the_immutability_assert(tmp_path):
  # The repository setting is the only thing keeping published assets
  # non-replaceable; a mutable publication must fail loudly.
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=False),
      ]
  )
  rc = _publish(tmp_path, gh, out)
  assert rc != 0
  assert any("NOT immutable" in line for line in out)


def test_still_draft_after_edit_fails(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=True),
      ]
  )
  assert _publish(tmp_path, gh) != 0


def test_already_published_immutable_and_identical_is_idempotent(tmp_path):
  # A finalize rerun after a successful publish: the immutable release
  # cannot (and need not) be edited.
  anchor = _anchor(tmp_path)
  gh = FakeGh([_release_json(anchor, draft=False, immutable=True)])
  rc = _publish(tmp_path, gh)
  assert rc == 0
  assert not gh.patch_calls


def test_already_published_immutable_but_tampered_fails(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh(
      [_release_json(anchor, draft=False, immutable=True, tamper=(WHEEL,))]
  )
  assert _publish(tmp_path, gh) != 0
  assert not gh.patch_calls


def test_fails_when_anchor_wheel_is_absent(tmp_path):
  with pytest.raises(FileNotFoundError):
    _publish(tmp_path, FakeGh([]))


def test_fails_when_any_anchor_asset_is_absent(tmp_path):
  _anchor(tmp_path)
  (tmp_path / PLUGIN).unlink()
  with pytest.raises(FileNotFoundError):
    _publish(tmp_path, FakeGh([]))


def test_propagates_gh_patch_failure(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh([_release_json(anchor, draft=True)], patch_rc=3)
  assert _publish(tmp_path, gh) == 3


def test_unparseable_release_fetch_fails(tmp_path):
  _anchor(tmp_path)
  gh = FakeGh([(0, "not-json")])
  assert _publish(tmp_path, gh) != 0
  gh = FakeGh([(1, "")])
  assert _publish(tmp_path, gh) != 0
