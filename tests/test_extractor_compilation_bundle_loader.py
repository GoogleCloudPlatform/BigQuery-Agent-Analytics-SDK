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

"""Tests for the bundle loader + discovery (#75 PR C2.a).

Strategy:

* Most tests construct hand-built bundles on disk so we exercise
  every ``LoadFailure`` code without depending on the full
  compile pipeline.
* One end-to-end test runs the real ``compile_extractor`` to
  produce a bundle, then loads it through the public surface and
  invokes the callable — proves the loader's contract holds for
  bundles produced by the rest of Phase C.
"""

from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

# ------------------------------------------------------------------ #
# Fixture helpers — hand-built bundles                                #
# ------------------------------------------------------------------ #


_VALID_FINGERPRINT = "a" * 64


def _write_manifest(
    bundle_dir: pathlib.Path,
    *,
    fingerprint: str = _VALID_FINGERPRINT,
    event_types: tuple[str, ...] = ("bka_decision",),
    module_filename: str = "extractor.py",
    function_name: str = "extract_bka",
    template_version: str = "v0.1",
    compiler_package_version: str = "0.0.0",
    transcript_builder_version: str = "tb-1",
    created_at: str = "2026-05-08T00:00:00Z",
) -> None:
  """Write a manifest.json with the supplied fields. Useful when
  a test wants a manifest for a callable that wouldn't pass the
  full compile pipeline (signature mismatch, missing function,
  etc.)."""
  bundle_dir.mkdir(parents=True, exist_ok=True)
  manifest = {
      "fingerprint": fingerprint,
      "event_types": list(event_types),
      "module_filename": module_filename,
      "function_name": function_name,
      "compiler_package_version": compiler_package_version,
      "template_version": template_version,
      "transcript_builder_version": transcript_builder_version,
      "created_at": created_at,
  }
  (bundle_dir / "manifest.json").write_text(
      json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
  )


def _write_module(
    bundle_dir: pathlib.Path,
    *,
    source: str,
    module_filename: str = "extractor.py",
) -> None:
  bundle_dir.mkdir(parents=True, exist_ok=True)
  (bundle_dir / module_filename).write_text(source, encoding="utf-8")


_MINIMAL_VALID_SOURCE = textwrap.dedent(
    """\
    def extract_bka(event, spec):
        return None
"""
)
"""Smallest source the loader will accept: a callable named
``extract_bka`` that takes ``(event, spec)``. The loader doesn't
care what the callable returns; that's the smoke gate's
responsibility at compile time."""


# ------------------------------------------------------------------ #
# load_bundle — happy path                                            #
# ------------------------------------------------------------------ #


