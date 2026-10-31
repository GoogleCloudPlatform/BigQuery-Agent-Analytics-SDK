#!/usr/bin/env python3
"""Finalize the BQCA live template sync from a dump of the live template.

Runs ``tools/validate_contracts.py --profile bqca --live-report-textproto
DUMP``. Only if it exits 0 does this record the verification in
``bindings/bqca_report_template.yaml``. In ``live_template_verification`` it:

* sets ``verified_date`` to ``--verified-date`` (default: today);
* copies the pinned ``manifest_sha256`` and ``template_bundle_sha256`` into
  ``verified_manifest_sha256`` and ``verified_template_bundle_sha256``;
* records the capture: ``verified_revision`` (``--revision``, the revision of
  the live report that the dump captured), ``verified_dump_sha256`` (the
  SHA-256 of the dump's bytes), ``capture_receipt_sha256`` (the SHA-256 of
  the dump's capture receipt) and ``captured_by`` (the receipt's signer);
* removes ``pending_live_sync``.

Every other line of the binding, comments included, is kept. The edit must
parse to exactly that change. The new binding replaces the old one
atomically, and the audit runs again after the write; if it fails, raises or
is interrupted, the original bytes are put back. ``--check`` runs the same
checks and audit, prints the change as a diff and every reason a write would
be refused, and writes nothing.

The dump, its capture receipt and the receipt's signature are each read once.
The checks, both audits (through a private, read-only copy of the dump) and
the recorded digests use those bytes, never the paths again, so a file that
replaces one of them while the tool runs cannot be mixed in.

Nothing in a dump proves where it came from, and an offline preview from
``tools/apply_bqca_rename_map.py`` audits exactly like a dump of the synced
template. So a write needs a capture receipt (``--receipt``): the JSON file
that ``tools/sign_live_capture.py`` writes for a fresh export of the live
template, with its SSH signature next to it (``<receipt>.sig``). A write
fails closed unless:

* ``ssh-keygen -Y verify`` verifies the signature in the
  ``bqca-live-capture-receipt`` namespace, made by a key that
  ``bindings/bqca_live_capture_signers`` lists for the receipt's ``signer``;
* the receipt is a well-formed ``bqca-live-capture-receipt/v1`` receipt for
  the binding's ``report_id``, for ``--revision``, and for the SHA-256 of the
  dump's exact bytes;
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

The offline tools never write a receipt, and only a listed signer can sign
one, so no offline preview can be written, whether or not its marker is
edited or removed. The trust is in the listed signers: one who signs a
receipt for anything but an unedited export of the live template makes a
false statement, which the receipt and the binding attribute to them.

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
import shutil
import stat
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parent.parent
BINDING_PATH = "bindings/bqca_report_template.yaml"
VALIDATOR_PATH = "tools/validate_contracts.py"
FIXTURES_PATH = "tests/fixtures"
# The SSH keys trusted to sign capture receipts, one OpenSSH allowed signers
# line each (see ssh-keygen(1)). A key is trusted only through a reviewed
# change to this file; no command-line option or environment variable names
# another one.
SIGNERS_PATH = "bindings/bqca_live_capture_signers"
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
    "capture_receipt_sha256": "<the SHA-256 of the --receipt file>",
    "captured_by": "<the signer of the --receipt>",
}
# A capture receipt is a JSON object with exactly these fields, signed byte
# for byte as it is; `tools/sign_live_capture.py` writes it.
RECEIPT_FORMAT = "bqca-live-capture-receipt/v1"
RECEIPT_FIELDS = (
    "format",
    "report_id",
    "revision",
    "dump_sha256",
    "signed_at",
    "signer",
)
# The namespace of a receipt's SSH signature, so that no signature the same
# key made for anything else (a commit, a file) verifies as a receipt's.
RECEIPT_NAMESPACE = "bqca-live-capture-receipt"
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
NEXT_STEPS = """\
In the same change:
  - commit the capture receipt and its signature as
    bindings/bqca_live_capture_receipt.json and
    bindings/bqca_live_capture_receipt.json.sig (they hold no secret), and
    the line that lists the signer in bindings/bqca_live_capture_signers if
    it is new; keep the dump privately: verified_dump_sha256 is its SHA-256;
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
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# A report ID, revision or signer is a plain token, the same in a receipt, on a
# command line and in the binding. A signer is also a literal principal of the
# signers file, never a pattern (`*`, `?`, `!`, `,`).
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")
_SIGNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}")
# The marker that `tools/apply_bqca_rename_map.py` writes, in any letter case
# and spacing, anywhere in the text. It is a tripwire for an offline preview;
# the receipt, not the marker, is what a write requires.
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
  if not _TOKEN_RE.fullmatch(value):
    raise argparse.ArgumentTypeError(
        f"{value!r} is not a revision: 1 to 128 letters, digits, '.', '_',"
        " ':', '+' or '-', starting with a letter or digit"
    )
  return value


