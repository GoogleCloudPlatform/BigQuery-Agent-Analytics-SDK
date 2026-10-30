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
--profile bqca --live-report-textproto` passes on a dump of the live template.
`tools/gen_bqca_rename_map.py` lists the rename that the sync applies to the
concepts of the live template.

The finalize tests run the tool from a copy of `dashboards/looker_studio`, so
they never write to the repository. The checked-in sanitized cuts of the
2026-10-29 dump stand in for dumps: the audit accepts the remapped twin and
rejects the generic cut.
"""

import collections
import copy
import csv
import difflib
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys

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
DATASOURCE_ID = "70e5ac1e-65bb-4c89-a9df-fa9990cb6029"
QT_ID = re.compile(r"qt_[a-z0-9]{3,10}")

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


def _finalize(tree, *args):
  return subprocess.run(
      [sys.executable, str(tree / "tools/finalize_live_sync.py"), *args],
      capture_output=True,
      text=True,
      check=False,
  )


def _since_date(tree):
  """A valid `--verified-date`: the day the pending sync started."""
  return _live(tree)[PENDING]["since_date"]


def test_finalize_records_the_live_verification_after_a_clean_audit(tree):
  """A dump that passes the audit finalizes the binding, and only that.

  `verified_date` and both verified digests change, `pending_live_sync` goes,
  and every other line of the binding, comments included, is kept.
  """
  date = _since_date(tree)
  before_text = (tree / BINDING).read_text(encoding="utf-8")
  before = yaml.safe_load(before_text)

  result = _finalize(
      tree, "--dump", str(REMAPPED_DUMP), "--verified-date", date
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
  del live[PENDING]
  assert yaml.safe_load(after_text) == expected

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


def test_finalize_check_prints_the_change_and_writes_nothing(tree):
  date = _since_date(tree)
  before = _snapshot(tree)

  result = _finalize(
      tree, "--dump", str(REMAPPED_DUMP), "--check", "--verified-date", date
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert _snapshot(tree) == before
  assert "ok: live report validated" in result.stdout
  assert f"--- a/{BINDING}\n+++ b/{BINDING}\n" in result.stdout
  assert "-  pending_live_sync:\n" in result.stdout
  assert f'+  verified_date: "{date}"\n' in result.stdout
  assert "nothing written" in result.stdout
  assert "finalized:" not in result.stdout


@pytest.mark.parametrize("check", [False, True], ids=["write", "check"])
@pytest.mark.parametrize(
    "dump, error",
    [
        (GENERIC_DUMP, "is not a qt_ column ID"),
        (
            FIXTURES / "no_such_dump.textproto",
            "cannot read it as protobuf text format",
        ),
    ],
    ids=["generic-dump", "missing-dump"],
)
def test_finalize_rejects_a_dump_that_fails_the_audit_and_writes_nothing(
    tree, dump, error, check
):
  """A stale or unreadable dump leaves every file of the copy unchanged."""
  before = _snapshot(tree)

  result = _finalize(tree, "--dump", str(dump), *(["--check"] if check else []))

  assert result.returncode == 1
  assert error in result.stderr
  assert (
      f"not finalized: tools/validate_contracts.py exited 1 on {dump};"
      f" {BINDING} is unchanged"
  ) in result.stderr
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
    tree, date, code, error
):
  before = _snapshot(tree)

  result = _finalize(
      tree, "--dump", str(REMAPPED_DUMP), "--verified-date", date
  )

  assert result.returncode == code
  assert error in result.stderr
  assert _snapshot(tree) == before


def test_finalize_refuses_to_record_a_checked_in_fixture(tree):
  """A dump among the repository's test fixtures is not the live template.

  `--check` may still preview the change with it.
  """
  stand_in = tree.parents[1] / "tests/fixtures/stand_in.textproto"
  stand_in.parent.mkdir(parents=True)
  shutil.copyfile(REMAPPED_DUMP, stand_in)
  before = _snapshot(tree)

  refused = _finalize(tree, "--dump", str(stand_in))

  assert refused.returncode == 1
  assert (
      f"not finalized: {stand_in} is a test fixture of this repository, not"
      " a dump of the live template"
  ) in refused.stderr
  assert _snapshot(tree) == before

  preview = _finalize(tree, "--dump", str(stand_in), "--check")

  assert preview.returncode == 0, preview.stdout + preview.stderr
  assert "-  pending_live_sync:\n" in preview.stdout
  assert _snapshot(tree) == before


def test_finalize_is_idempotent_and_can_record_a_later_sync(tree):
  date = _since_date(tree)
  first = _finalize(tree, "--dump", str(REMAPPED_DUMP), "--verified-date", date)
  assert first.returncode == 0, first.stdout + first.stderr
  finalized = _snapshot(tree)

  again = _finalize(tree, "--dump", str(REMAPPED_DUMP))

  assert again.returncode == 0, again.stdout + again.stderr
  assert f"nothing to finalize: {BINDING} has no" in again.stdout
  assert _snapshot(tree) == finalized

  _reopen(tree)
  later = _finalize(tree, "--dump", str(REMAPPED_DUMP), "--verified-date", date)

  assert later.returncode == 0, later.stdout + later.stderr
  live = _live(tree)
  assert PENDING not in live
  assert live["verified_manifest_sha256"] == live["manifest_sha256"]
  assert live["verified_template_bundle_sha256"] == (
      live["template_bundle_sha256"]
  )


def _load_finalize(tree):
  spec = importlib.util.spec_from_file_location(
      "live_sync_finalize", tree / "tools/finalize_live_sync.py"
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_finalize_restores_the_binding_when_the_audit_fails_after_the_write(
    tree, monkeypatch, capsys
):
  finalize = _load_finalize(tree)
  original = (tree / BINDING).read_bytes()
  calls = []

  def audit(dump):
    calls.append((dump, (tree / BINDING).read_bytes() == original))
    return 0 if len(calls) == 1 else 1

  monkeypatch.setattr(finalize, "_audit", audit)

  assert finalize.main(["--dump", str(REMAPPED_DUMP)]) == 1
  # The second audit ran on the written binding, which is then restored.
  assert calls == [(REMAPPED_DUMP, True), (REMAPPED_DUMP, False)]
  assert (tree / BINDING).read_bytes() == original
  assert (
      "not finalized: tools/validate_contracts.py exited 1 after the write,"
      f" so {BINDING} was restored"
  ) in capsys.readouterr().err


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
    finalize.finalized_binding_text(edited, "2026-10-30")


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


def _spec_columns(component):
  (inner,) = [
      value
      for value in component["spec"].values()
      if isinstance(value, dict) and "dataset" in value
  ]
  return inner["dataset"]["columns"]


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
      "Report-level datasource_id: '00000000-0000-4000-8000-000000000000' ->"
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
