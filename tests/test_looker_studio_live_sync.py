# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the tools of the BQCA live template sync (#559).

`tools/finalize_live_sync.py` records a live template verification in
`bindings/bqca_report_template.yaml` only after `tools/validate_contracts.py
--profile bqca --live-report-textproto` passes on a dump of the live template,
and only with positive evidence that the dump is one: a `--captured-from`
attestation that names the report, and a dump that names the report in a
field. `tools/gen_bqca_rename_map.py` lists the rename that the sync applies
to the concepts of the live template.

The finalize tests run the tool from a copy of `dashboards/looker_studio`, so
they never write to the repository. A successful write uses a dump that
`_render_dump` generates from the copy's manifest, with test-only provenance:
a `report_id` field and the ATTESTATION. The checked-in sanitized cuts of the
2026-10-29 dump name the report in no field, so neither they nor any
reordering or audit-inert edit of them can be written; `--check` still
previews with them.
"""

import collections
import copy
import csv
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import types

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboards/looker_studio"
FIXTURES = ROOT / "tests/fixtures"
GENERIC_DUMP = FIXTURES / "bqca_live_report_42e79a1f_sanitized.textproto"
REMAPPED_DUMP = (
    FIXTURES / "bqca_live_report_42e79a1f_sanitized_remapped.textproto"
)
BINDING = "bindings/bqca_report_template.yaml"
LIVE = "live_template_verification"
PENDING = "pending_live_sync"
REPORT_ID = "42e79a1f-a979-4de8-911f-80d5a99543d5"
DATASOURCE_ID = "70e5ac1e-65bb-4c89-a9df-fa9990cb6029"
STALE_DATASOURCE_ID = "00000000-0000-4000-8000-000000000000"
QT_ID = re.compile(r"qt_[a-z0-9]{3,10}")
# Test-only provenance for the dumps that `_render_dump` generates.
ATTESTATION = (
    "test-only: rendered by tests/test_looker_studio_live_sync.py from the"
    f" manifest of report {REPORT_ID}"
)
NAMES_NO_REPORT = (
    f"the dump names report {REPORT_ID} in no field (comments do not count)"
)

# `_reopen` gives a finalized copy the state of the binding before #559: a
# verification on 2026-10-09 and a live sync pending since 2026-10-29. The
# pending record goes after `verified_template_bundle_sha256`.
REOPENED_VERIFIED_DATE = "2026-10-09"
_REOPENED_PENDING = """\
  pending_live_sync:
    reason: A test re-opened the live sync.
    since_date: "2026-10-29"
    tracking_issue: https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/559
    required_actions:
      - Finalize the live sync again.
"""


def _load_validator():
  path = DASHBOARD / "tools/validate_contracts.py"
  spec = importlib.util.spec_from_file_location("live_sync_validator", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _load_finalize(tree):
  spec = importlib.util.spec_from_file_location(
      "live_sync_finalize", tree / "tools/finalize_live_sync.py"
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _snapshot(tree):
  """Return the bytes of every file under `tree` by relative path."""
  return {
      path.relative_to(tree).as_posix(): path.read_bytes()
      for path in sorted(tree.rglob("*"))
      if path.is_file() and "__pycache__" not in path.parts
  }


def _live(tree):
  binding = yaml.safe_load((tree / BINDING).read_text(encoding="utf-8"))
  return binding[LIVE]


def _reopen(tree):
  """Re-open the live sync of a finalized copy.

  Sets its `verified_date` back to REOPENED_VERIFIED_DATE and its verified
  digests to placeholders, so that the validator requires a
  `pending_live_sync` record, and inserts one.
  """
  path = tree / BINDING
  text = path.read_text(encoding="utf-8")
  assert PENDING not in yaml.safe_load(text)[LIVE]
  head, block = text.split(f"\n{LIVE}:\n")
  block, count = re.subn(
      r"(?m)^  verified_date: .*$",
      f'  verified_date: "{REOPENED_VERIFIED_DATE}"',
      block,
      count=1,
  )
  assert count == 1
  for key in ("verified_manifest_sha256", "verified_template_bundle_sha256"):
    # Quoted: unquoted, YAML reads 64 zeros as the integer 0.
    block, count = re.subn(
        rf"(?m)^  {key}: [0-9a-f]{{64}}$", f'  {key}: "{"0" * 64}"', block
    )
    assert count == 1, key
  anchor = re.search(r"(?m)^  verified_template_bundle_sha256: .*\n", block)
  assert anchor is not None
  block = block[: anchor.end()] + _REOPENED_PENDING + block[anchor.end() :]
  path.write_text(f"{head}\n{LIVE}:\n{block}", encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
  """A copy of dashboards/looker_studio with a pending live sync.

  The copy sits at `repo/dashboards/looker_studio`, so its tools see
  `repo/tests/fixtures` as the repository's fixtures. Until #559 is closed
  the repository binding already records the pending sync; after that, the
  copy is re-opened. The test fails if the repository binding changes.
  """
  copy_root = tmp_path / "repo/dashboards/looker_studio"
  shutil.copytree(
      DASHBOARD, copy_root, ignore=shutil.ignore_patterns("__pycache__")
  )
  if PENDING not in _live(copy_root):
    _reopen(copy_root)
  repository_binding = (DASHBOARD / BINDING).read_bytes()
  yield copy_root
  assert (DASHBOARD / BINDING).read_bytes() == repository_binding


def _spec_inner(component):
  (inner,) = [
      value
      for value in component["spec"].values()
      if isinstance(value, dict) and "dataset" in value
  ]
  return inner


def _spec_columns(component):
  return _spec_inner(component)["dataset"]["columns"]


def _spec_roles(component):
  """Return a component's dimension IDs, metric IDs, sorts and reverse flag."""
  fields = _spec_inner(component)["fields"]
  if "primaryMetricId" in fields:
    return [], [fields["primaryMetricId"]], [], False
  if "primaryAxis" in fields:
    axis = fields["primaryAxis"]
    dimensions, metrics = [axis["fieldId"]], fields["metricSeries"]["metricIds"]
  elif "dimensionId" in fields:
    axis = fields
    dimensions, metrics = [fields["dimensionId"]], [fields["metricId"]]
  else:
    axis = fields
    dimensions, metrics = fields["dimensionIds"], fields["metricIds"]
  sorts = [
      (clause["sortFieldId"], clause["sortDir"]) for clause in axis["sort"]
  ]
  return dimensions, metrics, sorts, axis.get("reverseSortOrder", False)


