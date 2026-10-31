#!/usr/bin/env python3
"""Finalize the BQCA live template sync from a capture of the live template.

Runs ``tools/validate_contracts.py --profile bqca --live-report-textproto
DUMP``. Only if it exits 0 does this record the verification in
``bindings/bqca_report_template.yaml``. In ``live_template_verification`` it:

* sets ``verified_date`` to ``--verified-date`` (default: today, in UTC);
* copies the pinned ``manifest_sha256`` and ``template_bundle_sha256`` into
  ``verified_manifest_sha256`` and ``verified_template_bundle_sha256``;
* records the capture: ``verified_revision`` (``--revision``, the revision of
  the live report that the dump captured), ``verified_dump_sha256`` (the
  SHA-256 of the dump's bytes), ``capture_receipt_sha256`` (the SHA-256 of
  the dump's capture receipt) and ``captured_by`` (the receipt's signer);
* removes ``pending_live_sync``.

It also commits the dump's capture receipt and signature as
``bindings/bqca_live_capture_receipt.json`` and
``bindings/bqca_live_capture_receipt.json.sig``, which
``tools/validate_contracts.py`` requires, and checks, of any binding without
``pending_live_sync``.

Every other line of the binding, comments included, is kept. The edit must
parse to exactly that change. The new files replace the old ones atomically,
and the audit runs again after the write; if it fails, raises or is
interrupted, the original bytes of all three are put back. ``--check`` runs
the same checks and audit, prints the change as a diff and every reason a
write would be refused, and writes nothing.

A capture is what ``tools/sign_live_capture.py`` writes when it exports the
live template: the export (``--dump``, or ``--before``), its capture receipt
(``<export>.receipt.json``) and the receipt's SSH signature
(``<export>.receipt.json.sig``). A write needs two: the capture after the
write-back (``--dump``) and the capture before it (``--before``), whose
receipt the dump's names. The exports, receipts and signatures are each read
once; the checks, both audits (through a private, read-only copy of the dump)
and the recorded digests use those bytes, never the paths again.

A write fails closed unless:

* ``ssh-keygen -Y verify`` verifies both receipts' signatures in the
  ``bqca-live-capture-receipt`` namespace, made by keys that
  ``bindings/bqca_live_capture_signers`` lists for their signers;
* both receipts are well-formed ``bqca-live-capture-receipt/v2`` receipts of
  the exports' exact bytes, for the binding's ``report_id``, its pinned
  manifest and bundle digests, and its pending sync, signed on or after the
  day that sync began (``pending_live_sync.since_date``) and not in the
  future; the dump's receipt is for ``--revision``;
* the dump's receipt names the ``--before`` capture (its SHA-256 and
  revision), the dump is of another revision, and the ``--before`` receipt is
  of a capture taken before any write-back;
* the dump's parsed content differs from that of the offline preview that
  ``tools/apply_bqca_rename_map.py`` writes for the ``--before`` export:
  comments, spacing and the order of fields and repeated values aside, the
  live report gained, lost or changed a field value after the write-back;
* the dump decodes as UTF-8 and parses as protobuf text format (with
  universal-newline normalization matching ``tools/validate_contracts.py``),
  and its ``report_config.shareable.id`` is exactly that ``report_id``, and
  its ``report_config.report.datasource_id`` and ``ds0`` data source alias
  are exactly the binding's ``datasource_id`` (``REPORT_ID_FIELD``,
  ``REPORT_DATASOURCE_FIELD``, ``ALIAS_FIELD``); the ID in any other field,
  inside a longer value, or in a comment does not count;
* the dump carries no offline-preview marker of
  ``tools/apply_bqca_rename_map.py`` in any letter case or spacing, and is
  not under this repository's ``tests/fixtures`` (checked by path and by
  ``os.path.samestat`` so a case-insensitive spelling cannot bypass it).

So the offline preview is refused, with its marker kept, edited or removed,
and so is any edit of its comments, spacing or quoting or reordering of its
fields. What no check can refuse is a listed signer who signs a receipt for
content that the export command did not fetch from the live report: one who
also edits a field value of the preview (writing a scalar another way, such
as an enum by its number, counts), or signs receipt bytes directly with
ssh-keygen. That is a false signed statement, which the receipt and the
binding (``captured_by``) attribute to the signer; review of the committed
receipt and of the signer list is the last check.

This tool does not edit documentation or tests. See "Finalizing the live
template sync" in ``docs/dashboard-implementation.md`` for the rest of the
change.

Exits 0 when the binding is finalized or already is, and with ``--check``
when the audit passes, even if a write would be refused (the output says
why); 1 when the audit fails or the binding cannot be finalized; 2 on bad
arguments.
"""

