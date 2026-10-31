#!/usr/bin/env python3
"""Capture the live BQCA template: export it and sign a receipt of the export.

Run this in place of the bare export command, twice per sync: once before the
write-back, and once after it with ``--before``, the first capture. It runs
the export command given after ``--``, which must write the report textproto
of the live template (``42e79a1f-a979-4de8-911f-80d5a99543d5``) to standard
output, and keeps exactly the bytes it reads from that pipe: it writes them to
``--out`` (mode 0600, outside this repository), with a capture receipt of
those bytes (``<out>.receipt.json``) and its SSH signature
(``<out>.receipt.json.sig``). It never signs bytes that it reads from a file.

The receipt, a ``bqca-live-capture-receipt/v2`` JSON object, gives:

* ``report_id``, ``revision`` (``--revision``), ``dump_sha256`` (of the bytes
  read), ``signed_at`` (UTC) and ``signer`` (``--signer``);
* ``export_command_sha256``: the SHA-256 of the export command as a JSON
  list, so that no internal command, path or token is published with it;
* ``manifest_sha256``, ``template_bundle_sha256`` and ``pending_since_date``:
  the binding's pinned digests and the day its pending sync began, so the
  receipt is for this sync only;
* ``before_dump_sha256`` and ``before_revision``: the ``--before`` capture,
  or null for a capture taken before the write-back.

``ssh-keygen -Y sign -n bqca-live-capture-receipt`` signs its exact bytes with
``--key``: a private key file, or the public key file of a key that ssh-agent
holds. The receipt is written only if ``bindings/bqca_live_capture_signers``
lists that key for ``--signer``, as ``tools/finalize_live_sync.py`` requires.

Nothing is written for an export that does not decode, parse and name the
live template in ``report_config.shareable.id``, or that carries the
offline-preview marker of ``tools/apply_bqca_rename_map.py`` in any form; after
the write-back, its report-level data source and ``ds0`` alias must also be the
binding's, as ``tools/finalize_live_sync.py`` requires. With ``--before``,
nothing is written either unless the ``--before`` capture is a trusted capture
taken before the write-back for this sync, the export is of another revision,
and its parsed content differs from that of the offline preview that
``tools/apply_bqca_rename_map.py`` writes for the ``--before`` export
(comments and spacing aside): an export that matches the preview in every
field does not show that the live report was written.

No tool can tell what the export command fetched. A signer who runs a command
that prints an edited file instead of fetching the live report makes a false
statement, which the receipt names them for; see "Capture receipts" in
``docs/dashboard-implementation.md``.

Exits 0 when the capture is written; 1 when it is refused, the export command
fails, the receipt cannot be signed or its key is not trusted, or the files
cannot be written; 2 on bad arguments.
"""

import argparse
import datetime
import functools
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import types

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
BINDING_PATH = "bindings/bqca_report_template.yaml"
SIGNATURE_HEADER = b"-----BEGIN SSH SIGNATURE-----\n"


@functools.cache
def _load_finalize() -> types.ModuleType:
  """Import `finalize_live_sync.py` from this tool's directory, once."""
  path = pathlib.Path(__file__).resolve().parent / "finalize_live_sync.py"
  spec = importlib.util.spec_from_file_location("finalize_live_sync", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def receipt_bytes(fields: dict) -> bytes:
  """Return a capture receipt: `fields`, in RECEIPT_FIELDS order, as JSON."""
  validator = _load_finalize()._load_validator()
  receipt = {
      name: fields[name] for name in validator.BQCA_CAPTURE_RECEIPT_FIELDS
  }
  return (json.dumps(receipt, indent=2) + "\n").encode("utf-8")


def sign(receipt: bytes, key: pathlib.Path) -> bytes:
  """Return the signature that `ssh-keygen -Y sign` makes of `receipt`.

  ssh-keygen reads a passphrase, if `key` needs one, from the terminal.

  Raises:
    ValueError: ssh-keygen is missing, fails, or writes no SSH signature.
  """
  ssh_keygen = shutil.which("ssh-keygen")
  if ssh_keygen is None:
    raise ValueError("ssh-keygen (OpenSSH 8.2 or later) is not on PATH")
  sys.stdout.flush()
  sys.stderr.flush()
  result = subprocess.run(
      [
          ssh_keygen,
          "-Y",
          "sign",
          "-f",
          str(key),
          "-n",
          _load_finalize()._load_validator().BQCA_CAPTURE_NAMESPACE,
      ],
      input=receipt,
      stdout=subprocess.PIPE,
      check=False,
  )
  if result.returncode != 0:
    raise ValueError(f"ssh-keygen -Y sign exited {result.returncode}")
  if not result.stdout.startswith(SIGNATURE_HEADER):
    raise ValueError("ssh-keygen -Y sign wrote no SSH signature")
  return result.stdout


def _public_key(key: pathlib.Path) -> str | None:
  """Return `<key type> <public key>` from the `.pub` file of `key`, if any."""
  for path in (key, key.with_name(f"{key.name}.pub")):
    if path.suffix != ".pub":
      continue
    try:
      fields = path.read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError):
      continue
    if len(fields) >= 2:
      return " ".join(fields[:2])
  return None


