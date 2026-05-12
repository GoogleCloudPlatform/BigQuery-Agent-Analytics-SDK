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

"""Tests for the ``bqaa-revalidate-extractors`` CLI.

Two layers:

* End-to-end happy / threshold-pass / threshold-violation
  paths using the real BKA fixtures + the real compile
  pipeline. Proves the CLI wires the harness correctly and
  produces the documented report shape.
* Per-input usage-error tests covering each branch of
  :func:`_load_config`, locked with the documented
  ``EXIT_USAGE_ERROR`` (=2) exit code.

The CLI is invoked via :func:`main` with explicit ``argv``;
``capsys`` captures the stderr message so failure cases
assert on the user-facing wording too.
"""

from __future__ import annotations

import json
import pathlib
import sys
import textwrap
import types

import pytest

# ------------------------------------------------------------------ #
# Fixture helpers — hand-built bundle + reference module             #
# ------------------------------------------------------------------ #


_VALID_FINGERPRINT = "a" * 64
_OTHER_FINGERPRINT = "b" * 64


def _write_manifest(
    bundle_dir: pathlib.Path,
    *,
    fingerprint: str = _VALID_FINGERPRINT,
    event_types: tuple[str, ...] = ("bka_decision",),
    module_filename: str = "extractor.py",
    function_name: str = "extract_bka",
) -> None:
  bundle_dir.mkdir(parents=True, exist_ok=True)
  (bundle_dir / "manifest.json").write_text(
      json.dumps(
          {
              "fingerprint": fingerprint,
              "event_types": list(event_types),
              "module_filename": module_filename,
              "function_name": function_name,
              "compiler_package_version": "0.0.0",
              "template_version": "v0.1",
              "transcript_builder_version": "tb-1",
              "created_at": "2026-05-12T00:00:00Z",
          },
          sort_keys=True,
          indent=2,
      ),
      encoding="utf-8",
  )


# Compiled extractor source: same shape as the handwritten
# BKA extractor, so parity matches on the happy path.
_COMPILED_SOURCE_AGREES = textwrap.dedent(
    """\
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    def extract_bka(event, spec):
        return extract_bka_decision_event(event, spec)
    """
)


# Compiled extractor that always emits a Ghost node — caught
# by the validator and downgraded to ``compiled_filtered``
# at runtime, which drops the parity match rate.
_COMPILED_SOURCE_DRIFTS = textwrap.dedent(
    """\
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extract_bka(event, spec):
        node = ExtractedNode(
            node_id="ghost",
            entity_name="GhostEntity",
            labels=["GhostEntity"],
            properties=[],
        )
        return StructuredExtractionResult(
            nodes=[node],
            edges=[],
            fully_handled_span_ids={event.get("span_id", "")},
            partially_handled_span_ids=set(),
        )
    """
)


def _build_bundle(
    bundles_root: pathlib.Path,
    *,
    name: str = "bka",
    source: str = _COMPILED_SOURCE_AGREES,
    fingerprint: str = _VALID_FINGERPRINT,
) -> pathlib.Path:
  bundle_dir = bundles_root / name
  _write_manifest(bundle_dir, fingerprint=fingerprint)
  (bundle_dir / "extractor.py").write_text(source, encoding="utf-8")
  return bundle_dir


def _write_events_jsonl(path: pathlib.Path, *, count: int = 3) -> pathlib.Path:
  events = [
      {
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": f"sp{i}",
          "content": {
              "decision_id": f"d{i}",
              "outcome": "approved",
              "confidence": 0.9,
          },
      }
      for i in range(1, count + 1)
  ]
  path.write_text(
      "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
  )
  return path


