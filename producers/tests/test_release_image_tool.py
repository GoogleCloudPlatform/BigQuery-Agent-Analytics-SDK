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

"""Tests for the release image injection/verification tool (issue #349).

The release contract: the public image coordinate + digest are injected
into the source tree BEFORE wheel/sdist are built (both must embed the
identical value), and the release job asserts equality mechanically.
"""

import hashlib
import pathlib
import subprocess
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import release_image_tool as tool

GOOD_COORDINATE = "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver:0.2.0"
GOOD_DIGEST = "sha256:" + "a" * 64
MODULE_RELPATH = "bigquery_agent_analytics_tracing/otlp/_release.py"


def _exec_release_module(text):
  ns = {}
  exec(text, ns)  # noqa: S102 - executing our own generated constant module
  return ns


class TestInject:

  def test_written_module_defines_pinned_reference(self, tmp_path):
    target = tmp_path / "_release.py"
    tool.inject(target, GOOD_COORDINATE, GOOD_DIGEST)
    ns = _exec_release_module(target.read_text())
    assert ns["RELEASE_IMAGE"] == f"{GOOD_COORDINATE}@{GOOD_DIGEST}"

  def test_rejects_malformed_digest(self, tmp_path):
    with pytest.raises(ValueError, match="digest"):
      tool.inject(tmp_path / "_release.py", GOOD_COORDINATE, "sha256:short")

  def test_rejects_latest_tag(self, tmp_path):
    with pytest.raises(ValueError, match="latest"):
      tool.inject(
          tmp_path / "_release.py",
          "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver:latest",
          GOOD_DIGEST,
      )

  def test_rejects_coordinate_without_version_tag(self, tmp_path):
    with pytest.raises(ValueError, match="tag"):
      tool.inject(
          tmp_path / "_release.py",
          "us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver",
          GOOD_DIGEST,
      )


class TestExtract:

  def _wheel_with(self, tmp_path, text):
    path = tmp_path / "pkg-0.2.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
      zf.writestr(MODULE_RELPATH, text)
    return path

  def _sdist_with(self, tmp_path, text):
    src = tmp_path / "srcdir" / f"pkg-0.2.0/src/{MODULE_RELPATH}"
    src.parent.mkdir(parents=True)
    src.write_text(text)
    path = tmp_path / "pkg-0.2.0.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
      tf.add(src, arcname=f"pkg-0.2.0/src/{MODULE_RELPATH}")
    return path

  def test_extracts_reference_from_wheel(self, tmp_path):
    ref = f"{GOOD_COORDINATE}@{GOOD_DIGEST}"
    wheel = self._wheel_with(tmp_path, f'RELEASE_IMAGE = "{ref}"\n')
    assert tool.extract_from_wheel(wheel) == ref

  def test_extracts_reference_from_sdist(self, tmp_path):
    ref = f"{GOOD_COORDINATE}@{GOOD_DIGEST}"
    sdist = self._sdist_with(tmp_path, f'RELEASE_IMAGE = "{ref}"\n')
    assert tool.extract_from_sdist(sdist) == ref

  def test_verify_passes_when_wheel_sdist_and_expected_agree(self, tmp_path):
    ref = f"{GOOD_COORDINATE}@{GOOD_DIGEST}"
    wheel = self._wheel_with(tmp_path, f'RELEASE_IMAGE = "{ref}"\n')
    sdist = self._sdist_with(tmp_path, f'RELEASE_IMAGE = "{ref}"\n')
    tool.verify_artifacts(wheel, sdist, expected=ref)

  def test_verify_fails_on_wheel_sdist_mismatch(self, tmp_path):
    ref = f"{GOOD_COORDINATE}@{GOOD_DIGEST}"
    other = ref.replace("a" * 64, "b" * 64)
    wheel = self._wheel_with(tmp_path, f'RELEASE_IMAGE = "{ref}"\n')
    sdist = self._sdist_with(tmp_path, f'RELEASE_IMAGE = "{other}"\n')
    with pytest.raises(tool.ReleaseVerificationError, match="sdist"):
      tool.verify_artifacts(wheel, sdist, expected=ref)

  def test_verify_fails_when_expected_differs(self, tmp_path):
    ref = f"{GOOD_COORDINATE}@{GOOD_DIGEST}"
    wheel = self._wheel_with(tmp_path, f'RELEASE_IMAGE = "{ref}"\n')
    sdist = self._sdist_with(tmp_path, f'RELEASE_IMAGE = "{ref}"\n')
    with pytest.raises(tool.ReleaseVerificationError, match="expected"):
      tool.verify_artifacts(
          wheel, sdist, expected=ref.replace("a" * 64, "c" * 64)
      )

  def test_verify_fails_on_dev_placeholder(self, tmp_path):
    wheel = self._wheel_with(tmp_path, "RELEASE_IMAGE = None\n")
    sdist = self._sdist_with(tmp_path, "RELEASE_IMAGE = None\n")
    with pytest.raises(tool.ReleaseVerificationError, match="placeholder"):
      tool.verify_artifacts(
          wheel, sdist, expected=f"{GOOD_COORDINATE}@{GOOD_DIGEST}"
      )


class TestChecksums:

  def test_writes_sha256sums_in_standard_format(self, tmp_path):
    (tmp_path / "a.whl").write_bytes(b"wheel-bytes")
    (tmp_path / "b.tar.gz").write_bytes(b"sdist-bytes")
    out = tool.write_checksums(tmp_path)
    lines = out.read_text().splitlines()
    expect_a = hashlib.sha256(b"wheel-bytes").hexdigest()
    assert f"{expect_a}  a.whl" in lines
    assert len(lines) == 2
    assert not any("SHA256SUMS" in l for l in lines)


class TestDevPlaceholder:

  def test_packaged_release_module_defaults_to_none(self):
    from bigquery_agent_analytics_tracing.otlp import _release

    assert _release.RELEASE_IMAGE is None


class TestCli:

  def test_inject_and_verify_roundtrip_via_cli(self, tmp_path):
    target = tmp_path / "_release.py"
    script = (
        pathlib.Path(__file__).parent.parent / "scripts/release_image_tool.py"
    )
    r = subprocess.run(
        [
            sys.executable,
            str(script),
            "inject",
            "--target",
            str(target),
            "--coordinate",
            GOOD_COORDINATE,
            "--digest",
            GOOD_DIGEST,
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    ns = _exec_release_module(target.read_text())
    assert ns["RELEASE_IMAGE"].endswith(GOOD_DIGEST)