def _render_dump(tree, report_datasource_id=None, name_report=True):
  """Render a dump of the synced live template from the copy's manifest.

  The dump is generated here, not taken from the checked-in fixtures: a
  `report_id` field naming the binding's report (test-only provenance, left
  out with `name_report=False`), one `component_config` per manifest
  component, in manifest order, with its `qt_` concepts and the references
  to them, then the report-level data source and `ds0` alias of the binding.
  `report_datasource_id` replaces the report-level data source.
  """
  manifest = yaml.safe_load(
      (tree / "spec/bqca_chart_manifest.yaml").read_text(encoding="utf-8")
  )
  binding = yaml.safe_load((tree / BINDING).read_text(encoding="utf-8"))
  report_datasource_id = report_datasource_id or binding["datasource_id"]
  lines = [
      "# Rendered by tests/test_looker_studio_live_sync.py",
      "report_config {",
  ]
  if name_report:
    lines.append(f'  report_id: "{binding["report_id"]}"')
  for page in manifest["pages"]:
    lines += ["  page {", f'    page_id: "{page["id"]}"', "    page {"]
    for component in page["components"]:
      dimensions, metrics, sorts, reverse = _spec_roles(component)
      datasource_id = component["datasource_id"]
      lines += [
          "      component_config {",
          f'        datasource_id: "{datasource_id}"',
          f'        component_id: "{component["component_id"]}"',
          "        property_config { component_property {",
          f'          dataset {{ dataset_id: "{datasource_id}" }}',
      ]
      for role, ids in (("dimensions", dimensions), ("metrics", metrics)):
        names = " ".join(f'concept_names: "{column_id}"' for column_id in ids)
        entry = (
            f' labeled_concepts {{ key: "primary" value {{ {names} }} }}'
            if ids
            else ""
        )
        lines.append(f"          {role} {{{entry} }}")
      for column_id, direction in sorts:
        lines.append(
            f"          sort {{ sort_dir: {direction}"
            f' sort_column: "{column_id}" }}'
        )
      if reverse:
        lines.append("          reverse_sort_order: true")
      lines.append("        } }")
      for column in _spec_columns(component):
        transform = f'source_field_name: "{column["fieldName"]}"'
        if column.get("aggregation"):
          transform += f" aggregation: {column['aggregation']}"
        lines.append(
            f'        concept_defs {{ id: "t0.{column["id"]}" name:'
            f' "{column["id"]}" query_time_transformation {{'
            f" data_transformation {{ {transform} }} }} }}"
        )
      lines.append("      }")
    lines += ["    }", "  }"]
  lines += [
      "  report {",
      f'    datasource_id: "{report_datasource_id}"',
      "    resource { datasource_alias {",
      f'      datasource_id: "{binding["datasource_id"]}"',
      f'      alias: "{binding["data_source_alias"]}"',
      "    } }",
      "  }",
      "}",
  ]
  return "\n".join(lines) + "\n"


@pytest.fixture
def dump(tree, tmp_path):
  """A generated dump of the synced live template, outside any fixtures."""
  path = tmp_path / "dumps/report.textproto"
  path.parent.mkdir()
  path.write_text(_render_dump(tree), encoding="utf-8")
  return path


def _finalize(tree, *args):
  return subprocess.run(
      [sys.executable, str(tree / "tools/finalize_live_sync.py"), *args],
      capture_output=True,
      text=True,
      check=False,
  )


def _audit(tree, path):
  return subprocess.run(
      [
          sys.executable,
          str(tree / "tools/validate_contracts.py"),
          "--profile",
          "bqca",
          "--live-report-textproto",
          str(path),
      ],
      capture_output=True,
      text=True,
      check=False,
  )


def _since_date(tree):
  """A valid `--verified-date`: the day the pending sync started."""
  return _live(tree)[PENDING]["since_date"]


def _mode(path):
  return stat.S_IMODE(path.stat().st_mode)


