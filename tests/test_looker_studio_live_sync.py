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

`tools/sign_live_capture.py` captures the live template: it runs the export
command, writes the bytes it read, and signs a capture receipt of them with a
key that `bindings/bqca_live_capture_signers` lists. A sync has two captures,
one before the write-back and one after it, which names the first.
`tools/finalize_live_sync.py` records a live template verification in
`bindings/bqca_report_template.yaml`, and commits the receipt of the capture
after the write-back as `bindings/bqca_live_capture_receipt.json`, only after
`tools/validate_contracts.py --profile bqca --live-report-textproto` passes on
that capture, both receipts are trusted for this pending sync, and the
capture's parsed content differs from the offline preview of the one before
it. The dump must also carry no offline-preview marker and name the report and
its data source in `report_config.shareable.id`,
`report_config.report.datasource_id`, and the `ds0` entry of
`report_config.report.resource.datasource_alias`. `tools/validate_contracts.py`
checks the committed receipt of a binding without `pending_live_sync`.
`tools/gen_bqca_rename_map.py` lists the rename that the sync applies to the
concepts of the live template. `tools/apply_bqca_rename_map.py` applies it to
a dump of the live template, offline, and writes the remapped dump with the
`# bqca-offline-preview:` header comment line; it never writes a receipt.

The finalize and capture tests run the tools from a copy of
`dashboards/looker_studio`, so they never write to the repository, and list a
test key in the copy's signers file (the repository's lists none). A stand-in
export command prints a file, as the internal export command prints the live
report: before the write-back, the sanitized pre-sync cut with a
`report_config.shareable` entry; after it, a dump that `_render_dump`
generates from the copy's manifest, with test-only provenance
(`report_config.shareable.id`). The checked-in sanitized cuts of the
2026-10-29 dump have no `report_config.shareable` entry, so neither they nor
any reordering or audit-inert edit of them can be written, even with a
receipt; `--check` still previews with them.

The apply tests remap the checked-in sanitized dump. Those that could write
into a repository (the refusals and the failures) run the tool from a copy of
`dashboards/looker_studio` too, and every output goes to `tmp_path`.
"""

import collections
import copy
import csv
import dataclasses
import datetime
import errno
import hashlib
import importlib.util
import io
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
SIGNERS = "bindings/bqca_live_capture_signers"
COMMITTED = "bindings/bqca_live_capture_receipt.json"
LIVE = "live_template_verification"
PENDING = "pending_live_sync"
REPORT_ID = "42e79a1f-a979-4de8-911f-80d5a99543d5"
DATASOURCE_ID = "70e5ac1e-65bb-4c89-a9df-fa9990cb6029"
STALE_DATASOURCE_ID = "00000000-0000-4000-8000-000000000000"
QT_ID = re.compile(r"qt_[a-z0-9]{3,10}")
# The structured field where a dump of the live template names its report.
REPORT_ID_FIELD = "report_config.shareable.id"
NO_REPORT_FIELD = f"the dump's {REPORT_ID_FIELD} is [], not ['{REPORT_ID}']"

SSH_KEYGEN = shutil.which("ssh-keygen")
RECEIPT_FORMAT = "bqca-live-capture-receipt/v2"
NAMESPACE = "bqca-live-capture-receipt"
# The signer that `tree` lists in the copy's signers file, with the `trusted`
# key of `signing_keys`, and the revisions of the live report that the
# captures before and after the write-back give.
SIGNER = "test-signer"
BEFORE_REVISION = "1830000000000"
REVISION = "1830000000001"
# The day that the pending sync began, in both states of `tree`.
SINCE_DATE = "2026-10-29"
# The revisions of the earlier sync that the `reopened` state records.
EARLIER_BEFORE_REVISION = "1820000000000"
EARLIER_REVISION = "1820000000001"
NO_REVISION = (
    "--revision is not given: a write needs the revision of the live report"
    " that the dump captured, as its receipt records it"
)
NO_BEFORE = (
    "--before is not given: a write needs the capture taken before the"
    " write-back, whose offline preview the dump must differ from"
)
NEEDS_BEFORE = (
    "a write needs --before, the capture before the write-back, and"
    " --revision, the live revision that the dump captured"
)
UNTRUSTED = (
    f"the receipt is not signed by a key that {SIGNERS} lists for its signer"
    f" '{SIGNER}'"
)
OFFLINE_PREVIEW = (
    "is an offline preview from tools/apply_bqca_rename_map.py, not a dump of"
    " the synced live template"
)
PREVIEW_CONTENT = (
    "has the parsed content of the offline preview that"
    " tools/apply_bqca_rename_map.py writes for --before"
)
# A stand-in for the internal export command: it prints the file it is given.
_EXPORT = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())"

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


def _today():
  """Today in UTC, the default `verified_date`."""
  return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _now():
  """The current UTC time, as a receipt's `signed_at` gives it."""
  return datetime.datetime.now(datetime.timezone.utc).strftime(
      "%Y-%m-%dT%H:%M:%SZ"
  )


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


def _ssh_environment():
  """The environment for ssh-keygen: no agent, so only key files sign."""
  environment = dict(os.environ)
  environment.pop("SSH_AUTH_SOCK", None)
  return environment


@pytest.fixture(scope="session")
def signing_keys(tmp_path_factory):
  """Two passphrase-less ed25519 key files, by name: trusted and untrusted."""
  if SSH_KEYGEN is None:
    pytest.skip("ssh-keygen (OpenSSH) is not installed")
  directory = tmp_path_factory.mktemp("keys")
  keys = {}
  for name in ("trusted", "untrusted"):
    keys[name] = directory / name
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", name, "-f"]
        + [str(keys[name])],
        check=True,
        env=_ssh_environment(),
    )
  return keys


def _public_key(key):
  """Return `<key type> <public key>` of the key file `key`."""
  text = key.with_name(f"{key.name}.pub").read_text(encoding="utf-8")
  return " ".join(text.split()[:2])


def _trust(tree, key, signer=SIGNER):
  """List `key` for `signer` in the signers file of the copy `tree`."""
  with (tree / SIGNERS).open("a", encoding="utf-8") as handle:
    handle.write(f'{signer} namespaces="{NAMESPACE}" {_public_key(key)}\n')


def _receipt_path(path):
  """Return where the capture receipt of the export `path` is."""
  return path.with_name(f"{path.name}.receipt.json")


def _export_command(source):
  """The stand-in export command that prints `source`."""
  return [sys.executable, "-c", _EXPORT, str(source)]


def _capture_tool(tree, *args):
  return subprocess.run(
      [sys.executable, str(tree / "tools/sign_live_capture.py"), *args],
      capture_output=True,
      text=True,
      check=False,
      env=_ssh_environment(),
  )


def _capture(
    tree, source, out, key, revision, before=None, signer=SIGNER, extra=()
):
  """Capture `source` to `out` with the copy's tool and the stand-in export."""
  args = ["--out", str(out), "--revision", revision, "--signer", signer]
  args += ["--key", str(key), *extra]
  if before is not None:
    args += ["--before", str(before)]
  return _capture_tool(tree, *args, "--", *_export_command(source))


def _captured(tree, source, out, key, revision, before=None, signer=SIGNER):
  """Capture `source` to `out`, which must succeed; return `out`."""
  result = _capture(tree, source, out, key, revision, before, signer)
  assert result.returncode == 0, result.stdout + result.stderr
  return out


def _pre_sync_text():
  """The pre-sync export: the sanitized cut, naming its report as one does."""
  return GENERIC_DUMP.read_text(encoding="utf-8").replace(
      "report_config {\n",
      "report_config {\n"
      "  shareable {\n"
      f'    id: "{REPORT_ID}"\n'
      '    name: "BQCA - Prompt & Response Logging"\n'
      "  }\n",
      1,
  )


def _receipt_bytes(tree, before=None, **fields):
  """Return a well-formed receipt for the copy's pending sync.

  It is of a capture after the write-back that names `before` (a capture
  path), or of one before it when `before` is None, for REVISION, SIGNER, now.
  """
  live = _live(tree)
  receipt = {
      "format": RECEIPT_FORMAT,
      "report_id": REPORT_ID,
      "revision": REVISION,
      "dump_sha256": "0" * 64,
      "signed_at": _now(),
      "signer": SIGNER,
      "export_command_sha256": "1" * 64,
      "manifest_sha256": live["manifest_sha256"],
      "template_bundle_sha256": live["template_bundle_sha256"],
      "pending_since_date": SINCE_DATE,
      "before_dump_sha256": (
          hashlib.sha256(before.read_bytes()).hexdigest()
          if before is not None
          else None
      ),
      "before_revision": BEFORE_REVISION if before is not None else None,
  }
  receipt.update(fields)
  return (json.dumps(receipt, indent=2) + "\n").encode("utf-8")


def _ssh_sign(data, key, namespace=NAMESPACE):
  """Return the signature of `data` that `ssh-keygen -Y sign` makes."""
  return subprocess.run(
      [SSH_KEYGEN, "-Y", "sign", "-f", str(key), "-n", namespace],
      input=data,
      capture_output=True,
      check=True,
      env=_ssh_environment(),
  ).stdout


def _write_receipt(path, data, key=None, namespace=NAMESPACE):
  """Write the receipt `data` to `path`, signed with `key` if it is given.

  Unlike `_capture`, this writes any receipt for any bytes, as a forger (or a
  signer who bypasses the capture tool) could.
  """
  path.write_bytes(data)
  if key is not None:
    path.with_name(f"{path.name}.sig").write_bytes(
        _ssh_sign(data, key, namespace)
    )
  return path


def _receipt_for(tree, path, key, before, **fields):
  """Write a receipt of the export `path`, naming `before`, signed by `key`."""
  digest = hashlib.sha256(path.read_bytes()).hexdigest()
  return _write_receipt(
      _receipt_path(path),
      _receipt_bytes(tree, before, **{"dump_sha256": digest, **fields}),
      key,
  )


def _write_args(before, revision=REVISION):
  return ["--before", str(before), "--revision", revision]


@pytest.fixture(params=["pending", "reopened"])
def tree(request, tmp_path, signing_keys):
  """A copy of dashboards/looker_studio with a pending live sync.

  The copy sits at `repo/dashboards/looker_studio`, so its tools see
  `repo/tests/fixtures` as the repository's fixtures, and its signers file
  lists the `trusted` key for SIGNER. In the `pending` state the copy keeps
  the repository binding's pending sync (re-opened once #559 is closed). In
  the `reopened` state the copy is first finalized by its own tools from two
  captures, so its binding carries a capture record and its committed
  receipt, and then re-opened, as a later sync would find it. The test fails
  if the repository's binding, signers file or committed receipt changes.
  """
  copy_root = tmp_path / "repo/dashboards/looker_studio"
  shutil.copytree(
      DASHBOARD, copy_root, ignore=shutil.ignore_patterns("__pycache__")
  )
  _trust(copy_root, signing_keys["trusted"])
  if PENDING not in _live(copy_root):
    _reopen(copy_root)
  if request.param == "reopened":
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    (earlier / "live-before.textproto").write_text(
        _pre_sync_text(), encoding="utf-8"
    )
    (earlier / "live-after.textproto").write_text(
        _render_dump(copy_root), encoding="utf-8"
    )
    before = _captured(
        copy_root,
        earlier / "live-before.textproto",
        earlier / "before.textproto",
        signing_keys["trusted"],
        EARLIER_BEFORE_REVISION,
    )
    after = _captured(
        copy_root,
        earlier / "live-after.textproto",
        earlier / "after.textproto",
        signing_keys["trusted"],
        EARLIER_REVISION,
        before,
    )
    result = _finalize(
        copy_root, "--dump", str(after), *_write_args(before, EARLIER_REVISION)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (copy_root / COMMITTED).is_file()
    _reopen(copy_root)
    assert _live(copy_root)["captured_by"] == SIGNER
  assert _live(copy_root)[PENDING]["since_date"] == SINCE_DATE
  repository = {
      relative: (DASHBOARD / relative).read_bytes()
      for relative in (BINDING, SIGNERS)
  }
  assert not (DASHBOARD / COMMITTED).exists()
  yield copy_root
  for relative, data in repository.items():
    assert (DASHBOARD / relative).read_bytes() == data, relative
  assert not (DASHBOARD / COMMITTED).exists()


@pytest.fixture
def trusted_key(signing_keys):
  return signing_keys["trusted"]


@pytest.fixture
def untrusted_key(signing_keys):
  return signing_keys["untrusted"]


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


def _render_dump(
    tree,
    report_datasource_id=None,
    report_ids=None,
    alias_datasource_id=None,
    extra_lines=(),
):
  """Render a dump of the synced live template from the copy's manifest.

  The dump is generated here, not taken from the checked-in fixtures: a
  `shareable` entry whose `id` names the binding's report (test-only
  provenance), one `component_config` per manifest component, in manifest
  order, with its `qt_` concepts and the references to them, then the
  report-level data source and `ds0` alias of the binding.
  `report_datasource_id`, `report_ids` (the `shareable.id` values) and
  `alias_datasource_id` replace those; `extra_lines` go first in
  `report_config`.
  """
  manifest = yaml.safe_load(
      (tree / "spec/bqca_chart_manifest.yaml").read_text(encoding="utf-8")
  )
  binding = yaml.safe_load((tree / BINDING).read_text(encoding="utf-8"))
  report_datasource_id = report_datasource_id or binding["datasource_id"]
  alias_datasource_id = alias_datasource_id or binding["datasource_id"]
  if report_ids is None:
    report_ids = [binding["report_id"]]
  lines = [
      "# Rendered by tests/test_looker_studio_live_sync.py",
      "report_config {",
      *(f"  {line}" for line in extra_lines),
      "  shareable {",
      *(f'    id: "{report_id}"' for report_id in report_ids),
      '    name: "BQCA - Prompt & Response Logging"',
      "  }",
  ]
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
      f'      datasource_id: "{alias_datasource_id}"',
      f'      alias: "{binding["data_source_alias"]}"',
      "    } }",
      "  }",
      "}",
  ]
  return "\n".join(lines) + "\n"


@pytest.fixture
def before(tree, tmp_path, trusted_key):
  """The capture before the write-back: the pre-sync export, captured."""
  live = tmp_path / "live"
  live.mkdir()
  (live / "before.textproto").write_text(_pre_sync_text(), encoding="utf-8")
  captures = tmp_path / "captures"
  captures.mkdir()
  return _captured(
      tree,
      live / "before.textproto",
      captures / "before.textproto",
      trusted_key,
      BEFORE_REVISION,
  )


@pytest.fixture
def dump(tree, tmp_path, before, trusted_key):
  """The capture after the write-back, naming `before`, outside any fixtures.

  The export is a dump of the synced live template that `_render_dump`
  generates; its parsed content differs from the offline preview of
  `before`.
  """
  source = tmp_path / "live/after.textproto"
  source.write_text(_render_dump(tree), encoding="utf-8")
  return _captured(
      tree,
      source,
      tmp_path / "captures/report.textproto",
      trusted_key,
      REVISION,
      before,
  )


@pytest.fixture
def receipt(dump):
  """The capture receipt of `dump`."""
  return _receipt_path(dump)


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
  """The day the pending sync started."""
  return _live(tree)[PENDING]["since_date"]


def _mode(path):
  return stat.S_IMODE(path.stat().st_mode)


# The keys of live_template_verification that finalizing writes.
_RECORD_KEYS = (
    "verified_date",
    "verified_manifest_sha256",
    "verified_template_bundle_sha256",
    "verified_revision",
    "verified_dump_sha256",
    "capture_receipt_sha256",
    "captured_by",
)


def _live_block(lines):
  """Return the (start, end) line range of live_template_verification."""
  start = lines.index(f"{LIVE}:\n") + 1
  end = next(
      (
          index
          for index in range(start, len(lines))
          if lines[index].strip() and not lines[index].startswith(" ")
      ),
      len(lines),
  )
  return start, end


def _is_record_line(line):
  return any(line.startswith(f"  {key}: ") for key in _RECORD_KEYS)


def _record_lines(text):
  """Return the record key lines of live_template_verification, in order."""
  lines = text.splitlines(keepends=True)
  start, end = _live_block(lines)
  return [line for line in lines[start:end] if _is_record_line(line)]


def _without_record(lines):
  """Return `lines` without the record lines of live_template_verification."""
  start, end = _live_block(lines)
  return (
      lines[:start]
      + [line for line in lines[start:end] if not _is_record_line(line)]
      + lines[end:]
  )