def _make_reference_module(
    *,
    module_name: str,
    has_extractors: bool = True,
    has_resolved_graph: bool = True,
    bad_extractor_value: object = None,
) -> str:
  """Synthesize a reference-extractors module on the fly and
  register it under ``sys.modules`` so ``importlib.import_module``
  finds it. Returns the dotted path."""
  import tempfile

  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_cli_ref_"))
  (tmp / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  resolved = resolve(ontology, binding)

  mod = types.ModuleType(module_name)
  if has_extractors:
    if bad_extractor_value is not None:
      mod.EXTRACTORS = bad_extractor_value
    else:
      mod.EXTRACTORS = {"bka_decision": extract_bka_decision_event}
  if has_resolved_graph:
    mod.RESOLVED_GRAPH = resolved
  sys.modules[module_name] = mod
  return module_name


@pytest.fixture
def cleanup_reference_modules():
  """Remove any synthesized reference modules from sys.modules
  after each test so module imports stay clean across cases."""
  added: list[str] = []
  prefix = "_bqaa_cli_test_ref_"

  def _make(suffix: str, **kwargs) -> str:
    name = f"{prefix}{suffix}"
    added.append(name)
    return _make_reference_module(module_name=name, **kwargs)

  yield _make
  for name in added:
    sys.modules.pop(name, None)


# ------------------------------------------------------------------ #
# End-to-end happy + threshold paths                                  #
# ------------------------------------------------------------------ #


class TestCliEndToEnd:

  def test_happy_path_writes_report_and_exits_zero(
      self, tmp_path: pathlib.Path, cleanup_reference_modules
  ):
    """Compiled bundle that agrees with the reference, no
    thresholds → exit 0, report JSON has the documented shape
    with ``threshold_check: null``."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)

    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("happy")
    report_out = tmp_path / "report.json"

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(report_out),
        ]
    )

    assert code == 0
    payload = json.loads(report_out.read_text(encoding="utf-8"))
    # Documented shape.
    assert set(payload.keys()) == {"report", "threshold_check"}
    assert payload["threshold_check"] is None
    assert payload["report"]["total_events"] == 3
    assert payload["report"]["total_compiled_unchanged"] == 3
    assert payload["report"]["total_parity_matches"] == 3

  def test_threshold_pass_exits_zero(
      self, tmp_path: pathlib.Path, cleanup_reference_modules
  ):
    """Thresholds supplied AND satisfied → exit 0,
    ``threshold_check.ok`` is true."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("pass")

    thresholds_path = tmp_path / "thresholds.json"
    thresholds_path.write_text(
        json.dumps(
            {
                "min_compiled_unchanged_rate": 0.95,
                "min_parity_match_rate": 0.95,
                "max_fallback_for_event_rate": 0.05,
            }
        ),
        encoding="utf-8",
    )
    report_out = tmp_path / "report.json"

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--thresholds-json",
            str(thresholds_path),
            "--report-out",
            str(report_out),
        ]
    )

    assert code == 0
    payload = json.loads(report_out.read_text(encoding="utf-8"))
    assert payload["threshold_check"]["ok"] is True
    assert payload["threshold_check"]["violations"] == []

  def test_threshold_violation_exits_one_and_writes_report(
      self, tmp_path: pathlib.Path, cleanup_reference_modules
  ):
    """Compiled extractor drifts → run completes but the
    threshold check fails. Exit 1, report still written with
    violations listed."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root, source=_COMPILED_SOURCE_DRIFTS)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("violation")

    thresholds_path = tmp_path / "thresholds.json"
    thresholds_path.write_text(
        json.dumps({"min_compiled_unchanged_rate": 0.95}),
        encoding="utf-8",
    )
    report_out = tmp_path / "report.json"

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--thresholds-json",
            str(thresholds_path),
            "--report-out",
            str(report_out),
        ]
    )

    assert code == 1
    payload = json.loads(report_out.read_text(encoding="utf-8"))
    assert payload["threshold_check"]["ok"] is False
    assert any(
        "compiled_unchanged_rate" in v
        for v in payload["threshold_check"]["violations"]
    )
    # The raw RevalidationReport is still included alongside
    # the threshold check.
    assert payload["report"]["total_compiled_filtered"] == 3


# ------------------------------------------------------------------ #
# Per-input usage errors → exit 2                                    #
# ------------------------------------------------------------------ #


class TestCliUsageErrors:

  def _common_args(
      self,
      tmp_path: pathlib.Path,
      *,
      bundles_root: pathlib.Path | None = None,
      events_path: pathlib.Path | None = None,
      ref_module: str = "_bqaa_cli_test_ref_missing",
      report_out: pathlib.Path | None = None,
  ) -> list[str]:
    return [
        "--bundles-root",
        str(bundles_root or tmp_path / "bundles"),
        "--events-jsonl",
        str(events_path or tmp_path / "events.jsonl"),
        "--reference-extractors-module",
        ref_module,
        "--report-out",
        str(report_out or tmp_path / "report.json"),
    ]

  def test_missing_events_file(
      self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
  ):
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(tmp_path / "does-not-exist.jsonl"),
            "--reference-extractors-module",
            "irrelevant",
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "--events-jsonl" in err
    assert "is not a file" in err
    # No report leaked out on usage failure.
    assert not (tmp_path / "report.json").exists()

  def test_malformed_jsonl_line(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """A non-empty line that isn't valid JSON aborts with exit
    2 naming the line number — the harness's ``skipped_events``
    path is for legitimately-shaped events without coverage,
    NOT corrupted input."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"event_type": "bka_decision", "span_id": "sp1", "session_id": "s", '
        '"content": {"decision_id": "d1"}}\n'
        "not valid json line\n",
        encoding="utf-8",
    )
    ref_module = cleanup_reference_modules("malformed")

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "line 2" in err

  def test_missing_bundles_root(
      self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
  ):
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    code = main(
        [
            "--bundles-root",
            str(tmp_path / "missing"),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            "irrelevant",
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    assert "--bundles-root" in capsys.readouterr().err

  def test_mixed_fingerprint_bundles_root(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """Two bundles under the same root declaring different
    fingerprints is a deployment mistake for revalidation. The
    CLI fails-closed at fingerprint detection (exit 2) so the
    harness never sees a mixed registry."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root, name="one", fingerprint=_VALID_FINGERPRINT)
    _build_bundle(bundles_root, name="two", fingerprint=_OTHER_FINGERPRINT)

    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("mixed")

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "multiple fingerprints" in err

  def test_empty_bundles_root(
      self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
  ):
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            "irrelevant",
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    assert "no bundle subdirectories" in capsys.readouterr().err

  def test_reference_module_not_importable(
      self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
  ):
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            "this.module.does.not.exist",
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    assert "not importable" in capsys.readouterr().err

  def test_reference_module_missing_extractors(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules(
        "no_extractors", has_extractors=False
    )

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "EXTRACTORS" in err

  def test_reference_module_missing_resolved_graph(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules(
        "no_resolved_graph", has_resolved_graph=False
    )

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "RESOLVED_GRAPH" in err

  def test_reference_module_bad_extractors_shape(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """``EXTRACTORS = "not a dict"`` is rejected at the CLI
    boundary so the harness never sees a malformed registry."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules(
        "bad_shape", bad_extractor_value="not a dict"
    )

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "non-empty dict" in err

  def test_thresholds_with_unknown_field(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """Unknown threshold fields are rejected so a typo doesn't
    silently produce a no-op gate."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("unknown_field")

    thresholds_path = tmp_path / "thresholds.json"
    thresholds_path.write_text(
        json.dumps({"min_compiled_unchnged_rate": 0.95}),  # typo
        encoding="utf-8",
    )

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--thresholds-json",
            str(thresholds_path),
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown fields" in err

  def test_thresholds_out_of_range(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """Rate threshold outside [0,1] is rejected via
    ``RevalidationThresholds.__post_init__`` and surfaces as
    a clean CLI usage error (not a Python traceback)."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("out_of_range")

    thresholds_path = tmp_path / "thresholds.json"
    thresholds_path.write_text(
        json.dumps({"max_fallback_for_event_rate": 5.0}),  # 5% typo
        encoding="utf-8",
    )

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--thresholds-json",
            str(thresholds_path),
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "validation failed" in err

  def test_report_out_parent_missing_exits_two_cleanly(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """``--report-out missing/report.json`` used to raise
    ``FileNotFoundError`` out of ``_write_report``. Preflight
    in ``_load_config`` now catches the missing parent
    directory before doing any work; the CLI exits 2 with a
    clean message instead of a traceback."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("missing_parent")

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(tmp_path / "does-not-exist" / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "--report-out" in err
    assert "does not exist" in err
    # Crucially: no report leaked into the (nonexistent)
    # parent dir.
    assert not (tmp_path / "does-not-exist").exists()

  def test_events_jsonl_invalid_utf8_exits_two_cleanly(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """Invalid UTF-8 in ``--events-jsonl`` (e.g. a binary file
    passed by mistake) used to escape as ``UnicodeDecodeError``.
    Now surfaces as a clean exit 2 with the file path
    named."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)

    # Bytes that aren't valid UTF-8 — 0xff is never a valid
    # start byte.
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(b"\xff\xfe not utf-8\n")

    ref_module = cleanup_reference_modules("bad_utf8_events")

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "--events-jsonl" in err
    assert "UTF-8" in err
    assert not (tmp_path / "report.json").exists()

  def test_thresholds_json_invalid_utf8_exits_two_cleanly(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
      cleanup_reference_modules,
  ):
    """Same defensive shape on the thresholds reader."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    _build_bundle(bundles_root)
    events_path = _write_events_jsonl(tmp_path / "events.jsonl")
    ref_module = cleanup_reference_modules("bad_utf8_thresholds")

    thresholds_path = tmp_path / "thresholds.json"
    thresholds_path.write_bytes(b"\xff\xfe not utf-8")

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-jsonl",
            str(events_path),
            "--reference-extractors-module",
            ref_module,
            "--thresholds-json",
            str(thresholds_path),
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "--thresholds-json" in err
    assert "UTF-8" in err

  def test_missing_required_flag_returns_two_not_systemexit(
      self,
      capsys: pytest.CaptureFixture,
  ):
    """``argparse.ArgumentParser.parse_args`` defaults to
    calling ``sys.exit(2)`` on missing required arguments,
    which would bypass ``main()``'s documented return-code
    contract — tests calling ``main([])`` would see a raised
    ``SystemExit`` rather than a return value. The custom
    ``_NonExitingArgumentParser.error()`` funnels argparse
    errors through ``_CliError`` so ``main(argv)`` reliably
    *returns* ``EXIT_USAGE_ERROR``."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    # No SystemExit must escape. Empty argv -> first
    # required-arg error.
    code = main([])
    assert code == 2
    err = capsys.readouterr().err
    # argparse's standard wording is preserved so users see
    # the same message they'd get from any argparse-driven
    # CLI.
    assert "the following arguments are required" in err
    # The usage line is still printed first (matches argparse's
    # default UX) so the user can see what flags exist.
    assert "usage:" in err

  def test_unrecognized_flag_returns_two_not_systemexit(
      self,
      tmp_path: pathlib.Path,
      capsys: pytest.CaptureFixture,
  ):
    """Same contract for unrecognized flags — argparse would
    call ``sys.exit(2)``; the custom parser routes through
    ``_CliError`` so ``main()`` returns instead."""
    from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

    # All required flags supplied so the unrecognized-arg
    # error is the one that fires (argparse reports
    # missing-required before unrecognized).
    code = main(
        [
            "--bundles-root",
            str(tmp_path),
            "--events-jsonl",
            str(tmp_path / "events.jsonl"),
            "--reference-extractors-module",
            "irrelevant",
            "--report-out",
            str(tmp_path / "report.json"),
            "--no-such-flag",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err

  def test_console_script_entry_point_registered(self):
    """The ``console_scripts`` entry in ``pyproject.toml``
    points at :func:`main`. Lock with importlib metadata so
    a typo in the entry-point string fails CI rather than
    breaking the binary at user-install time."""
    try:
      from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover — py<3.8
      return
    eps = entry_points(group="console_scripts")
    matches = [ep for ep in eps if ep.name == "bqaa-revalidate-extractors"]
    if not matches:
      pytest.skip(
          "console_scripts entry not visible in dev install; "
          "pyproject.toml is the source of truth"
      )
    assert (
        matches[0].value
        == "bigquery_agent_analytics.extractor_compilation.cli_revalidate:main"
    )