def test_finalize_records_the_live_verification_after_a_clean_audit(tree, dump):
  """A dump that passes the audit finalizes the binding, and only that.

  `verified_date` and both verified digests change, the attestation and the
  dump's SHA-256 are recorded, `pending_live_sync` goes, and every other line
  of the binding, comments included, is kept. The binding keeps its
  permissions, and no staged file is left behind.
  """
  date = _since_date(tree)
  dump_sha256 = hashlib.sha256(dump.read_bytes()).hexdigest()
  before_text = (tree / BINDING).read_text(encoding="utf-8")
  before = yaml.safe_load(before_text)
  before_mode = _mode(tree / BINDING)
  bindings = sorted(path.name for path in (tree / "bindings").iterdir())

  result = _finalize(
      tree,
      "--dump",
      str(dump),
      "--captured-from",
      ATTESTATION,
      "--verified-date",
      date,
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert "ok: live report validated" in result.stdout
  assert f"finalized: {BINDING}" in result.stdout
  after_text = (tree / BINDING).read_text(encoding="utf-8")
  expected = copy.deepcopy(before)
  live = expected[LIVE]
  live["verified_date"] = date
  live["verified_manifest_sha256"] = live["manifest_sha256"]
  live["verified_template_bundle_sha256"] = live["template_bundle_sha256"]
  live["captured_from"] = ATTESTATION
  live["verified_dump_sha256"] = dump_sha256
  del live[PENDING]
  assert yaml.safe_load(after_text) == expected
  assert _mode(tree / BINDING) == before_mode
  assert sorted(path.name for path in (tree / "bindings").iterdir()) == bindings

  diff = list(
      difflib.ndiff(
          before_text.splitlines(keepends=True),
          after_text.splitlines(keepends=True),
      )
  )
  added = [line[2:] for line in diff if line.startswith("+ ")]
  removed = [line[2:] for line in diff if line.startswith("- ")]
  assert added == [
      f'  verified_date: "{date}"\n',
      f"  verified_manifest_sha256: {live['manifest_sha256']}\n",
      f"  verified_template_bundle_sha256: {live['template_bundle_sha256']}\n",
      f"  captured_from: {json.dumps(ATTESTATION)}\n",
      f"  verified_dump_sha256: {dump_sha256}\n",
  ]
  assert [line.split(":")[0] for line in removed[:4]] == [
      "  verified_date",
      "  verified_manifest_sha256",
      "  verified_template_bundle_sha256",
      "  pending_live_sync",
  ]
  assert removed[3] == "  pending_live_sync:\n"
  assert all(line.startswith("    ") for line in removed[4:])
  assert len(removed) > 4

  # The finalized copy passes the contract validator on its own.
  check = subprocess.run(
      [
          sys.executable,
          str(tree / "tools/validate_contracts.py"),
          "--profile",
          "bqca",
      ],
      capture_output=True,
      text=True,
      check=False,
  )
  assert check.returncode == 0, check.stderr
  assert check.stdout == "ok: contracts validated (bqca)\n"


def test_finalize_records_the_attestation_text_exactly(tree, dump):
  attestation = f'export "v2" of report {REPORT_ID} \\ by the café team'

  result = _finalize(
      tree,
      "--dump",
      str(dump),
      "--captured-from",
      attestation,
      "--verified-date",
      _since_date(tree),
  )

  assert result.returncode == 0, result.stdout + result.stderr
  live = _live(tree)
  assert live["captured_from"] == attestation
  assert (
      live["verified_dump_sha256"]
      == hashlib.sha256(dump.read_bytes()).hexdigest()
  )


@pytest.mark.parametrize(
    "source, attest, refusal",
    [
        (
            "fixture",
            False,
            ", but a write would refuse it: --captured-from is not given;"
            f" {NAMES_NO_REPORT}; nothing written",
        ),
        (
            "generated",
            False,
            ", but a write would refuse it: --captured-from is not given;"
            " nothing written",
        ),
        ("generated", True, "; nothing written"),
    ],
    ids=["fixture", "generated-unattested", "generated-attested"],
)
def test_finalize_check_prints_the_change_and_writes_nothing(
    tree, dump, source, attest, refusal
):
  """`--check` previews the change, and says why a write would be refused.

  It previews with a checked-in stand-in too, and needs no attestation.
  """
  path = REMAPPED_DUMP if source == "fixture" else dump
  date = _since_date(tree)
  before = _snapshot(tree)

  result = _finalize(
      tree,
      "--dump",
      str(path),
      "--check",
      "--verified-date",
      date,
      *(["--captured-from", ATTESTATION] if attest else []),
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert _snapshot(tree) == before
  assert "ok: live report validated" in result.stdout
  assert f"--- a/{BINDING}\n+++ b/{BINDING}\n" in result.stdout
  assert "-  pending_live_sync:\n" in result.stdout
  assert f'+  verified_date: "{date}"\n' in result.stdout
  recorded = ATTESTATION if attest else "<the --captured-from attestation>"
  assert f"+  captured_from: {json.dumps(recorded)}\n" in result.stdout
  assert (
      f"check: the live audit of {path} passed; {BINDING} would change as"
      f" above{refusal}\n"
  ) in result.stdout
  assert "finalized:" not in result.stdout


def _stale_dump(tree):
  """A rendered dump that identifies the report but keeps a generic concept."""
  text = _render_dump(tree)
  assert text.count("qt_d779815a") == 3
  return text.replace("qt_d779815a", "met")


@pytest.mark.parametrize("check", [False, True], ids=["write", "check"])
@pytest.mark.parametrize("source", ["stale", "missing"])
def test_finalize_rejects_a_dump_that_fails_the_audit_and_writes_nothing(
    tree, tmp_path, source, check
):
  """A stale or unreadable dump leaves every file of the copy unchanged.

  The stale dump identifies the live template, but one of its concepts keeps
  a generic name, so its audit fails. A missing dump cannot be read at all.
  """
  path = tmp_path / "report.textproto"
  if source == "stale":
    path.write_text(_stale_dump(tree), encoding="utf-8")
    expected = [
        "is not a qt_ column ID",
        f"not finalized: tools/validate_contracts.py exited 1 on {path};"
        f" {BINDING} is unchanged",
    ]
  else:
    expected = [
        f"not finalized: cannot read {path}: No such file or directory;"
        f" {BINDING} is unchanged"
    ]
  before = _snapshot(tree)

  result = _finalize(
      tree,
      "--dump",
      str(path),
      *(["--check"] if check else ["--captured-from", ATTESTATION]),
  )

  assert result.returncode == 1
  for error in expected:
    assert error in result.stderr
  assert _snapshot(tree) == before


@pytest.mark.parametrize(
    "snapshot_kind, replacement_kind, replaced",
    [
        ("stale", "remapped", "after-identity-check"),
        ("valid", "stale", "after-identity-check"),
        ("valid", "remapped", "after-read"),
    ],
    ids=[
        "stale-snapshot-then-passing-file",
        "valid-snapshot-then-stale-file",
        "valid-snapshot-then-unidentified-file",
    ],
)
def test_finalize_uses_only_the_snapshot_it_read_first(
    tree, tmp_path, monkeypatch, snapshot_kind, replacement_kind, replaced
):
  """The dump is read once; a file that replaces it later changes nothing.

  Another file lands at the caller's path right after the identity check or
  right after the dump is read. The identity check, both audits (through a
  private copy) and the recorded digest use the snapshot, never that path:
  a stale snapshot is refused although its replacement (the remapped
  fixture) passes the audit, and a valid snapshot is finalized with its own
  digest although its replacement is stale or names no report.
  """
  finalize = _load_finalize(tree)
  valid, stale = _render_dump(tree), _stale_dump(tree)
  snapshot = (stale if snapshot_kind == "stale" else valid).encode("utf-8")
  replacement = (
      REMAPPED_DUMP.read_bytes()
      if replacement_kind == "remapped"
      else stale.encode("utf-8")
  )
  assert _audit(tree, REMAPPED_DUMP).returncode == 0
  path = tmp_path / "report.textproto"
  path.write_bytes(snapshot)
  original = (tree / BINDING).read_bytes()
  date = _since_date(tree)
  real_blockers, real_audit = finalize.write_blockers, finalize._audit
  real_finalize_snapshot = finalize._finalize_snapshot
  audited = []

  def blockers_then_replace(*args, **kwargs):
    blockers = real_blockers(*args, **kwargs)
    path.write_bytes(replacement)
    return blockers

  def replace_then_finalize(*args, **kwargs):
    path.write_bytes(replacement)
    return real_finalize_snapshot(*args, **kwargs)

  def audit(audited_path):
    audited.append((Path(audited_path), Path(audited_path).read_bytes()))
    return real_audit(audited_path)

  if replaced == "after-identity-check":
    monkeypatch.setattr(finalize, "write_blockers", blockers_then_replace)
  else:
    monkeypatch.setattr(finalize, "_finalize_snapshot", replace_then_finalize)
  monkeypatch.setattr(finalize, "_audit", audit)

  code = finalize.main(
      [
          "--dump",
          str(path),
          "--captured-from",
          ATTESTATION,
          "--verified-date",
          date,
      ]
  )

  assert path.read_bytes() == replacement
  assert audited
  assert [data for _, data in audited] == [snapshot] * len(audited)
  assert all(item.resolve() != path.resolve() for item, _ in audited)
  if snapshot_kind == "stale":
    assert code == 1
    assert len(audited) == 1
    assert (tree / BINDING).read_bytes() == original
  else:
    assert code == 0
    assert len(audited) == 2
    assert (
        _live(tree)["verified_dump_sha256"]
        == hashlib.sha256(snapshot).hexdigest()
    )


def _swap_id_and_name(text):
  """Swap the adjacent `id` and `name` fields of one concept."""
  pair = '          id: "t0.qt_1e5c0a97"\n          name: "qt_1e5c0a97"\n'
  assert text.count(pair) == 1
  swapped = '          name: "qt_1e5c0a97"\n          id: "t0.qt_1e5c0a97"\n'
  return text.replace(pair, swapped)


def _swap_first_two_components(text):
  lines = text.splitlines(keepends=True)
  starts = [
      index
      for index, line in enumerate(lines)
      if line == "      component_config {\n"
  ]
  first, second = starts[:2]
  end = lines.index("      }\n", second) + 1
  assert lines[second - 1] == "      }\n"
  return "".join(
      lines[:first] + lines[second:end] + lines[first:second] + lines[end:]
  )


def _with_inert_field(text):
  assert text.count("report_config {\n") == 1
  return text.replace(
      "report_config {\n",
      'report_config {\n  capture_note: "still a copied stand-in"\n',
  )


def _with_inert_fields_everywhere(text):
  return re.sub(
      r'(?m)^(        component_id: "[^"]+"\n)',
      r'\1        layout_note: "copied"\n',
      text,
  )


def _naming_the_report_in_comments(text):
  return (
      f'# report_id: "{REPORT_ID}"\n# captured from report {REPORT_ID}\n' + text
  )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text,
        _swap_id_and_name,
        _swap_first_two_components,
        _with_inert_field,
        _with_inert_fields_everywhere,
        _naming_the_report_in_comments,
    ],
    ids=[
        "byte-identical-copy",
        "reordered-fields",
        "reordered-components",
        "audit-inert-field",
        "audit-inert-fields-everywhere",
        "report-named-in-comments",
    ],
)
def test_finalize_refuses_a_reordered_or_audit_inert_stand_in(
    tree, tmp_path, mutate
):
  """No edit of a checked-in stand-in that keeps its fields finalizes.

  Each mutation of the remapped fixture, outside tests/fixtures, still passes
  the live audit, so only the positive evidence can refuse it: with an
  attestation, the write fails because no field of the dump names the
  report, and the binding stays byte-identical. `--check` still previews.
  """
  path = tmp_path / "elsewhere/report.textproto"
  path.parent.mkdir()
  path.write_text(
      mutate(REMAPPED_DUMP.read_text(encoding="utf-8")), encoding="utf-8"
  )
  audit = _audit(tree, path)
  assert audit.returncode == 0, audit.stderr
  binding = (tree / BINDING).read_bytes()
  before = _snapshot(tree)

  refused = _finalize(tree, "--dump", str(path), "--captured-from", ATTESTATION)

  assert refused.returncode == 1
  assert (
      f"not finalized: {NAMES_NO_REPORT}; {BINDING} is unchanged"
  ) in refused.stderr
  assert "ok: live report validated" not in refused.stdout
  assert (tree / BINDING).read_bytes() == binding
  assert _snapshot(tree) == before

  preview = _finalize(tree, "--dump", str(path), "--check")

  assert preview.returncode == 0, preview.stdout + preview.stderr
  assert f"a write would refuse it: --captured-from is not given;" in (
      preview.stdout
  )
  assert NAMES_NO_REPORT in preview.stdout
  assert _snapshot(tree) == before