import argparse
from collections.abc import Iterator
import copy
import dataclasses
import datetime
import difflib
import functools
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import types

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parent.parent
BINDING_PATH = "bindings/bqca_report_template.yaml"
VALIDATOR_PATH = "tools/validate_contracts.py"
FIXTURES_PATH = "tests/fixtures"
LIVE_KEY = "live_template_verification"
PENDING_KEY = "pending_live_sync"
# Each `verified_*` key and the pinned digest it takes.
VERIFIED_DIGEST_KEYS = {
    "verified_manifest_sha256": "manifest_sha256",
    "verified_template_bundle_sha256": "template_bundle_sha256",
}
# The capture record, written after `verified_template_bundle_sha256`.
CAPTURE_KEYS = (
    "verified_revision",
    "verified_dump_sha256",
    "capture_receipt_sha256",
    "captured_by",
)
# Shown in a `--check` preview for the values that only a write's arguments
# give.
CAPTURE_PLACEHOLDERS = {
    "verified_revision": "<the --revision>",
    "capture_receipt_sha256": "<the SHA-256 of the dump's capture receipt>",
    "captured_by": "<the signer of the dump's capture receipt>",
}
# The fields where a dump of the live template identifies it, as parsed field
# paths. A saved report's `report_config.report` holds its report-level data
# source and data source aliases, which the live audit reads too. Its own
# entry, `report_config.shareable`, holds its name; this tool expects the
# report ID as that entry's `id`. No dump in this repository keeps the report
# ID (the sanitized cuts drop it), so if the first real dump names the report
# in another field, a write is refused with that field's path, and this
# constant needs that path.
REPORT_ID_FIELD = ("report_config", "shareable", "id")
REPORT_DATASOURCE_FIELD = ("report_config", "report", "datasource_id")
ALIAS_FIELD = ("report_config", "report", "resource", "datasource_alias")
NO_REVISION = (
    "--revision is not given: a write needs the revision of the live report"
    " that the dump captured, as its receipt records it"
)
NO_BEFORE = (
    "--before is not given: a write needs the capture taken before the"
    " write-back, whose offline preview the dump must differ from"
)
NEXT_STEPS = """\
In the same change:
  - commit bindings/bqca_report_template.yaml with
    bindings/bqca_live_capture_receipt.json and
    bindings/bqca_live_capture_receipt.json.sig, which this tool wrote (they
    hold no secret), and the line that lists the signer in
    bindings/bqca_live_capture_signers if it is new; keep both exports
    privately: verified_dump_sha256 is the SHA-256 of the one after the
    write-back;
  - remove the #559 known-issue notes: #bqca-column-id-note in
    docs/index.html (then run tools/render_web_config.py), the BQCA notes in
    README.md and USER_MANUAL.md, and the section 7.4 troubleshooting row in
    docs/guides/bqca-prompt-response-logging-manual.md;
  - update the comments above live_template_verification in
    bindings/bqca_report_template.yaml and the tests that pin the pending
    sync (listed in docs/dashboard-implementation.md);
  - create a report with the 1-click link and check that its Total, Input,
    Output and Thinking Tokens scorecards each show their own value."""

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# The marker that `tools/apply_bqca_rename_map.py` writes, in any letter case
# and spacing, anywhere in the text. It is a tripwire for an offline preview;
# the comparison with the preview of the `--before` export does not depend on
# it.
_OFFLINE_PREVIEW_RE = re.compile(
    r"bqca[\s_-]*offline[\s_-]*preview", re.IGNORECASE
)
# A key of `live_template_verification`: a line indented by exactly 2 spaces.
_BLOCK_KEY_RE = re.compile(r"  ([A-Za-z_][A-Za-z0-9_]*):(?:\s|$)")


def _iso_date(value: str) -> str:
  """Return `value` if it is a YYYY-MM-DD date (an argparse type)."""
  try:
    if _ISO_DATE_RE.fullmatch(value):
      datetime.date.fromisoformat(value)
      return value
  except ValueError:
    pass
  raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")


def revision_argument(value: str) -> str:
  """Return `value` if it is a revision token (an argparse type)."""
  if not _load_validator().BQCA_CAPTURE_TOKEN_RE.fullmatch(value):
    raise argparse.ArgumentTypeError(
        f"{value!r} is not a revision: 1 to 128 letters, digits, '.', '_',"
        " ':', '+' or '-', starting with a letter or digit"
    )
  return value


def signer_argument(value: str) -> str:
  """Return `value` if it is a signer principal (an argparse type)."""
  if not _load_validator().BQCA_CAPTURE_SIGNER_RE.fullmatch(value):
    raise argparse.ArgumentTypeError(
        f"{value!r} is not a signer: 1 to 128 letters, digits, '.', '_', '@',"
        " '+' or '-', starting with a letter or digit"
    )
  return value


def _end_of_children(lines: list[str], index: int, end: int) -> int:
  """Return the index after the lines nested under the key before `index`.

  Nested lines are indented by more than 2 spaces; blank lines count as
  nested when a nested line follows them.
  """
  while index < end:
    if lines[index].startswith("   "):
      index += 1
      continue
    if not lines[index].strip():
      following = index
      while following < end and not lines[following].strip():
        following += 1
      if following < end and lines[following].startswith("   "):
        index = following
        continue
    break
  return index