def signer_argument(value: str) -> str:
  """Return `value` if it is a signer principal (an argparse type)."""
  if not _SIGNER_RE.fullmatch(value):
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


def _verified_date_error(verified_date: str, live: dict) -> str | None:
  """Return why `verified_date` cannot be recorded, or None."""
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


def dump_identity_errors(text: str, binding: dict) -> list[str]:
  """Return why a dump does not identify the live template.

  Each identity field must hold exactly one value, equal to the binding's:
  REPORT_ID_FIELD its `report_id`, REPORT_DATASOURCE_FIELD its
  `datasource_id`, and the `datasource_id` of the ALIAS_FIELD entry for its
  `data_source_alias` that same data source. A value elsewhere, or one that
  only contains the ID, does not count; when the dump has the report ID in
  other fields, the error names them. A dump that cannot be read as protobuf
  text format fails closed with its parse error.
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


def signature_path(receipt: pathlib.Path) -> pathlib.Path:
  """Return where the SSH signature of the capture receipt `receipt` is."""
  return receipt.with_name(f"{receipt.name}.sig")


def parse_receipt(data: bytes) -> dict[str, str]:
  """Return the fields of the capture receipt `data`.

  Raises:
    ValueError: `data` is not a RECEIPT_FORMAT receipt: a UTF-8 JSON object
      that gives each of RECEIPT_FIELDS, and nothing else, once, as a string
      of its form.
  """
  try:
    text = data.decode("utf-8")
  except UnicodeDecodeError as exc:
    raise ValueError(f"it is not UTF-8 text: {exc}") from exc

  def unique(pairs: list) -> dict:
    names = [name for name, _ in pairs]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
      raise ValueError(f"it gives {', '.join(repeated)} more than once")
    return dict(pairs)

  try:
    receipt = json.loads(text, object_pairs_hook=unique)
  except json.JSONDecodeError as exc:
    raise ValueError(f"it is not JSON: {exc}") from exc
  if not isinstance(receipt, dict):
    raise ValueError("it is not a JSON object")
  if sorted(receipt) != sorted(RECEIPT_FIELDS):
    raise ValueError(
        f"its fields are {sorted(receipt)}, not {sorted(RECEIPT_FIELDS)}"
    )
  forms = {
      "format": (re.compile(re.escape(RECEIPT_FORMAT)), repr(RECEIPT_FORMAT)),
      "report_id": (_TOKEN_RE, "a report ID"),
      "revision": (_TOKEN_RE, "a revision"),
      "dump_sha256": (_SHA256_RE, "a SHA-256 hex digest"),
      "signed_at": (_TIMESTAMP_RE, "a UTC time, YYYY-MM-DDTHH:MM:SSZ"),
      "signer": (_SIGNER_RE, "a signer"),
  }
  for name, (form, described) in forms.items():
    value = receipt[name]
    if not isinstance(value, str) or not form.fullmatch(value):
      raise ValueError(f"its {name} {value!r} is not {described}")
  try:
    datetime.datetime.strptime(receipt["signed_at"], _TIMESTAMP_FORMAT)
  except ValueError as exc:
    raise ValueError(
        f"its signed_at {receipt['signed_at']!r} is not a UTC time: {exc}"
    ) from exc
  return receipt


def receipt_signature_error(
    data: bytes, signature: bytes, signer: str
) -> str | None:
  """Return why `signature` is not a trusted signature of the receipt `data`.

  Runs `ssh-keygen -Y verify` in RECEIPT_NAMESPACE for the identity `signer`
  on private copies of SIGNERS_PATH, read once, and of `signature`. Returns
  None if the signature verifies.
  """
  ssh_keygen = shutil.which("ssh-keygen")
  if ssh_keygen is None:
    return (
        "cannot verify the receipt's signature: ssh-keygen (OpenSSH 8.2 or"
        " later) is not on PATH"
    )
  try:
    signers = (ROOT / SIGNERS_PATH).read_bytes()
  except OSError as exc:
    return f"cannot read {SIGNERS_PATH}: {exc.strerror or exc}"
  if not any(
      line.strip() and not line.lstrip().startswith(b"#")
      for line in signers.splitlines()
  ):
    return f"{SIGNERS_PATH} lists no signer, so it trusts no receipt"
  with tempfile.TemporaryDirectory(prefix="receipt_signature_") as private:
    signers_copy = pathlib.Path(private) / "allowed_signers"
    signature_copy = pathlib.Path(private) / "receipt.sig"
    signers_copy.write_bytes(signers)
    signature_copy.write_bytes(signature)
    return _signature_error(
        ssh_keygen, data, signers_copy, signature_copy, signer
    )


def _signature_error(
    ssh_keygen: str,
    data: bytes,
    signers: pathlib.Path,
    signature: pathlib.Path,
    signer: str,
) -> str | None:
  """Run `ssh-keygen -Y verify` on the files `signers` and `signature`."""
  verified = subprocess.run(
      [
          ssh_keygen,
          "-Y",
          "verify",
          "-f",
          str(signers),
          "-I",
          signer,
          "-n",
          RECEIPT_NAMESPACE,
          "-s",
          str(signature),
      ],
      input=data,
      capture_output=True,
      check=False,
  )
  if verified.returncode == 0:
    return None
  # The signature is valid, but the signers file does not trust its key for
  # this signer (ssh-keygen may say why: a malformed line, an expired key, a
  # key restricted to other namespaces); or it is not a valid receipt
  # signature at all.
  details = [
      line.strip().replace(str(signers), SIGNERS_PATH)
      for line in verified.stderr.decode("utf-8", "replace").splitlines()
      if line.strip() and line.strip() != "Could not verify signature."
  ]
  valid = subprocess.run(
      [
          ssh_keygen,
          "-Y",
          "check-novalidate",
          "-n",
          RECEIPT_NAMESPACE,
          "-s",
          str(signature),
      ],
      input=data,
      capture_output=True,
      check=False,
  )
  if valid.returncode == 0:
    detail = f" ({'; '.join(details)})" if details else ""
    return (
        f"the receipt is not signed by a key that {SIGNERS_PATH} lists for"
        f" its signer {signer!r}{detail}"
    )
  reason = next(
      (
          line.strip()
          for line in valid.stderr.decode("utf-8", "replace").splitlines()
          if line.strip()
      ),
      f"ssh-keygen exited {valid.returncode}",
  )
  return (
      f"the receipt's signature is not a valid {RECEIPT_NAMESPACE} signature"
      f" of the receipt: {reason}"
  )


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
    """Read the receipt at `path` and its signature, once each."""
    contents, errors = [], []
    for what, source in (
        ("the capture receipt", path),
        ("the receipt's signature", signature_path(path)),
    ):
      try:
        contents.append(source.read_bytes())
      except OSError as exc:
        contents.append(None)
        errors.append(f"cannot read {what} {source}: {exc.strerror or exc}")
    return cls(path, *contents, tuple(errors))

  def fields(self) -> dict[str, str] | None:
    """Return the receipt's fields, or None if it cannot be read as one."""
    if self.data is None:
      return None
    try:
      return parse_receipt(self.data)
    except ValueError:
      return None