@pytest.mark.parametrize(
    "fixture", sorted(FIXTURES.glob("*.textproto")), ids=lambda path: path.name
)
def test_checked_in_stand_ins_name_the_report_in_no_field(tree, fixture):
  """No textproto fixture names the report in a field, so none is written.

  A fixture names the report at most in a comment. A new fixture that names
  it in a field would let a copy of it pass the identity check; this test
  then fails.
  """
  finalize = _load_finalize(tree)
  binding = yaml.safe_load((tree / BINDING).read_text(encoding="utf-8"))
  text = fixture.read_text(encoding="utf-8")
  assert all(
      line.lstrip().startswith("#")
      for line in text.splitlines()
      if REPORT_ID in line
  )

  errors = finalize.dump_identity_errors(text, binding)

  assert errors is not None and errors[0] == NAMES_NO_REPORT


@pytest.mark.parametrize(
    "variant, error",
    [
        ("no-report-field", NAMES_NO_REPORT),
        (
            "other-report-datasource",
            f"its report-level datasource_id is '{STALE_DATASOURCE_ID}', not"
            f" '{DATASOURCE_ID}'",
        ),
    ],
)
def test_finalize_refuses_a_dump_that_does_not_identify_the_live_template(
    tree, tmp_path, variant, error
):
  """A write needs a dump that names the report and its report data source.

  Both are checked before the audit runs.
  """
  path = tmp_path / "report.textproto"
  path.write_text(
      _render_dump(
          tree,
          name_report=variant != "no-report-field",
          report_datasource_id=(
              STALE_DATASOURCE_ID
              if variant == "other-report-datasource"
              else None
          ),
      ),
      encoding="utf-8",
  )
  before = _snapshot(tree)

  result = _finalize(tree, "--dump", str(path), "--captured-from", ATTESTATION)

  assert result.returncode == 1
  assert f"not finalized: {error}; {BINDING} is unchanged" in result.stderr
  assert "ok: live report validated" not in result.stdout
  assert _snapshot(tree) == before


