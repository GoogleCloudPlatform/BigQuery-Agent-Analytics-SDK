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

"""Rerun-safe pre-upload gate (#356 rounds 12-13): absent → upload,
byte-identical existing publication → satisfied (skip the upload),
VALIDATED deviations → conflict (the version is burned on that index),
and malformed responses or broken local inputs → indeterminate (retry
advice, never burn advice off a CDN glitch)."""

import hashlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import check_index_publication

VERSION = "0.2.0"
WHEEL = f"bigquery_agent_analytics_tracing-{VERSION}-py3-none-any.whl"
SDIST = f"bigquery_agent_analytics_tracing-{VERSION}.tar.gz"


def _dist(tmp_path):
  dist = tmp_path / "dist"
  dist.mkdir(exist_ok=True)
  (dist / WHEEL).write_bytes(b"wheel-bytes")
  (dist / SDIST).write_bytes(b"sdist-bytes")
  return dist


def _index(dist, omit=(), extra=(), yanked=(), corrupt=()):
  urls = []
  for name in (WHEEL, SDIST):
    if name in omit:
      continue
    data = (dist / name).read_bytes()
    if name in corrupt:
      data = b"other-bytes"
    urls.append(
        {
            "filename": name,
            "yanked": name in yanked,
            "digests": {"sha256": hashlib.sha256(data).hexdigest()},
        }
    )
  for name in extra:
    urls.append(
        {"filename": name, "yanked": False, "digests": {"sha256": "0" * 64}}
    )
  return {"urls": urls}


def _check(tmp_path, index, dist=None):
  return check_index_publication.check(
      version=VERSION, dist_dir=dist or _dist(tmp_path), index=index
  )


def test_explicit_404_is_absent_and_proceeds(tmp_path):
  status, _ = _check(tmp_path, None)
  assert status == "absent"


def test_exact_existing_publication_is_satisfied(tmp_path):
  # THE regression from review: the uploader succeeded but the job
  # failed afterward — the rerun must be able to pass this stage
  # without a re-upload (skip-existing stays disabled).
  dist = _dist(tmp_path)
  status, _ = _check(tmp_path, _index(dist), dist=dist)
  assert status == "satisfied"


def test_partial_upload_is_conflict(tmp_path):
  dist = _dist(tmp_path)
  status, detail = _check(tmp_path, _index(dist, omit=(SDIST,)), dist=dist)
  assert status == "conflict"
  assert SDIST in detail


def test_extra_file_is_conflict(tmp_path):
  dist = _dist(tmp_path)
  status, detail = _check(
      tmp_path, _index(dist, extra=("evil.whl",)), dist=dist
  )
  assert status == "conflict"
  assert "evil.whl" in detail


def test_yanked_file_is_conflict(tmp_path):
  dist = _dist(tmp_path)
  status, _ = _check(tmp_path, _index(dist, yanked=(WHEEL,)), dist=dist)
  assert status == "conflict"


def test_digest_mismatch_is_conflict(tmp_path):
  dist = _dist(tmp_path)
  status, _ = _check(tmp_path, _index(dist, corrupt=(WHEEL,)), dist=dist)
  assert status == "conflict"


def test_zero_file_release_record_is_conflict(tmp_path):
  status, _ = _check(tmp_path, {"urls": []})
  assert status == "conflict"


def test_invalid_schema_is_indeterminate_not_a_burn(tmp_path):
  # A malformed HTTP-200 body proves neither absence nor incompatible
  # published files (#356 round 13) — the operator must refetch, never
  # bump + re-tag off a transient API/CDN glitch.
  for bad in ({"urls": None}, {}, {"urls": [{}]}):
    status, detail = _check(tmp_path, bad)
    assert status == "indeterminate", bad
    assert "refetch" in detail
    assert "burn" not in detail.replace("not an index burn", "")


def test_extra_local_distribution_blocks_the_upload(tmp_path):
  # Round-14 reproduction: the uploader publishes EVERY file left in
  # dist/, so a third local wheel returning `absent` would be uploaded
  # irreversibly at this version before finalize could reject it. The
  # local publish set must be exactly wheel + sdist.
  dist = _dist(tmp_path)
  (dist / "extra-platform.whl").write_bytes(b"surprise")
  for index in (None, _index(dist)):
    status, detail = check_index_publication.check(
        version=VERSION, dist_dir=dist, index=index
    )
    assert status == "indeterminate"
    assert "extra-platform.whl" in detail
    assert "irreversibly" in detail


def test_missing_local_distribution_is_indeterminate(tmp_path):
  # A broken build artifact is not an index burn either — investigate
  # the artifact rather than re-tag.
  dist = tmp_path / "dist"
  dist.mkdir()
  (dist / WHEEL).write_bytes(b"wheel-bytes")  # no sdist
  status, detail = check_index_publication.check(
      version=VERSION, dist_dir=dist, index=None
  )
  assert status == "indeterminate"
  assert SDIST in detail
  assert "artifact" in detail


class TestCli:

  def _main(self, tmp_path, args):
    out = []
    rc = check_index_publication.main(args, echo=out.append)
    status = next(l.split("=", 1)[1] for l in out if l.startswith("status="))
    return rc, status

  def test_absent_and_satisfied_exit_zero(self, tmp_path):
    dist = _dist(tmp_path)
    rc, status = self._main(
        tmp_path,
        ["--version", VERSION, "--dist-dir", str(dist), "--index-missing"],
    )
    assert rc == 0 and status == "absent"
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(_index(dist)))
    rc, status = self._main(
        tmp_path,
        [
            "--version",
            VERSION,
            "--dist-dir",
            str(dist),
            "--index-json",
            str(index_path),
        ],
    )
    assert rc == 0 and status == "satisfied"

  def test_conflict_exits_nonzero(self, tmp_path):
    dist = _dist(tmp_path)
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(_index(dist, omit=(SDIST,))))
    rc, status = self._main(
        tmp_path,
        [
            "--version",
            VERSION,
            "--dist-dir",
            str(dist),
            "--index-json",
            str(index_path),
        ],
    )
    assert rc != 0 and status == "conflict"

  def test_unparseable_body_is_indeterminate(self, tmp_path):
    dist = _dist(tmp_path)
    index_path = tmp_path / "index.json"
    index_path.write_bytes(b'{"urls": [{"filename"')
    rc, status = self._main(
        tmp_path,
        [
            "--version",
            VERSION,
            "--dist-dir",
            str(dist),
            "--index-json",
            str(index_path),
        ],
    )
    assert rc != 0 and status == "indeterminate"

  def test_index_args_are_mutually_required(self, tmp_path):
    dist = _dist(tmp_path)
    with pytest.raises(SystemExit):
      check_index_publication.main(
          ["--version", VERSION, "--dist-dir", str(dist)],
          echo=lambda _: None,
      )
    with pytest.raises(SystemExit):
      check_index_publication.main(
          [
              "--version",
              VERSION,
              "--dist-dir",
              str(dist),
              "--index-missing",
              "--index-json",
              "x.json",
          ],
          echo=lambda _: None,
      )
