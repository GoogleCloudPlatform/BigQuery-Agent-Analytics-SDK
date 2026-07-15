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

"""Snapshot-bound publish path (#356 rounds 12-14): the release state
is classified FIRST (a public mutable release gets burn guidance, not
draft advice), the immutable policy gates only the draft publication
path and is read with a SEPARATE Administration:read credential (the
job GITHUB_TOKEN can never carry it), the payload is JSON --input, the
draft flag must be Boolean, and the Latest assertion tolerates only an
explicit no-Latest 404."""

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
  """The standard job-token runner: serves /releases/{id} from a queue
  and /releases/latest; REFUSES the admin-only policy path."""

  def __init__(self, releases=(), *, latest="other", patch_rc=0):
    self.releases = list(releases)
    if latest == "other":
      latest = (0, json.dumps({"id": RELEASE_ID + 1}), "")
    elif latest == "missing":
      latest = (1, "", "gh: HTTP 404: Not Found")
    self.latest = latest
    self.patch_rc = patch_rc
    self.calls = []

  def __call__(self, argv):
    self.calls.append(argv)
    if "PATCH" in argv:
      return self.patch_rc, "", ""
    path = argv[2]
    assert "immutable-releases" not in path, (
        "the policy read must NEVER use the job token — it requires the"
        " Administration:read credential"
    )
    if path.endswith("/releases/latest"):
      return self.latest
    item = self.releases.pop(0)
    if isinstance(item, tuple):
      return item
    return 0, json.dumps(item), ""

  @property
  def patch_calls(self):
    return [c for c in self.calls if "PATCH" in c]


class FakeAdminGh:
  """The Administration:read runner: serves ONLY the policy path."""

  def __init__(self, setting=None):
    self.setting = {"enabled": True} if setting is None else setting
    self.calls = []

  def __call__(self, argv):
    self.calls.append(argv)
    path = argv[2]
    assert path.endswith("/immutable-releases"), (
        "the Administration:read credential must be used ONLY for the"
        f" policy read, got {path}"
    )
    if isinstance(self.setting, tuple):
      return self.setting
    return 0, json.dumps(self.setting), ""