def finalized_binding_text(
    text: str, verified_date: str, record: dict[str, str]
) -> str:
  """Return the binding `text` with its pending live sync recorded as verified.

  In `live_template_verification`, replaces the `verified_date` line and the
  two `verified_*_sha256` lines, writes the CAPTURE_KEYS lines of `record`
  (replacing them, or adding them after `verified_template_bundle_sha256`),
  and removes the `pending_live_sync:` line and the lines nested under it.
  Every other line is kept as it is.

  Args:
    text: The text of `bindings/bqca_report_template.yaml`.
    verified_date: The new `verified_date`, YYYY-MM-DD.
    record: The capture record: a string for each of CAPTURE_KEYS.

  Returns:
    The edited text.

  Raises:
    ValueError: The binding is not valid YAML or has no pending live sync, a
      pinned digest is not a SHA-256 hex digest, `record` does not give a
      string for exactly CAPTURE_KEYS, one of those lines is missing,
      repeated, or not a plain block entry, or the edited text does not
      parse to exactly that change.
  """
  try:
    binding = yaml.safe_load(text)
  except yaml.YAMLError as exc:
    raise ValueError(f"the binding is not valid YAML: {exc}") from exc
  live = binding.get(LIVE_KEY) if isinstance(binding, dict) else None
  if not isinstance(live, dict) or PENDING_KEY not in live:
    raise ValueError(f"{LIVE_KEY} has no {PENDING_KEY} to finalize")
  if sorted(record) != sorted(CAPTURE_KEYS) or not all(
      isinstance(value, str) for value in record.values()
  ):
    raise ValueError(
        f"the capture record must give a string for {CAPTURE_KEYS}"
    )
  values = {"verified_date": f'"{verified_date}"'}
  for verified_key, pinned_key in VERIFIED_DIGEST_KEYS.items():
    pinned = live.get(pinned_key)
    if not isinstance(pinned, str) or not _SHA256_RE.fullmatch(pinned):
      raise ValueError(
          f"{LIVE_KEY}.{pinned_key} {pinned!r} is not a SHA-256 hex digest"
      )
    values[verified_key] = pinned
  # A JSON string is a YAML double-quoted scalar, so a revision of digits
  # stays a string. ensure_ascii=False keeps a non-BMP character as itself:
  # its escaped surrogate pair would load back as two lone surrogates.
  quoted = {
      key: json.dumps(record[key], ensure_ascii=False) for key in CAPTURE_KEYS
  }

  lines = text.splitlines(keepends=True)
  starts = [
      i for i, line in enumerate(lines) if line.rstrip() == f"{LIVE_KEY}:"
  ]
  if len(starts) != 1:
    raise ValueError(
        f"expected exactly one '{LIVE_KEY}:' line, found {len(starts)}"
    )
  start = starts[0] + 1
  end = next(
      (
          i
          for i in range(start, len(lines))
          if lines[i].strip() and not lines[i].startswith(" ")
      ),
      len(lines),
  )
  block_keys = [
      match.group(1)
      for match in map(_BLOCK_KEY_RE.match, lines[start:end])
      if match
  ]
  for key in CAPTURE_KEYS:
    if block_keys.count(key) > 1:
      raise ValueError(
          f"expected at most one {key} line in {LIVE_KEY}, found"
          f" {block_keys.count(key)}"
      )
  counts = dict.fromkeys([*values, PENDING_KEY], 0)
  edited = lines[:start]
  index = start
  while index < end:
    line = lines[index]
    match = _BLOCK_KEY_RE.match(line)
    key = match.group(1) if match else None
    index += 1
    if key == PENDING_KEY:
      if line.rstrip() == f"  {PENDING_KEY}:":
        counts[key] += 1
        index = _end_of_children(lines, index, end)
        continue
    elif key in values:
      counts[key] += 1
      edited.append(f"  {key}: {values[key]}\n")
      if key == "verified_template_bundle_sha256":
        edited.extend(
            f"  {name}: {value}\n"
            for name, value in quoted.items()
            if name not in block_keys
        )
      continue
    elif key in quoted:
      edited.append(f"  {key}: {quoted[key]}\n")
      continue
    edited.append(line)
  edited.extend(lines[end:])
  for key, count in counts.items():
    if count != 1:
      described = f"'  {key}:'" if key == PENDING_KEY else key
      raise ValueError(
          f"expected exactly one {described} line in {LIVE_KEY}, found"
          f" {count}"
      )

  new_text = "".join(edited)
  expected = copy.deepcopy(binding)
  expected_live = expected[LIVE_KEY]
  expected_live["verified_date"] = verified_date
  for verified_key, pinned_key in VERIFIED_DIGEST_KEYS.items():
    expected_live[verified_key] = expected_live[pinned_key]
  expected_live.update(record)
  del expected_live[PENDING_KEY]
  try:
    parsed = yaml.safe_load(new_text)
  except yaml.YAMLError as exc:
    raise ValueError(f"the edited {LIVE_KEY} is not valid YAML: {exc}") from exc
  if parsed != expected:
    raise ValueError(
        f"the edited {LIVE_KEY} does not parse to the expected change"
    )
  return new_text


def _verified_date_error(
    verified_date: str, live: dict, signed_at: str | None
) -> str | None:
  """Return why `verified_date` cannot be recorded, or None.

  It must not be in the future, nor before `pending_live_sync.since_date`,
  the earlier `verified_date`, or the day (in UTC) that the dump's receipt
  was signed at `signed_at`, if one is given.
  """
  value = datetime.date.fromisoformat(verified_date)
  today = max(
      datetime.date.today(),
      datetime.datetime.now(datetime.timezone.utc).date(),
  )
  if value > today:
    return f"--verified-date {verified_date} is in the future"
  pending = live.get(PENDING_KEY)
  for label, earlier in (
      (
          f"{PENDING_KEY}.since_date",
          pending.get("since_date") if isinstance(pending, dict) else None,
      ),
      ("verified_date", live.get("verified_date")),
  ):
    if isinstance(earlier, str) and _ISO_DATE_RE.fullmatch(earlier):
      try:
        if value < datetime.date.fromisoformat(earlier):
          return f"--verified-date {verified_date} is before {label} {earlier}"
      except ValueError:
        continue
  if signed_at is not None:
    # A receipt signed in the future is refused on its own.
    signed = _load_validator().bqca_capture_time(signed_at).date()
    if value < signed <= today:
      return (
          f"--verified-date {verified_date} is before the dump's receipt was"
          f" signed, at {signed_at}"
      )
  return None


