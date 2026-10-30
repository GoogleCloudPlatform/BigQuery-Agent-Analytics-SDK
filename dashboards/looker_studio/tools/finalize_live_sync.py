#!/usr/bin/env python3
"""Finalize the BQCA live template sync from a dump of the live template.

Runs ``tools/validate_contracts.py --profile bqca --live-report-textproto
DUMP``. Only if it exits 0 does this record the verification in
``bindings/bqca_report_template.yaml``. In ``live_template_verification`` it:

* sets ``verified_date`` to ``--verified-date`` (default: today);
* copies the pinned ``manifest_sha256`` and ``template_bundle_sha256`` into
  ``verified_manifest_sha256`` and ``verified_template_bundle_sha256``;
* removes ``pending_live_sync``.

Every other line of the binding, comments included, is kept. The edit must
parse to exactly that change. The new binding replaces the old one
atomically, and the audit runs again after the write; if it fails, raises or
is interrupted, the original bytes are put back. ``--check`` runs the same
audit and prints the change as a diff without writing it.

The checked-in fixtures are stand-ins, not dumps of the live template:
``--check`` accepts them, a write does not. A write refuses a dump under this
repository's ``tests/fixtures``, and a dump anywhere whose content is that of
a checked-in stand-in (``normalized_dump_sha256``, which ignores comments and
whitespace).

This tool does not edit documentation or tests. See "Finalizing the live
template sync" in ``docs/dashboard-implementation.md`` for the rest of the
change.

Exits 0 when the binding is finalized, would be (``--check``), or already is;
1 when the audit fails or the binding cannot be finalized; 2 on bad arguments.
"""

