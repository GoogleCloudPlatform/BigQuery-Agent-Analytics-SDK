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

"""The codelab example seed_events.py stays a thin shim over the SDK (#246)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_WRAPPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples/codelab/periodic_materialization/seed_events.py"
)


def _load_wrapper():
  spec = importlib.util.spec_from_file_location(
      "_codelab_seed_events", _WRAPPER_PATH
  )
  module = importlib.util.module_from_spec(spec)
  assert spec and spec.loader
  spec.loader.exec_module(module)
  return module


def test_wrapper_forwards_parsed_args_to_run_seed_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
  module = _load_wrapper()
  captured: dict = {}

  class _Result:
    ok = True

    def to_json(self) -> dict:
      return {"ok": True}

  def fake_run_seed_events(**kwargs):
    captured.update(kwargs)
    return _Result()

  monkeypatch.setattr(module, "run_seed_events", fake_run_seed_events)
  monkeypatch.setattr(
      "sys.argv",
      [
          "seed_events.py",
          "--project-id",
          "p",
          "--dataset-id",
          "d",
          "--sessions",
          "4",
          "--seed",
          "9",
      ],
  )
  module.main()

  assert captured["project_id"] == "p"
  assert captured["dataset_id"] == "d"
  assert captured["sessions"] == 4
  assert captured["seed"] == 9

  # The full forward-and-print pipeline ran without error.
  assert "ok" in capsys.readouterr().out


def test_wrapper_exits_nonzero_on_insert_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
  """ok=False (BigQuery insert errors) must produce a nonzero shell exit.

  run_seed_events reports insert errors as ok=False rather than raising,
  so the wrapper has to fail the exit explicitly -- otherwise downloaded-kit
  users get a success exit on failed inserts (the old script raised).
  """
  module = _load_wrapper()

  class _FailedResult:
    ok = False

    def to_json(self) -> dict:
      return {"ok": False, "errors": [{"index": 0}]}

  monkeypatch.setattr(
      module, "run_seed_events", lambda **kwargs: _FailedResult()
  )
  monkeypatch.setattr(
      "sys.argv",
      ["seed_events.py", "--project-id", "p", "--dataset-id", "d"],
  )

  with pytest.raises(SystemExit) as excinfo:
    module.main()
  assert excinfo.value.code == 1
  # The report is still printed before the nonzero exit.
  assert "ok" in capsys.readouterr().out