@functools.cache
def _load_validator():
  """Import `validate_contracts.py` from this tool's directory, once."""
  path = pathlib.Path(__file__).resolve().parent / "validate_contracts.py"
  spec = importlib.util.spec_from_file_location("validate_contracts", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _values_at(message: list, path: tuple[str, ...]) -> list:
  """Return every value of the field at `path`, through repeated messages."""
  values: list = [message]
  for key in path:
    values = [
        value
        for parent in values
        if isinstance(parent, list)
        for name, value in parent
        if name == key
    ]
  return values


def _scalar_paths(
    message: list, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], str]]:
  """Yield `(field path, value)` for every scalar field of a message."""
  for name, value in message:
    if isinstance(value, list):
      yield from _scalar_paths(value, (*path, name))
    else:
      yield (*path, name), value


def _decode_snapshot(snapshot: bytes) -> str:
  """Decode `snapshot` as UTF-8 with universal newlines, like `read_text`."""
  return io.TextIOWrapper(
      io.BytesIO(snapshot), encoding="utf-8", newline=None
  ).read()


def _has_offline_preview_marker(text: str) -> bool:
  """Return whether `text` carries the offline-preview marker in any form."""
  return _OFFLINE_PREVIEW_RE.search(text) is not None


def _inside_directory(path: pathlib.Path, directory: pathlib.Path) -> bool:
  """Return whether `path` is inside `directory`, even when case differs."""
  root = directory.resolve()
  places = (
      path.resolve(),
      pathlib.Path(os.path.abspath(path)),
      path.parent.resolve(),
  )
  if any(place.is_relative_to(root) for place in places):
    return True
  try:
    root_stat = root.stat()
  except OSError:
    return False
  for place in places:
    for ancestor in (place, *place.parents):
      try:
        if os.path.samestat(ancestor.stat(), root_stat):
          return True
      except OSError:
        continue
  return False


def dump_identity_errors(
    text: str, binding: dict, data_sources: bool = True
) -> list[str]:
  """Return why a dump does not identify the live template.

  Each identity field must hold exactly one value, equal to the binding's:
  REPORT_ID_FIELD its `report_id`, REPORT_DATASOURCE_FIELD its
  `datasource_id`, and the `datasource_id` of the ALIAS_FIELD entry for its
  `data_source_alias` that same data source. A value elsewhere, or one that
  only contains the ID, does not count; when the dump has the report ID in
  other fields, the error names them. Without `data_sources`, as for an
  export taken before the sync repoints them, only REPORT_ID_FIELD is
  checked. A dump that cannot be read as protobuf text format fails closed
  with its parse error.
  """
  normalized = text.replace("\r\n", "\n").replace("\r", "\n")
  try:
    report = _load_validator().parse_report_textproto(normalized)
  except ValueError as exc:
    return [f"cannot read the dump as protobuf text format: {exc}"]
  errors = []
  report_id = binding.get("report_id")
  report_ids = _values_at(report, REPORT_ID_FIELD)
  if report_ids != [report_id]:
    found = sorted(
        {
            ".".join(path)
            for path, value in _scalar_paths(report)
            if value == report_id and path != REPORT_ID_FIELD
        }
    )
    where = f" (the report ID is in {', '.join(found)})" if found else ""
    errors.append(
        f"the dump's {'.'.join(REPORT_ID_FIELD)} is {report_ids}, not"
        f" [{report_id!r}]{where}"
    )
  if not data_sources:
    return errors
  datasource_id = binding.get("datasource_id")
  datasource_ids = _values_at(report, REPORT_DATASOURCE_FIELD)
  if datasource_ids != [datasource_id]:
    errors.append(
        f"the dump's {'.'.join(REPORT_DATASOURCE_FIELD)} is {datasource_ids},"
        f" not [{datasource_id!r}]"
    )
  alias = binding.get("data_source_alias")
  targets = [
      target
      for entry in _values_at(report, ALIAS_FIELD)
      if isinstance(entry, list) and _values_at(entry, ("alias",)) == [alias]
      for target in _values_at(entry, ("datasource_id",))
  ]
  if targets != [datasource_id]:
    errors.append(
        f"the dump's {alias} data source alias in {'.'.join(ALIAS_FIELD)}"
        f" points at {targets}, not [{datasource_id!r}]"
    )
  return errors