class TestLoadBundleHappyPath:

  def test_valid_bundle_loads(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadedBundle

    _write_manifest(tmp_path)
    _write_module(tmp_path, source=_MINIMAL_VALID_SOURCE)

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        expected_event_types=("bka_decision",),
    )
    assert isinstance(result, LoadedBundle)
    assert result.bundle_dir == tmp_path
    assert result.manifest.fingerprint == _VALID_FINGERPRINT
    assert callable(result.extractor)

  def test_event_types_subset_check_accepts_broader_manifest(
      self, tmp_path: pathlib.Path
  ):
    """The manifest is allowed to cover *more* event_types than
    the caller asked for. Subset semantics."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadedBundle

    _write_manifest(tmp_path, event_types=("bka_decision", "extra_event"))
    _write_module(tmp_path, source=_MINIMAL_VALID_SOURCE)

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        expected_event_types=("bka_decision",),
    )
    assert isinstance(result, LoadedBundle)

  def test_event_types_check_skipped_when_none(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadedBundle

    _write_manifest(tmp_path, event_types=("anything",))
    _write_module(tmp_path, source=_MINIMAL_VALID_SOURCE)

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        expected_event_types=None,
    )
    assert isinstance(result, LoadedBundle)


# ------------------------------------------------------------------ #
# load_bundle — failure codes (one test per code)                     #
# ------------------------------------------------------------------ #


class TestLoadBundleFailureCodes:
  """Each test exercises one stable LoadFailure code. The loader
  must never raise — every defect surfaces as a structured
  failure record."""

  def test_manifest_missing(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    # Empty bundle dir — no manifest.json, no module.
    tmp_path.mkdir(exist_ok=True)
    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "manifest_missing"
    assert result.bundle_dir == tmp_path

  def test_manifest_unreadable_invalid_json(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "manifest.json").write_text(
        "{not: 'valid json'", encoding="utf-8"
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "manifest_unreadable"

  def test_manifest_unreadable_missing_required_field(
      self, tmp_path: pathlib.Path
  ):
    """A JSON document that parses fine but doesn't satisfy the
    Manifest schema lands as ``manifest_unreadable``."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"only_field": "value"}), encoding="utf-8"
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "manifest_unreadable"

  def test_fingerprint_mismatch(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path, fingerprint="a" * 64)
    _write_module(tmp_path, source=_MINIMAL_VALID_SOURCE)

    result = load_bundle(
        tmp_path,
        expected_fingerprint="b" * 64,  # different
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "fingerprint_mismatch"
    # Detail names both fingerprints so an operator can diff.
    assert "a" * 64 in result.detail
    assert "b" * 64 in result.detail

  def test_event_types_mismatch(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path, event_types=("only_this",))
    _write_module(tmp_path, source=_MINIMAL_VALID_SOURCE)

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        expected_event_types=("only_this", "missing_one"),
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "event_types_mismatch"
    # Detail names the missing event type.
    assert "missing_one" in result.detail

  def test_module_not_found(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    # Manifest references extractor.py but no source file exists.
    _write_manifest(tmp_path)

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "module_not_found"

  def test_import_failed_syntax_error(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path)
    _write_module(tmp_path, source="def broken(:\n  pass\n")

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "import_failed"
    assert "SyntaxError" in result.detail

  def test_import_failed_runtime_exception(self, tmp_path: pathlib.Path):
    """An import-time exception must be captured as a failure
    record, never propagated. A runtime that imports a malicious
    or broken bundle wouldn't survive the import step otherwise."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path)
    _write_module(
        tmp_path,
        source="raise RuntimeError('boom at import time')\n",
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "import_failed"
    assert "boom at import time" in result.detail

  def test_import_failed_systemexit_captured(self, tmp_path: pathlib.Path):
    """``BaseException``s like ``SystemExit`` must also be
    captured — a bundle that calls ``sys.exit(1)`` at import
    time mustn't tear the loading process down."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path)
    _write_module(
        tmp_path,
        source=("import sys\n" "sys.exit('bundle decided to exit')\n"),
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "import_failed"
    assert "SystemExit" in result.detail

  def test_function_not_found(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    # Manifest claims function_name=extract_bka, module defines
    # something else.
    _write_manifest(tmp_path, function_name="extract_bka")
    _write_module(
        tmp_path,
        source="def some_other_name(event, spec):\n  return None\n",
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "function_not_found"
    assert "extract_bka" in result.detail

  def test_function_not_callable(self, tmp_path: pathlib.Path):
    """``function_name`` exists in the module but isn't callable
    — same code as ``function_not_found`` so callers don't have
    to switch on a separate ``function_not_callable`` value."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path, function_name="extract_bka")
    _write_module(
        tmp_path,
        source='extract_bka = "not a callable"\n',
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "function_not_found"

  def test_function_signature_mismatch_too_few_args(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path, function_name="extract_bka")
    _write_module(tmp_path, source="def extract_bka(event):\n  return None\n")

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "function_signature_mismatch"

  def test_function_signature_mismatch_kwargs_only(
      self, tmp_path: pathlib.Path
  ):
    """``def f(*, event, spec)`` can't be called as
    ``f(event_value, spec_value)`` — the loader rejects."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path, function_name="extract_bka")
    _write_module(
        tmp_path,
        source="def extract_bka(*, event, spec):\n  return None\n",
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "function_signature_mismatch"

  def test_function_signature_with_var_positional_accepted(
      self, tmp_path: pathlib.Path
  ):
    """``def f(*args)`` can be called as ``f(event, spec)`` —
    the signature gate is permissive for compatible variants."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadedBundle

    _write_manifest(tmp_path, function_name="extract_bka")
    _write_module(
        tmp_path,
        source="def extract_bka(*args, **kwargs):\n  return None\n",
    )

    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadedBundle)


# ------------------------------------------------------------------ #
# load_bundle — gate ordering                                         #
# ------------------------------------------------------------------ #


class TestLoadBundleGateOrdering:

  def test_fingerprint_check_runs_before_module_load(
      self, tmp_path: pathlib.Path
  ):
    """A bundle with a wrong fingerprint AND a broken module
    must fail with ``fingerprint_mismatch`` (the earlier gate),
    not ``import_failed``. The gate order is what makes the
    fingerprint a real boundary: an attacker can't side-effect
    via a broken module if their fingerprint doesn't match."""
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadFailure

    _write_manifest(tmp_path, fingerprint="a" * 64)
    _write_module(tmp_path, source="raise RuntimeError('should not import')\n")

    result = load_bundle(
        tmp_path,
        expected_fingerprint="b" * 64,
    )
    assert isinstance(result, LoadFailure)
    assert result.code == "fingerprint_mismatch"


# ------------------------------------------------------------------ #
# discover_bundles — happy path + filtering                           #
# ------------------------------------------------------------------ #


class TestDiscoverBundles:

  def test_empty_parent_dir_returns_empty_result(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert result.registry == {}
    assert result.loaded == ()
    assert result.failures == ()

  def test_nonexistent_parent_dir_returns_failure(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    result = discover_bundles(
        tmp_path / "does_not_exist",
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert result.registry == {}
    assert len(result.failures) == 1
    assert result.failures[0].code == "manifest_missing"

  def test_single_valid_bundle_registers(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    bundle_a = tmp_path / "bundle_a"
    _write_manifest(bundle_a, event_types=("bka_decision",))
    _write_module(bundle_a, source=_MINIMAL_VALID_SOURCE)

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert set(result.registry.keys()) == {"bka_decision"}
    assert callable(result.registry["bka_decision"])
    assert len(result.loaded) == 1
    assert result.failures == ()

  def test_multi_event_bundle_registers_all_event_types(
      self, tmp_path: pathlib.Path
  ):
    """A manifest declaring ``("a", "b")`` should register the
    same callable under both keys."""
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    bundle = tmp_path / "multi"
    _write_manifest(bundle, event_types=("event_a", "event_b"))
    _write_module(bundle, source=_MINIMAL_VALID_SOURCE)

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert set(result.registry.keys()) == {"event_a", "event_b"}
    assert result.registry["event_a"] is result.registry["event_b"]
    assert result.failures == ()

  def test_event_type_allowlist_filters_registry(self, tmp_path: pathlib.Path):
    """A manifest with broader coverage than the allowlist
    registers only the intersection — the bundle still loads,
    but unwanted event_types don't enter the registry."""
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    bundle = tmp_path / "broad"
    _write_manifest(bundle, event_types=("event_a", "event_b", "event_c"))
    _write_module(bundle, source=_MINIMAL_VALID_SOURCE)

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        event_type_allowlist=("event_a", "event_c"),
    )
    assert set(result.registry.keys()) == {"event_a", "event_c"}
    # Bundle loaded successfully even though event_b was filtered out.
    assert len(result.loaded) == 1
    assert result.failures == ()

  def test_empty_allowlist_registers_nothing(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    bundle = tmp_path / "any"
    _write_manifest(bundle)
    _write_module(bundle, source=_MINIMAL_VALID_SOURCE)

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        event_type_allowlist=(),
    )
    assert result.registry == {}
    # The bundle still loaded — empty allowlist is a scope filter,
    # not a load gate.
    assert len(result.loaded) == 1


# ------------------------------------------------------------------ #
# discover_bundles — collisions                                       #
# ------------------------------------------------------------------ #


class TestDiscoverBundlesCollisions:
  """Two bundles claiming the same event_type must fail closed:
  drop the event_type from the registry, emit one collision
  failure record per claimant. Silently picking one bundle would
  make runtime behavior depend on filesystem ordering."""

  def test_two_bundles_same_event_type_fails_closed(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    bundle_a = tmp_path / "bundle_a"
    _write_manifest(bundle_a, event_types=("shared_event",))
    _write_module(bundle_a, source=_MINIMAL_VALID_SOURCE)

    bundle_b = tmp_path / "bundle_b"
    _write_manifest(bundle_b, event_types=("shared_event",))
    _write_module(bundle_b, source=_MINIMAL_VALID_SOURCE)

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    # Failed closed: shared_event NOT in the registry.
    assert "shared_event" not in result.registry
    # Both bundles loaded successfully (the parse+import succeeded).
    assert len(result.loaded) == 2
    # One collision failure record per claimant.
    collision_failures = [
        f for f in result.failures if f.code == "event_type_collision"
    ]
    assert len(collision_failures) == 2
    assert {f.bundle_dir for f in collision_failures} == {
        bundle_a,
        bundle_b,
    }
    for failure in collision_failures:
      assert "shared_event" in failure.detail

  def test_partial_collision_preserves_unique_event_types(
      self, tmp_path: pathlib.Path
  ):
    """Bundle A: ('shared', 'a_only'). Bundle B: ('shared',
    'b_only'). 'shared' fails closed; 'a_only' and 'b_only'
    each register from their respective bundle."""
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    bundle_a = tmp_path / "bundle_a"
    _write_manifest(bundle_a, event_types=("shared", "a_only"))
    _write_module(bundle_a, source=_MINIMAL_VALID_SOURCE)

    bundle_b = tmp_path / "bundle_b"
    _write_manifest(bundle_b, event_types=("shared", "b_only"))
    _write_module(bundle_b, source=_MINIMAL_VALID_SOURCE)

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert set(result.registry.keys()) == {"a_only", "b_only"}
    collision_failures = [
        f for f in result.failures if f.code == "event_type_collision"
    ]
    assert len(collision_failures) == 2


# ------------------------------------------------------------------ #
# discover_bundles — non-bundle entries are tolerated                 #
# ------------------------------------------------------------------ #


class TestDiscoverBundlesNonBundleEntries:

  def test_loose_files_in_parent_are_ignored(self, tmp_path: pathlib.Path):
    """A bundle root may legitimately contain other files (a
    README, an index, …); they shouldn't make discovery fail."""
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    (tmp_path / "README.md").write_text("# bundles", encoding="utf-8")
    (tmp_path / "INDEX.txt").write_text("a\nb\n", encoding="utf-8")

    bundle = tmp_path / "real_bundle"
    _write_manifest(bundle)
    _write_module(bundle, source=_MINIMAL_VALID_SOURCE)

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert "bka_decision" in result.registry
    assert result.failures == ()

  def test_non_bundle_subdirectory_fails_with_manifest_missing(
      self, tmp_path: pathlib.Path
  ):
    """A child subdirectory without a manifest.json gets a
    ``manifest_missing`` failure — every directory the discovery
    walked is accounted for, no silent skips. Loose files (above)
    are skipped because they're files, not subdirectories."""
    from bigquery_agent_analytics.extractor_compilation import discover_bundles

    # No manifest, no module — just an empty subdir.
    (tmp_path / "empty_subdir").mkdir()

    result = discover_bundles(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert result.registry == {}
    assert len(result.failures) == 1
    assert result.failures[0].code == "manifest_missing"


# ------------------------------------------------------------------ #
# End-to-end: real compile_extractor → load_bundle → invoke           #
# ------------------------------------------------------------------ #


class TestBundleLoaderEndToEnd:
  """One integration test that runs the *real* compile pipeline
  and proves the loader loads its output. If this test passes,
  the loader's contract holds for bundles produced by the rest of
  Phase C — not just for hand-built fixtures."""

  def test_compiled_bka_bundle_loads_and_invokes(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadedBundle
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_RESOLVED_PLAN_DICT
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    # Resolve the BKA spec for the smoke gate.
    spec_dir = pathlib.Path(tmp_path / "spec")
    spec_dir.mkdir()
    (spec_dir / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
    (spec_dir / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
    ontology = load_ontology(str(spec_dir / "ont.yaml"))
    binding = load_binding(str(spec_dir / "bnd.yaml"), ontology=ontology)
    resolved_graph = resolve(ontology, binding)

    # Render + compile.
    plan = parse_resolved_extractor_plan_json(BKA_RESOLVED_PLAN_DICT)
    source = render_extractor_source(plan)
    bundle_root = tmp_path / "bundles"
    compile_result = compile_extractor(
        source=source,
        module_name="bka_loader_test_module",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=BKA_SAMPLE_EVENTS,
        spec=None,
        resolved_graph=resolved_graph,
        parent_bundle_dir=bundle_root,
        fingerprint_inputs=BKA_FINGERPRINT_INPUTS,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )
    assert compile_result.ok, (
        f"end-to-end compile failed: ast={compile_result.ast_report.failures} "
        f"smoke={compile_result.smoke_report and compile_result.smoke_report.exceptions or []}"
    )

    # Load the bundle through the public surface.
    bundle_dir = compile_result.bundle_dir
    assert bundle_dir is not None
    loaded = load_bundle(
        bundle_dir,
        expected_fingerprint=compile_result.manifest.fingerprint,
        expected_event_types=("bka_decision",),
    )
    assert isinstance(
        loaded, LoadedBundle
    ), f"expected LoadedBundle, got {loaded!r}"

    # Invoke the loaded callable on a sample event and assert it
    # produces output equivalent to the reference extractor —
    # proves the loaded callable is the *real* extractor, not a
    # placeholder.
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    for event in BKA_SAMPLE_EVENTS:
      ref_result = extract_bka_decision_event(event, None)
      loaded_result = loaded.extractor(event, None)
      ref_node_ids = {n.node_id for n in ref_result.nodes}
      loaded_node_ids = {n.node_id for n in loaded_result.nodes}
      assert ref_node_ids == loaded_node_ids
      assert (
          ref_result.fully_handled_span_ids
          == loaded_result.fully_handled_span_ids
      )
      assert (
          ref_result.partially_handled_span_ids
          == loaded_result.partially_handled_span_ids
      )

  def test_compiled_bka_bundle_discovers_through_discover_bundles(
      self, tmp_path: pathlib.Path
  ):
    """Same setup as above, but exercise ``discover_bundles``
    instead — proves the discovery path also works on a real
    compile output."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import discover_bundles
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_RESOLVED_PLAN_DICT
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    spec_dir = pathlib.Path(tmp_path / "spec")
    spec_dir.mkdir()
    (spec_dir / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
    (spec_dir / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
    ontology = load_ontology(str(spec_dir / "ont.yaml"))
    binding = load_binding(str(spec_dir / "bnd.yaml"), ontology=ontology)
    resolved_graph = resolve(ontology, binding)

    plan = parse_resolved_extractor_plan_json(BKA_RESOLVED_PLAN_DICT)
    source = render_extractor_source(plan)
    bundle_root = tmp_path / "bundles"
    compile_result = compile_extractor(
        source=source,
        module_name="bka_discover_test_module",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=BKA_SAMPLE_EVENTS,
        spec=None,
        resolved_graph=resolved_graph,
        parent_bundle_dir=bundle_root,
        fingerprint_inputs=BKA_FINGERPRINT_INPUTS,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )
    assert compile_result.ok

    discovered = discover_bundles(
        bundle_root,
        expected_fingerprint=compile_result.manifest.fingerprint,
        event_type_allowlist=("bka_decision",),
    )
    assert "bka_decision" in discovered.registry
    assert len(discovered.loaded) == 1
    assert discovered.failures == ()


# ------------------------------------------------------------------ #
# Strict manifest validation (review P1 #1)                           #
# ------------------------------------------------------------------ #
#
# The lenient ``Manifest.from_json`` would silently accept
# malformed shapes (``tuple("xy") == ("x", "y")``,
# ``module_filename: 42`` lands in the dataclass without raising,
# etc.). The loader's trust boundary now does its own strict
# parse — every test below feeds a deliberately-broken manifest
# JSON straight to disk and asserts the loader rejects with
# ``manifest_unreadable`` instead of raising or loading nonsense.


def _write_raw_manifest(bundle_dir: pathlib.Path, payload: dict) -> None:
  """Like ``_write_manifest`` but takes the full dict verbatim
  (no defaults, no field overrides). Used to write deliberately-
  malformed manifests."""
  bundle_dir.mkdir(parents=True, exist_ok=True)
  (bundle_dir / "manifest.json").write_text(
      json.dumps(payload), encoding="utf-8"
  )


def _baseline_manifest_dict() -> dict:
  return {
      "fingerprint": _VALID_FINGERPRINT,
      "event_types": ["bka_decision"],
      "module_filename": "extractor.py",
      "function_name": "extract_bka",
      "compiler_package_version": "0.0.0",
      "template_version": "v0.1",
      "transcript_builder_version": "tb-1",
      "created_at": "2026-05-08T00:00:00Z",
  }


@pytest.mark.parametrize(
    "label,mutate",
    [
        (
            "event_types as string coerces to chars",
            lambda d: d.update({"event_types": "xy"}),
        ),
        ("event_types empty list", lambda d: d.update({"event_types": []})),
        (
            "event_types with empty string item",
            lambda d: d.update({"event_types": ["a", ""]}),
        ),
        (
            "event_types with non-string item",
            lambda d: d.update({"event_types": [1, 2]}),
        ),
        (
            "event_types with duplicate items",
            lambda d: d.update({"event_types": ["a", "a"]}),
        ),
        ("module_filename as int", lambda d: d.update({"module_filename": 42})),
        (
            "module_filename without .py",
            lambda d: d.update({"module_filename": "extractor"}),
        ),
        (
            "module_filename with double dot",
            lambda d: d.update({"module_filename": "foo.bar.py"}),
        ),
        (
            "module_filename Python keyword",
            lambda d: d.update({"module_filename": "class.py"}),
        ),
        ("module_filename empty", lambda d: d.update({"module_filename": ""})),
        ("function_name as int", lambda d: d.update({"function_name": 42})),
        (
            "function_name with dash",
            lambda d: d.update({"function_name": "not-an-identifier"}),
        ),
        (
            "function_name Python keyword",
            lambda d: d.update({"function_name": "class"}),
        ),
        ("function_name empty", lambda d: d.update({"function_name": ""})),
        ("fingerprint as int", lambda d: d.update({"fingerprint": 12345})),
        ("fingerprint empty string", lambda d: d.update({"fingerprint": ""})),
        (
            "unknown extra field",
            lambda d: d.update({"surprising_extra_field": "value"}),
        ),
        ("missing required field", lambda d: d.pop("created_at")),
    ],
)
def test_malformed_manifest_rejected_with_manifest_unreadable(
    tmp_path: pathlib.Path, label: str, mutate
):
  from bigquery_agent_analytics.extractor_compilation import load_bundle
  from bigquery_agent_analytics.extractor_compilation import LoadFailure

  payload = _baseline_manifest_dict()
  mutate(payload)
  _write_raw_manifest(tmp_path, payload)
  _write_module(tmp_path, source=_MINIMAL_VALID_SOURCE)

  result = load_bundle(
      tmp_path,
      expected_fingerprint=_VALID_FINGERPRINT,
  )
  assert isinstance(
      result, LoadFailure
  ), f"[{label}] expected LoadFailure, got {result!r}"
  assert (
      result.code == "manifest_unreadable"
  ), f"[{label}] expected manifest_unreadable, got {result.code}"


def test_malformed_manifest_root_array_rejected(tmp_path: pathlib.Path):
  """A JSON document whose root is an array (not an object) must
  surface as ``manifest_unreadable`` — not raise."""
  from bigquery_agent_analytics.extractor_compilation import load_bundle
  from bigquery_agent_analytics.extractor_compilation import LoadFailure

  tmp_path.mkdir(exist_ok=True)
  (tmp_path / "manifest.json").write_text(
      json.dumps([1, 2, 3]), encoding="utf-8"
  )

  result = load_bundle(
      tmp_path,
      expected_fingerprint=_VALID_FINGERPRINT,
  )
  assert isinstance(result, LoadFailure)
  assert result.code == "manifest_unreadable"


def test_invalid_utf8_manifest_rejected(tmp_path: pathlib.Path):
  """A ``manifest.json`` containing bytes that aren't valid UTF-8
  must surface as ``manifest_unreadable``. ``Path.read_text`` raises
  ``UnicodeDecodeError`` (a subclass of ``UnicodeError``), which
  isn't caught by an ``OSError``-only clause; this test pins the
  loader's wider ``(OSError, UnicodeError)`` catch."""
  from bigquery_agent_analytics.extractor_compilation import load_bundle
  from bigquery_agent_analytics.extractor_compilation import LoadFailure

  tmp_path.mkdir(exist_ok=True)
  # Lone surrogates / BOM-like bytes that aren't valid UTF-8.
  (tmp_path / "manifest.json").write_bytes(b"\xff\xfe\xff\xfe")

  result = load_bundle(
      tmp_path,
      expected_fingerprint=_VALID_FINGERPRINT,
  )
  assert isinstance(result, LoadFailure)
  assert result.code == "manifest_unreadable"
  assert "UnicodeDecodeError" in result.detail


# ------------------------------------------------------------------ #
# Path-traversal defense (review P1 #2)                               #
# ------------------------------------------------------------------ #


def test_module_filename_path_traversal_rejected(tmp_path: pathlib.Path):
  """A manifest claiming ``module_filename = '../escape.py'`` must
  not succeed in importing a sibling file outside the bundle.
  This is the security boundary the trust boundary is for."""
  from bigquery_agent_analytics.extractor_compilation import load_bundle
  from bigquery_agent_analytics.extractor_compilation import LoadFailure

  bundle_dir = tmp_path / "bundle"
  bundle_dir.mkdir()
  outside = tmp_path / "escape.py"
  outside.write_text(
      "raise RuntimeError('outside-bundle module should not import')\n",
      encoding="utf-8",
  )
  _write_raw_manifest(
      bundle_dir,
      {**_baseline_manifest_dict(), "module_filename": "../escape.py"},
  )

  result = load_bundle(
      bundle_dir,
      expected_fingerprint=_VALID_FINGERPRINT,
  )
  # Must reject at the manifest-parse step, *before* any import
  # attempt. The outside module's RuntimeError must NOT have run.
  assert isinstance(result, LoadFailure)
  assert result.code == "manifest_unreadable"


def test_module_filename_absolute_path_rejected(tmp_path: pathlib.Path):
  from bigquery_agent_analytics.extractor_compilation import load_bundle
  from bigquery_agent_analytics.extractor_compilation import LoadFailure

  _write_raw_manifest(
      tmp_path,
      {**_baseline_manifest_dict(), "module_filename": "/etc/passwd.py"},
  )

  result = load_bundle(
      tmp_path,
      expected_fingerprint=_VALID_FINGERPRINT,
  )
  assert isinstance(result, LoadFailure)
  assert result.code == "manifest_unreadable"


# ------------------------------------------------------------------ #
# sys.modules leak (review P2)                                        #
# ------------------------------------------------------------------ #


def test_repeated_load_does_not_leak_sys_modules(tmp_path: pathlib.Path):
  """Each successful load used to leave a ``<stem>__loaded_<uuid>``
  entry in ``sys.modules``. Runtime discovery can run repeatedly,
  so that's process-global growth. The loader now pops the entry
  once the callable has been captured."""
  import sys

  from bigquery_agent_analytics.extractor_compilation import load_bundle
  from bigquery_agent_analytics.extractor_compilation import LoadedBundle

  _write_manifest(tmp_path)
  _write_module(tmp_path, source=_MINIMAL_VALID_SOURCE)

  before = sum(1 for name in sys.modules if "__loaded_" in name)
  for _ in range(5):
    result = load_bundle(
        tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(result, LoadedBundle)
    # The captured callable must remain valid even after
    # sys.modules cleanup — invoking it has to work.
    assert result.extractor({"event_type": "bka_decision"}, None) is None
  after = sum(1 for name in sys.modules if "__loaded_" in name)

  assert after == before, (
      f"sys.modules grew by {after - before} __loaded_ entries "
      f"across 5 load_bundle calls"
  )


# ------------------------------------------------------------------ #
# discover_bundles iterdir failure (review P3)                        #
# ------------------------------------------------------------------ #


def test_discover_bundles_handles_iterdir_oserror(
    monkeypatch, tmp_path: pathlib.Path
):
  """``parent_dir.iterdir()`` can raise ``PermissionError`` /
  ``OSError`` on filesystem races or restricted access. The
  module's contract is "never raises through to the caller";
  monkeypatch ``Path.iterdir`` to surface that path."""
  from bigquery_agent_analytics.extractor_compilation import discover_bundles

  def boom(self):
    raise PermissionError("EACCES (simulated)")

  monkeypatch.setattr(pathlib.Path, "iterdir", boom)

  result = discover_bundles(
      tmp_path,
      expected_fingerprint=_VALID_FINGERPRINT,
  )
  assert result.registry == {}
  assert result.loaded == ()
  assert len(result.failures) == 1
  failure = result.failures[0]
  assert failure.code == "manifest_missing"
  assert "PermissionError" in failure.detail
  assert "EACCES" in failure.detail