def receipt_blockers(
    receipt: Receipt, revision: str | None, dump_sha256: str, binding: dict
) -> list[str]:
  """Return why `receipt` is not a trusted receipt of the dump.

  The receipt must be read and well-formed, its signature must verify
  (`receipt_signature_error`), and it must be for the binding's
  `report_id`, for `revision` (when given), and for `dump_sha256`.
  """
  blockers = list(receipt.errors)
  if receipt.data is None:
    return blockers
  try:
    fields = parse_receipt(receipt.data)
  except ValueError as exc:
    return blockers + [
        f"{receipt.path} is not a {RECEIPT_FORMAT} capture receipt: {exc}"
    ]
  if receipt.signature is not None:
    error = receipt_signature_error(
        receipt.data, receipt.signature, fields["signer"]
    )
    if error is not None:
      blockers.append(error)
  report_id = binding.get("report_id")
  if fields["report_id"] != report_id:
    blockers.append(
        f"the receipt is for report {fields['report_id']!r}, not"
        f" {report_id!r}"
    )
  if revision is not None and fields["revision"] != revision:
    blockers.append(
        f"the receipt is for revision {fields['revision']!r}, not --revision"
        f" {revision!r}"
    )
  if fields["dump_sha256"] != dump_sha256:
    blockers.append(
        f"the receipt is for a dump with sha256 {fields['dump_sha256']}, not"
        f" this dump, whose sha256 is {dump_sha256}"
    )
  return blockers