@functools.cache
def _load_applier() -> types.ModuleType:
  """Import `apply_bqca_rename_map.py` from this tool's directory, once."""
  path = pathlib.Path(__file__).resolve().parent / "apply_bqca_rename_map.py"
  spec = importlib.util.spec_from_file_location("apply_bqca_rename_map", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def receipt_path(export: pathlib.Path) -> pathlib.Path:
  """Return where the capture receipt of the export `export` is."""
  return export.with_name(f"{export.name}.receipt.json")


@dataclasses.dataclass(frozen=True)
class Receipt:
  """A capture receipt and its signature, each read once from its file.

  `data` or `signature` is None when its file cannot be read; `errors` says
  why.
  """

  path: pathlib.Path
  data: bytes | None
  signature: bytes | None
  errors: tuple[str, ...]

  @classmethod
  def read(cls, path: pathlib.Path) -> "Receipt":
    """Read the receipt at `path` and then its signature, once each."""
    try:
      data = path.read_bytes()
    except OSError as exc:
      return cls(
          path,
          None,
          None,
          (f"cannot read the capture receipt {path}: {exc.strerror or exc}",),
      )
    signature_path = _load_validator().bqca_capture_signature_path(path)
    try:
      signature = signature_path.read_bytes()
    except OSError as exc:
      return cls(
          path,
          data,
          None,
          (
              "cannot read the receipt's signature"
              f" {signature_path}: {exc.strerror or exc}",
          ),
      )
    return cls(path, data, signature, ())

  def fields(self) -> dict | None:
    """Return the receipt's fields, or None if it cannot be read as one."""
    if self.data is None:
      return None
    try:
      return _load_validator().parse_bqca_capture_receipt(self.data)
    except ValueError:
      return None


@dataclasses.dataclass(frozen=True)
class Capture:
  """The capture before the write-back: its export and receipt, read once."""

  path: pathlib.Path
  data: bytes | None
  receipt: Receipt
  errors: tuple[str, ...]

  @classmethod
  def read(cls, path: pathlib.Path) -> "Capture":
    """Read the export at `path`, then its receipt and signature."""
    try:
      data, errors = path.read_bytes(), ()
    except OSError as exc:
      data = None
      errors = (f"cannot read --before {path}: {exc.strerror or exc}",)
    return cls(path, data, Receipt.read(receipt_path(path)), errors)


def receipt_blockers(
    receipt: Receipt,
    subject: str,
    export: str,
    export_sha256: str | None,
    binding: dict,
    signers: bytes,
    now: datetime.datetime,
) -> tuple[dict | None, list[str]]:
  """Return the fields of `receipt`, and why it is not trusted for this sync.

  The receipt, named `subject` in the reasons, must be read and well-formed,
  its signature must verify against `signers` (the bytes of the signers
  file), and it must be for the binding's report, its pinned manifest and
  bundle digests and its pending sync, signed on or after the day that sync
  began and not in the future, and for the bytes of the export named
  `export`, whose SHA-256 is `export_sha256` (unknown when None).
  """
  validator = _load_validator()
  blockers = list(receipt.errors)
  if receipt.data is None:
    return None, blockers
  try:
    fields = validator.parse_bqca_capture_receipt(receipt.data)
  except ValueError as exc:
    return None, blockers + [
        f"{receipt.path} is not a {validator.BQCA_CAPTURE_RECEIPT_FORMAT}"
        f" capture receipt: {exc}"
    ]
  if receipt.signature is not None:
    error = validator.bqca_capture_signature_error(
        receipt.data, receipt.signature, fields["signer"], signers
    )
    if error is not None:
      blockers.append(error)
  report_id = binding.get("report_id")
  if fields["report_id"] != report_id:
    blockers.append(
        f"{subject} is for report {fields['report_id']!r}, not {report_id!r}"
    )
  live = binding.get(LIVE_KEY)
  live = live if isinstance(live, dict) else {}
  for key in ("manifest_sha256", "template_bundle_sha256"):
    if fields[key] != live.get(key):
      blockers.append(
          f"{subject} is for {key} {fields[key]}, not the pinned"
          f" {live.get(key)}"
      )
  pending = live.get(PENDING_KEY)
  since = pending.get("since_date") if isinstance(pending, dict) else None
  if fields["pending_since_date"] != since:
    blockers.append(
        f"{subject} is for the live sync pending since"
        f" {fields['pending_since_date']}, not the one pending since {since}"
    )
  blockers += validator.bqca_capture_time_errors(fields, now, subject)
  if export_sha256 is not None and fields["dump_sha256"] != export_sha256:
    blockers.append(
        f"{subject} is for a dump with sha256 {fields['dump_sha256']}, not"
        f" {export}, whose sha256 is {export_sha256}"
    )
  return fields, blockers


def canonical_content(message: list) -> tuple:
  """Return a parsed textproto `message` in a form that ignores field order.

  Each message becomes the sorted tuple of its `(field, value)` pairs, with
  nested messages in this form too, so neither the order of distinct fields
  nor that of the values of a repeated field matters: two messages have the
  same form exactly when they hold the same multiset of fields and values.
  Comments, spacing, separators and quoting are already gone from the parsed
  message.
  """
  return tuple(
      sorted(
          (
              name,
              (
                  ("message", canonical_content(value))
                  if isinstance(value, list)
                  else ("scalar", value)
              ),
          )
          for name, value in message
      )
  )


def offline_preview_blockers(
    dump: pathlib.Path,
    snapshot: bytes,
    before: pathlib.Path,
    before_data: bytes,
) -> list[str]:
  """Return why the dump bytes `snapshot` are the offline preview of `before`.

  `tools/apply_bqca_rename_map.py` writes, for the export `before_data`
  (read from `before`), an offline preview whose parsed content is
  `preview_tree`. A dump with the same content (`canonical_content`) is
  refused, however its comments, spacing, quoting, marker or the order of its
  fields and repeated values differ: a capture of the live report after the
  write-back that holds the preview's fields and values, and no others, does
  not show that the live report was written.
  """
  try:
    preview = _load_applier().preview_tree(before_data)
  except ValueError as exc:
    return [
        f"cannot derive the offline preview of --before {before}, so nothing"
        f" tells the dump apart from it: {exc}"
    ]
  try:
    tree = _load_validator().parse_report_textproto(_decode_snapshot(snapshot))
  except (UnicodeDecodeError, ValueError):
    # `dump_blockers` says why the dump cannot be read.
    return []
  if canonical_content(tree) != canonical_content(preview):
    return []
  return [
      f"{dump} has the parsed content of the offline preview that"
      f" tools/apply_bqca_rename_map.py writes for --before {before}"
      " (comments, spacing and the order of fields aside): no field of the"
      " live report changed after the write-back, so it does not show that"
      " the live template was written"
  ]


def before_blockers(
    fields: dict | None,
    before: Capture,
    binding: dict,
    signers: bytes,
    now: datetime.datetime,
) -> list[str]:
  """Return why `before` is not the capture before the dump's write-back.

  `fields` are those of the dump's receipt, if it could be read. The
  `--before` receipt must be trusted for this sync like the dump's and be of
  a capture taken before any write-back, the dump's receipt must name it (its
  SHA-256 and revision) and be signed no earlier, and the dump must be of
  another revision.
  """
  validator = _load_validator()
  before_sha256 = (
      hashlib.sha256(before.data).hexdigest()
      if before.data is not None
      else None
  )
  before_fields, blockers = receipt_blockers(
      before.receipt,
      "the receipt of --before",
      "--before",
      before_sha256,
      binding,
      signers,
      now,
  )
  blockers = list(before.errors) + blockers
  if before_fields is None:
    return blockers
  if before_fields["before_dump_sha256"] is not None:
    blockers.append(
        f"--before {before.path} is a capture taken after a write-back, not"
        " the capture before it"
    )
  if fields is None:
    return blockers
  if before_sha256 is not None and fields["before_dump_sha256"] not in (
      None,
      before_sha256,
  ):
    blockers.append(
        "the receipt names the capture before the write-back with sha256"
        f" {fields['before_dump_sha256']}, not --before, whose sha256 is"
        f" {before_sha256}"
    )
  if fields["before_revision"] not in (None, before_fields["revision"]):
    blockers.append(
        "the receipt names the capture before the write-back of revision"
        f" {fields['before_revision']!r}, not that of --before,"
        f" {before_fields['revision']!r}"
    )
  if fields["revision"] == before_fields["revision"]:
    blockers.append(
        f"the dump and --before are both of revision {fields['revision']!r}:"
        " the live report has no new revision after the write-back"
    )
  if validator.bqca_capture_time(
      fields["signed_at"]
  ) < validator.bqca_capture_time(before_fields["signed_at"]):
    blockers.append(
        f"the receipt was signed at {fields['signed_at']}, before the receipt"
        f" of --before, at {before_fields['signed_at']}"
    )
  return blockers


def dump_blockers(
    dump: pathlib.Path,
    snapshot: bytes,
    binding: dict,
    data_sources: bool = True,
) -> list[str]:
  """Return why the dump bytes `snapshot`, read from `dump`, are refused.

  `dump` must not be a test fixture of this repository, and `snapshot` must
  decode as UTF-8, carry no offline-preview marker, and identify the report
  (`dump_identity_errors`, with `data_sources`).
  """
  blockers = []
  fixtures = REPOSITORY_ROOT / FIXTURES_PATH
  if _inside_directory(dump, fixtures):
    blockers.append(f"{dump} is a test fixture of this repository")
  try:
    text = _decode_snapshot(snapshot)
  except UnicodeDecodeError as exc:
    blockers.append(f"cannot read {dump} as UTF-8 text: {exc}")
    return blockers
  if _has_offline_preview_marker(text):
    blockers.append(
        f"{dump} is an offline preview from tools/apply_bqca_rename_map.py,"
        " not a dump of the synced live template"
    )
  return blockers + dump_identity_errors(text, binding, data_sources)


def write_blockers(
    dump: pathlib.Path,
    snapshot: bytes,
    receipt: Receipt,
    before: Capture | None,
    revision: str | None,
    binding: dict,
    now: datetime.datetime,
) -> list[str]:
  """Return why a write would be refused (empty if it would not).

  A write needs a trusted receipt of the dump bytes `snapshot`, of a capture
  after the write-back, for the revision `revision` (`receipt_blockers`); the
  capture before the write-back that it names (`before_blockers`); dump bytes
  whose content differs from the offline preview of that capture
  (`offline_preview_blockers`); and dump bytes that `dump_blockers` does not
  refuse. The signers file is read once, here.
  """
  validator = _load_validator()
  try:
    signers = (ROOT / validator.BQCA_CAPTURE_SIGNERS_PATH).read_bytes()
  except OSError as exc:
    signers = b""
    signers_error = [
        f"cannot read {validator.BQCA_CAPTURE_SIGNERS_PATH}:"
        f" {exc.strerror or exc}"
    ]
  else:
    signers_error = []
  fields, blockers = receipt_blockers(
      receipt,
      "the receipt",
      "this dump",
      hashlib.sha256(snapshot).hexdigest(),
      binding,
      signers,
      now,
  )
  blockers = signers_error + blockers
  if fields is not None:
    if revision is not None and fields["revision"] != revision:
      blockers.append(
          f"the receipt is for revision {fields['revision']!r}, not --revision"
          f" {revision!r}"
      )
    if fields["before_dump_sha256"] is None:
      blockers.append(
          "the receipt is of a capture taken before the write-back (it names"
          " no capture before it), not after it"
      )
  if revision is None:
    blockers.append(NO_REVISION)
  if before is None:
    blockers.append(NO_BEFORE)
  else:
    blockers += before_blockers(fields, before, binding, signers, now)
    if before.data is not None:
      blockers += offline_preview_blockers(
          dump, snapshot, before.path, before.data
      )
  return blockers + dump_blockers(dump, snapshot, binding)


def _replace_file(path: pathlib.Path, data: bytes) -> None:
  """Atomically replace or create `path` with `data`.

  The data is written and synced to a staged file next to `path`, which
  then replaces it with `os.replace`; on any failure the staged file is
  removed and `path` keeps its old bytes. A replaced file keeps its
  permission bits; a new one gets mode 0644.
  """
  try:
    mode = stat.S_IMODE(path.stat().st_mode)
  except FileNotFoundError:
    mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
  descriptor, staged = tempfile.mkstemp(
      dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
  )
  try:
    with os.fdopen(descriptor, "wb") as handle:
      handle.write(data)
      handle.flush()
      os.fsync(handle.fileno())
    os.chmod(staged, mode)
    os.replace(staged, path)
  except BaseException:
    pathlib.Path(staged).unlink(missing_ok=True)
    raise


def _restore(originals: list[tuple[pathlib.Path, bytes | None]]) -> None:
  """Put back each file's original bytes, or remove a file that was new."""
  for path, data in originals:
    if data is None:
      path.unlink(missing_ok=True)
    else:
      _replace_file(path, data)


def _audit(dump: pathlib.Path) -> int:
  """Run the contract validator with the live audit of `dump`."""
  sys.stdout.flush()
  sys.stderr.flush()
  command = [
      sys.executable,
      str(ROOT / VALIDATOR_PATH),
      "--profile",
      "bqca",
      "--live-report-textproto",
      str(dump),
  ]
  return subprocess.run(command, check=False).returncode


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
      "--dump",
      type=pathlib.Path,
      required=True,
      metavar="PATH",
      help=(
          "the capture of the live template after the write-back, which"
          " tools/sign_live_capture.py wrote with its receipt at"
          " PATH.receipt.json"
      ),
  )
  parser.add_argument(
      "--check",
      action="store_true",
      help=(
          "audit the dump, print the change and every reason a write would"
          " be refused, and write nothing"
      ),
  )
  parser.add_argument(
      "--before",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "required to write: the capture of the live template before the"
          " write-back, with its receipt at PATH.receipt.json"
      ),
  )
  parser.add_argument(
      "--revision",
      type=revision_argument,
      metavar="REVISION",
      help=(
          "required to write: the revision of the live report that the dump"
          " captured, as its receipt records it; recorded in the binding"
      ),
  )
  parser.add_argument(
      "--verified-date",
      type=_iso_date,
      metavar="YYYY-MM-DD",
      help="the date to record as verified_date (default: today, in UTC)",
  )
  args = parser.parse_args(argv)
  if not args.check and (args.before is None or args.revision is None):
    parser.error(
        "a write needs --before, the capture before the write-back, and"
        " --revision, the live revision that the dump captured; --check lists"
        " what a write would refuse without them"
    )
  # Read each export, receipt and signature once. The fixture-path check uses
  # the dump's path as given; everything else uses only these bytes (the
  # audits through a private, read-only copy of the dump), never the paths
  # again, so a file that replaces one of them while this runs cannot be
  # mixed in.
  dump = args.dump.resolve()
  try:
    snapshot = dump.read_bytes()
  except OSError as exc:
    print(
        f"not finalized: cannot read {args.dump}: {exc.strerror or exc};"
        f" {BINDING_PATH} is unchanged",
        file=sys.stderr,
    )
    return 1
  print(
      f"snapshot of {args.dump}: {len(snapshot)} bytes, sha256"
      f" {hashlib.sha256(snapshot).hexdigest()}"
  )
  receipt = Receipt.read(receipt_path(args.dump))
  before = Capture.read(args.before) if args.before is not None else None
  for label, data in (
      (f"receipt {receipt.path}", receipt.data),
      (
          f"--before {args.before}",
          before.data if before is not None else None,
      ),
      (
          f"receipt {before.receipt.path}" if before is not None else "",
          before.receipt.data if before is not None else None,
      ),
  ):
    if data is not None:
      print(
          f"{label}: {len(data)} bytes, sha256"
          f" {hashlib.sha256(data).hexdigest()}"
      )
  with tempfile.TemporaryDirectory(prefix="finalize_live_sync_") as private:
    audited = pathlib.Path(private) / (dump.name or "dump.textproto")
    audited.write_bytes(snapshot)
    audited.chmod(stat.S_IRUSR)
    return _finalize_snapshot(args, dump, snapshot, receipt, before, audited)