import argparse
import copy
import datetime
import difflib
import functools
import hashlib
import importlib.util
import json
import os
import pathlib
import re
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
LIVE_KEY = "live_template_verification"
PENDING_KEY = "pending_live_sync"
# The normalized digests (`normalized_dump_sha256`) of the checked-in
# stand-ins, so that a copy of one is refused wherever it is, even when this
# checkout has no `tests/fixtures`. tests/test_looker_studio_live_sync.py
# checks that they are those of the files.
KNOWN_FIXTURE_SHA256 = {
    "65749b99444871cdfa49239acd693c96d1cd8b45e91b19d3fc2f8dc0b330afe6": (
        "tests/fixtures/bqca_live_report_42e79a1f_sanitized.textproto"
    ),
    "c7714fe895a6699e5d3f6a042d028c3d7be142110564b5f7c9e2925c50233e37": (
        "tests/fixtures/bqca_live_report_42e79a1f_sanitized_remapped.textproto"
    ),
}
# Each `verified_*` key and the pinned digest it takes.
VERIFIED_DIGEST_KEYS = {
    "verified_manifest_sha256": "manifest_sha256",
    "verified_template_bundle_sha256": "template_bundle_sha256",
}
NEXT_STEPS = """\
In the same change:
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


def finalized_binding_text(text: str, verified_date: str) -> str:
  """Return the binding `text` with its pending live sync recorded as verified.

  In `live_template_verification`, replaces the `verified_date` line and the
  two `verified_*_sha256` lines, and removes the `pending_live_sync:` line
  and the lines nested under it. Every other line is kept as it is.

  Args:
    text: The text of `bindings/bqca_report_template.yaml`.
    verified_date: The new `verified_date`, YYYY-MM-DD.

  Returns:
    The edited text.

  Raises:
    ValueError: The binding is not valid YAML or has no pending live sync, a
      pinned digest is not a SHA-256 hex digest, one of those lines is
      missing, repeated, or not a plain block entry, or the edited text does
      not parse to exactly that change.
  """
  try:
    binding = yaml.safe_load(text)
  except yaml.YAMLError as exc:
    raise ValueError(f"the binding is not valid YAML: {exc}") from exc
  live = binding.get(LIVE_KEY) if isinstance(binding, dict) else None
  if not isinstance(live, dict) or PENDING_KEY not in live:
    raise ValueError(f"{LIVE_KEY} has no {PENDING_KEY} to finalize")
  values = {"verified_date": f'"{verified_date}"'}
  for verified_key, pinned_key in VERIFIED_DIGEST_KEYS.items():
    pinned = live.get(pinned_key)
    if not isinstance(pinned, str) or not _SHA256_RE.fullmatch(pinned):
      raise ValueError(
          f"{LIVE_KEY}.{pinned_key} {pinned!r} is not a SHA-256 hex digest"
      )
    values[verified_key] = pinned

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


def normalized_dump_sha256(text: str) -> str | None:
  """Return the SHA-256 of a dump's fields, or None if it cannot be read.

  The digest covers the fields and values that the live audit's reader
  parses, in order, so comments, whitespace, separators and quoting do not
  change it.
  """
  try:
    message = _load_validator().parse_report_textproto(text)
  except ValueError:
    return None
  canonical = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stand_in(dump: pathlib.Path) -> str | None:
  """Return why `dump` is a stand-in rather than a dump, or None.

  A file under this repository's `tests/fixtures` is a stand-in by its path.
  A file anywhere whose normalized digest is that of a checked-in stand-in
  (KNOWN_FIXTURE_SHA256, or any `*.textproto` under `tests/fixtures`) is one
  by its content.
  """
  fixtures = REPOSITORY_ROOT / FIXTURES_PATH
  if dump.resolve().is_relative_to(fixtures.resolve()):
    return f"{dump} is a test fixture of this repository"
  try:
    digest = normalized_dump_sha256(dump.read_text(encoding="utf-8"))
  except (OSError, UnicodeDecodeError):
    return None
  known = dict(KNOWN_FIXTURE_SHA256)
  for fixture in sorted(fixtures.glob("*.textproto")):
    fixture_digest = normalized_dump_sha256(fixture.read_text(encoding="utf-8"))
    known.setdefault(fixture_digest, f"{FIXTURES_PATH}/{fixture.name}")
  if digest is not None and digest in known:
    return f"{dump} has the content of {known[digest]}, a stand-in"
  return None


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
      help="audit the dump and print the change without writing it",
  )
  parser.add_argument(
      "--verified-date",
      type=_iso_date,
      metavar="YYYY-MM-DD",
      help="the date to record as verified_date (default: today)",
  )
  args = parser.parse_args(argv)
  verified_date = args.verified_date or datetime.date.today().isoformat()
  unchanged = f"{BINDING_PATH} is unchanged"

  stand_in = _stand_in(args.dump)
  if stand_in is not None and not args.check:
    print(
        f"not finalized: {stand_in}, not a dump of the live template;"
        f" {unchanged}",
        file=sys.stderr,
    )
    return 1
  code = _audit(args.dump)
  if code != 0:
    print(
        f"not finalized: {VALIDATOR_PATH} exited {code} on {args.dump};"
        f" {unchanged}",
        file=sys.stderr,
    )
    return 1

  binding_path = ROOT / BINDING_PATH
  original = binding_path.read_bytes().decode("utf-8")
  binding = yaml.safe_load(original)
  live = binding.get(LIVE_KEY) if isinstance(binding, dict) else None
  if not isinstance(live, dict) or PENDING_KEY not in live:
    print(
        f"nothing to finalize: {BINDING_PATH} has no"
        f" {LIVE_KEY}.{PENDING_KEY}; it is unchanged"
    )
    return 0
  error = _verified_date_error(verified_date, live)
  if error is None:
    try:
      updated = finalized_binding_text(original, verified_date)
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
    note = " (a stand-in: a write would refuse it)" if stand_in else ""
    print(
        f"check: the live audit of {args.dump}{note} passed; {BINDING_PATH}"
        " would change as above; nothing written"
    )
    return 0

  # Until the audit of the written binding passes, any failure, exception or
  # interrupt puts the original bytes back.
  original_bytes = original.encode("utf-8")
  try:
    _replace_file(binding_path, updated.encode("utf-8"))
    code = _audit(args.dump)
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
      f" {verified_date} and has no {PENDING_KEY}."
  )
  print(NEXT_STEPS)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