@pytest.mark.parametrize(
    "args, code, error",
    [
        ([], 2, "a write needs --captured-from"),
        (
            ["--captured-from", "exported by hand from the live template"],
            1,
            f"not finalized: --captured-from does not name report {REPORT_ID}",
        ),
        (["--captured-from", "   "], 2, "must be 1 to 500 characters long"),
        (
            ["--captured-from", f"report {REPORT_ID}\nsecond line"],
            2,
            "must be one line of printable text",
        ),
        (
            ["--captured-from", REPORT_ID + "x" * 500],
            2,
            "must be 1 to 500 characters long",
        ),
    ],
    ids=["missing", "not-naming-the-report", "blank", "two-lines", "too-long"],
)
def test_finalize_requires_an_attestation_that_names_the_report(
    tree, dump, args, code, error
):
  before = _snapshot(tree)

  result = _finalize(tree, "--dump", str(dump), *args)

  assert result.returncode == code
  assert error in result.stderr
  assert _snapshot(tree) == before


def test_finalize_refuses_to_record_a_checked_in_fixture(tree, dump):
  """A dump among the repository's test fixtures is not the live template.

  The path alone refuses a write, whatever the content; `--check` may still
  preview the change with it.
  """
  stand_in = tree.parents[1] / "tests/fixtures/stand_in.textproto"
  stand_in.parent.mkdir(parents=True)
  shutil.copyfile(dump, stand_in)
  before = _snapshot(tree)

  refused = _finalize(
      tree, "--dump", str(stand_in), "--captured-from", ATTESTATION
  )

  assert refused.returncode == 1
  assert (
      f"not finalized: {stand_in} is a test fixture of this repository;"
      f" {BINDING} is unchanged"
  ) in refused.stderr
  assert _snapshot(tree) == before

  preview = _finalize(tree, "--dump", str(stand_in), "--check")

  assert preview.returncode == 0, preview.stdout + preview.stderr
  assert "-  pending_live_sync:\n" in preview.stdout
  assert f"{stand_in} is a test fixture of this repository" in preview.stdout
  assert _snapshot(tree) == before


@pytest.mark.parametrize(
    "date, code, error",
    [
        ("2026-10-28", 1, "is before pending_live_sync.since_date"),
        ("2999-01-01", 1, "--verified-date 2999-01-01 is in the future"),
        ("2026-13-01", 2, "'2026-13-01' is not a YYYY-MM-DD date"),
        ("20261030", 2, "'20261030' is not a YYYY-MM-DD date"),
    ],
)
def test_finalize_rejects_a_verified_date_it_cannot_record(
    tree, dump, date, code, error
):
  before = _snapshot(tree)

  result = _finalize(
      tree,
      "--dump",
      str(dump),
      "--captured-from",
      ATTESTATION,
      "--verified-date",
      date,
  )

  assert result.returncode == code
  assert error in result.stderr
  assert _snapshot(tree) == before


def test_finalize_is_idempotent_and_can_record_a_later_sync(tree, dump):
  date = _since_date(tree)
  attested = ("--captured-from", ATTESTATION)
  first = _finalize(
      tree, "--dump", str(dump), *attested, "--verified-date", date
  )
  assert first.returncode == 0, first.stdout + first.stderr
  finalized = _snapshot(tree)

  again = _finalize(tree, "--dump", str(dump), *attested)

  assert again.returncode == 0, again.stdout + again.stderr
  assert f"nothing to finalize: {BINDING} has no" in again.stdout
  assert _snapshot(tree) == finalized

  # A later sync replaces the capture record instead of adding another.
  _reopen(tree)
  later_attestation = f"a later capture of report {REPORT_ID}"
  later = _finalize(
      tree,
      "--dump",
      str(dump),
      "--captured-from",
      later_attestation,
      "--verified-date",
      date,
  )

  assert later.returncode == 0, later.stdout + later.stderr
  live = _live(tree)
  assert PENDING not in live
  assert live["verified_manifest_sha256"] == live["manifest_sha256"]
  assert live["verified_template_bundle_sha256"] == (
      live["template_bundle_sha256"]
  )
  assert live["captured_from"] == later_attestation
  text = (tree / BINDING).read_text(encoding="utf-8")
  assert text.count("\n  captured_from: ") == 1
  assert text.count("\n  verified_dump_sha256: ") == 1