def capture_record(
    revision: str | None, snapshot: bytes, receipt: Receipt
) -> dict[str, str]:
  """Return the capture record for the dump `snapshot` and its `receipt`.

  A value that only a write's arguments give is a placeholder when it is
  missing (CAPTURE_PLACEHOLDERS), as in a `--check` preview.
  """
  fields = receipt.fields()
  return {
      "verified_revision": revision
      or CAPTURE_PLACEHOLDERS["verified_revision"],
      "verified_dump_sha256": hashlib.sha256(snapshot).hexdigest(),
      "capture_receipt_sha256": (
          hashlib.sha256(receipt.data).hexdigest()
          if receipt.data is not None
          else CAPTURE_PLACEHOLDERS["capture_receipt_sha256"]
      ),
      "captured_by": (
          fields["signer"]
          if fields is not None
          else CAPTURE_PLACEHOLDERS["captured_by"]
      ),
  }


def _finalize_snapshot(
    args: argparse.Namespace,
    dump: pathlib.Path,
    snapshot: bytes,
    receipt: Receipt,
    before: Capture | None,
    audited: pathlib.Path,
) -> int:
  """Check, audit and record the dump `snapshot`, read from `dump`.

  `receipt` is the dump's capture receipt and `before` the capture before
  the write-back, if given, each read once. Both audits read `audited`, a
  private copy of `snapshot`.
  """
  validator = _load_validator()
  now = datetime.datetime.now(datetime.timezone.utc)
  verified_date = args.verified_date or now.date().isoformat()
  unchanged = f"{BINDING_PATH} is unchanged"
  binding_path = ROOT / BINDING_PATH
  original = binding_path.read_bytes().decode("utf-8")
  binding = yaml.safe_load(original)
  binding = binding if isinstance(binding, dict) else {}
  live = binding.get(LIVE_KEY)
  if not isinstance(live, dict) or PENDING_KEY not in live:
    print(
        f"nothing to finalize: {BINDING_PATH} has no"
        f" {LIVE_KEY}.{PENDING_KEY}; it is unchanged"
    )
    return 0

  # A write fails closed without a trusted capture of these bytes.
  blockers = write_blockers(
      dump, snapshot, receipt, before, args.revision, binding, now
  )
  if blockers and not args.check:
    print(f"not finalized: {'; '.join(blockers)}; {unchanged}", file=sys.stderr)
    return 1
  code = _audit(audited)
  if code != 0:
    print(
        f"not finalized: {VALIDATOR_PATH} exited {code} on {args.dump};"
        f" {unchanged}",
        file=sys.stderr,
    )
    return 1

  record = capture_record(args.revision, snapshot, receipt)
  fields = receipt.fields()
  error = _verified_date_error(
      verified_date, live, fields["signed_at"] if fields is not None else None
  )
  if error is None:
    try:
      updated = finalized_binding_text(original, verified_date, record)
    except ValueError as exc:
      error = str(exc)
  if error is not None:
    print(f"not finalized: {error}; {unchanged}", file=sys.stderr)
    return 1

  sys.stdout.writelines(
      difflib.unified_diff(
          original.splitlines(keepends=True),
          updated.splitlines(keepends=True),
          f"a/{BINDING_PATH}",
          f"b/{BINDING_PATH}",
      )
  )
  committed = validator.BQCA_CAPTURE_RECEIPT_PATH
  if args.check:
    refusal = (
        f", but a write would refuse it: {'; '.join(blockers)}"
        if blockers
        else ""
    )
    print(
        f"check: the live audit of {args.dump} passed; {BINDING_PATH} would"
        f" change as above, and {committed} and its signature would be"
        f" written{refusal}; nothing written"
    )
    return 0

  # The receipt and its signature go first and the binding last; until the
  # audit of the written binding passes, any failure, exception or interrupt
  # puts the original bytes of all three back.
  receipt_target = ROOT / committed
  writes = [
      (receipt_target, receipt.data),
      (
          validator.bqca_capture_signature_path(receipt_target),
          receipt.signature,
      ),
      (binding_path, updated.encode("utf-8")),
  ]
  originals = []
  for path, _ in reversed(writes):
    try:
      originals.append((path, path.read_bytes()))
    except FileNotFoundError:
      originals.append((path, None))
  try:
    for path, data in writes:
      _replace_file(path, data)
    code = _audit(audited)
  except BaseException as exc:
    _restore(originals)
    if not isinstance(exc, Exception):
      print(
          f"interrupted: {BINDING_PATH} and {committed} were restored",
          file=sys.stderr,
      )
      raise
    print(
        f"not finalized: {type(exc).__name__}: {exc} after the write, so"
        f" {BINDING_PATH} and {committed} were restored",
        file=sys.stderr,
    )
    return 1
  if code != 0:
    _restore(originals)
    print(
        f"not finalized: {VALIDATOR_PATH} exited {code} after the write, so"
        f" {BINDING_PATH} and {committed} were restored",
        file=sys.stderr,
    )
    return 1
  print(
      f"finalized: {BINDING_PATH} records the live verification of"
      f" {verified_date} (revision {record['verified_revision']}, receipt"
      f" signed by {record['captured_by']}) and has no {PENDING_KEY};"
      f" {committed} and its signature are written."
  )
  print(NEXT_STEPS)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