def _shown(path: pathlib.Path) -> str:
  """Return `path` relative to the working directory, if it is below it."""
  relative = os.path.relpath(path)
  return str(path) if relative.startswith(os.pardir) else relative


def write_blockers(out: pathlib.Path, force: bool) -> list[str]:
  """Return why writing the capture to `out` would be refused."""
  finalize = _load_finalize()
  validator = finalize._load_validator()
  receipt = finalize.receipt_path(out)
  blockers = []
  if finalize._load_applier()._inside_repository(out):
    blockers.append(
        f"{out} is inside this repository, which must not hold a dump of the"
        " live template"
    )
  for path in (out, receipt, validator.bqca_capture_signature_path(receipt)):
    if path.is_dir():
      blockers.append(f"{path} is a directory")
    elif os.path.lexists(path) and not force:
      blockers.append(f"{path} exists; --force replaces it")
  # `resolve()` folds a `missing/..` away, but the write opens `out.parent` as
  # it is spelled, where `missing/..` is not a directory.
  if not (out.resolve().parent.is_dir() and out.parent.is_dir()):
    blockers.append(f"the directory of {out} does not exist")
  return blockers


def _write(path: pathlib.Path, data: bytes, mode: int, force: bool) -> None:
  """Write `data` to `path` with `mode`, atomically.

  The data is written and synced to a staged file next to `path`, which then
  takes its place: with `os.replace` for `force`, else with `os.link`, which
  fails if `path` exists. On any failure the staged file is removed.
  """
  descriptor, staged = tempfile.mkstemp(
      dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
  )
  try:
    with os.fdopen(descriptor, "wb") as handle:
      handle.write(data)
      handle.flush()
      os.fsync(handle.fileno())
    os.chmod(staged, mode)
    if force:
      os.replace(staged, path)
    else:
      os.link(staged, path)
  finally:
    pathlib.Path(staged).unlink(missing_ok=True)


def _fail(errors: list[str]) -> int:
  """Print `errors`, say that nothing was written, and return the exit code."""
  for error in errors:
    print(f"ERROR: {error}", file=sys.stderr)
  print("not captured: nothing written", file=sys.stderr)
  return 1