def test_finalize_restores_the_binding_when_the_audit_fails_after_the_write(
    tree, dump, monkeypatch, capsys
):
  finalize = _load_finalize(tree)
  original = (tree / BINDING).read_bytes()
  calls = []

  def audit(path):
    calls.append((path, (tree / BINDING).read_bytes() == original))
    return 0 if len(calls) == 1 else 1

  monkeypatch.setattr(finalize, "_audit", audit)

  assert (
      finalize.main(["--dump", str(dump), "--captured-from", ATTESTATION]) == 1
  )
  # Both audits read the same private copy of the dump; the second ran on the
  # written binding, which is then restored.
  ((audited, before_write), (audited_again, after_write)) = calls
  assert audited == audited_again and audited.resolve() != dump.resolve()
  assert (before_write, after_write) == (True, False)
  assert (tree / BINDING).read_bytes() == original
  assert (
      "not finalized: tools/validate_contracts.py exited 1 after the write,"
      f" so {BINDING} was restored"
  ) in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    ["audit-launch-error", "audit-interrupted", "write-interrupted"],
)
def test_finalize_restores_the_binding_after_an_error_or_interrupt(
    tree, dump, monkeypatch, capsys, failure
):
  """An exception or interrupt during the write or post-write audit rolls back.

  The post-write audit fails to launch (OSError) or is interrupted, or the
  write is interrupted right after the new binding replaced the old one.
  Each time the original bytes come back, and no staged file is left.
  """
  finalize = _load_finalize(tree)
  original = (tree / BINDING).read_bytes()
  bindings = sorted(path.name for path in (tree / "bindings").iterdir())
  launches = []
  real_run = subprocess.run

  def run(*args, **kwargs):
    launches.append(args)
    if len(launches) == 2:
      if failure == "audit-launch-error":
        raise OSError("cannot launch the validator")
      raise KeyboardInterrupt
    return real_run(*args, **kwargs)

  writes = []
  real_replace_file = finalize._replace_file

  def replace_file(path, data):
    writes.append(data)
    real_replace_file(path, data)
    if len(writes) == 1:
      assert (tree / BINDING).read_bytes() != original
      raise KeyboardInterrupt

  if failure == "write-interrupted":
    monkeypatch.setattr(finalize, "_replace_file", replace_file)
  else:
    monkeypatch.setattr(finalize, "subprocess", types.SimpleNamespace(run=run))
  argv = ["--dump", str(dump), "--captured-from", ATTESTATION]

  if failure == "audit-launch-error":
    assert finalize.main(argv) == 1
    assert (
        "not finalized: OSError: cannot launch the validator after the write,"
        f" so {BINDING} was restored"
    ) in capsys.readouterr().err
  else:
    with pytest.raises(KeyboardInterrupt):
      finalize.main(argv)
    assert f"interrupted: {BINDING} was restored" in capsys.readouterr().err
  assert (tree / BINDING).read_bytes() == original
  assert sorted(path.name for path in (tree / "bindings").iterdir()) == bindings
  if failure == "write-interrupted":
    assert writes[1] == original


def test_replace_file_keeps_the_target_until_the_staged_copy_is_complete(
    tree, monkeypatch
):
  """The binding is replaced atomically: staged next to it, then renamed."""
  finalize = _load_finalize(tree)
  target = tree / BINDING
  original = target.read_bytes()
  mode = _mode(target)
  bindings = sorted(path.name for path in target.parent.iterdir())
  renames = []
  real_replace = os.replace

  def failing_fsync(descriptor):
    raise OSError("disk full")

  monkeypatch.setattr(os, "fsync", failing_fsync)
  with pytest.raises(OSError, match="disk full"):
    finalize._replace_file(target, b"partial\n")
  monkeypatch.undo()
  assert target.read_bytes() == original
  assert sorted(path.name for path in target.parent.iterdir()) == bindings

  def replace(source, destination):
    renames.append((Path(source), Path(destination)))
    real_replace(source, destination)

  monkeypatch.setattr(os, "replace", replace)
  finalize._replace_file(target, b"replaced\n")
  monkeypatch.undo()
  assert target.read_bytes() == b"replaced\n"
  assert _mode(target) == mode
  ((staged, destination),) = renames
  assert destination == target
  assert staged.parent == target.parent and staged != target
  assert sorted(path.name for path in target.parent.iterdir()) == bindings


@pytest.mark.parametrize(
    "pattern, replacement, error",
    [
        (
            r"^  pending_live_sync:$",
            "  pending_live_sync: &pending",
            "exactly one '  pending_live_sync:' line",
        ),
        (
            r"^  method:$",
            '  verified_date: "2026-10-30"\n  method:',
            "exactly one verified_date line",
        ),
        (
            r"^  method:$",
            '  captured_from: "a"\n  captured_from: "b"\n  method:',
            "at most one captured_from line",
        ),
        # A one-line edit of a multi-line value would leave a stray line.
        (
            r'^  verified_date: "(\d{4}-\d{2}-\d{2})"$',
            r"  verified_date: >-\n    \1",
            "the edited live_template_verification is not valid YAML",
        ),
    ],
    ids=[
        "pending-not-a-plain-block",
        "repeated-verified-date",
        "repeated-captured-from",
        "folded-verified-date",
    ],
)
def test_finalized_binding_text_refuses_a_binding_it_cannot_edit_exactly(
    tree, pattern, replacement, error
):
  finalize = _load_finalize(tree)
  text = (tree / BINDING).read_text(encoding="utf-8")
  # The first match is in live_template_verification, which comes before
  # product_verification and its own `verified_date`.
  edited, count = re.subn(
      pattern, replacement, text, count=1, flags=re.MULTILINE
  )
  assert count == 1
  # The input itself is valid YAML with a pending live sync.
  assert PENDING in yaml.safe_load(edited)[LIVE]

  with pytest.raises(ValueError, match=re.escape(error)):
    finalize.finalized_binding_text(edited, "2026-10-30", ATTESTATION, "a" * 64)


RENAME_MAP_HEADER = [
    "page",
    "page_name",
    "component_id",
    "component_title",
    "chart_type",
    "old_name",
    "new_qt_id",
    "source_field",
    "aggregation",
    "slot",
    "display_name",
    "references",
]


