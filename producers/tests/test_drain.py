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

"""Tests for the drainer packaging — envelope I/O, grouping, dead-letter,
and packaged-module invocability. Network I/O against BigQuery is covered
by the gated smoke tests, not this file.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from bigquery_agent_analytics_tracing._writer_identity import DEFAULT_WRITER_LABEL
from bigquery_agent_analytics_tracing.drain import _Envelope
from bigquery_agent_analytics_tracing.drain import _group_envelopes
from bigquery_agent_analytics_tracing.drain import _list_pending
from bigquery_agent_analytics_tracing.drain import _read_envelope


def _write_envelope(spool: Path, name: str, config: dict, row: dict) -> Path:
  spool.mkdir(parents=True, exist_ok=True)
  path = spool / name
  path.write_text(
      json.dumps({"config": config, "row": row}, sort_keys=True),
      encoding="utf-8",
  )
  return path


def test_list_pending_returns_only_event_files(tmp_path):
  spool = tmp_path / "spool"
  spool.mkdir()
  (spool / "event-1.json").write_text("{}")
  (spool / "event-2.json").write_text("{}")
  (spool / "other.json").write_text("{}")
  (spool / ".drainer.pid").write_text("123")

  pending = _list_pending(spool)

  assert [p.name for p in pending] == ["event-1.json", "event-2.json"]


def test_list_pending_returns_empty_when_spool_absent(tmp_path):
  assert _list_pending(tmp_path / "missing") == []


def test_read_envelope_parses_valid_json(tmp_path):
  path = _write_envelope(
      tmp_path / "spool",
      "event-ok.json",
      config={"project_id": "p", "dataset": "d", "table": "t"},
      row={"event_type": "STATE_DELTA"},
  )

  envelope = _read_envelope(path)

  assert envelope is not None
  assert envelope.config_dict["project_id"] == "p"
  assert envelope.row["event_type"] == "STATE_DELTA"


def test_read_envelope_quarantines_corrupt_json(tmp_path):
  spool = tmp_path / "spool"
  spool.mkdir()
  corrupt = spool / "event-bad.json"
  corrupt.write_text("not json {{{")

  result = _read_envelope(corrupt)

  assert result is None
  assert not corrupt.exists()
  dead = list((spool / "dead-letter").glob("event-bad.corrupt-json.json"))
  assert len(dead) == 1


def test_group_envelopes_keys_by_destination_and_writer_label():
  envelopes = [
      _Envelope(
          path=Path("a"),
          config_dict={
              "project_id": "p1",
              "dataset": "d",
              "table": "t",
              "writer_label": "label-a/1.0",
          },
          row={},
      ),
      _Envelope(
          path=Path("b"),
          config_dict={
              "project_id": "p1",
              "dataset": "d",
              "table": "t",
              "writer_label": "label-a/1.0",
          },
          row={},
      ),
      _Envelope(
          path=Path("c"),
          config_dict={
              "project_id": "p2",
              "dataset": "d",
              "table": "t",
              "writer_label": "label-b/1.0",
          },
          row={},
      ),
  ]

  grouped = _group_envelopes(envelopes)

  assert len(grouped) == 2
  group_sizes = sorted(len(v) for v in grouped.values())
  assert group_sizes == [1, 2]


def test_group_envelopes_falls_back_to_default_writer_label():
  envelope = _Envelope(
      path=Path("a"),
      config_dict={
          "project_id": "p",
          "dataset": "d",
          "table": "t",
          # No writer_label — fallback should kick in.
      },
      row={},
  )

  grouped = _group_envelopes([envelope])

  key = next(iter(grouped))
  # writer_label is the 5th element of the tuple key.
  assert key[4] == DEFAULT_WRITER_LABEL


def test_group_envelopes_honors_per_envelope_auto_create_flags():
  envelopes = [
      _Envelope(
          path=Path("a"),
          config_dict={
              "project_id": "p",
              "dataset": "d",
              "table": "t",
              "auto_create_table": True,
              "auto_create_dataset": False,
          },
          row={},
      ),
      _Envelope(
          path=Path("b"),
          config_dict={
              "project_id": "p",
              "dataset": "d",
              "table": "t",
              "auto_create_table": False,
              "auto_create_dataset": True,
          },
          row={},
      ),
  ]

  grouped = _group_envelopes(envelopes)

  assert len(grouped) == 2, (
      "differing auto-create policies must land in separate groups so the"
      " fallback path honors each producer's intent"
  )


def test_drain_module_is_invocable_as_python_m(tmp_path):
  """The packaged-module spawn (-m bigquery_agent_analytics_tracing.drain)
  is the canonical wheel-install invocation. With dry_run=true the drainer
  exits 0 immediately without touching BigQuery."""
  src_root = Path(__file__).resolve().parents[1] / "src"
  env = {
      "PYTHONPATH": str(src_root),
      "BQAA_DRY_RUN": "true",
      "BQAA_SPOOL_DIR": str(tmp_path / "spool"),
      "BQAA_LOG_FILE": str(tmp_path / "bqaa.log"),
      "PATH": "/usr/bin:/bin",
  }
  result = subprocess.run(
      [sys.executable, "-m", "bigquery_agent_analytics_tracing.drain"],
      env=env,
      capture_output=True,
      text=True,
      timeout=15,
  )
  assert (
      result.returncode == 0
  ), f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_pythonpath_invocation_works_for_vendored_plugin(tmp_path):
  """Same as above, but uses the PYTHONPATH-based path that the vendored
  Claude plugin will use (no wheel install required on BQAA_PYTHON)."""
  # Simulate a vendored plugin: copy the package source to a fresh dir
  # outside any installed site-packages and verify -m still works with
  # only PYTHONPATH pointing at it.
  import shutil

  src_root = Path(__file__).resolve().parents[1] / "src"
  vendor_dir = tmp_path / "vendored"
  shutil.copytree(src_root, vendor_dir)

  env = {
      "PYTHONPATH": str(vendor_dir),
      "BQAA_DRY_RUN": "true",
      "BQAA_SPOOL_DIR": str(tmp_path / "spool"),
      "BQAA_LOG_FILE": str(tmp_path / "bqaa.log"),
      "PATH": "/usr/bin:/bin",
  }
  result = subprocess.run(
      [sys.executable, "-m", "bigquery_agent_analytics_tracing.drain"],
      env=env,
      capture_output=True,
      text=True,
      timeout=15,
  )
  assert (
      result.returncode == 0
  ), f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_row_to_arrow_dict_serializes_structured_part_attributes():
  """part_attributes is pa.string() in the Arrow schema. Structured values
  must be JSON-serialized so PyArrow can pack the batch."""
  from bigquery_agent_analytics_tracing.drain import _row_to_arrow_dict

  row = {
      "timestamp": "2026-05-22T00:00:00.000000Z",
      "event_type": "STATE_DELTA",
      "content_parts": [
          {
              "mime_type": "text/plain",
              "uri": None,
              "object_ref": None,
              "text": "hi",
              "part_index": 0,
              "part_attributes": {"lang": "en", "confidence": 0.9},
              "storage_mode": "INLINE",
          },
          {
              "mime_type": "text/plain",
              "uri": None,
              "object_ref": None,
              "text": "raw",
              "part_index": 1,
              "part_attributes": "already-string",
              "storage_mode": "INLINE",
          },
      ],
  }

  arrow = _row_to_arrow_dict(row)

  assert isinstance(arrow["content_parts"][0]["part_attributes"], str)
  assert json.loads(arrow["content_parts"][0]["part_attributes"]) == {
      "confidence": 0.9,
      "lang": "en",
  }
  assert arrow["content_parts"][1]["part_attributes"] == "already-string"


@pytest.mark.skipif(
    not Path("/tmp").exists(), reason="POSIX-only spool path test"
)
def test_envelope_dataclass_round_trip(tmp_path):
  path = _write_envelope(
      tmp_path / "spool",
      "event-rt.json",
      config={
          "project_id": "p",
          "dataset": "d",
          "table": "t",
          "location": "US",
          "writer_label": "rt/1.0",
          "auto_create_table": True,
          "auto_create_dataset": False,
      },
      row={
          "event_type": "STATE_DELTA",
          "content": '{"x":1}',
          "attributes": '{"writer":{"plugin":"p"}}',
      },
  )

  envelope = _read_envelope(path)

  assert envelope is not None
  assert envelope.path == path
  assert envelope.config_dict["location"] == "US"
  assert envelope.row["content"] == '{"x":1}'
