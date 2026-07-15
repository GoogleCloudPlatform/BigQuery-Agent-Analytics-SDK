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

"""Snapshot-bound publish path (#356 rounds 12-13): the immutable
policy is a PREREQUISITE checked before anything becomes public, the
release is fetched BY ID, asset digests AND canonical editable metadata
are verified on every snapshot, and the publish carries the rendered
notes in a JSON --input payload (gh raw fields do not read @files)."""

import hashlib
import json
import pathlib
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import publish_release_body
import render_release_notes

VERSION = "0.2.0"
PUBLIC_IMAGE = "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver"
DIGEST = "sha256:" + "e" * 64
TAG = "tracing-v0.2.0"
REPO = "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
RELEASE_ID = 4242

WHEEL = f"bigquery_agent_analytics_tracing-{VERSION}-py3-none-any.whl"
SDIST = f"bigquery_agent_analytics_tracing-{VERSION}.tar.gz"
PLUGIN = f"bigquery-agent-analytics-tracing-claude-code-{VERSION}.tar.gz"

EXPECTED_NAME = f"Tracing {TAG}"
EXPECTED_BODY = render_release_notes.render(
    version=VERSION, digest=DIGEST, public_image=PUBLIC_IMAGE
)


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
    name=EXPECTED_NAME,
    body=EXPECTED_BODY,
    prerelease=False,
    tamper=(),
    extra=(),
    omit=(),
):
  expected = publish_release_body.expected_asset_digests(VERSION, anchor_dir)
  assets = []
  for i, asset_name in enumerate(sorted(expected)):
    if asset_name in omit:
      continue
    digest = "0" * 64 if asset_name in tamper else expected[asset_name]
    assets.append(
        {
            "name": asset_name,
            "id": 1000 + i,
            "digest": f"sha256:{digest}",
            "state": "uploaded",
        }
    )
  for asset_name in extra:
    assets.append(
        {
            "name": asset_name,
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
      "name": name,
      "body": body,
      "prerelease": prerelease,
      "assets": assets,
  }


class FakeGh:
  """Routes fetches by path; records every call.

  ``releases`` is a queue served for /releases/{id} fetches. ``latest``
  is the /releases/latest object (None → the endpoint errors).
  ``setting`` is the /immutable-releases object (a tuple = raw (rc,
  out)). PATCH calls return ``patch_rc``."""

  def __init__(self, releases=(), *, setting=None, latest="other", patch_rc=0):
    self.releases = list(releases)
    self.setting = {"enabled": True} if setting is None else setting
    if latest == "other":
      latest = {"id": RELEASE_ID + 1}
    self.latest = latest
    self.patch_rc = patch_rc
    self.calls = []

  def __call__(self, argv):
    self.calls.append(argv)
    if "PATCH" in argv:
      return self.patch_rc, ""
    path = argv[2]
    if path.endswith("/immutable-releases"):
      if isinstance(self.setting, tuple):
        return self.setting
      return 0, json.dumps(self.setting)
    if path.endswith("/releases/latest"):
      if self.latest is None:
        return 1, ""
      return 0, json.dumps(self.latest)
    item = self.releases.pop(0)
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


def test_happy_path_publishes_by_id_with_json_payload(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=True),
      ]
  )
  rc = _publish(tmp_path, gh)
  assert rc == 0
  (patch,) = gh.patch_calls
  assert f"repos/{REPO}/releases/{RELEASE_ID}" in patch  # by ID, not tag
  assert "--input" in patch
  payload = json.loads(
      pathlib.Path(patch[patch.index("--input") + 1]).read_text()
  )
  assert payload == {
      "tag_name": TAG,
      "name": EXPECTED_NAME,
      "body": EXPECTED_BODY,
      "draft": False,
      "prerelease": False,
      "make_latest": "false",
  }


def test_payload_carries_rendered_notes_not_a_filename(tmp_path):
  # Reviewer reproduction (#356 round 13): `gh api -f body=@file`
  # publishes the literal string "@file". The transmitted value must be
  # the rendered notes themselves, via a JSON --input payload.
  anchor = _anchor(tmp_path)
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=True),
      ]
  )
  assert _publish(tmp_path, gh) == 0
  (patch,) = gh.patch_calls
  assert not any("=@" in arg for arg in patch)
  payload = json.loads(
      pathlib.Path(patch[patch.index("--input") + 1]).read_text()
  )
  assert payload["body"] == EXPECTED_BODY
  assert f"{PUBLIC_IMAGE}:{VERSION}@{DIGEST}" in payload["body"]
  assert not payload["body"].startswith("@")


def test_disabled_immutable_setting_blocks_before_anything_is_public(
    tmp_path,
):
  # The prerequisite (#356 round 13): enabling the setting later is NOT
  # retroactive, so nothing may be published while it is off. The
  # release must not even be fetched, let alone PATCHed.
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh([_release_json(anchor, draft=True)], setting={"enabled": False})
  rc = _publish(tmp_path, gh, out)
  assert rc != 0
  assert not gh.patch_calls
  assert len(gh.calls) == 1  # only the policy check ran
  assert any("NOT retroactive" in line for line in out)
  assert any("still an unpublished draft" in line for line in out)