def main(argv: list[str] | None = None) -> int:
  finalize = _load_finalize()
  validator = finalize._load_validator()
  parser = argparse.ArgumentParser(
      description=__doc__.splitlines()[0],
      usage=(
          "%(prog)s --out PATH --revision REVISION --signer NAME --key PATH"
          " [--before PATH] [--force] -- COMMAND [ARG ...]"
      ),
  )
  parser.add_argument(
      "--out",
      type=pathlib.Path,
      required=True,
      metavar="PATH",
      help=(
          "where to write the export, outside this repository; the receipt"
          " goes to PATH.receipt.json and its signature to"
          " PATH.receipt.json.sig"
      ),
  )
  parser.add_argument(
      "--revision",
      type=finalize.revision_argument,
      required=True,
      metavar="REVISION",
      help="the revision of the live report that the export captures",
  )
  parser.add_argument(
      "--signer",
      type=finalize.signer_argument,
      required=True,
      metavar="NAME",
      help=(
          f"the name that {validator.BQCA_CAPTURE_SIGNERS_PATH} lists for"
          " --key"
      ),
  )
  parser.add_argument(
      "--key",
      type=pathlib.Path,
      required=True,
      metavar="PATH",
      help=(
          "an SSH private key file, or the public key file of a key that"
          " ssh-agent holds"
      ),
  )
  parser.add_argument(
      "--before",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "after the write-back: the capture taken before it, with its"
          " receipt at PATH.receipt.json"
      ),
  )
  parser.add_argument(
      "--force",
      action="store_true",
      help="replace an existing capture at --out",
  )
  # Everything after `--` is the export command, which writes the report
  # textproto of the live template to standard output.
  argv = sys.argv[1:] if argv is None else list(argv)
  command = argv[argv.index("--") + 1 :] if "--" in argv else []
  args = parser.parse_args(argv[: argv.index("--")] if "--" in argv else argv)
  if not command:
    parser.error(
        "give the export command after --; it must write the report"
        " textproto of the live template to standard output"
    )
  now = datetime.datetime.now(datetime.timezone.utc)
  binding = yaml.safe_load((ROOT / BINDING_PATH).read_text(encoding="utf-8"))
  binding = binding if isinstance(binding, dict) else {}
  live = binding.get(finalize.LIVE_KEY)
  live = live if isinstance(live, dict) else {}
  pending = live.get(finalize.PENDING_KEY)
  since = pending.get("since_date") if isinstance(pending, dict) else None
  if not isinstance(since, str):
    return _fail(
        [
            f"{BINDING_PATH} has no {finalize.PENDING_KEY}.since_date, so"
            " there is no live sync to capture"
        ]
    )
  blockers = write_blockers(args.out, args.force)
  try:
    signers = (ROOT / validator.BQCA_CAPTURE_SIGNERS_PATH).read_bytes()
  except OSError as exc:
    signers = b""
    blockers.append(
        f"cannot read {validator.BQCA_CAPTURE_SIGNERS_PATH}:"
        f" {exc.strerror or exc}"
    )
  before = None
  before_fields = None
  if args.before is not None:
    before = finalize.Capture.read(args.before)
    blockers += finalize.before_blockers(None, before, binding, signers, now)
    before_fields = before.receipt.fields()
    if before_fields is not None and before_fields["revision"] == args.revision:
      blockers.append(
          f"--revision {args.revision} is the revision of --before: after the"
          " write-back, the live report has a new revision"
      )
    if before.data is not None:
      try:
        finalize._load_applier().preview_tree(before.data)
      except ValueError as exc:
        blockers.append(
            f"cannot derive the offline preview of --before {args.before}, so"
            f" nothing would tell the export apart from it: {exc}"
        )
  if blockers:
    return _fail(blockers)

  # The export: the bytes it writes to the pipe, never a file's.
  sys.stdout.flush()
  sys.stderr.flush()
  try:
    result = subprocess.run(command, stdout=subprocess.PIPE, check=False)
  except OSError as exc:
    return _fail([f"cannot run the export command: {exc.strerror or exc}"])
  if result.returncode != 0:
    return _fail([f"the export command exited {result.returncode}"])
  export = result.stdout
  if not export:
    return _fail(["the export command wrote nothing to standard output"])
  export_sha256 = hashlib.sha256(export).hexdigest()
  print(f"export: {len(export)} bytes, sha256 {export_sha256}")
  out = args.out.resolve()
  # Before the write-back, the report-level data source is still the one the
  # sync repoints, so only the report ID identifies the live template.
  blockers = finalize.dump_blockers(
      out, export, binding, data_sources=before is not None
  )
  if before is not None and before.data is not None:
    if export == before.data:
      blockers.append(
          "the export is byte for byte the capture before the write-back:"
          " the live report did not change"
      )
    blockers += finalize.offline_preview_blockers(
        out, export, args.before, before.data
    )
  if blockers:
    return _fail(blockers)

  receipt = receipt_bytes(
      {
          "format": validator.BQCA_CAPTURE_RECEIPT_FORMAT,
          "report_id": binding.get("report_id"),
          "revision": args.revision,
          "dump_sha256": export_sha256,
          "signed_at": now.strftime(validator.BQCA_CAPTURE_TIME_FORMAT),
          "signer": args.signer,
          "export_command_sha256": hashlib.sha256(
              json.dumps(command).encode("utf-8")
          ).hexdigest(),
          "manifest_sha256": live.get("manifest_sha256"),
          "template_bundle_sha256": live.get("template_bundle_sha256"),
          "pending_since_date": since,
          "before_dump_sha256": (
              hashlib.sha256(before.data).hexdigest()
              if before is not None and before.data is not None
              else None
          ),
          "before_revision": (
              before_fields["revision"] if before_fields is not None else None
          ),
      }
  )
  try:
    validator.parse_bqca_capture_receipt(receipt)
    signature = sign(receipt, args.key)
  except ValueError as exc:
    return _fail([f"cannot sign the receipt: {exc}"])
  # Write only a receipt that tools/finalize_live_sync.py would trust.
  error = validator.bqca_capture_signature_error(
      receipt, signature, args.signer, signers
  )
  if error is not None:
    line = (
        f'{args.signer} namespaces="{validator.BQCA_CAPTURE_NAMESPACE}"'
        f" {_public_key(args.key) or '<the public key of --key>'}"
    )
    return _fail(
        [
            error,
            "to trust the key, list it in"
            f" {validator.BQCA_CAPTURE_SIGNERS_PATH} in a reviewed change,"
            f" then capture again: {line}",
        ]
    )

  receipt_file = finalize.receipt_path(args.out)
  signature_file = validator.bqca_capture_signature_path(receipt_file)
  shared = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
  written: list[pathlib.Path] = []
  for path, data, mode in (
      (args.out, export, stat.S_IRUSR | stat.S_IWUSR),
      (signature_file, signature, shared),
      (receipt_file, receipt, shared),
  ):
    try:
      _write(path, data, mode, args.force)
    except OSError as exc:
      for done in written:
        done.unlink(missing_ok=True)
      return _fail([f"cannot write {path}: {exc.strerror or exc}"])
    written.append(path)
  print(
      f"captured: {args.out} (mode 600), {receipt_file} and {signature_file}:"
      f" revision {args.revision}, signed by {args.signer} at"
      f" {now.strftime(validator.BQCA_CAPTURE_TIME_FORMAT)}"
  )
  tools = pathlib.Path(__file__).resolve().parent
  if before is None:
    print(
        "next: preview the sync offline with python3"
        f" {_shown(tools / 'apply_bqca_rename_map.py')} --dump {args.out}"
        " --out <preview>, write it back to the live template, then capture"
        f" again with --before {args.out}"
    )
  else:
    print(
        f"next: python3 {_shown(tools / 'finalize_live_sync.py')} --dump"
        f" {args.out} --before {args.before} --revision {args.revision}"
        " --check"
    )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