def _publish(tmp_path, gh, admin=None, out=None):
  return publish_release_body.publish(
      version=VERSION,
      public_image=PUBLIC_IMAGE,
      anchor_dir=tmp_path,
      tag=TAG,
      repo=REPO,
      release_id=RELEASE_ID,
      out_dir=tmp_path,
      run_gh=gh,
      admin_run_gh=admin if admin is not None else FakeAdminGh(),
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
  admin = FakeAdminGh()
  rc = _publish(tmp_path, gh, admin)
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
  # The policy read happened exactly once, on the admin credential.
  assert len(admin.calls) == 1


def test_payload_carries_rendered_notes_not_a_filename(tmp_path):
  # Round-13 reproduction: `gh api -f body=@file` publishes the literal
  # string "@file". The transmitted value must be the rendered notes.
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


class TestCredentialBoundary:
  """The immutable-releases endpoint needs Administration:read, which
  the workflow GITHUB_TOKEN can never carry (#356 round 14 P1)."""

  def test_policy_read_uses_only_the_admin_credential(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh(
        [
            _release_json(anchor, draft=True),
            _release_json(anchor, draft=False, immutable=True),
        ]
    )
    admin = FakeAdminGh()
    assert _publish(tmp_path, gh, admin) == 0
    # The fakes THEMSELVES assert the boundary (FakeGh refuses the
    # policy path; FakeAdminGh refuses everything else) — here we pin
    # the call counts: one policy read, nothing else on admin.
    assert [c[2] for c in admin.calls] == [f"repos/{REPO}/immutable-releases"]
    assert all("immutable-releases" not in c[2] for c in gh.calls)

  def test_failing_admin_credential_fails_closed(self, tmp_path):
    anchor = _anchor(tmp_path)
    out = []
    gh = FakeGh([_release_json(anchor, draft=True)])
    admin = FakeAdminGh(
        setting=(1, "", "gh: HTTP 403: Resource not accessible")
    )
    rc = _publish(tmp_path, gh, admin, out)
    assert rc != 0
    assert not gh.patch_calls
    assert any("policy check failed" in line for line in out)

  def test_env_token_runner_fails_without_the_variable(self, monkeypatch):
    monkeypatch.delenv("BQAA_TEST_ADMIN_TOKEN", raising=False)
    runner = publish_release_body.make_env_token_run_gh("BQAA_TEST_ADMIN_TOKEN")
    rc, out, err = runner(["gh", "api", "whatever"])
    assert rc != 0
    assert "Administration:read" in err


def test_disabled_immutable_setting_blocks_the_draft_publication(tmp_path):
  # Enabling the setting later is NOT retroactive, so nothing may be
  # published while it is off — checked AFTER classifying the release
  # (round 14: the setting must not mask a public mutable release).
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh([_release_json(anchor, draft=True)])
  admin = FakeAdminGh(setting={"enabled": False})
  rc = _publish(tmp_path, gh, admin, out)
  assert rc != 0
  assert not gh.patch_calls
  assert any("NOT retroactive" in line for line in out)
  assert any("still an unpublished draft" in line for line in out)


def test_published_mutable_release_gets_burn_guidance_even_with_setting_off(
    tmp_path,
):
  # Round-14 reproduction: setting disabled AND the release already
  # published mutable — the old order returned "enable and rerun, it's
  # still a draft", leaving a customer-visible mutable release exposed.
  # The release state must be classified FIRST.
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh([_release_json(anchor, draft=False, immutable=False)])
  admin = FakeAdminGh(setting={"enabled": False})
  rc = _publish(tmp_path, gh, admin, out)
  assert rc != 0
  assert not gh.patch_calls
  assert not admin.calls  # never reached the policy check
  assert any("burned" in line for line in out)
  assert not any("still an unpublished draft" in line for line in out)


class TestDraftFlagSchema:
  """A missing/None/string draft flag must never fall through to the
  publish edit (#356 round 14)."""

  def test_non_boolean_draft_refuses_to_publish(self, tmp_path):
    anchor = _anchor(tmp_path)
    for bad in (None, "false", "true", 0):
      release = _release_json(anchor, draft=bad)
      out = []
      gh = FakeGh([release])
      rc = _publish(tmp_path, gh, out=out)
      assert rc != 0, bad
      assert not gh.patch_calls, bad
      assert any("Boolean" in line for line in out), bad

  def test_missing_draft_field_refuses_to_publish(self, tmp_path):
    anchor = _anchor(tmp_path)
    release = _release_json(anchor)
    del release["draft"]
    gh = FakeGh([release])
    assert _publish(tmp_path, gh) != 0
    assert not gh.patch_calls


def test_tampered_asset_before_publish_refuses_to_publish(tmp_path):
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh([_release_json(anchor, tamper=(WHEEL,))])
  rc = _publish(tmp_path, gh, out=out)
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


def test_post_publish_tamper_is_detected(tmp_path):
  anchor = _anchor(tmp_path)
  out = []
  gh = FakeGh(
      [
          _release_json(anchor, draft=True),
          _release_json(anchor, draft=False, immutable=True, tamper=(SDIST,)),
      ]
  )
  rc = _publish(tmp_path, gh, out=out)
  assert rc != 0
  assert any("post-publish" in line for line in out)


def test_post_publish_canonical_drift_is_detected(tmp_path):
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
  rc = _publish(tmp_path, gh, out=out)
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


class TestLatestAssertion:
  """Only an explicit no-Latest 404 passes; every other failure fails
  closed (#356 round 14 — make_latest constrains only our own PATCH)."""

  def test_becoming_repository_latest_fails(self, tmp_path):
    anchor = _anchor(tmp_path)
    out = []
    gh = FakeGh(
        [
            _release_json(anchor, draft=True),
            _release_json(anchor, draft=False, immutable=True),
        ],
        latest=(0, json.dumps({"id": RELEASE_ID}), ""),
    )
    rc = _publish(tmp_path, gh, out=out)
    assert rc != 0
    assert any("Latest" in line for line in out)

  def test_explicit_no_latest_404_passes(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh(
        [
            _release_json(anchor, draft=True),
            _release_json(anchor, draft=False, immutable=True),
        ],
        latest="missing",
    )
    assert _publish(tmp_path, gh) == 0

  def test_auth_rate_limit_and_transport_errors_fail_closed(self, tmp_path):
    anchor = _anchor(tmp_path)
    for err in (
        "gh: HTTP 403: rate limit exceeded",
        "gh: HTTP 401: Bad credentials",
        "gh: HTTP 500: Internal Server Error",
        "dial tcp: lookup api.github.com: no such host",
    ):
      out = []
      gh = FakeGh(
          [
              _release_json(anchor, draft=True),
              _release_json(anchor, draft=False, immutable=True),
          ],
          latest=(1, "", err),
      )
      rc = _publish(tmp_path, gh, out=out)
      assert rc != 0, err
      assert any("refusing to assume" in line for line in out), err

  def test_unparseable_latest_body_fails_closed(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh(
        [
            _release_json(anchor, draft=True),
            _release_json(anchor, draft=False, immutable=True),
        ],
        latest=(0, "not-json", ""),
    )
    assert _publish(tmp_path, gh) != 0

  def test_idempotent_rerun_also_asserts_latest(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh(
        [_release_json(anchor, draft=False, immutable=True)],
        latest=(0, json.dumps({"id": RELEASE_ID}), ""),
    )
    assert _publish(tmp_path, gh) != 0


class TestIdempotentRerun:

  def test_published_immutable_canonical_is_a_no_op(self, tmp_path):
    anchor = _anchor(tmp_path)
    gh = FakeGh([_release_json(anchor, draft=False, immutable=True)])
    admin = FakeAdminGh()
    rc = _publish(tmp_path, gh, admin)
    assert rc == 0
    assert not gh.patch_calls
    assert not admin.calls  # already immutably published: no policy read

  def test_editable_metadata_drift_is_repaired(self, tmp_path):
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
    rc = _publish(tmp_path, gh, out=out)
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
    rc = _publish(tmp_path, gh, out=out)
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
  gh = FakeGh([(0, "not-json", "")])
  assert _publish(tmp_path, gh) != 0
  gh = FakeGh([(1, "", "gh: HTTP 500")])
  assert _publish(tmp_path, gh) != 0