def _rename_map(*args):
  return subprocess.run(
      [sys.executable, str(DASHBOARD / "tools/gen_bqca_rename_map.py"), *args],
      capture_output=True,
      text=True,
      check=False,
  )


def _dump_concepts(validator, path):
  """Return each component's concept names, in dump order."""
  report = validator.parse_report_textproto(path.read_text(encoding="utf-8"))
  return {
      validator._tp_scalar(config, "component_id"): [
          validator._tp_scalar(value, "name")
          for key, value in config
          if key == "concept_defs"
      ]
      for config in validator._tp_messages(report, "component_config")
  }


def test_rename_map_lists_the_65_concepts_in_page_and_component_order(
    tmp_path,
):
  out_csv, out_md = tmp_path / "rename-map.csv", tmp_path / "rename-map.md"

  result = _rename_map("--csv", str(out_csv), "--md", str(out_md))

  assert result.returncode == 0, result.stdout + result.stderr
  with out_csv.open(newline="", encoding="utf-8") as handle:
    reader = csv.DictReader(handle)
    assert reader.fieldnames == RENAME_MAP_HEADER
    rows = list(reader)
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text(encoding="utf-8")
  )
  # One row per manifest column: pages, then components, then slots.
  assert [
      (
          row["page"],
          row["page_name"],
          row["component_id"],
          row["component_title"],
          row["chart_type"],
          row["slot"],
          row["new_qt_id"],
          row["source_field"],
          row["aggregation"],
          row["display_name"],
      )
      for row in rows
  ] == [
      (
          str(page["index"]),
          page["name"],
          component["component_id"],
          component["title"],
          component["chart_type"],
          str(slot),
          column["id"],
          column["fieldName"],
          column.get("aggregation", ""),
          column["displayName"],
      )
      for page in manifest["pages"]
      for component in page["components"]
      for slot, column in enumerate(_spec_columns(component))
  ]
  assert len(rows) == 65
  assert len({row["component_id"] for row in rows}) == 34
  assert rows[0]["component_id"] == "cd-vWRq2EdpQyI"
  assert (rows[0]["old_name"], rows[0]["new_qt_id"]) == ("met", "qt_d779815a")

  # The old names are the generic names of the dump, the new IDs those of its
  # remapped twin, slot for slot.
  validator = _load_validator()
  generic = _dump_concepts(validator, GENERIC_DUMP)
  remapped = _dump_concepts(validator, REMAPPED_DUMP)
  by_component = collections.defaultdict(list)
  for row in rows:
    by_component[row["component_id"]].append(row)
  assert set(by_component) == set(generic) == set(remapped)
  for component_id, component_rows in by_component.items():
    assert [row["old_name"] for row in component_rows] == generic[component_id]
    assert [row["new_qt_id"] for row in component_rows] == (
        remapped[component_id]
    )
  old_names = collections.Counter(row["old_name"] for row in rows)
  assert not any(QT_ID.fullmatch(name) for name in old_names)
  assert old_names == {
      "met": 22,
      "dim": 10,
      "m0": 12,
      "m1": 4,
      "m2": 3,
      "m3": 1,
      "d0": 3,
      "d1": 2,
      **{f"d{index}": 1 for index in range(2, 10)},
  }
  assert len({row["new_qt_id"] for row in rows}) == 65
  # 65 concept_names references (23 dimensions, 42 metrics) and 13 sorts.
  assert collections.Counter(
      role for row in rows for role in row["references"].split("; ")
  ) == {"dimension": 23, "metric": 42, "sort": 13}

  assert result.stdout == ""
  assert result.stderr.splitlines() == [
      "65 concepts in 34 components on 7 pages; the manifest has 65 columns"
      " in 34 components (21 scorecards + 13 charts).",
      "The live audit reports 65 concept names in the dump that are not qt_"
      " IDs.",
      "78 references to rename: 65 concept_names (23 dimension, 42 metric)"
      " and 13 sort_column.",
      f"Report-level datasource_id: '{STALE_DATASOURCE_ID}' ->"
      f" '{DATASOURCE_ID}'.",
      "Cross-checked with the remapped twin"
      " tests/fixtures/bqca_live_report_42e79a1f_sanitized_remapped.textproto:"
      " the live audit accepts it, and it carries every new ID.",
  ]
  markdown = out_md.read_text(encoding="utf-8")
  assert markdown.startswith("# BQCA live template sync (#559): rename map\n")
  assert "python3 dashboards/looker_studio/tools/gen_bqca_rename_map.py" in (
      markdown
  )
  assert markdown.count("\n## Page ") == 7
  assert markdown.count("| `cd-") == 65
  for row in rows:
    assert f"`{row['old_name']}` | `{row['new_qt_id']}`" in markdown
  assert f"`{DATASOURCE_ID}`" in markdown
  assert str(ROOT) not in markdown

  # The same inputs give the same bytes.
  again_csv, again_md = tmp_path / "again.csv", tmp_path / "again.md"
  assert (
      _rename_map("--csv", str(again_csv), "--md", str(again_md)).returncode
      == 0
  )
  assert again_csv.read_bytes() == out_csv.read_bytes()
  assert again_md.read_bytes() == out_md.read_bytes()


def test_rename_map_writes_csv_to_stdout_by_default():
  result = _rename_map()

  assert result.returncode == 0, result.stderr
  lines = result.stdout.splitlines()
  assert lines[0] == ",".join(RENAME_MAP_HEADER)
  assert len(lines) == 66
  assert "65 concepts in 34 components on 7 pages" in result.stderr