def test_unreadable_immutable_setting_fails_closed(tmp_path):
  _anchor(tmp_path)
  gh = FakeGh([], setting=(1, ""))
  assert _publish(tmp_path, gh) != 0
  assert not gh.patch_calls


def test_tampered_asset_before_publish_refuses_to_publish(tmp_path):
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh([_release_json(anchor, tamper=(WHEEL,))])
  rc = _publish(tmp_path, gh, out)
  assert rc != 0
  assert not gh.patch_calls
  assert any("snapshot changed" in line for line in out)


def test_extra_or_missing_asset_before_publish_refuses(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh([_release_json(anchor, extra=("evil.bin",))])
  assert _publish(tmp_path, gh) != 0
  gh = FakeGh([_release_json(anchor, omit=(PLUGIN,))])
  assert _publish(tmp_path, gh) != 0


def test_wrong_release_id_or_tag_refuses_to_publish(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh([_release_json(anchor, release_id=999)])
  assert _publish(tmp_path, gh) != 0
  gh = FakeGh([_release_json(anchor, tag="tracing-v9.9.9")])
  assert _publish(tmp_path, gh) != 0


def test_published_mutable_release_is_burned_not_repaired(tmp_path):
  # Published while the setting was off: immutability cannot be applied
  # retroactively, so "enable and re-run" would be false advice.
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh([_release_json(anchor, draft=False, immutable=False)])
  rc = _publish(tmp_path, gh, out)
  assert rc != 0
  assert not gh.patch_calls
  assert any("burned" in line for line in out)
  assert any("retroactively" in line for line in out)


def test_post_publish_tamper_is_detected(tmp_path):
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


def test_post_publish_canonical_drift_is_detected(tmp_path):
  # The canonical metadata must be verified on the published snapshot
  # too (#356 round 13) — title/notes stay editable forever.
  anchor = _anchor(tmp_path)
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(
              anchor, draft=False, immutable=True, name="Renamed by someone"
          ),
      ]
  )
  assert _publish(tmp_path, gh) != 0
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(
              anchor, draft=False, immutable=True, body="tampered notes"
          ),
      ]
  )
  assert _publish(tmp_path, gh) != 0


def test_post_publish_mutable_fails_as_defense_in_depth(tmp_path):
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
  assert any("MUTABLE" in line for line in out)


def test_still_draft_after_edit_fails(tmp_path):
  anchor = _anchor(tmp_path)
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=True),
      ]
  )
  assert _publish(tmp_path, gh) != 0


def test_becoming_repository_latest_fails(tmp_path):
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=True),
      ],
      latest={"id": RELEASE_ID},
  )
  rc = _publish(tmp_path, gh, out)
  assert rc != 0
  assert any("Latest" in line for line in out)


def test_unreadable_latest_is_noted_not_fatal(tmp_path):
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=True),
      ],
      latest=None,
  )
  rc = _publish(tmp_path, gh, out)
  assert rc == 0
  assert any("Latest lookup unavailable" in line for line in out)


class TestIdempotentRerun:

  def test_published_immutable_canonical_is_a_no_op(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh([_release_json(anchor, draft=False, immutable=True)])
    rc = _publish(tmp_path, gh)
    assert rc == 0
    assert not gh.patch_calls

  def test_editable_metadata_drift_is_repaired(self, tmp_path):
    # Immutable releases still allow title/notes edits (#356 round 13):
    # drift is repaired with a metadata-only PATCH.
    anchor = _anchor(tmp_path)
    out = []
    gh = FakeGh(
        [
            _release_json(
                anchor, draft=False, immutable=True, name="Defaced title"
            ),
            _release_json(anchor, draft=False, immutable=True),
        ]
    )
    rc = _publish(tmp_path, gh, out)
    assert rc == 0
    (patch,) = gh.patch_calls
    payload = json.loads(
        pathlib.Path(patch[patch.index("--input") + 1]).read_text()
    )
    assert payload == {"name": EXPECTED_NAME, "body": EXPECTED_BODY}
    assert any("repairing" in line for line in out)

  def test_repair_that_does_not_converge_fails(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh(
        [
            _release_json(anchor, draft=False, immutable=True, body="defaced"),
            _release_json(
                anchor, draft=False, immutable=True, body="still defaced"
            ),
        ]
    )
    assert _publish(tmp_path, gh) != 0

  def test_prerelease_drift_is_unrepairable(self, tmp_path):
    anchor = _anchor(tmp_path)
    out = []
    gh = FakeGh(
        [_release_json(anchor, draft=False, immutable=True, prerelease=True)]
    )
    rc = _publish(tmp_path, gh, out)
    assert rc != 0
    assert not gh.patch_calls
    assert any("unrepairable" in line for line in out)

  def test_tampered_assets_on_immutable_release_fail(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh(
        [_release_json(anchor, draft=False, immutable=True, tamper=(WHEEL,))]
    )
    assert _publish(tmp_path, gh) != 0
    assert not gh.patch_calls


def test_expected_digests_come_from_anchor_bytes(tmp_path):
  anchor = _anchor(tmp_path)
  expected = publish_release_body.expected_asset_digests(VERSION, anchor)
  assert set(expected) == {WHEEL, SDIST, PLUGIN, "SHA256SUMS"}
  assert expected[SDIST] == hashlib.sha256(b"sdist-bytes").hexdigest()


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
