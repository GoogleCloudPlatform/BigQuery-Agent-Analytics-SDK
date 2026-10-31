#!/usr/bin/env python3
"""Sign the capture receipt of a fresh export of the live BQCA template.

Run this right after exporting the live template with the internal report
tooling, on the export itself. It reads the dump once and writes, for exactly
those bytes, a capture receipt (``--out``, default ``<dump>.receipt.json``):

    {
      "format": "bqca-live-capture-receipt/v1",
      "report_id": "42e79a1f-a979-4de8-911f-80d5a99543d5",
      "revision": "<--revision>",
      "dump_sha256": "<the SHA-256 of the dump's bytes>",
      "signed_at": "<the UTC time of signing, YYYY-MM-DDTHH:MM:SSZ>",
      "signer": "<--signer>"
    }

and its SSH signature (``<receipt>.sig``), which ``ssh-keygen -Y sign -n
bqca-live-capture-receipt`` makes over the receipt's exact bytes with
``--key``: a private key file, or the public key file of a key that
ssh-agent holds. ``report_id`` is the one in
``bindings/bqca_report_template.yaml``. ``tools/finalize_live_sync.py``
writes the binding only with such a receipt, and only if
``bindings/bqca_live_capture_signers`` lists the key for ``--signer``; this
tool checks that too, and writes nothing otherwise.

It refuses, writing nothing, a dump that ``tools/finalize_live_sync.py``
refuses: one under this repository's ``tests/fixtures``, one that does not
decode as UTF-8, parse as protobuf text format and name the live template in
its identity fields, and one that carries the offline-preview marker of
``tools/apply_bqca_rename_map.py`` in any form. It never writes the receipt or
its signature over the dump, nor over an existing file unless ``--force``.

No tool can tell a fresh export from an edited file. Signing a receipt is the
signer's statement that the dump is an unedited export of the live template
at ``--revision``, and the receipt and the binding name the signer.

Exits 0 when the receipt and its signature are written; 1 when the dump is
refused, the receipt cannot be signed or its key is not trusted, or the files
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


def receipt_bytes(
    report_id: str,
    revision: str,
    dump_sha256: str,
    signed_at: str,
    signer: str,
) -> bytes:
  """Return a capture receipt: its fields, in RECEIPT_FIELDS order, as JSON."""
  finalize = _load_finalize()
  values = (
      finalize.RECEIPT_FORMAT,
      report_id,
      revision,
      dump_sha256,
      signed_at,
      signer,
  )
  receipt = dict(zip(finalize.RECEIPT_FIELDS, values, strict=True))
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
          _load_finalize().RECEIPT_NAMESPACE,
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


def write_blockers(
    dump: pathlib.Path, out: pathlib.Path, force: bool
) -> list[str]:
  """Return why writing the receipt to `out` and its signature is refused.

  `dump` is the resolved path of the dump.
  """
  blockers = []
  for path in (out, _load_finalize().signature_path(out)):
    if path.resolve() == dump or (path.exists() and path.samefile(dump)):
      blockers.append(f"{path} is the dump itself")
    elif path.is_dir():
      blockers.append(f"{path} is a directory")
    elif os.path.lexists(path) and not force:
      blockers.append(f"{path} exists; --force replaces it")
  # `resolve()` folds a `missing/..` away, but the write opens `out.parent` as
  # it is spelled, where `missing/..` is not a directory.
  if not (out.resolve().parent.is_dir() and out.parent.is_dir()):
    blockers.append(f"the directory of {out} does not exist")
  return blockers


def _write(path: pathlib.Path, data: bytes, force: bool) -> None:
  """Write `data` to `path` with mode 0644, atomically.

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
    os.chmod(staged, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
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
  print("not signed: nothing written", file=sys.stderr)
  return 1


def main(argv: list[str] | None = None) -> int:
  finalize = _load_finalize()
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
      "--dump",
      type=pathlib.Path,
      required=True,
      metavar="PATH",
      help="a fresh textproto export of the live BQCA template",
  )
  parser.add_argument(
      "--revision",
      type=finalize.revision_argument,
      required=True,
      metavar="REVISION",
      help="the revision of the live report that the export captured",
  )
  parser.add_argument(
      "--signer",
      type=finalize.signer_argument,
      required=True,
      metavar="NAME",
      help=f"the name that {finalize.SIGNERS_PATH} lists for --key",
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
      "--out",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "where to write the receipt (default: <dump>.receipt.json); its"
          " signature goes to PATH.sig"
      ),
  )
  parser.add_argument(
      "--force",
      action="store_true",
      help="replace an existing receipt and signature",
  )
  args = parser.parse_args(argv)
  out = args.out or args.dump.with_name(f"{args.dump.name}.receipt.json")
  # Read the dump once; the checks and the receipt use only these bytes.
  dump = args.dump.resolve()
  try:
    snapshot = dump.read_bytes()
  except OSError as exc:
    return _fail([f"cannot read {args.dump}: {exc.strerror or exc}"])
  dump_sha256 = hashlib.sha256(snapshot).hexdigest()
  print(f"snapshot of {args.dump}: {len(snapshot)} bytes, sha256 {dump_sha256}")
  binding = yaml.safe_load((ROOT / BINDING_PATH).read_text(encoding="utf-8"))
  binding = binding if isinstance(binding, dict) else {}
  blockers = finalize.dump_blockers(dump, snapshot, binding)
  blockers += write_blockers(dump, out, args.force)
  if blockers:
    return _fail(blockers)

  signed_at = datetime.datetime.now(datetime.timezone.utc).strftime(
      finalize._TIMESTAMP_FORMAT
  )
  receipt = receipt_bytes(
      str(binding.get("report_id")),
      args.revision,
      dump_sha256,
      signed_at,
      args.signer,
  )
  try:
    finalize.parse_receipt(receipt)
    signature = sign(receipt, args.key)
  except ValueError as exc:
    return _fail([f"cannot sign the receipt: {exc}"])
  # Write only a receipt that tools/finalize_live_sync.py would trust.
  error = finalize.receipt_signature_error(receipt, signature, args.signer)
  if error is not None:
    line = (
        f'{args.signer} namespaces="{finalize.RECEIPT_NAMESPACE}"'
        f" {_public_key(args.key) or '<the public key of --key>'}"
    )
    return _fail(
        [
            error,
            f"to trust the key, list it in {finalize.SIGNERS_PATH} in a"
            f" reviewed change, then sign again: {line}",
        ]
    )

  signature_file = finalize.signature_path(out)
  written: list[pathlib.Path] = []
  for path, data in ((signature_file, signature), (out, receipt)):
    try:
      _write(path, data, args.force)
    except OSError as exc:
      for done in written:
        done.unlink(missing_ok=True)
      return _fail([f"cannot write {path}: {exc.strerror or exc}"])
    written.append(path)
  print(
      f"receipt: {out} ({len(receipt)} bytes, sha256"
      f" {hashlib.sha256(receipt).hexdigest()}): revision {args.revision},"
      f" signed by {args.signer} at {signed_at}"
  )
  print(f"signature: {signature_file}")
  print(
      "next: python3 tools/finalize_live_sync.py --dump"
      f" {args.dump} --receipt {out} --revision {args.revision} --check"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