@pytest.mark.parametrize(
    "fixture, old, new, error",
    [
        (
            REMAPPED_DUMP,
            "qt_d779815a",
            "qt_deadbeef",
            "remapped twin: component kpi_total_tokens (cd-vWRq2EdpQyI)"
            " concept 0 is 'qt_deadbeef', not 'qt_d779815a'",
        ),
        (
            GENERIC_DUMP,
            'source_field_name: "_input_tokens_"\n',
            'source_field_name: "_no_such_field_"\n',
            "matches no column of the manifest",
        ),
    ],
    ids=["twin-with-another-id", "dump-with-another-field"],
)
def test_rename_map_rejects_inputs_that_do_not_line_up(
    tmp_path, fixture, old, new, error
):
  text = fixture.read_text(encoding="utf-8")
  assert old in text
  mutated = tmp_path / fixture.name
  mutated.write_text(text.replace(old, new, 3), encoding="utf-8")
  dump, remapped = (
      (GENERIC_DUMP, mutated)
      if fixture == REMAPPED_DUMP
      else (mutated, REMAPPED_DUMP)
  )
  out_csv = tmp_path / "rename-map.csv"

  result = _rename_map(
      "--dump",
      str(dump),
      "--remapped",
      str(remapped),
      "--csv",
      str(out_csv),
  )

  assert result.returncode == 1
  assert error in result.stderr
  assert not out_csv.exists()


# A concept-bearing component that the manifest does not have, inserted
# before the first component of a dump.
_EXTRA_COMPONENT = """\
      component_config {
        datasource_id: "70e5ac1e-65bb-4c89-a9df-fa9990cb6029"
        component_id: "cd-extra"
        property_config {
          component_property {
            dataset {
              dataset_id: "70e5ac1e-65bb-4c89-a9df-fa9990cb6029"
            }
            metrics {
              labeled_concepts {
                key: "primary"
                value {
                  concept_names: "NAME"
                }
              }
            }
          }
        }
        concept_defs {
          id: "t0.NAME"
          name: "NAME"
          query_time_transformation {
            data_transformation {
              source_field_name: "_total_tokens_"
              aggregation: SUM
            }
          }
        }
      }
"""
# A component that the manifest does not have, with a reference and no
# concepts.
_REFERENCE_ONLY_COMPONENT = """\
      component_config {
        datasource_id: "70e5ac1e-65bb-4c89-a9df-fa9990cb6029"
        component_id: "cd-reference-only"
        property_config {
          component_property {
            metrics {
              labeled_concepts {
                key: "primary"
                value {
                  concept_names: "ghost"
                }
              }
            }
          }
        }
      }
"""


def _with_component(fixture, path, component):
  text = fixture.read_text(encoding="utf-8")
  anchor = "      component_config {\n"
  assert anchor in text
  path.write_text(text.replace(anchor, component + anchor, 1), encoding="utf-8")
  return path


def _with_dangling_reference(fixture, path):
  text = fixture.read_text(encoding="utf-8")
  old = 'concept_names: "met"\n'
  assert old in text
  path.write_text(
      text.replace(old, 'concept_names: "ghost"\n', 1), encoding="utf-8"
  )
  return path


_EXTRA_IN_DUMP = (
    "component cd-extra of the dump defines concepts ['met'] but is not in"
    " spec/bqca_chart_manifest.yaml"
)
_REFERENCE_ONLY_IN_DUMP = (
    "component cd-reference-only of the dump references ['ghost'] but is not"
    " in spec/bqca_chart_manifest.yaml"
)


@pytest.mark.parametrize(
    "case, error",
    [
        ("extra-in-dump", _EXTRA_IN_DUMP),
        ("extra-in-dump-with-remapped", _EXTRA_IN_DUMP),
        (
            "extra-in-remapped",
            "remapped twin: its components with concepts or references differ"
            " from the dump's: dump only [], twin only ['cd-extra']",
        ),
        ("reference-only-in-dump", _REFERENCE_ONLY_IN_DUMP),
        ("reference-only-in-dump-with-remapped", _REFERENCE_ONLY_IN_DUMP),
        (
            "reference-only-in-remapped",
            "remapped twin: its components with concepts or references differ"
            " from the dump's: dump only [], twin only ['cd-reference-only']",
        ),
        (
            "dangling-reference",
            "concept_names 'ghost' at"
            " component_property.metrics.labeled_concepts.value.concept_names"
            " names none of its concepts",
        ),
    ],
    ids=[
        "extra-in-dump",
        "extra-in-dump-with-remapped",
        "extra-in-remapped",
        "reference-only-in-dump",
        "reference-only-in-dump-with-remapped",
        "reference-only-in-remapped",
        "dangling-reference",
    ],
)
def test_rename_map_rejects_concepts_and_references_it_cannot_map(
    tmp_path, case, error
):
  """Every concept and reference of the dump must be in the map.

  A component of the dump that is not in the manifest and defines concepts
  or holds references, with or without `--remapped`, a remapped twin with
  such a component, and a reference that names no concept of its component
  each fail, writing neither output file.
  """
  dump, remapped = GENERIC_DUMP, None
  if case.startswith("extra-in-dump"):
    extra = _EXTRA_COMPONENT.replace("NAME", "met")
    dump = _with_component(GENERIC_DUMP, tmp_path / "dump.txtpb", extra)
  elif case.startswith("reference-only-in-dump"):
    dump = _with_component(
        GENERIC_DUMP, tmp_path / "dump.txtpb", _REFERENCE_ONLY_COMPONENT
    )
  elif case == "extra-in-remapped":
    extra = _EXTRA_COMPONENT.replace("NAME", "qt_extra01")
    remapped = _with_component(REMAPPED_DUMP, tmp_path / "twin.txtpb", extra)
  elif case == "reference-only-in-remapped":
    remapped = _with_component(
        REMAPPED_DUMP, tmp_path / "twin.txtpb", _REFERENCE_ONLY_COMPONENT
    )
  else:
    dump = _with_dangling_reference(GENERIC_DUMP, tmp_path / "dump.txtpb")
  if case.endswith("with-remapped"):
    remapped = REMAPPED_DUMP
  out_csv, out_md = tmp_path / "rename-map.csv", tmp_path / "rename-map.md"

  result = _rename_map(
      "--dump",
      str(dump),
      *(["--remapped", str(remapped)] if remapped else []),
      "--csv",
      str(out_csv),
      "--md",
      str(out_md),
  )

  assert result.returncode == 1
  assert error in result.stderr
  assert not out_csv.exists()
  assert not out_md.exists()