def dump_blockers(
    dump: pathlib.Path, snapshot: bytes, binding: dict
) -> list[str]:
  """Return why the dump bytes `snapshot`, read from `dump`, are refused.

  `dump` must not be a test fixture of this repository, and `snapshot` must
  decode as UTF-8, carry no offline-preview marker, and identify the report
  (`dump_identity_errors`).
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
  return blockers + dump_identity_errors(text, binding)


def write_blockers(
    dump: pathlib.Path,
    snapshot: bytes,
    receipt: Receipt | None,
    revision: str | None,
    binding: dict,
) -> list[str]:
  """Return why a write would be refused (empty if it would not).

  A write needs a trusted capture receipt `receipt` of the dump bytes
  `snapshot` for the revision `revision` (`receipt_blockers`), and dump bytes
  that `dump_blockers` does not refuse.
  """
  blockers = []
  if receipt is None:
    blockers.append(
        "--receipt is not given: a write needs the capture receipt that"
        " tools/sign_live_capture.py signs for a fresh export of the live"
        " template"
    )
  if revision is None:
    blockers.append(
        "--revision is not given: a write needs the revision of the live"
        " report that the dump captured, as its receipt records it"
    )
  if receipt is not None:
    blockers += receipt_blockers(
        receipt, revision, hashlib.sha256(snapshot).hexdigest(), binding
    )
  return blockers + dump_blockers(dump, snapshot, binding)


def _replace_file(path: pathlib.Path, data: bytes) -> None:
  """Atomically replace `path` with `data`, keeping its permission bits.

  The data is written and synced to a staged file next to `path`, which
  then replaces it with `os.replace`; on any failure the staged file is
  removed and `path` keeps its old bytes.
  """
  descriptor, staged = tempfile.mkstemp(
      dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
  )
  try:
    with os.fdopen(descriptor, "wb") as handle:
      handle.write(data)
      handle.flush()
      os.fsync(handle.fileno())
    os.chmod(staged, stat.S_IMODE(path.stat().st_mode))
    os.replace(staged, path)
  except BaseException:
    pathlib.Path(staged).unlink(missing_ok=True)
    raise


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
      help="Looker Studio report textproto dump of the live BQCA template",
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
      "--receipt",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "required to write: the capture receipt that"
          " tools/sign_live_capture.py signed for the dump, with its"
          " signature at PATH.sig"
      ),
  )
  parser.add_argument(
      "--revision",
      type=revision_argument,
      metavar="REVISION",
      help=(
          "required to write: the revision of the live report that the dump"
          " captured, as the receipt records it; recorded in the binding"
      ),
  )
  parser.add_argument(
      "--verified-date",
      type=_iso_date,
      metavar="YYYY-MM-DD",
      help="the date to record as verified_date (default: today)",
  )
  args = parser.parse_args(argv)
  if not args.check and (args.receipt is None or args.revision is None):
    parser.error(
        "a write needs --receipt, the signed capture receipt of the dump, and"
        " --revision, the live revision it captured; --check lists what a"
        " write would refuse without them"
    )
  # Read the dump, the receipt and its signature once. The fixture-path check
  # uses the dump's path as given; everything else uses only these bytes (the
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
  receipt = Receipt.read(args.receipt) if args.receipt is not None else None
  if receipt is not None and receipt.data is not None:
    print(
        f"receipt {args.receipt}: {len(receipt.data)} bytes, sha256"
        f" {hashlib.sha256(receipt.data).hexdigest()}"
    )
  with tempfile.TemporaryDirectory(prefix="finalize_live_sync_") as private:
    audited = pathlib.Path(private) / (dump.name or "dump.textproto")
    audited.write_bytes(snapshot)
    audited.chmod(stat.S_IRUSR)
    return _finalize_snapshot(args, dump, snapshot, receipt, audited)


def capture_record(
    revision: str | None, snapshot: bytes, receipt: Receipt | None
) -> dict[str, str]:
  """Return the capture record for the dump `snapshot` and its `receipt`.

  A value that only a write's arguments give is a placeholder when it is
  missing (CAPTURE_PLACEHOLDERS), as in a `--check` preview.
  """
  fields = receipt.fields() if receipt is not None else None
  return {
      "verified_revision": revision
      or CAPTURE_PLACEHOLDERS["verified_revision"],
      "verified_dump_sha256": hashlib.sha256(snapshot).hexdigest(),
      "capture_receipt_sha256": (
          hashlib.sha256(receipt.data).hexdigest()
          if receipt is not None and receipt.data is not None
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
    receipt: Receipt | None,
    audited: pathlib.Path,
) -> int:
  """Check, audit and record the dump `snapshot`, read from `dump`.

  `receipt` is the capture receipt given, if any, read once. Both audits
  read `audited`, a private copy of `snapshot`.
  """
  verified_date = args.verified_date or datetime.date.today().isoformat()
  unchanged = f"{BINDING_PATH} is unchanged"
  binding_path = ROOT / BINDING_PATH
  original = binding_path.read_bytes().decode("utf-8")
  binding = yaml.safe_load(original)
  binding = binding if isinstance(binding, dict) else {}

  # A write fails closed without a trusted capture receipt of these bytes.
  blockers = write_blockers(dump, snapshot, receipt, args.revision, binding)
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

  live = binding.get(LIVE_KEY)
  if not isinstance(live, dict) or PENDING_KEY not in live:
    print(
        f"nothing to finalize: {BINDING_PATH} has no"
        f" {LIVE_KEY}.{PENDING_KEY}; it is unchanged"
    )
    return 0
  record = capture_record(args.revision, snapshot, receipt)
  error = _verified_date_error(verified_date, live)
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
  if args.check:
    refusal = (
        f", but a write would refuse it: {'; '.join(blockers)}"
        if blockers
        else ""
    )
    print(
        f"check: the live audit of {args.dump} passed; {BINDING_PATH} would"
        f" change as above{refusal}; nothing written"
    )
    return 0

  # Until the audit of the written binding passes, any failure, exception or
  # interrupt puts the original bytes back.
  original_bytes = original.encode("utf-8")
  try:
    _replace_file(binding_path, updated.encode("utf-8"))
    code = _audit(audited)
  except BaseException as exc:
    _replace_file(binding_path, original_bytes)
    if not isinstance(exc, Exception):
      print(f"interrupted: {BINDING_PATH} was restored", file=sys.stderr)
      raise
    print(
        f"not finalized: {type(exc).__name__}: {exc} after the write, so"
        f" {BINDING_PATH} was restored",
        file=sys.stderr,
    )
    return 1
  if code != 0:
    _replace_file(binding_path, original_bytes)
    print(
        f"not finalized: {VALIDATOR_PATH} exited {code} after the write, so"
        f" {BINDING_PATH} was restored",
        file=sys.stderr,
    )
    return 1
  print(
      f"finalized: {BINDING_PATH} records the live verification of"
      f" {verified_date} (revision {record['verified_revision']}, receipt"
      f" signed by {record['captured_by']}) and has no {PENDING_KEY}."
  )
  print(NEXT_STEPS)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