def test_finalize_records_the_live_verification_after_a_clean_audit(
    tree, dump, before, receipt
):
  """Two trusted captures and a clean audit finalize the binding, and only that.

  `verified_date` and both verified digests change, the revision, the
  SHA-256 of the dump and of its receipt, and the receipt's signer are
  recorded, `pending_live_sync` goes, and every other line of the binding,
  comments included, is kept. The dump's receipt and signature are committed
  next to the binding, byte for byte. The binding keeps its permissions, no
  staged file is left behind, and the copy passes the contract validator,
  which checks the committed receipt.
  """
  date = _today()
  dump_sha256 = hashlib.sha256(dump.read_bytes()).hexdigest()
  receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
  before_text = (tree / BINDING).read_text(encoding="utf-8")
  before_binding = yaml.safe_load(before_text)
  before_mode = _mode(tree / BINDING)
  bindings = {path.name for path in (tree / "bindings").iterdir()}

  result = _finalize(
      tree, "--dump", str(dump), *_write_args(before), "--verified-date", date
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert "ok: live report validated" in result.stdout
  assert (
      f"finalized: {BINDING} records the live verification of {date}"
      f" (revision {REVISION}, receipt signed by {SIGNER}) and has no"
      f" {PENDING}; {COMMITTED} and its signature are written.\n"
  ) in result.stdout
  after_text = (tree / BINDING).read_text(encoding="utf-8")
  expected = copy.deepcopy(before_binding)
  live = expected[LIVE]
  live["verified_date"] = date
  live["verified_manifest_sha256"] = live["manifest_sha256"]
  live["verified_template_bundle_sha256"] = live["template_bundle_sha256"]
  live["verified_revision"] = REVISION
  live["verified_dump_sha256"] = dump_sha256
  live["capture_receipt_sha256"] = receipt_sha256
  live["captured_by"] = SIGNER
  del live[PENDING]
  assert yaml.safe_load(after_text) == expected
  assert _mode(tree / BINDING) == before_mode
  committed = tree / COMMITTED
  assert committed.read_bytes() == receipt.read_bytes()
  assert (tree / f"{COMMITTED}.sig").read_bytes() == (
      receipt.with_name(f"{receipt.name}.sig").read_bytes()
  )
  assert _mode(committed) == _mode(tree / f"{COMMITTED}.sig") == 0o644
  assert {path.name for path in (tree / "bindings").iterdir()} == bindings | {
      committed.name,
      f"{committed.name}.sig",
  }

  # The verification record of live_template_verification is exactly these
  # lines, in this order, whether the binding had a capture record before
  # (the `reopened` state) or not.
  assert _record_lines(after_text) == [
      f'  verified_date: "{date}"\n',
      f"  verified_manifest_sha256: {live['manifest_sha256']}\n",
      f"  verified_template_bundle_sha256: {live['template_bundle_sha256']}\n",
      f'  verified_revision: "{REVISION}"\n',
      f'  verified_dump_sha256: "{dump_sha256}"\n',
      f'  capture_receipt_sha256: "{receipt_sha256}"\n',
      f'  captured_by: "{SIGNER}"\n',
  ]
  # Every other line, comments included, is kept, and only the lines of
  # pending_live_sync (its own line and the lines nested under it) are gone.
  before_lines = before_text.splitlines(keepends=True)
  start = before_lines.index(f"  {PENDING}:\n")
  end = start + 1
  while before_lines[end].startswith("    "):
    end += 1
  assert end > start + 1
  kept = before_lines[:start] + before_lines[end:]
  assert _without_record(kept) == _without_record(
      after_text.splitlines(keepends=True)
  )

  # The finalized copy passes the contract validator on its own.
  check = _validate(tree)
  assert check.returncode == 0, check.stderr
  assert check.stdout == "ok: contracts validated (bqca)\n"


def _validate(tree):
  return subprocess.run(
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


@pytest.mark.parametrize(
    "revision, signer",
    [
        ("0x1F", SIGNER),
        ("12:30", SIGNER),
        ("1.5", SIGNER),
        ("v2026-11-02T18:04:11.123Z", "maintainer@example.com"),
    ],
    ids=["hex-like", "sexagesimal-like", "float-like", "timestamp-and-at"],
)
def test_finalize_records_the_revision_and_signer_as_strings(
    tree, tmp_path, before, trusted_key, revision, signer
):
  """The revision and the signer are recorded exactly, as YAML strings.

  Unquoted, YAML would read `0x1F`, `12:30` or `1.5` as a number.
  """
  _trust(tree, trusted_key, signer)
  source = tmp_path / "live/after.textproto"
  source.write_text(_render_dump(tree), encoding="utf-8")
  capture = _captured(
      tree,
      source,
      tmp_path / "captures/report.textproto",
      trusted_key,
      revision,
      before,
      signer,
  )

  result = _finalize(
      tree, "--dump", str(capture), *_write_args(before, revision)
  )

  assert result.returncode == 0, result.stdout + result.stderr
  live = _live(tree)
  assert live["verified_revision"] == revision
  assert live["captured_by"] == signer
  text = (tree / BINDING).read_text(encoding="utf-8")
  assert f"  verified_revision: {json.dumps(revision)}\n" in text
  assert _validate(tree).returncode == 0


@pytest.mark.parametrize(
    "source, arguments",
    [("fixture", False), ("capture", False), ("capture", True)],
    ids=["fixture", "capture-without-arguments", "capture-with-arguments"],
)
def test_finalize_check_prints_the_change_and_writes_nothing(
    tree, dump, before, receipt, source, arguments
):
  """`--check` previews the change, and says why a write would be refused.

  It previews with a checked-in stand-in too, which has no receipt, and needs
  neither `--before` nor `--revision`; the preview shows placeholders for the
  values that a write records from what is missing.
  """
  path = REMAPPED_DUMP if source == "fixture" else dump
  date = _today()
  before_snapshot = _snapshot(tree)

  result = _finalize(
      tree,
      "--dump",
      str(path),
      "--check",
      "--verified-date",
      date,
      *(_write_args(before) if arguments else []),
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert _snapshot(tree) == before_snapshot
  assert "ok: live report validated" in result.stdout
  assert f"--- a/{BINDING}\n+++ b/{BINDING}\n" in result.stdout
  assert "-  pending_live_sync:\n" in result.stdout
  assert f'+  verified_date: "{date}"\n' in result.stdout
  recorded = {
      "verified_dump_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
      "verified_revision": REVISION if arguments else "<the --revision>",
  }
  if source == "fixture":
    recorded["capture_receipt_sha256"] = (
        "<the SHA-256 of the dump's capture receipt>"
    )
    recorded["captured_by"] = "<the signer of the dump's capture receipt>"
    refusal = (
        ", but a write would refuse it: cannot read the capture receipt"
        f" {_receipt_path(path)}: No such file or directory; {NO_REVISION};"
        f" {NO_BEFORE}; {NO_REPORT_FIELD}"
    )
  else:
    recorded["capture_receipt_sha256"] = hashlib.sha256(
        receipt.read_bytes()
    ).hexdigest()
    recorded["captured_by"] = SIGNER
    refusal = (
        ""
        if arguments
        else f", but a write would refuse it: {NO_REVISION};" f" {NO_BEFORE}"
    )
  # A record line that the `reopened` state already has is diff context.
  for key, value in recorded.items():
    line = re.escape(f"  {key}: {json.dumps(value)}")
    assert re.search(rf"(?m)^[+ ]{line}$", result.stdout), key
  assert (
      f"check: the live audit of {path} passed; {BINDING} would change as"
      f" above, and {COMMITTED} and its signature would be written{refusal};"
      " nothing written\n"
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
    tree, tmp_path, before, trusted_key, source, check
):
  """A stale or unreadable dump leaves every file of the copy unchanged.

  The stale dump identifies the live template and is captured after the
  write-back with a trusted receipt, but one of its concepts keeps a generic
  name, so its audit fails. A missing dump cannot be read at all.
  """
  path = tmp_path / "captures/report.textproto"
  if source == "stale":
    stale = tmp_path / "live/stale.textproto"
    stale.write_text(_stale_dump(tree), encoding="utf-8")
    _captured(tree, stale, path, trusted_key, REVISION, before)
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
  before_snapshot = _snapshot(tree)

  result = _finalize(
      tree,
      "--dump",
      str(path),
      *(["--check"] if check else _write_args(before)),
  )

  assert result.returncode == 1
  for error in expected:
    assert error in result.stderr
  assert _snapshot(tree) == before_snapshot


@pytest.mark.parametrize(
    "snapshot_kind, replacement_kind, replaced",
    [
        ("stale", "remapped", "after-the-checks"),
        ("valid", "stale", "after-the-checks"),
        ("valid", "remapped", "after-read"),
    ],
    ids=[
        "stale-snapshot-then-passing-file",
        "valid-snapshot-then-stale-file",
        "valid-snapshot-then-unidentified-file",
    ],
)
def test_finalize_uses_only_the_snapshot_it_read_first(
    tree,
    tmp_path,
    before,
    trusted_key,
    monkeypatch,
    snapshot_kind,
    replacement_kind,
    replaced,
):
  """The dump is read once; a file that replaces it later changes nothing.

  Another file lands at the caller's path right after the write checks or
  right after the dump is read. The checks, both audits (through a private
  copy) and the recorded digest use the snapshot, never that path: a stale
  snapshot is refused although its replacement (the remapped fixture) passes
  the audit, and a valid snapshot is finalized with its own digest although
  its replacement is stale or names no report. Each snapshot has a trusted
  receipt of its own bytes.
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
  _receipt_for(tree, path, trusted_key, before)
  original = (tree / BINDING).read_bytes()
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

  if replaced == "after-the-checks":
    monkeypatch.setattr(finalize, "write_blockers", blockers_then_replace)
  else:
    monkeypatch.setattr(finalize, "_finalize_snapshot", replace_then_finalize)
  monkeypatch.setattr(finalize, "_audit", audit)

  code = finalize.main(["--dump", str(path), *_write_args(before)])

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


@pytest.mark.parametrize(
    "replaced",
    ["receipt-valid-first", "receipt-other-revision-first", "before-export"],
)
def test_finalize_uses_only_the_captures_it_read_first(
    tree, dump, before, trusted_key, monkeypatch, replaced
):
  """The receipts, signatures and `--before` export are read once, too.

  Right after they are read, other files replace them. A valid receipt read
  first is recorded by its own SHA-256 although another receipt of the same
  dump, signed with the trusted key for another revision, replaces it; that
  receipt read first is refused although the valid one replaces it; and the
  `--before` export read first is the one the dump's receipt names, although
  other bytes replace it.
  """
  finalize = _load_finalize(tree)
  digest = hashlib.sha256(dump.read_bytes()).hexdigest()
  valid = _receipt_bytes(tree, before, dump_sha256=digest)
  other = _receipt_bytes(tree, before, dump_sha256=digest, revision="18300")
  read_first, replacement = (
      (other, valid)
      if replaced == "receipt-other-revision-first"
      else (valid, other)
  )
  receipt = _write_receipt(_receipt_path(dump), read_first, trusted_key)
  replacement_signature = _ssh_sign(replacement, trusted_key)
  original = (tree / BINDING).read_bytes()
  real_finalize_snapshot = finalize._finalize_snapshot

  def replace_then_finalize(*args, **kwargs):
    if replaced == "before-export":
      before.write_bytes(_render_dump(tree).encode("utf-8"))
    else:
      receipt.write_bytes(replacement)
      receipt.with_name(f"{receipt.name}.sig").write_bytes(
          replacement_signature
      )
    return real_finalize_snapshot(*args, **kwargs)

  monkeypatch.setattr(finalize, "_finalize_snapshot", replace_then_finalize)

  code = finalize.main(["--dump", str(dump), *_write_args(before)])

  if replaced == "receipt-other-revision-first":
    assert code == 1
    assert (tree / BINDING).read_bytes() == original
  else:
    assert code == 0
    assert (
        _live(tree)["capture_receipt_sha256"]
        == hashlib.sha256(valid).hexdigest()
    )
    assert (tree / COMMITTED).read_bytes() == valid


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


def _naming_the_report_in_an_inert_field(text):
  assert text.count("report_config {\n") == 1
  return text.replace(
      "report_config {\n",
      f'report_config {{\n  capture_note: "copied fixture for {REPORT_ID}"\n',
  )


def _naming_the_report_in_another_field(text):
  assert text.count("report_config {\n") == 1
  return text.replace(
      "report_config {\n", f'report_config {{\n  report_id: "{REPORT_ID}"\n'
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
        _naming_the_report_in_an_inert_field,
        _naming_the_report_in_another_field,
    ],
    ids=[
        "byte-identical-copy",
        "reordered-fields",
        "reordered-components",
        "audit-inert-field",
        "audit-inert-fields-everywhere",
        "report-named-in-comments",
        "report-named-in-an-inert-field",
        "report-named-in-another-field",
    ],
)
def test_finalize_refuses_a_reordered_or_audit_inert_stand_in(
    tree, tmp_path, before, trusted_key, mutate
):
  """No edit of a checked-in stand-in that keeps its fields finalizes.

  Each mutation of the remapped fixture, outside tests/fixtures, still passes
  the live audit. The copy's capture tool will not capture it after the
  write-back, and even with a receipt that the trusted key signed for its
  bytes, the write fails because report_config.shareable.id does not name
  the report, even when another field (an audit-inert note, or another
  `report_id` field) contains the report ID; the binding stays
  byte-identical. `--check` still previews.
  """
  source = tmp_path / "live/stand_in.textproto"
  source.write_text(
      mutate(REMAPPED_DUMP.read_text(encoding="utf-8")), encoding="utf-8"
  )
  audit = _audit(tree, source)
  assert audit.returncode == 0, audit.stderr
  path = tmp_path / "elsewhere/report.textproto"
  path.parent.mkdir()
  binding = (tree / BINDING).read_bytes()
  before_snapshot = _snapshot(tree)

  refused = _capture(tree, source, path, trusted_key, REVISION, before)

  assert refused.returncode == 1
  assert f"ERROR: {NO_REPORT_FIELD}" in refused.stderr
  assert list(path.parent.iterdir()) == []

  shutil.copyfile(source, path)
  _receipt_for(tree, path, trusted_key, before)
  result = _finalize(tree, "--dump", str(path), *_write_args(before))

  assert result.returncode == 1
  assert f"not finalized: {NO_REPORT_FIELD}" in result.stderr
  assert f"{BINDING} is unchanged" in result.stderr
  assert "ok: live report validated" not in result.stdout
  assert (tree / BINDING).read_bytes() == binding
  assert _snapshot(tree) == before_snapshot

  preview = _finalize(tree, "--dump", str(path), "--check")

  assert preview.returncode == 0, preview.stdout + preview.stderr
  assert (
      f"a write would refuse it: {NO_REVISION}; {NO_BEFORE}; {NO_REPORT_FIELD}"
  ) in preview.stdout
  assert _snapshot(tree) == before_snapshot


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

  assert errors and errors[0] == NO_REPORT_FIELD


_DATASOURCE_FIELD = "report_config.report.datasource_id"


@pytest.mark.parametrize(
    "variant, render, error",
    [
        ("no-report-field", {"report_ids": []}, NO_REPORT_FIELD),
        (
            "report-id-in-a-longer-value",
            {"report_ids": [f"copy of {REPORT_ID}"]},
            f"the dump's {REPORT_ID_FIELD} is ['copy of {REPORT_ID}'], not"
            f" ['{REPORT_ID}']",
        ),
        (
            "report-id-twice",
            {"report_ids": [REPORT_ID, REPORT_ID]},
            f"the dump's {REPORT_ID_FIELD} is ['{REPORT_ID}', '{REPORT_ID}'],"
            f" not ['{REPORT_ID}']",
        ),
        (
            "report-id-in-another-field",
            {"report_ids": [], "extra_lines": [f'report_id: "{REPORT_ID}"']},
            f"{NO_REPORT_FIELD} (the report ID is in report_config.report_id)",
        ),
        (
            "other-report-datasource",
            {"report_datasource_id": STALE_DATASOURCE_ID},
            f"the dump's {_DATASOURCE_FIELD} is ['{STALE_DATASOURCE_ID}'], not"
            f" ['{DATASOURCE_ID}']",
        ),
        (
            "other-alias-datasource",
            {"alias_datasource_id": STALE_DATASOURCE_ID},
            f"the dump's ds0 data source alias in"
            f" report_config.report.resource.datasource_alias points at"
            f" ['{STALE_DATASOURCE_ID}'], not ['{DATASOURCE_ID}']",
        ),
    ],
    ids=[
        "no-report-field",
        "report-id-in-a-longer-value",
        "report-id-twice",
        "report-id-in-another-field",
        "other-report-datasource",
        "other-alias-datasource",
    ],
)
def test_finalize_refuses_a_dump_that_does_not_identify_the_live_template(
    tree, tmp_path, before, trusted_key, variant, render, error
):
  """A write needs the exact report and data source IDs in their fields.

  The report ID must be the one value of report_config.shareable.id, the
  report-level data source the one value of
  report_config.report.datasource_id, and the `ds0` alias must point at it;
  all are checked before the audit runs, even with a trusted receipt.
  """
  path = tmp_path / "report.textproto"
  path.write_text(_render_dump(tree, **render), encoding="utf-8")
  _receipt_for(tree, path, trusted_key, before)
  before_snapshot = _snapshot(tree)

  result = _finalize(tree, "--dump", str(path), *_write_args(before))

  assert result.returncode == 1
  assert f"not finalized: {error}; {BINDING} is unchanged" in result.stderr
  assert "ok: live report validated" not in result.stdout
  assert _snapshot(tree) == before_snapshot


def test_finalize_refuses_a_stand_in_with_a_carriage_return_comment(
    tree, tmp_path, before, trusted_key
):
  """A standalone `\\r` after `#` cannot hide a missing report_id.

  `validate_contracts.py` reads the dump with `Path.read_text`, which
  normalizes `\\r` to `\\n`, so `capture_metadata { # comment\\r}\\n` closes its
  message and passes the audit. `finalize_live_sync.py` must decode with the
  same universal newlines and fail closed when `report_config.shareable.id` is
  missing, even with a trusted receipt.
  """
  path = tmp_path / "elsewhere/report.textproto"
  path.parent.mkdir()
  path.write_bytes(
      b"capture_metadata { # carriage-return comment\r}\n"
      + REMAPPED_DUMP.read_bytes()
  )
  audit = _audit(tree, path)
  assert audit.returncode == 0, audit.stderr
  _receipt_for(tree, path, trusted_key, before)
  before_snapshot = _snapshot(tree)

  refused = _finalize(tree, "--dump", str(path), *_write_args(before))

  assert refused.returncode == 1
  assert (
      f"not finalized: {NO_REPORT_FIELD}; {BINDING} is unchanged"
      in refused.stderr
  )
  assert "ok: live report validated" not in refused.stdout
  assert _snapshot(tree) == before_snapshot


def test_finalize_fails_closed_when_the_dump_cannot_be_decoded_or_parsed(
    tree, tmp_path, before, trusted_key, monkeypatch, capsys
):
  """Unreadable UTF-8 or broken textproto produces a write blocker."""
  finalize = _load_finalize(tree)
  binding = yaml.safe_load((tree / BINDING).read_text(encoding="utf-8"))
  before_snapshot = _snapshot(tree)

  assert finalize.dump_identity_errors("report_config {\n", binding) == [
      "cannot read the dump as protobuf text format: unterminated message:"
      " missing '}'"
  ]
  assert (
      finalize.dump_identity_errors(
          "capture_metadata { # comment\r}\n" + _render_dump(tree), binding
      )
      == []
  )
  assert finalize.dump_identity_errors(
      "capture_metadata { # comment\r}\n"
      + REMAPPED_DUMP.read_text(encoding="utf-8"),
      binding,
  ) == [NO_REPORT_FIELD]

  # Even if the validator subprocess returned 0, and with a trusted receipt,
  # a write refuses non-UTF-8 bytes and malformed textproto before any audit
  # or write runs.
  monkeypatch.setattr(finalize, "_audit", lambda audited_path: 0)
  for name, payload, expected in [
      ("bad_utf8.textproto", b"report_config {\n\xff}\n", "as UTF-8 text:"),
      (
          "broken.textproto",
          b"report_config {\n  shareable {\n",
          "cannot read the dump as protobuf text format: unterminated message:"
          " missing '}'",
      ),
  ]:
    path = tmp_path / name
    path.write_bytes(payload)
    _receipt_for(tree, path, trusted_key, before)
    code = finalize.main(["--dump", str(path), *_write_args(before)])
    assert code == 1
    stderr = capsys.readouterr().err
    assert expected in stderr
    assert f"{BINDING} is unchanged" in stderr
    assert _snapshot(tree) == before_snapshot


@pytest.mark.parametrize(
    "line_ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"]
)
def test_finalize_accepts_a_valid_dump_with_any_universal_newline_ending(
    tree, tmp_path, before, trusted_key, line_ending
):
  """A capture with LF, CRLF or CR line endings and `#` comments finalizes."""
  text = _render_dump(
      tree, extra_lines=["capture_metadata { # comment before brace", "  }"]
  ).replace("\n", line_ending)
  source = tmp_path / "live/universal.textproto"
  data = text.encode("utf-8")
  source.write_bytes(data)
  path = _captured(
      tree,
      source,
      tmp_path / "captures/universal.textproto",
      trusted_key,
      REVISION,
      before,
  )
  assert path.read_bytes() == data

  result = _finalize(tree, "--dump", str(path), *_write_args(before))

  assert result.returncode == 0, result.stdout + result.stderr
  assert _live(tree)["verified_dump_sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "args, error",
    [
        ([], NEEDS_BEFORE),
        (["--revision", REVISION], NEEDS_BEFORE),
        (["--before", "before.textproto"], NEEDS_BEFORE),
        (
            [
                "--captured-from",
                f"full textproto export of report {REPORT_ID} via the internal"
                " report tooling",
            ],
            "unrecognized arguments: --captured-from",
        ),
        (
            [
                "--receipt",
                "report.textproto.receipt.json",
                "--before",
                "before.textproto",
                "--revision",
                REVISION,
            ],
            "unrecognized arguments: --receipt",
        ),
        (
            ["--before", "before.textproto", "--revision", "two words"],
            "'two words' is not a revision",
        ),
        (
            ["--before", "before.textproto", "--revision", "r" * 129],
            "is not a revision",
        ),
    ],
    ids=[
        "nothing",
        "revision-only",
        "before-only",
        "attestation-only",
        "receipt-option",
        "revision-with-a-space",
        "revision-too-long",
    ],
)
def test_finalize_write_needs_a_before_capture_and_a_revision(
    tree, dump, args, error
):
  """A write without `--before` and `--revision` is a usage error.

  A free-text `--captured-from` attestation, which round 6 took as evidence,
  is no option at all, and the dump's receipt is found next to it, not given.
  """
  before_snapshot = _snapshot(tree)

  result = _finalize(tree, "--dump", str(dump), *args)

  assert result.returncode == 2
  assert error in result.stderr
  assert _snapshot(tree) == before_snapshot


def test_finalize_refuses_to_record_a_checked_in_fixture(
    tree, dump, before, trusted_key
):
  """A dump among the repository's test fixtures is not the live template.

  The path alone refuses a write, whatever the content and even with a
  trusted receipt of its bytes; `--check` may still preview the change with
  it.
  """
  stand_in = tree.parents[1] / "tests/fixtures/stand_in.textproto"
  stand_in.parent.mkdir(parents=True)
  shutil.copyfile(dump, stand_in)
  _receipt_for(tree, stand_in, trusted_key, before)
  before_snapshot = _snapshot(tree)

  refused = _finalize(tree, "--dump", str(stand_in), *_write_args(before))

  assert refused.returncode == 1
  assert (
      f"not finalized: {stand_in} is a test fixture of this repository;"
      f" {BINDING} is unchanged"
  ) in refused.stderr
  assert _snapshot(tree) == before_snapshot

  preview = _finalize(tree, "--dump", str(stand_in), "--check")

  assert preview.returncode == 0, preview.stdout + preview.stderr
  assert "-  pending_live_sync:\n" in preview.stdout
  assert f"{stand_in} is a test fixture of this repository" in preview.stdout
  assert _snapshot(tree) == before_snapshot


def test_finalize_refuses_a_fixture_on_a_case_insensitive_filesystem(
    tree, dump, before, trusted_key, monkeypatch, capsys
):
  """A case-variant path into tests/fixtures is still refused by samestat."""
  finalize = _load_finalize(tree)
  fixtures = (tree.parents[1] / "tests/fixtures").resolve()
  stand_in = fixtures / "stand_in.textproto"
  stand_in.parent.mkdir(parents=True, exist_ok=True)
  shutil.copyfile(dump, stand_in)
  _receipt_for(tree, stand_in, trusted_key, before)
  upper = tree.parents[1] / "TESTS/FIXTURES/stand_in.textproto"
  assert not upper.is_relative_to(fixtures)
  before_snapshot = _snapshot(tree)
  real_resolve = Path.resolve
  real_stat = Path.stat
  real_read_bytes = Path.read_bytes

  def map_case(path):
    text = str(path)
    target = str(tree.parents[1] / "TESTS/FIXTURES")
    if text == target or text.startswith(target + os.sep):
      return Path(str(fixtures) + text[len(target) :])
    return None

  def fake_resolve(self, *args, **kwargs):
    if map_case(self) is not None:
      return Path(os.path.abspath(self))
    return real_resolve(self, *args, **kwargs)

  def fake_stat(self, *args, **kwargs):
    mapped = map_case(self)
    return real_stat(mapped if mapped is not None else self, *args, **kwargs)

  def fake_read_bytes(self):
    mapped = map_case(self)
    return real_read_bytes(mapped if mapped is not None else self)

  monkeypatch.setattr(Path, "resolve", fake_resolve)
  monkeypatch.setattr(Path, "stat", fake_stat)
  monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

  code = finalize.main(["--dump", str(upper), *_write_args(before)])

  assert code == 1
  stderr = capsys.readouterr().err
  assert f"{upper} is a test fixture of this repository" in stderr
  assert f"{BINDING} is unchanged" in stderr
  stand_in.unlink()
  assert _snapshot(tree) == before_snapshot


@pytest.fixture
def offline_preview(tmp_path, before):
  """The offline preview that apply_bqca_rename_map.py writes for `before`.

  `before` is the pre-sync export, which names its report as one does, so the
  preview identifies the live template and passes the live audit; its parsed
  content is what a capture after a faithful write-back would have, unless a
  field of the live report changed.
  """
  work = tmp_path / "offline"
  work.mkdir()
  preview = work / "preview.textproto"
  applied = _apply("--dump", str(before), "--out", str(preview))
  assert applied.returncode == 0, applied.stdout + applied.stderr
  marker = _load_apply(DASHBOARD).OFFLINE_PREVIEW_MARKER
  assert preview.read_text(encoding="utf-8").startswith(f"{marker}\n")
  # The applier writes the preview and nothing else: no receipt, no signature.
  assert [path.name for path in work.iterdir()] == ["preview.textproto"]
  return preview


# The offline preview as written, and edited so that its marker changes case
# or spacing, moves, or is gone.
_MARKED = ["as-written", "upper-case", "respaced", "moved-to-the-end"]
# Without the marker, as it is or with its distinct fields (one concept's
# `id` and `name`) or its repeated values (the first two components) in
# another order: the same fields and values, so the same protobuf content.
_OFFLINE_VARIANTS = [
    *_MARKED,
    "stripped",
    "reordered-fields",
    "reordered-components",
]


def _offline_variant(preview, variant, directory):
  """Write `variant` of the offline preview to `directory`; return its path."""
  data = preview.read_bytes()
  marker = b"bqca-offline-preview:"
  assert data.count(marker) == 1
  first, rest = data.split(b"\n", 1)
  payload = {
      "as-written": data,
      "upper-case": data.replace(marker, b"BQCA-OFFLINE-PREVIEW:", 1),
      "respaced": data.replace(marker, b"Bqca Offline_Preview:", 1),
      "moved-to-the-end": rest + first + b"\n",
      "stripped": rest,
      "reordered-fields": _swap_id_and_name(rest.decode()).encode(),
      "reordered-components": (
          _swap_first_two_components(rest.decode()).encode()
      ),
  }[variant]
  path = directory / f"{variant}.textproto"
  path.write_bytes(payload)
  return path


@pytest.mark.parametrize(
    "variant", ["stripped", "reordered-fields", "reordered-components"]
)
def test_canonical_content_ignores_the_order_of_fields_and_repeated_values(
    tree, tmp_path, offline_preview, variant
):
  """Round 8: the preview comparison does not depend on the order of fields.

  Round 8 compared ordered parsed trees, so swapping one concept's adjacent
  `id` and `name` fields gave the stripped preview other content, and it was
  captured and finalized. Each variant here parses to another ordered tree
  (except the stripped one) but has the preview's fields and values, and the
  live audit passes it; a changed field value does change the content.
  """
  finalize = _load_finalize(tree)
  validator = _load_validator()
  candidate = _offline_variant(offline_preview, variant, tmp_path)
  assert _audit(tree, candidate).returncode == 0
  preview = validator.parse_report_textproto(
      offline_preview.read_text(encoding="utf-8")
  )
  parsed = validator.parse_report_textproto(
      candidate.read_text(encoding="utf-8")
  )

  assert (parsed == preview) == (variant == "stripped")
  assert finalize.canonical_content(parsed) == finalize.canonical_content(
      preview
  )
  edited = validator.parse_report_textproto(
      candidate.read_text(encoding="utf-8").replace(
          '"qt_1e5c0a97"', '"qt_1e5c0a98"', 1
      )
  )
  assert finalize.canonical_content(edited) != finalize.canonical_content(
      preview
  )


@pytest.mark.parametrize("variant", _OFFLINE_VARIANTS)
def test_finalize_refuses_an_offline_preview_with_only_an_attestation(
    tree, tmp_path, offline_preview, variant
):
  """Round 6: an edited offline preview never finalizes on an attestation.

  Round 6 took a free-text `--captured-from` attestation, and the absence of
  a case-sensitive marker comment, as evidence of a live capture: changing
  the marker's case or deleting it, with an attestation that named the
  report, finalized the offline preview. Each variant passes the live audit;
  a write now needs two trusted captures, and the attestation is no option.
  """
  candidate = _offline_variant(offline_preview, variant, tmp_path)
  audit = _audit(tree, candidate)
  assert audit.returncode == 0, audit.stderr
  original = (tree / BINDING).read_bytes()

  for attestation in (
      f"Locally transformed saved configuration of report {REPORT_ID};"
      " remote report unchanged",
      f"Exported report {REPORT_ID}",
  ):
    result = _finalize(
        tree, "--dump", str(candidate), "--captured-from", attestation
    )

    assert result.returncode != 0
    assert "unrecognized arguments: --captured-from" in result.stderr
    assert (tree / BINDING).read_bytes() == original
    assert PENDING in _live(tree)


@pytest.mark.parametrize(
    "forgery",
    [
        "no-receipt",
        "unsigned",
        "untrusted-key",
        "other-namespace",
        "trusted-signature-of-another-receipt",
    ],
)
@pytest.mark.parametrize("variant", _OFFLINE_VARIANTS)
def test_finalize_refuses_an_offline_preview_with_a_forged_receipt(
    tree,
    tmp_path,
    offline_preview,
    before,
    dump,
    receipt,
    untrusted_key,
    trusted_key,
    variant,
    forgery,
):
  """No receipt that the offline path can make finalizes an offline preview.

  Each forged receipt is for the variant's exact bytes and names `before`.
  Without the trusted key's signature over exactly those bytes the write
  fails, and whatever the receipt, the variant has the parsed content of the
  offline preview of `before`; the binding stays byte-identical.
  """
  candidate = _offline_variant(offline_preview, variant, tmp_path)
  digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
  forged = _receipt_path(candidate)
  data = _receipt_bytes(tree, before, dump_sha256=digest)
  invalid = (
      f"the receipt's signature is not a valid {NAMESPACE} signature of the"
      " receipt:"
  )
  if forgery == "no-receipt":
    error = (
        f"cannot read the capture receipt {forged}: No such file or directory"
    )
  elif forgery == "unsigned":
    _write_receipt(forged, data)
    error = (
        f"cannot read the receipt's signature {forged}.sig: No such file or"
        " directory"
    )
  elif forgery == "untrusted-key":
    _write_receipt(forged, data, untrusted_key)
    error = UNTRUSTED
  elif forgery == "other-namespace":
    _write_receipt(forged, data, trusted_key, namespace="file")
    error = invalid
  else:
    # The genuine receipt of `dump`, edited to name the preview, keeps the
    # genuine signature, which is not a signature of the edit.
    genuine = receipt.read_bytes()
    _write_receipt(
        forged,
        genuine.replace(
            hashlib.sha256(dump.read_bytes()).hexdigest().encode(),
            digest.encode(),
        ),
    )
    shutil.copyfile(f"{receipt}.sig", f"{forged}.sig")
    error = invalid
  original = (tree / BINDING).read_bytes()

  result = _finalize(tree, "--dump", str(candidate), *_write_args(before))

  assert result.returncode == 1
  assert result.stderr.startswith("not finalized: ")
  assert error in result.stderr
  assert f"{candidate} {PREVIEW_CONTENT}" in result.stderr
  assert (f"{candidate} {OFFLINE_PREVIEW}" in result.stderr) == (
      variant in _MARKED
  )
  assert "ok: live report validated" not in result.stdout
  assert (tree / BINDING).read_bytes() == original


@pytest.mark.parametrize("variant", _OFFLINE_VARIANTS)
def test_finalize_refuses_an_offline_preview_even_with_a_trusted_receipt(
    tree, tmp_path, offline_preview, before, trusted_key, variant
):
  """Round 7: a trusted receipt of the preview, marker or none, is refused.

  Round 7 finalized the preview with its marker line removed, given a
  receipt that a listed key signed for it. Now the receipt is trusted and
  names `before`, and still the variant has the parsed content of the
  offline preview of `before`, comments and spacing aside, so the write
  fails; a variant that keeps the marker in any case, spacing or place is
  refused for it too.
  """
  candidate = _offline_variant(offline_preview, variant, tmp_path)
  _receipt_for(tree, candidate, trusted_key, before)
  original = (tree / BINDING).read_bytes()

  result = _finalize(tree, "--dump", str(candidate), *_write_args(before))

  assert result.returncode == 1
  assert f"{candidate} {PREVIEW_CONTENT}" in result.stderr
  assert (f"{candidate} {OFFLINE_PREVIEW}" in result.stderr) == (
      variant in _MARKED
  )
  assert f"{BINDING} is unchanged" in result.stderr
  assert (tree / BINDING).read_bytes() == original
  assert PENDING in _live(tree)


@pytest.mark.parametrize("variant", _OFFLINE_VARIANTS)
def test_capture_refuses_an_offline_preview(
    tree, tmp_path, offline_preview, before, trusted_key, variant
):
  """The capture tool signs no receipt of the preview, marker or none.

  Round 7's signing tool signed any local file; with the marker line
  removed, it signed the preview. The capture tool only signs what an export
  command prints, and with `--before`, it compares that with the offline
  preview of `before`: even an "export command" that prints the stripped
  preview gets no receipt.
  """
  candidate = _offline_variant(offline_preview, variant, tmp_path)
  out = tmp_path / "captures/report.textproto"

  result = _capture(tree, candidate, out, trusted_key, REVISION, before)

  assert result.returncode == 1
  assert f"ERROR: {out} {PREVIEW_CONTENT}" in result.stderr
  assert (f"ERROR: {out} {OFFLINE_PREVIEW}" in result.stderr) == (
      variant in _MARKED
  )
  assert result.stderr.endswith("not captured: nothing written\n")
  assert sorted(path.name for path in out.parent.iterdir()) == [
      "before.textproto",
      "before.textproto.receipt.json",
      "before.textproto.receipt.json.sig",
  ]


@pytest.mark.parametrize("variant", _OFFLINE_VARIANTS)
def test_finalize_check_previews_an_offline_preview_and_lists_what_is_missing(
    tree, tmp_path, offline_preview, before, variant
):
  """`--check` previews an offline preview and says why a write refuses it."""
  candidate = _offline_variant(offline_preview, variant, tmp_path)
  before_snapshot = _snapshot(tree)

  checked = _finalize(
      tree, "--dump", str(candidate), "--check", *_write_args(before)
  )

  assert checked.returncode == 0, checked.stdout + checked.stderr
  assert "ok: live report validated" in checked.stdout
  assert "-  pending_live_sync:\n" in checked.stdout
  reasons = [
      f"cannot read the capture receipt {_receipt_path(candidate)}: No such"
      " file or directory"
  ]
  reasons.append(f"{candidate} {PREVIEW_CONTENT} {before}")
  if variant in _MARKED:
    reasons.append(f"{candidate} {OFFLINE_PREVIEW}")
  for reason in reasons:
    assert reason in checked.stdout
  assert checked.stdout.endswith("; nothing written\n")
  assert _snapshot(tree) == before_snapshot


def test_finalize_refuses_the_marker_anywhere_in_a_dump(
    tree, tmp_path, before, trusted_key
):
  """A dump that carries the marker in a string literal is refused too."""
  marker = _load_apply(DASHBOARD).OFFLINE_PREVIEW_MARKER
  path = tmp_path / "quoted_marker.textproto"
  path.write_text(
      _render_dump(tree, extra_lines=[f"capture_note: {json.dumps(marker)}"]),
      encoding="utf-8",
  )
  _receipt_for(tree, path, trusted_key, before)
  original = (tree / BINDING).read_bytes()

  result = _finalize(tree, "--dump", str(path), *_write_args(before))

  assert result.returncode == 1
  assert f"not finalized: {path} {OFFLINE_PREVIEW}" in result.stderr
  assert (tree / BINDING).read_bytes() == original


def test_a_listed_signer_who_edits_a_field_of_the_preview_is_the_trust_boundary(
    tree, tmp_path, offline_preview, before, trusted_key
):
  """What no check can refuse: a listed signer capturing an edited preview.

  An "export command" that prints the preview with its marker removed and
  one audit-inert field added gives content that is no longer the preview's,
  so the capture is signed and finalized. Only the signer's honesty and the
  review of the committed receipt (which names the signer) gate this path;
  docs/dashboard-implementation.md says so.
  """
  edited = tmp_path / "edited.textproto"
  text = offline_preview.read_text(encoding="utf-8").split("\n", 1)[1]
  assert text.count("report_config {\n") == 1
  edited.write_text(
      text.replace("report_config {\n", 'report_config {\n  note: "x"\n'),
      encoding="utf-8",
  )
  capture = _captured(
      tree,
      edited,
      tmp_path / "captures/report.textproto",
      trusted_key,
      REVISION,
      before,
  )

  result = _finalize(tree, "--dump", str(capture), *_write_args(before))

  assert result.returncode == 0, result.stdout + result.stderr
  assert _live(tree)["captured_by"] == SIGNER
  assert _validate(tree).returncode == 0


@pytest.mark.parametrize(
    "mismatch",
    [
        "report",
        "revision",
        "dump",
        "other-sync",
        "other-contracts",
        "before-revision",
        "before-dump",
        "same-revision-as-before",
    ],
)
def test_finalize_refuses_a_receipt_for_another_report_revision_dump_or_sync(
    tree, tmp_path, dump, before, trusted_key, mismatch
):
  """A trusted receipt binds report, revision, dump, sync and earlier capture.

  Each receipt is signed with the trusted key, so only the mismatch refuses
  it: one for another report; one that the copy's tool captured for another
  revision; one it captured for another dump; one for another pending sync
  or other pinned contracts; one that names another revision or export as
  the capture before the write-back; and one of the same revision as that
  capture.
  """
  digest = hashlib.sha256(dump.read_bytes()).hexdigest()
  live = _live(tree)
  if mismatch == "report":
    other = "00000000-0000-4000-8000-000000000559"
    _receipt_for(tree, dump, trusted_key, before, report_id=other)
    error = f"the receipt is for report '{other}', not '{REPORT_ID}'"
  elif mismatch == "revision":
    source = tmp_path / "live/after.textproto"
    other = _captured(
        tree, source, tmp_path / "other.textproto", trusted_key, "18305", before
    )
    for suffix in (".receipt.json", ".receipt.json.sig"):
      shutil.copyfile(f"{other}{suffix}", f"{dump}{suffix}")
    error = f"the receipt is for revision '18305', not --revision '{REVISION}'"
  elif mismatch == "dump":
    stale = tmp_path / "live/stale.textproto"
    stale.write_text(_stale_dump(tree), encoding="utf-8")
    other = _captured(
        tree, stale, tmp_path / "other.textproto", trusted_key, REVISION, before
    )
    for suffix in (".receipt.json", ".receipt.json.sig"):
      shutil.copyfile(f"{other}{suffix}", f"{dump}{suffix}")
    error = (
        "the receipt is for a dump with sha256"
        f" {hashlib.sha256(other.read_bytes()).hexdigest()}, not this dump,"
        f" whose sha256 is {digest}"
    )
  elif mismatch == "other-sync":
    _receipt_for(
        tree, dump, trusted_key, before, pending_since_date="2026-10-30"
    )
    error = (
        "the receipt is for the live sync pending since 2026-10-30, not the"
        f" one pending since {_since_date(tree)}"
    )
  elif mismatch == "other-contracts":
    _receipt_for(tree, dump, trusted_key, before, manifest_sha256="a" * 64)
    error = (
        f"the receipt is for manifest_sha256 {'a' * 64}, not the pinned"
        f" {live['manifest_sha256']}"
    )
  elif mismatch == "before-revision":
    _receipt_for(tree, dump, trusted_key, before, before_revision="18300")
    error = (
        "the receipt names the capture before the write-back of revision"
        f" '18300', not that of --before, '{BEFORE_REVISION}'"
    )
  elif mismatch == "before-dump":
    _receipt_for(tree, dump, trusted_key, before, before_dump_sha256="b" * 64)
    error = (
        "the receipt names the capture before the write-back with sha256"
        f" {'b' * 64}, not --before, whose sha256 is"
        f" {hashlib.sha256(before.read_bytes()).hexdigest()}"
    )
  else:
    _receipt_for(tree, dump, trusted_key, before, revision=BEFORE_REVISION)
    error = (
        f"the dump and --before are both of revision '{BEFORE_REVISION}': the"
        " live report has no new revision after the write-back"
    )
  revision = (
      BEFORE_REVISION if mismatch == "same-revision-as-before" else REVISION
  )
  before_snapshot = _snapshot(tree)

  refused = _finalize(tree, "--dump", str(dump), *_write_args(before, revision))

  assert refused.returncode == 1
  assert error in refused.stderr
  assert f"{BINDING} is unchanged" in refused.stderr
  assert "ok: live report validated" not in refused.stdout
  assert _snapshot(tree) == before_snapshot

  preview = _finalize(
      tree, "--dump", str(dump), "--check", *_write_args(before, revision)
  )

  assert preview.returncode == 0, preview.stdout + preview.stderr
  assert error in preview.stdout
  assert _snapshot(tree) == before_snapshot


def _write_capture_receipts(tree, dump, before, key, before_at, dump_at):
  """Sign both receipts by hand, at the given `signed_at` times."""
  _write_receipt(
      _receipt_path(before),
      _receipt_bytes(
          tree,
          revision=BEFORE_REVISION,
          dump_sha256=hashlib.sha256(before.read_bytes()).hexdigest(),
          signed_at=before_at,
      ),
      key,
  )
  _receipt_for(tree, dump, key, before, signed_at=dump_at)


@pytest.mark.parametrize(
    "signed_at, error",
    [
        (
            "2026-10-28T23:59:59Z",
            "the receipt was signed at 2026-10-28T23:59:59Z, before the live"
            " sync it is for began on 2026-10-29",
        ),
        (
            "2999-01-01T00:00:00Z",
            "the receipt was signed at 2999-01-01T00:00:00Z, which is in the"
            " future",
        ),
        ("an hour from now", "which is in the future"),
        ("2026-10-29T00:00:00Z", None),
    ],
    ids=["before-the-sync-began", "far-future", "an-hour-ahead", "on-the-day"],
)
def test_finalize_refuses_a_stale_or_future_receipt(
    tree, dump, before, trusted_key, signed_at, error
):
  """A receipt counts only if signed on or after the day its sync began.

  Round 7 checked only the format of `signed_at`, so a receipt signed before
  the pending sync began, or dated in the future, finalized it. The day the
  sync began is inclusive; a receipt more than a few minutes ahead of the
  clock is refused. Both receipts are signed by hand, the one before the
  write-back no later than the dump's.
  """
  assert _since_date(tree) == SINCE_DATE
  if signed_at == "an hour from now":
    signed_at = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
  before_at = min(signed_at, "2026-10-29T00:00:00Z")
  _write_capture_receipts(tree, dump, before, trusted_key, before_at, signed_at)
  before_snapshot = _snapshot(tree)
  args = ["--dump", str(dump), *_write_args(before)]
  args += ["--verified-date", _today()]

  result = _finalize(tree, *args)

  if error is None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert PENDING not in _live(tree)
    return
  assert result.returncode == 1
  assert error in result.stderr
  assert _snapshot(tree) == before_snapshot
  assert PENDING in _live(tree)

  preview = _finalize(tree, *args, "--check")

  assert preview.returncode == 0, preview.stdout + preview.stderr
  assert error in preview.stdout
  assert _snapshot(tree) == before_snapshot


def _without_signers(tree):
  """Give the copy the repository's signers file, which lists no signer."""
  shutil.copyfile(DASHBOARD / SIGNERS, tree / SIGNERS)


@pytest.mark.parametrize(
    "problem",
    [
        "missing-receipt",
        "missing-signature",
        "garbled-signature",
        "untrusted-key",
        "another-signer",
        "other-namespace",
        "edited-after-signing",
        "no-signer-listed",
        "before-untrusted",
        "before-is-after-a-write-back",
        "dump-is-before-a-write-back",
    ],
)
def test_finalize_refuses_a_receipt_it_cannot_trust(
    tree, dump, before, receipt, trusted_key, untrusted_key, problem
):
  """A write needs receipts that keys listed for their signers signed."""
  signature = receipt.with_name(f"{receipt.name}.sig")
  digest = hashlib.sha256(dump.read_bytes()).hexdigest()
  invalid = (
      f"the receipt's signature is not a valid {NAMESPACE} signature of the"
      " receipt:"
  )
  if problem == "missing-receipt":
    signature.unlink()
    receipt.unlink()
    error = (
        f"cannot read the capture receipt {receipt}: No such file or directory"
    )
  elif problem == "missing-signature":
    signature.unlink()
    error = (
        f"cannot read the receipt's signature {signature}: No such file or"
        " directory"
    )
  elif problem == "garbled-signature":
    signature.write_bytes(b"not a signature\n")
    error = invalid
  elif problem == "untrusted-key":
    _receipt_for(tree, dump, untrusted_key, before)
    error = UNTRUSTED
  elif problem == "another-signer":
    _receipt_for(tree, dump, trusted_key, before, signer="another-signer")
    error = (
        f"the receipt is not signed by a key that {SIGNERS} lists for its"
        " signer 'another-signer'"
    )
  elif problem == "other-namespace":
    _write_receipt(
        receipt,
        _receipt_bytes(tree, before, dump_sha256=digest),
        trusted_key,
        "file",
    )
    error = invalid
  elif problem == "edited-after-signing":
    # The same fields, written without indentation, are other bytes.
    receipt.write_bytes(
        json.dumps(json.loads(receipt.read_bytes())).encode() + b"\n"
    )
    error = invalid
  elif problem == "no-signer-listed":
    _without_signers(tree)
    error = f"{SIGNERS} lists no signer, so it trusts no receipt"
  elif problem == "before-untrusted":
    _write_receipt(
        _receipt_path(before),
        _receipt_bytes(
            tree,
            revision=BEFORE_REVISION,
            dump_sha256=hashlib.sha256(before.read_bytes()).hexdigest(),
        ),
        untrusted_key,
    )
    error = UNTRUSTED
  elif problem == "dump-is-before-a-write-back":
    # The dump's receipt names no capture before it.
    _receipt_for(tree, dump, trusted_key, None)
    error = (
        "the receipt is of a capture taken before the write-back (it names no"
        " capture before it), not after it"
    )
  else:
    # The receipt of `before` claims a capture taken after a write-back.
    _write_receipt(
        _receipt_path(before),
        _receipt_bytes(
            tree,
            dump,
            revision=BEFORE_REVISION,
            dump_sha256=hashlib.sha256(before.read_bytes()).hexdigest(),
        ),
        trusted_key,
    )
    error = (
        f"--before {before} is a capture taken after a write-back, not the"
        " capture before it"
    )
  before_snapshot = _snapshot(tree)

  refused = _finalize(tree, "--dump", str(dump), *_write_args(before))

  assert refused.returncode == 1
  assert error in refused.stderr
  assert refused.stderr.startswith("not finalized: ")
  assert "ok: live report validated" not in refused.stdout
  assert _snapshot(tree) == before_snapshot


_MALFORMED = [
    ("not-utf8", lambda tree, before: b"\xff\xfe{}\n", "it is not UTF-8 text"),
    ("empty", lambda tree, before: b"", "it is not JSON"),
    ("not-an-object", lambda tree, before: b"[]\n", "it is not a JSON object"),
    (
        "missing-fields",
        lambda tree, before: b'{"format": "x"}\n',
        "its fields are ['format'], not",
    ),
    (
        "extra-field",
        lambda tree, before: _receipt_bytes(tree, before, note="extra"),
        "its fields are ['before_dump_sha256', 'before_revision',",
    ),
    (
        "repeated-field",
        lambda tree, before: _receipt_bytes(tree, before).replace(
            f'  "revision": "{REVISION}",\n'.encode(),
            f'  "revision": "{REVISION}",\n  "revision": "18300",\n'.encode(),
        ),
        "it gives revision more than once",
    ),
    (
        "v1-format",
        lambda tree, before: _receipt_bytes(
            tree, before, format="bqca-live-capture-receipt/v1"
        ),
        "its format 'bqca-live-capture-receipt/v1' is not"
        f" '{RECEIPT_FORMAT}'",
    ),
    (
        "bad-digest",
        lambda tree, before: _receipt_bytes(tree, before, dump_sha256="ABC"),
        "its dump_sha256 'ABC' is not a SHA-256 hex digest",
    ),
    (
        "number-revision",
        lambda tree, before: _receipt_bytes(tree, before, revision=18300),
        "its revision 18300 is not a revision",
    ),
    (
        "pattern-signer",
        lambda tree, before: _receipt_bytes(tree, before, signer="*"),
        "its signer '*' is not a signer",
    ),
    (
        "bad-time",
        lambda tree, before: _receipt_bytes(
            tree, before, signed_at="2026-13-01T00:00:00Z"
        ),
        "its signed_at '2026-13-01T00:00:00Z' is not a valid date or time",
    ),
    (
        "bad-since-date",
        lambda tree, before: _receipt_bytes(
            tree, before, pending_since_date="2026-02-30"
        ),
        "its pending_since_date '2026-02-30' is not a valid date or time",
    ),
    (
        "half-null-before",
        lambda tree, before: _receipt_bytes(tree, before, before_revision=None),
        "it gives one of before_dump_sha256 and before_revision as null, not"
        " both",
    ),
    (
        "no-export-command",
        lambda tree, before: _receipt_bytes(
            tree, before, export_command_sha256=None
        ),
        "its export_command_sha256 None is not a SHA-256 hex digest",
    ),
]


@pytest.mark.parametrize(
    "make, reason",
    [(make, reason) for _, make, reason in _MALFORMED],
    ids=[name for name, _, _ in _MALFORMED],
)
def test_finalize_refuses_a_malformed_receipt(
    tree, dump, before, trusted_key, make, reason
):
  """A receipt that is not exactly a v2 receipt is refused, even if signed."""
  receipt = _write_receipt(_receipt_path(dump), make(tree, before), trusted_key)
  before_snapshot = _snapshot(tree)

  refused = _finalize(tree, "--dump", str(dump), *_write_args(before))

  assert refused.returncode == 1
  assert (
      f"not finalized: {receipt} is not a {RECEIPT_FORMAT} capture receipt:"
      f" {reason}"
  ) in refused.stderr
  assert _snapshot(tree) == before_snapshot


def test_finalize_refuses_a_receipt_without_ssh_keygen(
    tree, dump, before, monkeypatch, capsys
):
  finalize = _load_finalize(tree)
  original = (tree / BINDING).read_bytes()
  monkeypatch.setattr(shutil, "which", lambda name: None)

  code = finalize.main(["--dump", str(dump), *_write_args(before)])

  assert code == 1
  assert (
      "not finalized: cannot verify the receipt's signature: ssh-keygen"
      " (OpenSSH 8.2 or later) is not on PATH"
  ) in capsys.readouterr().err
  assert (tree / BINDING).read_bytes() == original


@pytest.mark.parametrize(
    "date, code, error",
    [
        ("2026-10-28", 1, "is before pending_live_sync.since_date"),
        ("2026-10-29", 1, "is before the dump's receipt was signed"),
        ("2999-01-01", 1, "--verified-date 2999-01-01 is in the future"),
        ("2026-13-01", 2, "'2026-13-01' is not a YYYY-MM-DD date"),
        ("20261030", 2, "'20261030' is not a YYYY-MM-DD date"),
    ],
)
def test_finalize_rejects_a_verified_date_it_cannot_record(
    tree, dump, before, date, code, error
):
  """`verified_date` is not before the pending sync, nor before the capture."""
  before_snapshot = _snapshot(tree)

  result = _finalize(
      tree,
      "--dump",
      str(dump),
      *_write_args(before),
      "--verified-date",
      date,
  )

  assert result.returncode == code
  assert error in result.stderr
  assert _snapshot(tree) == before_snapshot


def test_finalize_is_idempotent_and_can_record_a_later_sync(
    tree, tmp_path, dump, before, trusted_key
):
  first = _finalize(tree, "--dump", str(dump), *_write_args(before))
  assert first.returncode == 0, first.stdout + first.stderr
  finalized = _snapshot(tree)

  again = _finalize(tree, "--dump", str(dump), *_write_args(before))

  assert again.returncode == 0, again.stdout + again.stderr
  assert f"nothing to finalize: {BINDING} has no" in again.stdout
  assert _snapshot(tree) == finalized

  # A later sync replaces the capture record and the committed receipt
  # instead of adding others.
  _reopen(tree)
  later_revision = "1840000000000"
  later = _captured(
      tree,
      tmp_path / "live/after.textproto",
      tmp_path / "captures/later.textproto",
      trusted_key,
      later_revision,
      before,
  )
  result = _finalize(
      tree, "--dump", str(later), *_write_args(before, later_revision)
  )

  assert result.returncode == 0, result.stdout + result.stderr
  live = _live(tree)
  assert PENDING not in live
  assert live["verified_manifest_sha256"] == live["manifest_sha256"]
  assert live["verified_template_bundle_sha256"] == (
      live["template_bundle_sha256"]
  )
  assert live["verified_revision"] == later_revision
  later_receipt = _receipt_path(later).read_bytes()
  assert (
      live["capture_receipt_sha256"]
      == hashlib.sha256(later_receipt).hexdigest()
  )
  assert (tree / COMMITTED).read_bytes() == later_receipt
  text = (tree / BINDING).read_text(encoding="utf-8")
  for key in (
      "verified_revision",
      "verified_dump_sha256",
      "capture_receipt_sha256",
      "captured_by",
  ):
    assert text.count(f"\n  {key}: ") == 1, key
  assert _validate(tree).returncode == 0


def _committed_files(tree):
  """The bytes of the binding and the committed receipt files, if any."""
  return {
      relative: (tree / relative).read_bytes()
      if (tree / relative).exists()
      else None
      for relative in (BINDING, COMMITTED, f"{COMMITTED}.sig")
  }


def test_finalize_restores_the_binding_when_the_audit_fails_after_the_write(
    tree, dump, before, monkeypatch, capsys
):
  finalize = _load_finalize(tree)
  original = _committed_files(tree)
  calls = []

  def audit(path):
    calls.append((path, (tree / BINDING).read_bytes() == original[BINDING]))
    return 0 if len(calls) == 1 else 1

  monkeypatch.setattr(finalize, "_audit", audit)

  assert finalize.main(["--dump", str(dump), *_write_args(before)]) == 1
  # Both audits read the same private copy of the dump; the second ran on the
  # written binding, which is then restored with the committed receipt.
  (audited, before_write), (audited_again, after_write) = calls
  assert audited == audited_again and audited.resolve() != dump.resolve()
  assert (before_write, after_write) == (True, False)
  assert _committed_files(tree) == original
  assert (
      "not finalized: tools/validate_contracts.py exited 1 after the write,"
      f" so {BINDING} and {COMMITTED} were restored"
  ) in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    ["audit-launch-error", "audit-interrupted", "write-interrupted"],
)
def test_finalize_restores_the_binding_after_an_error_or_interrupt(
    tree, dump, before, monkeypatch, capsys, failure
):
  """An exception or interrupt during the write or post-write audit rolls back.

  The post-write audit fails to launch (OSError) or is interrupted, or the
  write is interrupted right after the new binding replaced the old one.
  Each time the original bytes of the binding and of the committed receipt
  come back (a receipt that was not there is removed), and no staged file is
  left.
  """
  finalize = _load_finalize(tree)
  original = _committed_files(tree)
  bindings = sorted(path.name for path in (tree / "bindings").iterdir())
  launches = []
  real_run = subprocess.run

  def run(command, *args, **kwargs):
    if any(str(part).endswith("validate_contracts.py") for part in command):
      launches.append(command)
      if len(launches) == 2:
        if failure == "audit-launch-error":
          raise OSError("cannot launch the validator")
        raise KeyboardInterrupt
    return real_run(command, *args, **kwargs)

  writes = []
  real_replace_file = finalize._replace_file

  def replace_file(path, data):
    writes.append((path, data))
    real_replace_file(path, data)
    if path == tree / BINDING and len(writes) == 3:
      assert (tree / BINDING).read_bytes() != original[BINDING]
      raise KeyboardInterrupt

  if failure == "write-interrupted":
    monkeypatch.setattr(finalize, "_replace_file", replace_file)
  else:
    monkeypatch.setattr(finalize, "subprocess", types.SimpleNamespace(run=run))
  argv = ["--dump", str(dump), *_write_args(before)]

  if failure == "audit-launch-error":
    assert finalize.main(argv) == 1
    assert (
        "not finalized: OSError: cannot launch the validator after the write,"
        f" so {BINDING} and {COMMITTED} were restored"
    ) in capsys.readouterr().err
  else:
    with pytest.raises(KeyboardInterrupt):
      finalize.main(argv)
    assert (
        f"interrupted: {BINDING} and {COMMITTED} were restored"
        in capsys.readouterr().err
    )
  assert _committed_files(tree) == original
  assert sorted(path.name for path in (tree / "bindings").iterdir()) == bindings
  if failure == "write-interrupted":
    assert [path for path, _ in writes[:3]] == [
        tree / COMMITTED,
        tree / f"{COMMITTED}.sig",
        tree / BINDING,
    ]
    assert writes[3] == (tree / BINDING, original[BINDING])
  else:
    assert len(launches) == 2


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

  # A new file, such as the first committed receipt, gets mode 0644.
  created = target.parent / "created.json"
  finalize._replace_file(created, b"created\n")
  assert created.read_bytes() == b"created\n"
  assert _mode(created) == 0o644
  created.unlink()


# A capture record for `finalized_binding_text`.
_RECORD = {
    "verified_revision": REVISION,
    "verified_dump_sha256": "a" * 64,
    "capture_receipt_sha256": "b" * 64,
    "captured_by": SIGNER,
}


@pytest.mark.parametrize(
    "pattern, replacement, record, error",
    [
        (
            r"^  pending_live_sync:$",
            "  pending_live_sync: &pending",
            _RECORD,
            "exactly one '  pending_live_sync:' line",
        ),
        (
            r"^  method:$",
            '  verified_date: "2026-10-30"\n  method:',
            _RECORD,
            "exactly one verified_date line",
        ),
        (
            r"^  method:$",
            '  captured_by: "a"\n  captured_by: "b"\n  method:',
            _RECORD,
            "at most one captured_by line",
        ),
        # A one-line edit of a multi-line value would leave a stray line.
        (
            r'^  verified_date: "(\d{4}-\d{2}-\d{2})"$',
            r"  verified_date: >-\n    \1",
            _RECORD,
            "the edited live_template_verification is not valid YAML",
        ),
        (
            r"^  method:$",
            "  method:",
            {k: v for k, v in _RECORD.items() if k != "captured_by"},
            "the capture record must give a string for",
        ),
    ],
    ids=[
        "pending-not-a-plain-block",
        "repeated-verified-date",
        "repeated-captured-by",
        "folded-verified-date",
        "incomplete-record",
    ],
)
def test_finalized_binding_text_refuses_a_binding_it_cannot_edit_exactly(
    tree, pattern, replacement, record, error
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
    finalize.finalized_binding_text(edited, "2026-10-30", record)


def _load_capture(tree):
  spec = importlib.util.spec_from_file_location(
      "live_sync_capture", tree / "tools/sign_live_capture.py"
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_capture_writes_the_export_and_a_receipt_that_ssh_keygen_verifies(
    tree, tmp_path, trusted_key
):
  """The tool writes the bytes the export command printed, and their receipt.

  Before the write-back the receipt names no earlier capture; after it, it
  names the `--before` capture. Anyone can check the signatures with
  ssh-keygen and the signers file; the receipts carry the binding's pinned
  digests and pending sync, and only a digest of the export command.
  """
  live = _live(tree)
  sources = tmp_path / "live"
  sources.mkdir()
  (sources / "before.textproto").write_text(_pre_sync_text(), encoding="utf-8")
  (sources / "after.textproto").write_text(_render_dump(tree), encoding="utf-8")
  captures = tmp_path / "captures"
  captures.mkdir()
  results = {}
  for name, revision, before in (
      ("before", BEFORE_REVISION, None),
      ("after", REVISION, captures / "before.textproto"),
  ):
    out = captures / f"{name}.textproto"
    results[name] = _capture(
        tree, sources / f"{name}.textproto", out, trusted_key, revision, before
    )
    assert results[name].returncode == 0, (
        results[name].stdout + results[name].stderr
    )
    export = (sources / f"{name}.textproto").read_bytes()
    assert out.read_bytes() == export
    assert _mode(out) == 0o600
    receipt = _receipt_path(out)
    signature = receipt.with_name(f"{receipt.name}.sig")
    data = receipt.read_bytes()
    fields = json.loads(data)
    assert list(fields) == [
        "format",
        "report_id",
        "revision",
        "dump_sha256",
        "signed_at",
        "signer",
        "export_command_sha256",
        "manifest_sha256",
        "template_bundle_sha256",
        "pending_since_date",
        "before_dump_sha256",
        "before_revision",
    ]
    command = _export_command(sources / f"{name}.textproto")
    assert data == _receipt_bytes(
        tree,
        before,
        revision=revision,
        dump_sha256=hashlib.sha256(export).hexdigest(),
        signed_at=fields["signed_at"],
        export_command_sha256=hashlib.sha256(
            json.dumps(command).encode()
        ).hexdigest(),
        before_revision=BEFORE_REVISION if before is not None else None,
    )
    assert fields["manifest_sha256"] == live["manifest_sha256"]
    assert fields["pending_since_date"] == live[PENDING]["since_date"]
    assert str(sources) not in data.decode()
    assert signature.read_bytes().startswith(b"-----BEGIN SSH SIGNATURE-----\n")
    assert _mode(receipt) == _mode(signature) == 0o644
    verified = subprocess.run(
        [SSH_KEYGEN, "-Y", "verify", "-f", str(tree / SIGNERS), "-I", SIGNER]
        + ["-n", NAMESPACE, "-s", str(signature)],
        input=data,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    lines = results[name].stdout.splitlines()
    assert lines[0] == (
        f"export: {len(export)} bytes, sha256"
        f" {hashlib.sha256(export).hexdigest()}"
    )
    assert lines[1] == (
        f"captured: {out} (mode 600), {receipt} and {signature}: revision"
        f" {revision}, signed by {SIGNER} at {fields['signed_at']}"
    )
  assert results["after"].stdout.splitlines()[2] == (
      f"next: python3 {tree / 'tools/finalize_live_sync.py'} --dump"
      f" {captures / 'after.textproto'} --before"
      f" {captures / 'before.textproto'} --revision {REVISION} --check"
  )


@pytest.mark.parametrize(
    "problem",
    [
        "no-report-field-before",
        "no-report-field-after",
        "other-data-source-after",
        "not-utf8",
        "not-textproto",
        "same-bytes-as-before",
    ],
)
def test_capture_refuses_an_export_that_finalize_refuses(
    tree, tmp_path, before, trusted_key, problem
):
  """An export that is not a dump of the live template gets no receipt.

  Before the write-back, the report-level data source is still the one the
  sync repoints, so only the report ID must identify the template; after it,
  the data sources must be the binding's too.
  """
  source = tmp_path / "live/export.textproto"
  after = problem != "no-report-field-before"
  payload, error = {
      "no-report-field-before": (
          GENERIC_DUMP.read_bytes(),
          NO_REPORT_FIELD,
      ),
      "no-report-field-after": (REMAPPED_DUMP.read_bytes(), NO_REPORT_FIELD),
      "other-data-source-after": (
          _render_dump(tree, report_datasource_id=STALE_DATASOURCE_ID).encode(),
          f"the dump's report_config.report.datasource_id is"
          f" ['{STALE_DATASOURCE_ID}']",
      ),
      "not-utf8": (b"report_config {\n\xff}\n", "as UTF-8 text:"),
      "not-textproto": (
          b"report_config {\n",
          "cannot read the dump as protobuf text format",
      ),
      "same-bytes-as-before": (
          before.read_bytes(),
          "the export is byte for byte the capture before the write-back",
      ),
  }[problem]
  source.write_bytes(payload)
  out = tmp_path / "out/export.textproto"
  out.parent.mkdir()

  result = _capture(
      tree,
      source,
      out,
      trusted_key,
      REVISION,
      before if after else None,
  )

  assert result.returncode == 1
  assert error in result.stderr
  assert result.stderr.endswith("not captured: nothing written\n")
  assert list(out.parent.iterdir()) == []


@pytest.mark.parametrize(
    "problem",
    [
        "out-inside-the-repository",
        "out-exists",
        "no-pending-sync",
        "before-without-receipt",
        "before-is-after-a-write-back",
        "same-revision-as-before",
        "export-fails",
        "export-prints-nothing",
        "export-not-found",
    ],
)
def test_capture_writes_nothing_when_it_cannot_capture(
    tree, tmp_path, dump, before, trusted_key, problem
):
  source = tmp_path / "live/after.textproto"
  out = tmp_path / "out/report.textproto"
  out.parent.mkdir()
  revision, use_before, command = REVISION, before, _export_command(source)
  if problem == "out-inside-the-repository":
    out = tree / "report.textproto"
    error = f"{out} is inside this repository"
  elif problem == "out-exists":
    out.write_bytes(b"earlier")
    error = f"{out} exists; --force replaces it"
  elif problem == "no-pending-sync":
    finalized = _finalize(tree, "--dump", str(dump), *_write_args(before))
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    error = f"{BINDING} has no {PENDING}.since_date"
  elif problem == "before-without-receipt":
    _receipt_path(before).unlink()
    error = f"cannot read the capture receipt {_receipt_path(before)}"
  elif problem == "before-is-after-a-write-back":
    use_before = dump
    error = f"--before {dump} is a capture taken after a write-back"
  elif problem == "same-revision-as-before":
    revision = BEFORE_REVISION
    error = f"--revision {BEFORE_REVISION} is the revision of --before"
  elif problem == "export-fails":
    command = [sys.executable, "-c", "raise SystemExit(3)"]
    error = "the export command exited 3"
  elif problem == "export-prints-nothing":
    command = [sys.executable, "-c", "pass"]
    error = "the export command wrote nothing to standard output"
  else:
    command = [str(tmp_path / "no-such-export-command")]
    error = "cannot run the export command:"
  files = sorted(out.parent.iterdir())
  args = ["--out", str(out), "--revision", revision, "--signer", SIGNER]
  args += ["--key", str(trusted_key), "--before", str(use_before)]

  result = _capture_tool(tree, *args, "--", *command)

  assert result.returncode == 1
  assert error in result.stderr
  assert result.stderr.endswith("not captured: nothing written\n")
  assert sorted(out.parent.iterdir()) == files


def test_capture_writes_nothing_for_a_key_that_is_not_listed(
    tree, tmp_path, untrusted_key
):
  """A receipt that finalize would not trust is not written.

  The error says which line would list the key.
  """
  source = tmp_path / "before.textproto"
  source.write_text(_pre_sync_text(), encoding="utf-8")
  out = tmp_path / "out/before.textproto"
  out.parent.mkdir()

  result = _capture(tree, source, out, untrusted_key, BEFORE_REVISION)

  assert result.returncode == 1
  assert f"ERROR: {UNTRUSTED}\n" in result.stderr
  assert (
      f"ERROR: to trust the key, list it in {SIGNERS} in a reviewed change,"
      f' then capture again: {SIGNER} namespaces="{NAMESPACE}"'
      f" {_public_key(untrusted_key)}\n"
  ) in result.stderr
  assert result.stderr.endswith("not captured: nothing written\n")
  assert list(out.parent.iterdir()) == []


def test_capture_writes_nothing_when_ssh_keygen_cannot_sign(tree, tmp_path):
  source = tmp_path / "before.textproto"
  source.write_text(_pre_sync_text(), encoding="utf-8")
  out = tmp_path / "out/before.textproto"
  out.parent.mkdir()

  result = _capture(
      tree, source, out, tmp_path / "no-such-key", BEFORE_REVISION
  )

  assert result.returncode == 1
  assert "ERROR: cannot sign the receipt: ssh-keygen -Y sign exited" in (
      result.stderr
  )
  assert list(out.parent.iterdir()) == []


def test_capture_replaces_a_capture_only_with_force(tree, before, trusted_key):
  source = before.parent.parent / "live/before.textproto"
  receipt = _receipt_path(before)
  data = receipt.read_bytes()

  again = _capture(tree, source, before, trusted_key, "1830000000002")

  assert again.returncode == 1
  for path in (before, receipt, receipt.with_name(f"{receipt.name}.sig")):
    assert f"ERROR: {path} exists; --force replaces it\n" in again.stderr
  assert receipt.read_bytes() == data

  forced = _capture(
      tree, source, before, trusted_key, "1830000000002", extra=["--force"]
  )

  assert forced.returncode == 0, forced.stdout + forced.stderr
  assert json.loads(receipt.read_bytes())["revision"] == "1830000000002"


@pytest.mark.parametrize(
    "args, error",
    [
        (
            [],
            "the following arguments are required: --out, --revision,"
            " --signer, --key",
        ),
        (
            ["--revision", REVISION, "--signer", SIGNER],
            "give the export command",
        ),
        (
            ["--revision", "two words", "--signer", SIGNER, "--", "x"],
            "'two words' is not a revision",
        ),
        (
            ["--revision", REVISION, "--signer", "*", "--", "x"],
            "'*' is not a signer",
        ),
        (
            ["--revision", REVISION, "--signer", "one,two", "--", "x"],
            "'one,two' is not a signer",
        ),
        (
            ["--dump", "d", "--revision", REVISION, "--signer", SIGNER],
            "unrecognized arguments: --dump",
        ),
    ],
    ids=[
        "nothing",
        "no-export-command",
        "bad-revision",
        "pattern-signer",
        "two-signers",
        "a-dump-file",
    ],
)
def test_capture_exits_2_on_bad_arguments(tree, capsys, args, error):
  """The tool signs only what an export command prints, never a dump file."""
  capture = _load_capture(tree)
  if args:
    args = ["--out", "export.textproto", "--key", "key", *args]

  with pytest.raises(SystemExit) as exit_info:
    capture.main(args)

  assert exit_info.value.code == 2
  assert error in capsys.readouterr().err


def _hand_finalize(tree):
  """Mark the copy's live sync done by hand, as finalize would not.

  Re-pins both verified digests and deletes `pending_live_sync`, keeping the
  rest of `live_template_verification` (in the `reopened` state, the capture
  record and committed receipt of the earlier sync).
  """
  path = tree / BINDING
  binding = yaml.safe_load(path.read_text(encoding="utf-8"))
  live = binding[LIVE]
  text = path.read_text(encoding="utf-8")
  for key, pinned in (
      ("verified_manifest_sha256", live["manifest_sha256"]),
      ("verified_template_bundle_sha256", live["template_bundle_sha256"]),
  ):
    text, count = re.subn(
        rf"(?m)^  {key}: .*$", f"  {key}: {pinned}", text, count=1
    )
    assert count == 1, key
  start = text.index(f"\n  {PENDING}:\n") + 1
  end = start + len(f"  {PENDING}:\n")
  while text[end:].startswith("    "):
    end = text.index("\n", end) + 1
  path.write_text(text[:start] + text[end:], encoding="utf-8")
  assert PENDING not in _live(tree)


def test_validate_contracts_refuses_a_live_sync_recorded_by_hand(tree):
  """Re-pinning the digests and deleting pending_live_sync is not a sync.

  Without a capture of the synced template, the committed receipt is missing
  (`pending`) or is the earlier sync's, signed after the stale
  `verified_date` that the hand edit leaves (`reopened`).
  """
  assert _validate(tree).returncode == 0
  _hand_finalize(tree)

  result = _validate(tree)

  assert result.returncode == 1
  if (tree / COMMITTED).exists():
    expected = (
        f"bqca live_template_verification.verified_date"
        f" '{REOPENED_VERIFIED_DATE}' is before {COMMITTED} was signed at"
    )
  else:
    expected = (
        "bqca live_template_verification records a live sync (its verified"
        " digests are the pinned ones), so it needs the capture receipt that"
        f" finalized it: cannot read {COMMITTED}: No such file or directory"
    )
  assert expected in result.stderr


# How a hand edit can leave `pending_live_sync` once the digests are
# re-pinned: gone, or present with a null, empty or wrong-typed value.
_PENDING_SPELLINGS = {
    "absent": None,
    "null": "null",
    "tilde": "~",
    "no-value": "",
    "empty-mapping": "{}",
    "empty-list": "[]",
    "empty-string": '""',
    "false": "false",
}


@pytest.mark.parametrize("spelling", list(_PENDING_SPELLINGS))
def test_validate_contracts_refuses_a_hand_sync_whatever_pending_says(
    tree, spelling
):
  """Round 8: `pending_live_sync: null` no longer bypasses the receipt check.

  With both digests re-pinned, the stale `verified_date` kept, no committed
  receipt and the shipped signers file, which lists no signer, the binding
  fails validation whether `pending_live_sync` is gone or present with any
  value that is not a well-formed mapping.
  """
  assert _validate(tree).returncode == 0
  _hand_finalize(tree)
  value = _PENDING_SPELLINGS[spelling]
  if value is not None:
    path = tree / BINDING
    text = path.read_text(encoding="utf-8")
    anchor = re.search(r"(?m)^  verified_template_bundle_sha256: .*\n", text)
    assert anchor is not None
    line = f"  {PENDING}: {value}".rstrip() + "\n"
    path.write_text(
        text[: anchor.end()] + line + text[anchor.end() :], encoding="utf-8"
    )
    assert PENDING in _live(tree)
  for suffix in ("", ".sig"):
    (tree / f"{COMMITTED}{suffix}").unlink(missing_ok=True)
  _without_signers(tree)

  result = _validate(tree)

  assert result.returncode == 1
  assert (
      "bqca live_template_verification records a live sync (its verified"
      " digests are the pinned ones), so it needs the capture receipt that"
      f" finalized it: cannot read {COMMITTED}: No such file or directory"
  ) in result.stderr
  assert (
      f"bqca live_template_verification.{PENDING} must be removed"
      in result.stderr
  ) == (value is not None)


def _set_record(tree, key, value):
  """Set one line of the capture record of the copy's binding."""
  path = tree / BINDING
  text, count = re.subn(
      rf"(?m)^  {key}: .*$",
      f"  {key}: {json.dumps(value)}",
      path.read_text(encoding="utf-8"),
      count=1,
  )
  assert count == 1, key
  path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "edit",
    [
        "verified_dump_sha256",
        "verified_revision",
        "capture_receipt_sha256",
        "captured_by",
        "verified_date",
        "receipt-bytes",
        "receipt-removed",
        "signature-removed",
        "signer-removed",
        "receipt-before-the-write-back",
        "receipt-for-other-contracts",
    ],
)
def test_validate_contracts_checks_the_committed_receipt_of_a_finalized_binding(
    tree, dump, before, receipt, trusted_key, edit
):
  """Only a binding and receipt that finalize wrote together validate.

  After finalize, the copy validates. Each edit of the record, the committed
  receipt, its signature or the signers file then fails the validator; an
  edit that re-signs another receipt with the trusted key fails on what the
  receipt says.
  """
  finalized = _finalize(tree, "--dump", str(dump), *_write_args(before))
  assert finalized.returncode == 0, finalized.stdout + finalized.stderr
  assert _validate(tree).returncode == 0
  committed = tree / COMMITTED
  prefix = "bqca live_template_verification"
  live = _live(tree)
  if edit in (
      "verified_dump_sha256",
      "capture_receipt_sha256",
      "verified_revision",
      "captured_by",
  ):
    value = "c" * 64 if edit.endswith("sha256") else "someone-else"
    _set_record(tree, edit, value)
    expected = f"{prefix}.{edit} {value!r} is not {live[edit]!r}"
  elif edit == "verified_date":
    _set_record(tree, edit, SINCE_DATE)
    expected = (
        f"{prefix}.verified_date '{SINCE_DATE}' is before {COMMITTED} was"
        " signed at"
    )
  elif edit == "receipt-bytes":
    committed.write_bytes(
        json.dumps(json.loads(committed.read_bytes())).encode() + b"\n"
    )
    expected = (
        f"{prefix}: {COMMITTED}: the receipt's signature is not a valid"
        f" {NAMESPACE} signature of the receipt:"
    )
  elif edit == "receipt-removed":
    committed.unlink()
    expected = f"cannot read {COMMITTED}: No such file or directory"
  elif edit == "signature-removed":
    (tree / f"{COMMITTED}.sig").unlink()
    expected = f"cannot read {COMMITTED}.sig: No such file or directory"
  elif edit == "signer-removed":
    _without_signers(tree)
    expected = f"{SIGNERS} lists no signer, so it trusts no receipt"
  else:
    # A receipt signed with the trusted key, and a record that matches it:
    # only what the receipt says can fail.
    if edit == "receipt-before-the-write-back":
      data = _receipt_bytes(
          tree, revision=BEFORE_REVISION, dump_sha256="d" * 64
      )
      expected = (
          f"{prefix}: {COMMITTED} is the receipt of a capture taken before the"
          " write-back, not after it"
      )
    else:
      data = _receipt_bytes(
          tree, before, dump_sha256="d" * 64, manifest_sha256="e" * 64
      )
      expected = (
          f"{prefix}: {COMMITTED} is for manifest_sha256 {'e' * 64}, not the"
          f" pinned {live['manifest_sha256']}"
      )
    _write_receipt(committed, data, trusted_key)
    fields = json.loads(data)
    for key, value in (
        ("verified_revision", fields["revision"]),
        ("verified_dump_sha256", fields["dump_sha256"]),
        ("capture_receipt_sha256", hashlib.sha256(data).hexdigest()),
    ):
      _set_record(tree, key, value)

  result = _validate(tree)

  assert result.returncode == 1
  assert expected in result.stderr


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
  # Every input the map is built from is listed with its digest, the
  # binding (which gives the repointed datasource_id) included.
  inputs = [
      ("dump", GENERIC_DUMP),
      ("manifest", DASHBOARD / "spec/bqca_chart_manifest.yaml"),
      ("binding", DASHBOARD / BINDING),
      ("remapped twin", REMAPPED_DUMP),
  ]
  assert [line for line in markdown.splitlines() if line.endswith("`)")] == [
      f"- {role}: `{path.relative_to(ROOT).as_posix()}` (sha256"
      f" `{hashlib.sha256(path.read_bytes()).hexdigest()}`)"
      for role, path in inputs
  ]

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


APPLY_TOOL = DASHBOARD / "tools/apply_bqca_rename_map.py"


def _load_apply(tree):
  spec = importlib.util.spec_from_file_location(
      "live_sync_apply", tree / "tools/apply_bqca_rename_map.py"
  )
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _apply(*args):
  return subprocess.run(
      [sys.executable, str(APPLY_TOOL), *args],
      capture_output=True,
      text=True,
      check=False,
  )


def _header_and_body(data):
  """Split a dump's bytes into its leading `#` comment lines and the rest."""
  lines = data.splitlines(keepends=True)
  count = 0
  while count < len(lines) and lines[count].startswith(b"#"):
    count += 1
  return b"".join(lines[:count]), b"".join(lines[count:])


def _remapped_sanitized_dump():
  """Return the bytes that the tool writes for the sanitized dump.

  The tool prepends OFFLINE_PREVIEW_MARKER and keeps every comment, and the
  sanitized dump and its remapped twin have different ones. So the result is
  the marker line, the sanitized dump's header and, from its first line that
  is not a comment on, the twin.
  """
  marker = f"{_load_apply(DASHBOARD).OFFLINE_PREVIEW_MARKER}\n".encode("utf-8")
  header = _header_and_body(GENERIC_DUMP.read_bytes())[0]
  return marker + header + _header_and_body(REMAPPED_DUMP.read_bytes())[1]


@pytest.fixture
def apply_tree(tmp_path):
  """A copy of dashboards/looker_studio, as `repo/dashboards/looker_studio`.

  The tool sees the copy as its repository, so a write that it should refuse
  lands in `tmp_path`, never in this repository.
  """
  copy_root = tmp_path / "repo/dashboards/looker_studio"
  shutil.copytree(
      DASHBOARD, copy_root, ignore=shutil.ignore_patterns("__pycache__")
  )
  return copy_root


@pytest.fixture
def work(tmp_path):
  """An empty directory outside any repository, for dumps and outputs."""
  path = tmp_path / "work"
  path.mkdir()
  return path


def test_apply_renames_the_sanitized_dump_into_its_remapped_twin(tmp_path):
  out = tmp_path / "remapped.textproto"

  result = _apply("--dump", str(GENERIC_DUMP), "--out", str(out))

  assert result.returncode == 0, result.stdout + result.stderr
  assert result.stderr == ""
  written = out.read_bytes()
  assert _mode(out) == 0o600
  # The comments are kept, so the output has the offline-preview marker and the
  # sanitized dump's comments, not the twin's. From its first line that is not
  # a comment on, it is the twin, byte for byte.
  marker = f"{_load_apply(DASHBOARD).OFFLINE_PREVIEW_MARKER}\n".encode("utf-8")
  assert written != REMAPPED_DUMP.read_bytes()
  assert written.startswith(marker)
  header, body = _header_and_body(written)
  assert header == marker + _header_and_body(GENERIC_DUMP.read_bytes())[0]
  assert body == _header_and_body(REMAPPED_DUMP.read_bytes())[1]
  assert written == _remapped_sanitized_dump()
  # After the prepended marker line, 209 lines change, one literal each: the 65
  # concept names and ids, the 65 `concept_names` (23 dimensions, 42 metrics),
  # the 13 `sort_column`s and the report-level `datasource_id`.
  before = GENERIC_DUMP.read_text(encoding="utf-8").splitlines()
  after = written.decode("utf-8").splitlines()
  assert after[0] == _load_apply(DASHBOARD).OFFLINE_PREVIEW_MARKER
  assert len(after[1:]) == len(before)
  assert collections.Counter(
      new.split(":")[0].strip()
      for old, new in zip(before, after[1:])
      if old != new
  ) == {
      "name": 65,
      "id": 65,
      "concept_names": 65,
      "sort_column": 13,
      "datasource_id": 1,
  }
  generic = GENERIC_DUMP.read_bytes()
  summary = result.stdout.splitlines()
  assert summary[:-1] == [
      f"snapshot of {GENERIC_DUMP}: {len(generic)} bytes, sha256"
      f" {hashlib.sha256(generic).hexdigest()}",
      "map: 65 concepts in 34 components, from the dump and"
      " spec/bqca_chart_manifest.yaml",
      "edits: 209 scalars in 209 lines: name 65, id 65, dimension 23, metric"
      " 42, sort 13, datasource_id 1",
      f"report datasource_id: '{STALE_DATASOURCE_ID}' -> '{DATASOURCE_ID}'",
      "verified: only the planned scalars differ, and the remapped dump is the"
      " dump with the map applied",
      "audit: tools/validate_contracts.py --profile bqca"
      " --live-report-textproto exits 0 on the remapped dump, which"
      " gen_bqca_rename_map.py accepts as its twin",
      f"written: {out} ({len(written)} bytes, sha256"
      f" {hashlib.sha256(written).hexdigest()}, mode 600)",
  ]
  # It says what the result is not: a dump of a synced template.
  assert "is not a dump of a synced template" in summary[-1]
  assert "tools/finalize_live_sync.py" in summary[-1]

  # The same dump gives the same bytes, and a file is replaced only when asked.
  again = _apply("--dump", str(GENERIC_DUMP), "--out", str(out))
  assert again.returncode == 1
  assert f"{out} exists; --force replaces it" in again.stderr
  assert out.read_bytes() == written
  out.chmod(0o644)
  forced = _apply("--dump", str(GENERIC_DUMP), "--out", str(out), "--force")
  assert forced.returncode == 0, forced.stdout + forced.stderr
  assert out.read_bytes() == written
  assert _mode(out) == 0o600
  # It writes the remapped dump and nothing else: never a capture receipt.
  assert [path.name for path in tmp_path.iterdir()] == ["remapped.textproto"]


def test_apply_maps_a_dump_that_has_the_new_ids_to_itself(tmp_path):
  out = tmp_path / "remapped.textproto"
  marker = f"{_load_apply(DASHBOARD).OFFLINE_PREVIEW_MARKER}\n".encode("utf-8")

  result = _apply("--dump", str(REMAPPED_DUMP), "--out", str(out))

  assert result.returncode == 0, result.stdout + result.stderr
  assert out.read_bytes() == marker + REMAPPED_DUMP.read_bytes()
  assert "edits: none; the dump already carries every new ID" in result.stdout
  assert f"report datasource_id: already '{DATASOURCE_ID}'" in result.stdout

  again = tmp_path / "remapped_again.textproto"
  second = _apply("--dump", str(out), "--out", str(again))
  assert second.returncode == 0, second.stdout + second.stderr
  assert again.read_bytes() == out.read_bytes()


def test_apply_accepts_the_map_that_gen_bqca_rename_map_writes(tmp_path):
  map_csv = tmp_path / "rename-map.csv"
  assert _rename_map("--csv", str(map_csv)).returncode == 0
  plain, mapped = tmp_path / "plain.textproto", tmp_path / "mapped.textproto"
  assert (
      _apply("--dump", str(GENERIC_DUMP), "--out", str(plain)).returncode == 0
  )

  result = _apply(
      "--dump", str(GENERIC_DUMP), "--map", str(map_csv), "--out", str(mapped)
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert mapped.read_bytes() == plain.read_bytes()
  assert f"; {map_csv} matches it exactly" in result.stdout


@pytest.mark.parametrize("target", ["none", "new", "existing"])
def test_apply_check_runs_every_check_and_writes_nothing(tmp_path, target):
  out = tmp_path / "remapped.textproto"
  args = ["--dump", str(GENERIC_DUMP), "--check"]
  if target != "none":
    args += ["--out", str(out)]
  if target == "existing":
    out.write_bytes(b"earlier")

  result = _apply(*args)

  assert result.returncode == 0, result.stdout + result.stderr
  assert result.stderr == ""
  lines = result.stdout.splitlines()
  assert lines[2].startswith("edits: 209 scalars in 209 lines: ")
  assert lines[4].startswith("verified: ")
  assert lines[5].startswith("audit: ")
  if target == "existing":
    assert lines[6] == (
        f"check: every check passed, but a write would refuse it: {out} exists;"
        " --force replaces it; nothing written"
    )
    assert out.read_bytes() == b"earlier"
    assert list(tmp_path.iterdir()) == [out]
  else:
    assert lines[6] == "check: every check passed; nothing written"
    assert list(tmp_path.iterdir()) == []
  assert "is not a dump of a synced template" in lines[7]


@pytest.mark.parametrize("via", ["path", "dot-dot", "symlink"])
def test_apply_refuses_an_out_inside_the_repository(
    apply_tree, work, capsys, via
):
  apply = _load_apply(apply_tree)
  repository = apply_tree.parent.parent
  inside = repository / "report.textproto"
  out = {
      "path": inside,
      "dot-dot": apply_tree / "../../report.textproto",
      "symlink": work / "link.textproto",
  }[via]
  if via == "symlink":
    out.symlink_to(inside)
  before = _snapshot(repository)

  code = apply.main(["--dump", str(GENERIC_DUMP), "--out", str(out), "--force"])

  assert code == 1
  assert "is inside this repository" in capsys.readouterr().err
  assert not inside.exists()
  assert _snapshot(repository) == before


@pytest.mark.parametrize("nested", [False, True], ids=["root", "subdir"])
def test_apply_refuses_an_out_inside_the_repository_on_a_case_insensitive_filesystem(
    apply_tree, work, monkeypatch, capsys, nested
):
  """A case-variant spelling of the repository root is still refused by samestat."""
  apply = _load_apply(apply_tree)
  repository = apply_tree.parent.parent.resolve()
  if nested:
    (repository / "sub").mkdir()
  upper_repo = repository.parent / "REPO"
  out = (
      (upper_repo / "sub/report.textproto")
      if nested
      else (upper_repo / "report.textproto")
  )
  assert not out.is_relative_to(repository)
  before = _snapshot(repository)
  real_resolve = Path.resolve
  real_stat = Path.stat
  real_is_dir = Path.is_dir

  def map_case(path):
    text = str(path)
    target = str(upper_repo)
    if text == target or text.startswith(target + os.sep):
      return Path(str(repository) + text[len(target) :])
    return None

  def fake_resolve(self, *args, **kwargs):
    if map_case(self) is not None:
      return Path(os.path.abspath(self))
    return real_resolve(self, *args, **kwargs)

  def fake_stat(self, *args, **kwargs):
    mapped = map_case(self)
    return real_stat(mapped if mapped is not None else self, *args, **kwargs)

  def fake_is_dir(self):
    mapped = map_case(self)
    return real_is_dir(mapped if mapped is not None else self)

  monkeypatch.setattr(Path, "resolve", fake_resolve)
  monkeypatch.setattr(Path, "stat", fake_stat)
  monkeypatch.setattr(Path, "is_dir", fake_is_dir)

  code = apply.main(["--dump", str(GENERIC_DUMP), "--out", str(out), "--force"])

  assert code == 1
  assert f"ERROR: {out} is inside this repository" in capsys.readouterr().err
  assert _snapshot(repository) == before

  check_code = apply.main(
      ["--dump", str(GENERIC_DUMP), "--out", str(out), "--check"]
  )
  assert check_code == 0
  stdout = capsys.readouterr().out
  assert (
      f"check: every check passed, but a write would refuse it: {out} is"
      " inside this repository, which must not hold a dump of the live"
      " template; nothing written"
  ) in stdout
  assert _snapshot(repository) == before


def _out_by_link(repository, work, via):
  """Lay out a link by which an `--out` is, or seems to be, in the repository.

  Returns the `--out`, the link of the repository that must survive the run,
  and what that link leads to outside the repository.
  """
  outside = work / "outside.textproto"
  if via == "directory-link-to-outside":
    outside = work / "elsewhere"
    outside.mkdir()
    link = repository / "linked"
    link.symlink_to(outside)
    return link / "out.textproto", link, outside
  if via != "link-to-missing-file":
    outside.write_bytes(b"earlier")
  if via == "outside-link-to-directory":
    (repository / "sub").mkdir()
    link = repository / "sub/leaked.textproto"
    link.symlink_to(outside)
    (work / "linked").symlink_to(repository / "sub")
    return work / "linked/leaked.textproto", link, outside
  link = repository / "leaked.textproto"
  link.symlink_to(outside)
  return link, link, outside


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
@pytest.mark.parametrize(
    "via",
    [
        "link-to-file",
        "link-to-missing-file",
        "outside-link-to-directory",
        "directory-link-to-outside",
    ],
)
def test_apply_refuses_an_out_that_a_link_puts_in_the_repository(
    apply_tree, work, capsys, via, force
):
  """A write replaces the entry `--out` and never writes through a link.

  So an `--out` that is a link in the repository, or is in a directory that
  leads there, would become a regular file in the repository, wherever the link
  points. An `--out` whose path goes through a link in the repository is
  refused too, wherever the link leads: it can be pointed elsewhere.
  """
  apply = _load_apply(apply_tree)
  repository = apply_tree.parent.parent
  out, link, target = _out_by_link(repository, work, via)
  repository_before, work_before = _snapshot(repository), _snapshot(work)
  args = ["--dump", str(GENERIC_DUMP), "--out", str(out)]

  code = apply.main([*args, "--force"] if force else args)

  assert code == 1
  assert "is inside this repository" in capsys.readouterr().err
  assert link.is_symlink() and Path(os.readlink(link)) == target
  assert _snapshot(repository) == repository_before
  assert _snapshot(work) == work_before


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
@pytest.mark.parametrize("via", ["same-path", "symlink", "hard-link"])
def test_apply_refuses_an_out_that_is_the_dump(
    apply_tree, work, capsys, via, force
):
  apply = _load_apply(apply_tree)
  dump = work / "dump.textproto"
  shutil.copyfile(GENERIC_DUMP, dump)
  out = work / "out.textproto"
  if via == "symlink":
    out.symlink_to(dump)
  elif via == "hard-link":
    os.link(dump, out)
  else:
    out = dump
  args = ["--dump", str(dump), "--out", str(out)] + (
      ["--force"] if force else []
  )

  assert apply.main(args) == 1

  assert f"{out} is the dump itself" in capsys.readouterr().err
  assert dump.read_bytes() == GENERIC_DUMP.read_bytes()
  assert sorted(path.name for path in work.iterdir()) == sorted(
      {"dump.textproto", out.name}
  )


def test_apply_replaces_an_existing_out_only_with_force(
    apply_tree, work, capsys
):
  apply = _load_apply(apply_tree)
  out = work / "out.textproto"
  out.write_bytes(b"earlier")
  out.chmod(0o644)
  args = ["--dump", str(GENERIC_DUMP), "--out", str(out)]

  assert apply.main(args) == 1

  assert f"{out} exists; --force replaces it" in capsys.readouterr().err
  assert (out.read_bytes(), _mode(out)) == (b"earlier", 0o644)

  assert apply.main([*args, "--force"]) == 0

  assert out.read_bytes() == _remapped_sanitized_dump()
  assert _mode(out) == 0o600
  assert [path.name for path in work.iterdir()] == ["out.textproto"]


def test_apply_refuses_an_out_it_cannot_create(apply_tree, work, capsys):
  apply = _load_apply(apply_tree)
  (work / "directory").mkdir()
  missing = work / "missing/out.textproto"

  for out, error in [
      (missing, f"the directory of {missing} does not exist"),
      (work / "directory", f"{work / 'directory'} is a directory"),
  ]:
    code = apply.main(
        ["--dump", str(GENERIC_DUMP), "--out", str(out), "--force"]
    )

    assert code == 1
    assert error in capsys.readouterr().err
  assert [path.name for path in work.iterdir()] == ["directory"]


def test_apply_refuses_an_out_below_a_missing_directory_and_dot_dot(work):
  # `missing/..` folds into `work`, which exists, but a write opens the path as
  # it is spelled, and fails. So the write and the check must agree.
  out = work / "missing/../out.textproto"
  error = f"the directory of {out} does not exist"
  args = ["--dump", str(GENERIC_DUMP), "--out", str(out)]

  written = _apply(*args)
  checked = _apply(*args, "--check")

  assert written.returncode == 1
  assert error in written.stderr
  assert checked.returncode == 0, checked.stdout + checked.stderr
  assert (
      f"check: every check passed, but a write would refuse it: {error};"
      " nothing written"
  ) in checked.stdout
  assert list(work.iterdir()) == []


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
def test_apply_leaves_nothing_behind_when_the_write_fails(
    apply_tree, work, capsys, monkeypatch, force
):
  apply = _load_apply(apply_tree)
  monkeypatch.setattr(apply, "_audit", lambda path: (0, ""))
  out = work / "out.textproto"
  if force:
    out.write_bytes(b"earlier")

  def fail(*args, **kwargs):
    raise OSError(errno.EIO, "the disk failed")

  monkeypatch.setattr(apply.os, "replace" if force else "link", fail)
  args = ["--dump", str(GENERIC_DUMP), "--out", str(out)]

  code = apply.main([*args, "--force"] if force else args)

  assert code == 1
  assert f"cannot write {out}: the disk failed" in capsys.readouterr().err
  assert [path.name for path in work.iterdir()] == (
      ["out.textproto"] if force else []
  )
  if force:
    assert out.read_bytes() == b"earlier"


@pytest.mark.parametrize(
    "args, error",
    [
        ([], "the following arguments are required: --dump"),
        (
            ["--dump", "dump.textproto"],
            "a write needs --out; --check runs the checks without it",
        ),
        (
            ["--dump", "dump.textproto", "--check", "--force"],
            "--force replaces an existing --out, which is not given",
        ),
        # The applier neither writes nor reads a capture receipt.
        (
            ["--dump", "dump.textproto", "--out", "out", "--receipt", "r.json"],
            "unrecognized arguments: --receipt",
        ),
    ],
    ids=["no-dump", "no-out-or-check", "force-without-out", "receipt"],
)
def test_apply_exits_2_on_bad_arguments(apply_tree, capsys, args, error):
  apply = _load_apply(apply_tree)

  with pytest.raises(SystemExit) as exit_info:
    apply.main(args)

  assert exit_info.value.code == 2
  assert error in capsys.readouterr().err


@pytest.fixture(scope="module")
def rename_map_text(tmp_path_factory):
  """Return the rename map of the sanitized dump, as the generator writes it."""
  path = tmp_path_factory.mktemp("rename-map") / "rename-map.csv"
  result = _rename_map("--csv", str(path))
  assert result.returncode == 0, result.stderr
  return path.read_text(encoding="utf-8")


def _csv_rows(text):
  return list(csv.reader(io.StringIO(text, newline="")))


def _csv_text(rows):
  out = io.StringIO()
  csv.writer(out, lineterminator="\n").writerows(rows)
  return out.getvalue()


def _with_cell(rows, index, column, value):
  edited = [list(row) for row in rows]
  edited[index][RENAME_MAP_HEADER.index(column)] = value
  return edited


@pytest.mark.parametrize(
    "mutate, error",
    [
        (
            lambda rows: _with_cell(rows, 1, "new_qt_id", "qt_deadbeef"),
            "new_qt_id is 'qt_deadbeef', not '",
        ),
        (
            lambda rows: _with_cell(rows, 7, "old_name", "met2"),
            "old_name is 'met2', not '",
        ),
        (
            lambda rows: _with_cell(rows, 3, "display_name", "Another name"),
            "display_name is 'Another name', not '",
        ),
        (
            lambda rows: rows[:-1],
            "the map has 64 rows, but the dump and the manifest give 65",
        ),
        (
            lambda rows: [*rows, rows[-1]],
            "the map has 66 rows, but the dump and the manifest give 65",
        ),
        (
            lambda rows: rows[:1],
            "the map has 0 rows, but the dump and the manifest give 65",
        ),
        (
            lambda rows: [rows[0], rows[2], rows[1], *rows[3:]],
            "map line 2 (",
        ),
        (
            lambda rows: [["pages", *RENAME_MAP_HEADER[1:]], *rows[1:]],
            "the map's columns are ['pages', ",
        ),
        (lambda rows: [], "the map is empty"),
        (
            lambda rows: [*rows[:3], [*rows[3], "extra"], *rows[4:]],
            "map line 4 does not have 12 cells",
        ),
        (
            lambda rows: [*rows[:3], rows[3][:-1], *rows[4:]],
            "map line 4 does not have 12 cells",
        ),
    ],
    ids=[
        "another-new-id",
        "another-old-name",
        "another-display-name",
        "missing-row",
        "extra-row",
        "header-only",
        "swapped-rows",
        "renamed-column",
        "empty",
        "extra-cell",
        "missing-cell",
    ],
)
def test_apply_rejects_a_map_that_differs_from_the_derived_one(
    apply_tree, work, capsys, rename_map_text, mutate, error
):
  apply = _load_apply(apply_tree)
  map_path, out = work / "rename-map.csv", work / "out.textproto"
  map_path.write_text(
      _csv_text(mutate(_csv_rows(rename_map_text))),
      encoding="utf-8",
      newline="",
  )

  code = apply.main(
      ["--dump", str(GENERIC_DUMP), "--map", str(map_path), "--out", str(out)]
  )

  assert code == 1
  stderr = capsys.readouterr().err
  assert f"ERROR: {map_path}: " in stderr
  assert error in stderr
  assert stderr.endswith("not remapped: nothing written\n")
  assert not out.exists()


@pytest.mark.parametrize(
    "content", [None, b"page,\xff\n"], ids=["missing", "not-utf-8"]
)
def test_apply_rejects_a_map_it_cannot_read(apply_tree, work, capsys, content):
  apply = _load_apply(apply_tree)
  map_path, out = work / "rename-map.csv", work / "out.textproto"
  if content is not None:
    map_path.write_bytes(content)

  code = apply.main(
      ["--dump", str(GENERIC_DUMP), "--map", str(map_path), "--out", str(out)]
  )

  assert code == 1
  assert f"ERROR: cannot read the map {map_path}: " in capsys.readouterr().err
  assert not out.exists()


def _replaced(old, new):
  """Return a function that replaces the first `old` of a dump with `new`."""

  def replace(text):
    assert old in text
    return text.replace(old, new, 1)

  return replace


def _with_component_text(component):
  """Return a function that adds `component` before a dump's first one."""

  def insert(text):
    anchor = "      component_config {\n"
    assert anchor in text
    return text.replace(anchor, component + anchor, 1)

  return insert


def _without_first_component(text):
  closer = "\n      }\n"
  start = text.index("      component_config {\n")
  return text[:start] + text[text.index(closer, start) + len(closer) :]


_REPORT_DATASOURCE_ID = f'    datasource_id: "{STALE_DATASOURCE_ID}"\n'
_NOT_ONE_QUOTED_STRING = (
    "name 'met' is not one quoted string, so its literal cannot be replaced in"
    " place"
)


@pytest.mark.parametrize(
    "mutate, error",
    [
        (
            _replaced('concept_names: "met"\n', 'concept_names: "ghost"\n'),
            "concept_names 'ghost' at"
            " component_property.metrics.labeled_concepts.value.concept_names"
            " names none of its concepts",
        ),
        (
            _with_component_text(_EXTRA_COMPONENT.replace("NAME", "met")),
            _EXTRA_IN_DUMP,
        ),
        (
            _with_component_text(_REFERENCE_ONLY_COMPONENT),
            _REFERENCE_ONLY_IN_DUMP,
        ),
        (
            _with_component_text(_EXTRA_COMPONENT.replace("NAME", "met") * 2),
            "component cd-extra appears twice",
        ),
        (_without_first_component, "of the manifest is not in the dump"),
        (
            _replaced('id: "t0.met"', 'id: "t1.met"'),
            "has the id 't1.met', not 't0.met'",
        ),
        (
            _replaced('name: "met"', 'name: "m" "et"'),
            _NOT_ONE_QUOTED_STRING,
        ),
        (_replaced('name: "met"', "name: met"), _NOT_ONE_QUOTED_STRING),
        (
            _replaced(
                "            dimensions {\n            }\n",
                "            dimensions {\n            }\n"
                '            filters {\n              concept_names: "met"\n'
                "            }\n",
            ),
            "concept_names 'met' at component_property.filters.concept_names,"
            " which the live audit does not read",
        ),
        (
            _replaced(
                "        concept_defs {\n",
                "        component_config {\n        }\n        concept_defs {\n",
            ),
            "a component_config is nested in a component_config",
        ),
        (
            _replaced(
                "        concept_defs {\n",
                "        property_config {\n        }\n        concept_defs {\n",
            ),
            "has more than one property_config or component_property",
        ),
        (
            _replaced(_REPORT_DATASOURCE_ID, ""),
            "report_config.report has 0 datasource_id fields, not one",
        ),
        (
            _replaced(_REPORT_DATASOURCE_ID, _REPORT_DATASOURCE_ID * 2),
            "report_config.report has 2 datasource_id fields, not one",
        ),
        (
            _replaced(_REPORT_DATASOURCE_ID, "    datasource_id: unquoted\n"),
            "the report datasource_id 'unquoted' is not one quoted string",
        ),
        (
            lambda text: text + "}\n",
            "cannot read the dump: expected a field name, got '}'",
        ),
        (
            lambda text: text.rstrip("\n").removesuffix("}"),
            "cannot read the dump: unterminated message: missing '}'",
        ),
        (
            lambda text: '# comment\rcapture_note: "hidden"\n' + text,
            "the dump reads differently here than in"
            " tools/validate_contracts.py",
        ),
    ],
    ids=[
        "dangling-reference",
        "extra-component",
        "reference-only-component",
        "repeated-component",
        "missing-component",
        "id-is-not-t0-name",
        "name-in-two-literals",
        "name-is-not-quoted",
        "reference-at-a-path-the-audit-does-not-read",
        "nested-component-config",
        "two-property-configs",
        "no-report-datasource-id",
        "two-report-datasource-ids",
        "unquoted-report-datasource-id",
        "closing-brace-too-many",
        "closing-brace-missing",
        "carriage-return-comment-divergence",
    ],
)
def test_apply_refuses_a_dump_it_cannot_remap_one_to_one(
    apply_tree, work, capsys, mutate, error
):
  apply = _load_apply(apply_tree)
  dump, out = work / "dump.textproto", work / "out.textproto"
  dump.write_bytes(
      mutate(GENERIC_DUMP.read_text(encoding="utf-8")).encode("utf-8")
  )

  code = apply.main(["--dump", str(dump), "--out", str(out)])

  assert code == 1
  stderr = capsys.readouterr().err
  assert "ERROR: " in stderr
  assert error in stderr
  assert stderr.endswith("not remapped: nothing written\n")
  assert [path.name for path in work.iterdir()] == ["dump.textproto"]


@pytest.mark.parametrize("target", ["none", "new"])
def test_apply_check_rejects_what_a_write_rejects(
    apply_tree, work, capsys, target
):
  apply = _load_apply(apply_tree)
  dump = work / "dump.textproto"
  dump.write_text(
      _replaced('concept_names: "met"\n', 'concept_names: "ghost"\n')(
          GENERIC_DUMP.read_text(encoding="utf-8")
      ),
      encoding="utf-8",
  )
  out = work / "out.textproto"
  args = ["--dump", str(dump), "--check"]
  if target == "new":
    args += ["--out", str(out)]

  code = apply.main(args)

  assert code == 1
  stderr = capsys.readouterr().err
  assert "concept_names 'ghost' at" in stderr
  assert stderr.endswith("not remapped: nothing written\n")
  assert not out.exists()


@pytest.mark.parametrize(
    "content", [None, b"report_config {\n\xff}\n"], ids=["missing", "not-utf-8"]
)
def test_apply_rejects_a_dump_it_cannot_read(apply_tree, work, capsys, content):
  apply = _load_apply(apply_tree)
  dump, out = work / "dump.textproto", work / "out.textproto"
  if content is not None:
    dump.write_bytes(content)

  code = apply.main(["--dump", str(dump), "--out", str(out)])

  assert code == 1
  stderr = capsys.readouterr().err
  if content is None:
    assert f"not remapped: cannot read {dump}: " in stderr
  else:
    assert f"ERROR: {dump} is not UTF-8 text: " in stderr
  assert not out.exists()


# What the tool must keep as it is: a byte order mark, a comment and a field
# of no meaning to the audit, with text outside the Basic Multilingual Plane,
# a line of white space, and CRLF line ends.
_NOISE = (
    "\ufeff# caf\u00e9 \U0001f600 stays as it is\n"
    'capture_note: "caf\\u00e9 \U0001f600"; # and so does this\n'
    "\t\n"
    "\n"
)


def _noisy(text):
  return (_NOISE + text).replace("\n", "\r\n")


def test_apply_keeps_every_byte_that_it_does_not_edit(apply_tree, work):
  apply = _load_apply(apply_tree)
  generic = GENERIC_DUMP.read_text(encoding="utf-8")
  # The first name is single-quoted, and the quotes stay. Another is written
  # with an escape, and a plain literal replaces it.
  quoted = generic.replace('name: "met"', "name: 'met'", 1)
  escaped = quoted.replace('name: "dim"', 'name: "d\\x69m"', 1)
  assert "name: 'met'" in escaped and escaped.count("\\x69") == 1
  noisy = _noisy(escaped).encode("utf-8")
  dump, out = work / "dump.textproto", work / "out.textproto"
  dump.write_bytes(noisy)

  code = apply.main(["--dump", str(dump), "--out", str(out)])

  assert code == 0
  remapped = (
      _remapped_sanitized_dump()
      .decode("utf-8")
      .removeprefix(f"{apply.OFFLINE_PREVIEW_MARKER}\n")
  )
  first = re.search(r'name: "(qt_[a-z0-9]+)"', remapped)
  assert first is not None
  noisy_remapped = _noisy(
      remapped.replace(first.group(), f"name: '{first.group(1)}'", 1)
  )
  expected = (
      "\ufeff"
      + f"{apply.OFFLINE_PREVIEW_MARKER}\r\n"
      + noisy_remapped.removeprefix("\ufeff")
  ).encode("utf-8")
  written = out.read_bytes()
  assert written == expected
  assert written.startswith(
      f"\ufeff{apply.OFFLINE_PREVIEW_MARKER}\r\n# caf\u00e9 \U0001f600".encode(
          "utf-8"
      )
  )
  assert written.count(b"\n") == written.count(b"\r\n")
  assert dump.read_bytes() == noisy


def test_apply_refuses_a_remapped_dump_that_the_audit_refuses(
    apply_tree, work, capsys
):
  """The tool does not edit the data source of the alias, so the audit does."""
  apply = _load_apply(apply_tree)
  alias = f'        datasource_id: "{DATASOURCE_ID}"\n        alias: "ds0"\n'
  dump, out = work / "dump.textproto", work / "out.textproto"
  dump.write_text(
      _replaced(alias, alias.replace(DATASOURCE_ID, STALE_DATASOURCE_ID))(
          GENERIC_DUMP.read_text(encoding="utf-8")
      ),
      encoding="utf-8",
  )

  code = apply.main(["--dump", str(dump), "--out", str(out)])

  assert code == 1
  stderr = capsys.readouterr().err
  assert (
      "ERROR: tools/validate_contracts.py exited 1 on the remapped dump:"
      in stderr
  )
  assert f"data source alias 'ds0' points at '{STALE_DATASOURCE_ID}'" in stderr
  assert "apply_bqca_rename_map_" not in stderr
  assert stderr.endswith("not remapped: nothing written\n")
  assert [path.name for path in work.iterdir()] == ["dump.textproto"]


def test_apply_refuses_a_remapped_dump_that_is_not_the_twin(
    apply_tree, work, capsys, monkeypatch
):
  """The twin check of gen_bqca_rename_map.py rejects a result on its own."""
  apply = _load_apply(apply_tree)
  apply_edits = apply.apply_edits

  def without_the_first_sort_edit(text, edits):
    skipped = next(edit for edit in edits if edit.kind == "sort")
    return apply_edits(text, [edit for edit in edits if edit is not skipped])

  monkeypatch.setattr(apply, "apply_edits", without_the_first_sort_edit)
  # The other checks stay out of it, so that this one has to reject the result.
  monkeypatch.setattr(apply, "verification_errors", lambda *args: [])
  out = work / "out.textproto"

  code = apply.main(["--dump", str(GENERIC_DUMP), "--out", str(out)])

  assert code == 1
  stderr = capsys.readouterr().err
  assert re.search(
      r"^ERROR: remapped dump: remapped twin: .* references ", stderr, re.M
  )
  assert stderr.endswith("not remapped: nothing written\n")
  assert not out.exists()


@pytest.mark.parametrize("count", [2, 25], ids=["few", "capped"])
def test_apply_prints_the_errors_of_the_audit(
    apply_tree, work, capsys, monkeypatch, count
):
  apply = _load_apply(apply_tree)
  audited = []

  def audit(path):
    audited.append(path)
    return 1, "".join(
        f"ERROR: live report {path}: problem {number}\n\n"
        for number in range(count)
    )

  monkeypatch.setattr(apply, "_audit", audit)
  out = work / "out.textproto"

  code = apply.main(["--dump", str(GENERIC_DUMP), "--out", str(out)])

  assert code == 1
  expected = [
      "ERROR: tools/validate_contracts.py exited 1 on the remapped dump:",
      *[
          f"ERROR:   live report the remapped dump: problem {number}"
          for number in range(count)
      ],
  ]
  if len(expected) > 20:
    expected = [*expected[:20], f"ERROR: ... and {len(expected) - 20} more"]
  assert capsys.readouterr().err.splitlines() == [
      *expected,
      "not remapped: nothing written",
  ]
  assert not out.exists()
  assert len(audited) == 1 and not audited[0].exists()


def test_apply_checks_and_writes_the_bytes_that_it_read(
    apply_tree, work, monkeypatch
):
  """A file that replaces the dump while the tool runs is never read."""
  apply = _load_apply(apply_tree)
  gen = apply._load_tool("gen_bqca_rename_map")
  dump, out = work / "dump.textproto", work / "out.textproto"
  shutil.copyfile(GENERIC_DUMP, dump)
  read_dump, audit = gen.read_dump, apply._audit
  readers, audited = [], []

  def replacing_read_dump(validator, path):
    readers.append((path, _mode(path), path.read_bytes()))
    dump.write_text("# another file\n", encoding="utf-8")
    return read_dump(validator, path)

  def replacing_audit(path):
    audited.append((path, _mode(path), path.read_bytes()))
    shutil.copyfile(REMAPPED_DUMP, dump)
    return audit(path)

  monkeypatch.setattr(gen, "read_dump", replacing_read_dump)
  monkeypatch.setattr(apply, "_audit", replacing_audit)

  code = apply.main(["--dump", str(dump), "--out", str(out)])

  assert code == 0
  written = out.read_bytes()
  assert written == _remapped_sanitized_dump()
  # The map is read from a read-only copy of the dump, the twin from one of the
  # remapped dump, and the audit reads the same remapped bytes that are
  # written.
  assert [(mode, data) for _, mode, data in readers] == [
      (0o400, GENERIC_DUMP.read_bytes()),
      (0o400, written),
  ]
  assert [(mode, data) for _, mode, data in audited] == [(0o400, written)]
  private = [path for path, _, _ in [*readers, *audited]]
  assert dump.resolve() not in private and out.resolve() not in private
  assert not any(path.exists() for path in private)
  assert dump.read_bytes() == REMAPPED_DUMP.read_bytes()


def test_apply_sets_the_report_data_source_that_the_binding_names(
    apply_tree, work, capsys, monkeypatch
):
  apply = _load_apply(apply_tree)
  # The audit compares the dump with the binding's data source, and this
  # binding names another one.
  monkeypatch.setattr(apply, "_audit", lambda path: (0, ""))
  binding = apply_tree / BINDING
  text, count = re.subn(
      r"(?m)^datasource_id: .*$",
      "datasource_id: another-source.id_2",
      binding.read_text(encoding="utf-8"),
  )
  assert count == 1
  binding.write_text(text, encoding="utf-8")
  out = work / "out.textproto"

  code = apply.main(["--dump", str(GENERIC_DUMP), "--out", str(out)])

  assert code == 0
  report = f'\n    datasource_id: "{DATASOURCE_ID}"\n'
  remapped = _remapped_sanitized_dump().decode("utf-8")
  assert remapped.count(report) == 1
  assert out.read_text(encoding="utf-8") == remapped.replace(
      report, '\n    datasource_id: "another-source.id_2"\n'
  )
  assert (
      f"report datasource_id: '{STALE_DATASOURCE_ID}' ->"
      " 'another-source.id_2'" in capsys.readouterr().out
  )


@pytest.mark.parametrize(
    "line, shown",
    [
        ('datasource_id: "has space"', "'has space'"),
        ("datasource_id: 12345", "12345"),
        ("", "None"),
    ],
    ids=["not-plain", "not-a-string", "missing"],
)
def test_apply_refuses_a_binding_without_a_plain_data_source_id(
    apply_tree, work, capsys, line, shown
):
  apply = _load_apply(apply_tree)
  binding = apply_tree / BINDING
  text, count = re.subn(
      r"(?m)^datasource_id: .*\n",
      f"{line}\n" if line else "",
      binding.read_text(encoding="utf-8"),
  )
  assert count == 1
  binding.write_text(text, encoding="utf-8")
  out = work / "out.textproto"

  code = apply.main(["--dump", str(GENERIC_DUMP), "--out", str(out)])

  assert code == 1
  assert (
      f"ERROR: {BINDING} datasource_id {shown} is not a plain ID"
      in capsys.readouterr().err
  )
  assert not out.exists()


@pytest.fixture(scope="module")
def apply_module():
  """The tool, loaded from this repository, for tests that write nothing."""
  return _load_apply(DASHBOARD)


@pytest.fixture(scope="module")
def sanitized_plan(apply_module):
  """The map, the edits and the remapped text of the sanitized dump."""
  validator = apply_module._load_tool("validate_contracts")
  gen = apply_module._load_tool("gen_bqca_rename_map")
  manifest = yaml.safe_load(
      (DASHBOARD / "spec/bqca_chart_manifest.yaml").read_text(encoding="utf-8")
  )
  components, _, errors = gen.read_dump(validator, GENERIC_DUMP)
  rows, row_errors = gen.build_rows(validator, manifest, components)
  assert errors == [] and row_errors == []
  renames = {
      (row["component_id"], row["old_name"]): row["new_qt_id"] for row in rows
  }
  text = GENERIC_DUMP.read_text(encoding="utf-8")
  edits = apply_module.plan_edits(
      apply_module.read_textproto(text), renames, DATASOURCE_ID
  )
  return types.SimpleNamespace(
      renames=renames,
      text=text,
      edits=edits,
      remapped=apply_module.apply_edits(text, edits),
      apply=apply_module.apply_edits,
  )


# Text format that exercises the reader: comments, both separators, both
# quotes, adjacent strings, every kind of escape, lists, and `<>` messages.
_TRICKY = r"""
# Comments are skipped, and so are the separators.
count: 1; ratio: -1.5e3, flag: true
words: [one, 'two', "thr" 'ee']
joined: "a" 'b'   "c"
escapes: "tab\there \\ \x41\101\u00e9\U0001F600 \"q\" \'s\'"
single: 'it\'s'
angled < inner: 1 >
nested {
  list: [ { n: 1 }, < n: 2 > ]
  empty {}
}
"""


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            lambda: GENERIC_DUMP.read_text(encoding="utf-8"), id="sanitized"
        ),
        pytest.param(
            lambda: REMAPPED_DUMP.read_text(encoding="utf-8"), id="remapped"
        ),
        pytest.param(lambda: _TRICKY, id="every-kind-of-syntax"),
        pytest.param(lambda: "\ufeff" + _TRICKY, id="byte-order-mark"),
        pytest.param(lambda: "", id="empty"),
        pytest.param(lambda: "a {" * 64 + "}" * 64, id="64-levels-deep"),
    ],
)
def test_apply_reads_textproto_as_the_validator_does(apply_module, text):
  text = text()
  validator = apply_module._load_tool("validate_contracts")

  tree = apply_module.read_textproto(text.removeprefix("\ufeff"))

  assert apply_module._plain(tree) == validator.parse_report_textproto(text)


@pytest.mark.parametrize(
    "text",
    [
        "a:",
        "a 1",
        "a [ { } ]",
        "a: [1 2]",
        "a { b: 1",
        "a < b: 1",
        "}",
        "a: }",
        "[a]: 1",
        "[type.googleapis.com/x] { }",
        "1a: 2",
        'a: "x',
        r'a: "\q"',
        r'a: "\xZ"',
        r'a: "\u12"',
        r'a: "\400"',
        "a {" * 65 + "}" * 65,
    ],
    ids=[
        "no-value",
        "no-colon",
        "message-list-without-colon",
        "list-without-comma",
        "unterminated-message",
        "unterminated-angle-message",
        "stray-closer",
        "closer-as-value",
        "list-as-field-name",
        "extension-name",
        "digit-field-name",
        "unterminated-string",
        "unsupported-escape",
        "hex-escape-without-digits",
        "short-unicode-escape",
        "octal-escape-too-big",
        "65-levels-deep",
    ],
)
def test_apply_rejects_textproto_as_the_validator_does(apply_module, text):
  validator = apply_module._load_tool("validate_contracts")
  with pytest.raises(ValueError) as expected:
    validator.parse_report_textproto(text)

  with pytest.raises(ValueError) as actual:
    apply_module.read_textproto(text)

  assert str(actual.value) == str(expected.value)


_MICRO_COMPONENT = """\
    component_config {
      datasource_id: "source"
      component_id: "@C@"
      property_config {
        component_property {
          dimensions {
            labeled_concepts {
              key: "primary"
              value { concept_names: "@D@" }
            }
          }
          metrics {
            labeled_concepts {
              key: "primary"
              value { concept_names: "t0.@M@" }
            }
          }
          sort { sort_column: "t0.@M@" }
        }
      }
      concept_defs { id: "t0.@D@" name: "@D@" }
      concept_defs { id: "t0.@M@" name: "@M@" }
    }
"""
_MICRO_REPORT = '  report { datasource_id: "old-source" }\n'
_MICRO_RENAMES = {("c1", "d0"): "qt_dim00", ("c1", "m0"): "qt_met00"}


def _micro_component(component="c1", dimension="d0", metric="m0"):
  return (
      _MICRO_COMPONENT.replace("@C@", component)
      .replace("@D@", dimension)
      .replace("@M@", metric)
  )


def _micro_dump(*components, report=_MICRO_REPORT):
  return (
      "report_config {\n  page {\n"
      + "".join(components or [_micro_component()])
      + "  }\n"
      + report
      + "}\n"
  )


def _micro_plan(apply_module, text, renames=None, datasource_id="new-source"):
  return apply_module.plan_edits(
      apply_module.read_textproto(text),
      _MICRO_RENAMES if renames is None else renames,
      datasource_id,
  )


def test_apply_plans_each_literal_in_the_order_of_the_dump(apply_module):
  text = _micro_dump()

  edits = _micro_plan(apply_module, text)

  assert [(edit.kind, edit.old, edit.new) for edit in edits] == [
      ("dimension", "d0", "qt_dim00"),
      ("metric", "t0.m0", "t0.qt_met00"),
      ("sort", "t0.m0", "t0.qt_met00"),
      ("id", "t0.d0", "t0.qt_dim00"),
      ("name", "d0", "qt_dim00"),
      ("id", "t0.m0", "t0.qt_met00"),
      ("name", "m0", "qt_met00"),
      ("datasource_id", "old-source", "new-source"),
  ]
  assert apply_module.apply_edits(text, edits) == _micro_dump(
      _micro_component("c1", "qt_dim00", "qt_met00"),
      report='  report { datasource_id: "new-source" }\n',
  )


def test_apply_keys_a_rename_by_component_and_name(apply_module):
  """Two components that name a concept alike get their own new IDs."""
  text = _micro_dump(_micro_component("c1"), _micro_component("c2"))
  renames = {
      ("c1", "d0"): "qt_aaa",
      ("c1", "m0"): "qt_bbb",
      ("c2", "d0"): "qt_ccc",
      ("c2", "m0"): "qt_ddd",
  }

  edits = _micro_plan(apply_module, text, renames)

  assert apply_module.apply_edits(text, edits) == _micro_dump(
      _micro_component("c1", "qt_aaa", "qt_bbb"),
      _micro_component("c2", "qt_ccc", "qt_ddd"),
      report='  report { datasource_id: "new-source" }\n',
  )


def test_apply_plans_nothing_for_values_that_have_their_new_value(
    apply_module,
):
  text = _micro_dump(
      _micro_component("c1", "qt_dim00", "qt_met00"),
      report='  report { datasource_id: "new-source" }\n',
  )
  renames = {("c1", "qt_dim00"): "qt_dim00", ("c1", "qt_met00"): "qt_met00"}

  assert _micro_plan(apply_module, text, renames) == []


def test_apply_plans_a_dump_that_has_no_report_config(apply_module):
  text = _micro_component() + _MICRO_REPORT
  edits = _micro_plan(apply_module, text)

  remapped = apply_module.apply_edits(text, edits)

  assert remapped == _micro_component(
      "c1", "qt_dim00", "qt_met00"
  ) + _MICRO_REPORT.replace("old-source", "new-source")
  assert (
      apply_module.verification_errors(
          text, remapped, edits, _MICRO_RENAMES, "new-source"
      )
      == []
  )


@pytest.mark.parametrize(
    "mutate, error",
    [
        (
            lambda text: 'report_config {\n  report { datasource_id: "x" }\n}\n',
            "the dump has no component_config",
        ),
        (
            _replaced(
                '      concept_defs { id: "t0.d0"',
                '      component_config {}\n      concept_defs { id: "t0.d0"',
            ),
            "a component_config is nested in a component_config",
        ),
        (
            _replaced('      component_id: "c1"\n', ""),
            "a component_config has 0 component_id fields, not one quoted",
        ),
        (
            _replaced(
                'component_id: "c1"\n',
                'component_id: "c1"\n component_id: "c1"\n',
            ),
            "a component_config has 2 component_id fields, not one quoted",
        ),
        (
            _replaced('component_id: "c1"', "component_id: c1"),
            "a component_config has 1 component_id fields, not one quoted",
        ),
        (
            lambda text: _micro_dump(_micro_component(), _micro_component()),
            "component c1 appears twice",
        ),
        (
            _replaced(
                'concept_defs { id: "t0.d0" name: "d0" }',
                'concept_defs { name: "d0" }',
            ),
            "component c1 has a concept_defs without exactly one name and one"
            " id",
        ),
        (
            _replaced('name: "d0" }', 'name: "d0" name: "d0" }'),
            "component c1 has a concept_defs without exactly one name and one"
            " id",
        ),
        (
            _replaced('id: "t0.d0" name', 'id: "t1.d0" name'),
            "component c1 concept 'd0' has the id 't1.d0', not 't0.d0'",
        ),
        (
            _replaced(
                '      concept_defs { id: "t0.m0"',
                '      concept_defs { id: "t0.d0" name: "d0" }\n'
                '      concept_defs { id: "t0.m0"',
            ),
            "component c1 repeats the concept 'd0'",
        ),
        (
            _replaced('concept_names: "d0"', 'concept_names: "ghost"'),
            "component c1 concept_names 'ghost' names none of its concepts",
        ),
        (
            _replaced('sort_column: "t0.m0"', 'sort_column: "t0.ghost"'),
            "component c1 sort_column 't0.ghost' names none of its concepts",
        ),
        (
            _replaced(
                '          sort { sort_column: "t0.m0" }\n',
                '          sort { sort_column: "t0.m0" }\n'
                '          filters { concept_names: "d0" }\n',
            ),
            "component c1 has concept_names 'd0' at"
            " component_property.filters.concept_names, which the live audit"
            " does not read",
        ),
        (
            _replaced(
                '      concept_defs { id: "t0.d0"',
                '      property_config {}\n      concept_defs { id: "t0.d0"',
            ),
            "component c1 has more than one property_config or"
            " component_property",
        ),
        (
            _replaced(
                '        }\n      }\n      concept_defs { id: "t0.d0"',
                "        }\n        component_property {}\n      }\n"
                '      concept_defs { id: "t0.d0"',
            ),
            "component c1 has more than one property_config or"
            " component_property",
        ),
        (
            _replaced(
                '  report { datasource_id: "old-source" }\n', "  report {}\n"
            ),
            "report_config.report has 0 datasource_id fields, not one",
        ),
        (
            _replaced(
                'datasource_id: "old-source" }',
                'datasource_id: "old-source" datasource_id: "old-source" }',
            ),
            "report_config.report has 2 datasource_id fields, not one",
        ),
        (
            _replaced(
                'datasource_id: "old-source"', "datasource_id: old-source"
            ),
            "the report datasource_id 'old-source' is not one quoted string,"
            " so its literal cannot be replaced in place",
        ),
        (
            _replaced(
                'datasource_id: "old-source"', 'datasource_id: "old-" "source"'
            ),
            "the report datasource_id 'old-source' is not one quoted string,"
            " so its literal cannot be replaced in place",
        ),
        (
            _replaced('name: "d0" }', 'name: "d" "0" }'),
            "component c1 name 'd0' is not one quoted string, so its literal"
            " cannot be replaced in place",
        ),
    ],
    ids=[
        "no-component",
        "nested-component",
        "no-component-id",
        "two-component-ids",
        "unquoted-component-id",
        "repeated-component",
        "concept-without-id",
        "concept-with-two-names",
        "id-is-not-t0-name",
        "repeated-concept",
        "dangling-reference",
        "dangling-sort-column",
        "reference-at-a-path-the-audit-does-not-read",
        "two-property-configs",
        "two-component-properties",
        "no-report-datasource-id",
        "two-report-datasource-ids",
        "unquoted-report-datasource-id",
        "split-report-datasource-id",
        "split-concept-name",
    ],
)
def test_apply_plan_rejects_a_dump_it_cannot_rename_one_to_one(
    apply_module, mutate, error
):
  text = mutate(_micro_dump())

  with pytest.raises(ValueError) as exc_info:
    _micro_plan(apply_module, text)

  assert str(exc_info.value) == error


@pytest.mark.parametrize(
    "renames, datasource_id, error",
    [
        (
            {("c1", "d0"): "qt_dim00"},
            "new-source",
            "component c1 concept 'm0' is not in the rename map",
        ),
        (
            {**_MICRO_RENAMES, ("c9", "x"): "qt_zzz00"},
            "new-source",
            "the rename map has concepts the dump lacks: [('c9', 'x')]",
        ),
        (
            {**_MICRO_RENAMES, ("c1", "d0"): 'bad"id'},
            "new-source",
            "component c1 would need the name 'bad\"id' escaped",
        ),
        (
            {**_MICRO_RENAMES, ("c1", "d0"): "bad\\id"},
            "new-source",
            "component c1 would need the name 'bad\\\\id' escaped",
        ),
        (
            {**_MICRO_RENAMES, ("c1", "d0"): "bad id"},
            "new-source",
            "component c1 would need the name 'bad id' escaped",
        ),
        (
            _MICRO_RENAMES,
            "bad id",
            "the report would need the datasource_id 'bad id' escaped",
        ),
    ],
    ids=[
        "concept-not-in-map",
        "concept-not-in-dump",
        "new-id-with-quote",
        "new-id-with-backslash",
        "new-id-with-space",
        "data-source-id-with-space",
    ],
)
def test_apply_plan_rejects_a_map_that_does_not_fit_the_dump(
    apply_module, renames, datasource_id, error
):
  with pytest.raises(ValueError) as exc_info:
    _micro_plan(apply_module, _micro_dump(), renames, datasource_id)

  assert str(exc_info.value) == error


def test_apply_plans_the_edits_that_make_the_remapped_twin(
    apply_module, sanitized_plan
):
  validator = apply_module._load_tool("validate_contracts")
  plan, twin = sanitized_plan, REMAPPED_DUMP.read_text(encoding="utf-8")

  assert collections.Counter(edit.kind for edit in plan.edits) == {
      "name": 65,
      "id": 65,
      "dimension": 23,
      "metric": 42,
      "sort": 13,
      "datasource_id": 1,
  }
  assert [edit.node.start for edit in plan.edits] == sorted(
      edit.node.start for edit in plan.edits
  )
  assert (
      _header_and_body(plan.remapped.encode("utf-8"))[1]
      == _header_and_body(twin.encode("utf-8"))[1]
  )
  # The traversal that checks the edits shares no code with them.
  assert apply_module.expected_tree(
      validator.parse_report_textproto(plan.text),
      plan.renames,
      DATASOURCE_ID,
  ) == validator.parse_report_textproto(twin)
  assert (
      apply_module.verification_errors(
          plan.text, plan.remapped, plan.edits, plan.renames, DATASOURCE_ID
      )
      == []
  )


@pytest.mark.parametrize(
    "doctor, error",
    [
        (
            _replaced(
                'source_field_name: "_turn_latency_ms_"',
                'source_field_name: "_turn_latency_ms"',
            ),
            "210 tokens of the remapped dump differ from the dump's, but 209"
            " edits were planned",
        ),
        (
            _replaced("report_config {", "report_config  {"),
            "210 tokens of the remapped dump differ from the dump's, but 209"
            " edits were planned",
        ),
        (
            _replaced("# A sanitized cut", "# A sanitised cut"),
            "210 tokens of the remapped dump differ from the dump's, but 209"
            " edits were planned",
        ),
        (
            _replaced(
                f'\n    datasource_id: "{DATASOURCE_ID}"\n',
                f"\n    datasource_id: '{DATASOURCE_ID}'\n",
            ),
            "was not rewritten in place from"
            f" '{STALE_DATASOURCE_ID}' to '{DATASOURCE_ID}'",
        ),
        (lambda text: text + "# trailing\n", " tokens, the dump "),
        (
            lambda text: text + '"',
            "the remapped dump cannot be read: unexpected character '\"'",
        ),
    ],
    ids=[
        "another-scalar-changed",
        "white-space-changed",
        "comment-changed",
        "quote-changed",
        "trailing-text",
        "unterminated-string",
    ],
)
def test_apply_verification_rejects_a_result_that_is_not_the_planned_edits(
    apply_module, sanitized_plan, doctor, error
):
  plan = sanitized_plan

  errors = apply_module.verification_errors(
      plan.text,
      doctor(plan.remapped),
      plan.edits,
      plan.renames,
      DATASOURCE_ID,
  )

  assert any(error in message for message in errors), errors


@pytest.mark.parametrize(
    "doctor, error",
    [
        (
            lambda plan: plan.apply(plan.text, plan.edits[1:]),
            "208 tokens of the remapped dump differ from the dump's, but 209"
            " edits were planned",
        ),
        (
            lambda plan: plan.apply(
                plan.text,
                [
                    dataclasses.replace(plan.edits[0], new="qt_other0"),
                    *plan.edits[1:],
                ],
            ),
            "the remapped dump does not parse to the dump with the map applied",
        ),
    ],
    ids=["edit-missing", "another-new-value"],
)
def test_apply_verification_rejects_a_result_made_from_other_edits(
    apply_module, sanitized_plan, doctor, error
):
  plan = sanitized_plan

  errors = apply_module.verification_errors(
      plan.text, doctor(plan), plan.edits, plan.renames, DATASOURCE_ID
  )

  assert any(error in message for message in errors), errors


def test_apply_verification_rejects_a_map_or_data_source_it_was_not_given(
    apply_module, sanitized_plan
):
  plan = sanitized_plan

  other_source = apply_module.verification_errors(
      plan.text, plan.remapped, plan.edits, plan.renames, "another-source"
  )
  fewer = dict(plan.renames)
  del fewer[next(iter(fewer))]
  other_map = apply_module.verification_errors(
      plan.text, plan.remapped, plan.edits, fewer, DATASOURCE_ID
  )

  assert (
      "the remapped dump does not parse to the dump with the map applied"
      in other_source
  )
  assert any(
      error.startswith("the dump with the map applied cannot be built: ")
      for error in other_map
  )


def test_apply_verification_says_which_dump_it_could_not_use(
    apply_module, sanitized_plan
):
  plan = sanitized_plan
  fewer = dict(plan.renames)
  del fewer[next(iter(fewer))]

  unbuildable = apply_module.verification_errors(
      plan.text, plan.remapped, plan.edits, fewer, DATASOURCE_ID
  )
  unparsable = apply_module.verification_errors(
      plan.text, plan.remapped + "}", plan.edits, plan.renames, DATASOURCE_ID
  )

  assert any(
      error.startswith("the dump with the map applied cannot be built: ")
      for error in unbuildable
  )
  assert not any("could not be parsed" in error for error in unbuildable)
  assert any(
      error.startswith(
          "remapped dump could not be parsed for tree comparison: "
      )
      for error in unparsable
  )
  assert not any("cannot be built" in error for error in unparsable)
