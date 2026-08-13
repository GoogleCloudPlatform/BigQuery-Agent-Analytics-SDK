#!/usr/bin/env python3
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
"""One-command local Grafana for BigQuery Agent Analytics.

    python3 grafana/run_local.py --project MY_PROJECT --dataset MY_DATASET

does everything the manual setup chain in grafana/README.md does: downloads
a pinned Grafana, installs the pinned BigQuery datasource plugin, provisions
the datasource (Application Default Credentials by default, --sa-key for the
documented JWT path), writes a copy of bqaa-dashboard.json with the six
constant variables filled in, and launches bound to 127.0.0.1 with the
plugin preinstaller disabled. `--stop` tears it down. Everything generated
lives under grafana/.local/ and is disposable; the committed dashboard and
example files are never modified.

Requires only the Python standard library. Views (`adk_*`) are created via
`bq-agent-sdk views create-all` when that CLI is on PATH; otherwise the
command to run is printed and the dashboard will show "No data" until the
views exist.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request

# The BigQuery datasource plugin (>=3.3.1) declares
# grafanaDependency ">=11.6.11-0 <12 || >=12.0.10-0 <12.1 || ...
# || >=12.2.5-0". A stock 11.6.0 fails with an opaque
# "react/jsx-runtime 404" (issue #421); this pin satisfies the
# unbounded ">=12.2.5-0" range. The plugin version is pinned too so a
# future plugin release cannot silently raise the floor above this
# Grafana pin and reintroduce the same failure.
GRAFANA_VERSION = "12.3.0"
PLUGIN_ID = "grafana-bigquery-datasource"
PLUGIN_VERSION = "3.3.1"
DASHBOARD_UID = "bqaa-dashboard"
DATASOURCE_UID = "bqaa-bigquery"
# Matches grafana/datasource.example.yaml: a per-query BigQuery cost cap of
# 100 MB billed. The key spelling is significant (see the example file).
DEFAULT_MAX_BYTES_BILLED = "100000000"

# Identifier rules mirror dashboard/looker_studio/tools/hydrate_dashboard.py
# (the repository's source of truth for BigQuery identifier hygiene).
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$")
VIEW_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# BigQuery locations: multi-regions ("US", "EU") or regions
# ("us-central1", "asia-northeast1").
LOCATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,31}$")
# The two price constants are interpolated into panel arithmetic
# (grafana/README.md warns a Textbox there is an injection risk), so only a
# strict decimal literal is accepted. Same reasoning for the bytes cap.
PRICE_RE = re.compile(r"^\d{1,9}(\.\d{1,9})?$")
BYTES_RE = re.compile(r"^[1-9]\d{0,17}$")

CONSTANT_VARIABLES = (
    "project",
    "dataset",
    "table",
    "view_prefix",
    "price_per_1m_input_tokens",
    "price_per_1m_output_tokens",
)


def require_identifier(label: str, value: str, pattern: re.Pattern) -> str:
  value = str(value).strip()
  if not pattern.fullmatch(value):
    raise ValueError(
        f"{label} {value!r} does not match {pattern.pattern}; refusing to"
        " write it into provisioning or dashboard constants."
    )
  return value


def require_price(label: str, value: str) -> str:
  value = str(value).strip()
  if not PRICE_RE.fullmatch(value):
    raise ValueError(
        f"{label} {value!r} must be a plain decimal like 1.25 (it is"
        " interpolated into panel arithmetic; see grafana/README.md)."
    )
  return value


def require_max_bytes(value: str) -> str:
  value = str(value).strip()
  if not BYTES_RE.fullmatch(value):
    raise ValueError(
        f"max bytes billed {value!r} must be a positive integer (bytes);"
        " to run uncapped, edit the generated datasource YAML deliberately."
    )
  return value


def patch_dashboard(
    dashboard: dict,
    constants: dict,
    time_from: str | None = None,
    time_to: str | None = None,
) -> dict:
  """Returns a copy with the constant variables filled in.

  Only variables of type `constant` are ever written: the dashboard's
  injection-safety contract (constants stay constants) is enforced here,
  not merely documented.
  """
  unknown = set(constants) - set(CONSTANT_VARIABLES)
  if unknown:
    raise ValueError(f"not a patchable constant: {sorted(unknown)}")
  patched = copy.deepcopy(dashboard)
  seen = set()
  for variable in patched.get("templating", {}).get("list", []):
    name = variable.get("name")
    if name not in constants:
      continue
    if variable.get("type") != "constant":
      raise ValueError(
          f"dashboard variable {name!r} is {variable.get('type')!r}, not"
          " constant; refusing to patch a non-constant variable."
      )
    value = constants[name]
    variable["query"] = value
    variable["current"] = {"text": value, "value": value}
    seen.add(name)
  missing = set(constants) - seen
  if missing:
    raise ValueError(f"dashboard has no constant variable: {sorted(missing)}")
  if time_from and time_to:
    patched["time"] = {"from": time_from, "to": time_to}
  return patched


def load_service_account_key(path: Path) -> dict:
  key = json.loads(path.read_text())
  for field in ("client_email", "private_key", "token_uri"):
    if not key.get(field):
      raise ValueError(f"service-account key {path} is missing {field!r}")
  return key


def render_datasource_yaml(
    default_project: str,
    sa_key: dict | None = None,
    processing_location: str | None = None,
    max_bytes_billed: str = DEFAULT_MAX_BYTES_BILLED,
) -> str:
  """Provisioning YAML for the BigQuery datasource.

  With no key: `gce` authentication, which the plugin resolves through
  Application Default Credentials when Grafana runs off-GCE — no
  service-account key needed for local evaluation. With a key: the JWT path
  documented in grafana/README.md, with the private key as a real YAML
  block scalar (never literal \\n escapes).

  `processingLocation` is omitted by default so the plugin selects the job
  location automatically; hard-coding a multi-region breaks datasets that
  live anywhere else. `MaxBytesBilled` preserves the per-query cost cap
  from grafana/datasource.example.yaml.
  """
  header = (
      "apiVersion: 1\n"
      "datasources:\n"
      "  - name: BigQuery\n"
      f"    type: {PLUGIN_ID}\n"
      f"    uid: {DATASOURCE_UID}\n"
      "    access: proxy\n"
      "    isDefault: true\n"
      "    jsonData:\n"
  )
  location_line = (
      f"      processingLocation: {processing_location}\n"
      if processing_location
      else ""
  )
  guard_line = f"      MaxBytesBilled: {max_bytes_billed}\n"
  if sa_key is None:
    return header + (
        "      authenticationType: gce\n"
        f"      defaultProject: {default_project}\n"
        + location_line
        + guard_line
    )
  private_key = "".join(
      f"        {line}\n" for line in sa_key["private_key"].splitlines()
  )
  return header + (
      "      authenticationType: jwt\n"
      f"      clientEmail: {sa_key['client_email']}\n"
      f"      defaultProject: {default_project}\n"
      f"      tokenUri: {sa_key['token_uri']}\n"
      + location_line
      + guard_line
      + "    secureJsonData:\n"
      "      privateKey: |\n"
      f"{private_key}"
  )


def render_dashboards_yaml(dashboards_dir: Path) -> str:
  return (
      "apiVersion: 1\n"
      "providers:\n"
      "  - name: bqaa\n"
      "    type: file\n"
      "    options:\n"
      f"      path: {dashboards_dir}\n"
  )


def write_private(path: Path, text: str) -> None:
  """Writes a credential-bearing file atomically with mode 0600.

  The parent directory is tightened to 0700 first so no window exists in
  which another local account can list or read the provisioning secrets
  (the JWT path copies a service-account private key into this file).
  """
  os.chmod(path.parent, 0o700)
  tmp = path.with_suffix(path.suffix + ".tmp")
  descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  try:
    with os.fdopen(descriptor, "w") as handle:
      handle.write(text)
  except Exception:
    tmp.unlink(missing_ok=True)
    raise
  os.replace(tmp, path)


def launch_env(workdir: Path, provisioning: Path, port: int) -> dict:
  """Environment for the Grafana process. Pure, so tests can pin it.

  GF_SERVER_HTTP_ADDR is 127.0.0.1: Grafana's default (empty) http_addr
  binds every interface, which would expose an admin/admin instance backed
  by the operator's own credentials to the local network.
  GF_PLUGINS_PREINSTALL_DISABLED avoids the failed-preinstall import-map
  poisoning that breaks all plugin loading (issue #421).
  """
  return dict(
      os.environ,
      GF_PATHS_DATA=str(workdir / "data"),
      GF_PATHS_PLUGINS=str(workdir / "plugins"),
      GF_PATHS_PROVISIONING=str(provisioning),
      GF_SERVER_HTTP_ADDR="127.0.0.1",
      GF_SERVER_HTTP_PORT=str(port),
      GF_ANALYTICS_REPORTING_ENABLED="false",
      GF_PLUGINS_PREINSTALL_DISABLED="true",
  )


def build_views_command(
    project: str, dataset: str, table: str, prefix: str
) -> list:
  return [
      "bq-agent-sdk",
      "views",
      "create-all",
      "--project-id",
      project,
      "--dataset-id",
      dataset,
      "--table-id",
      table,
      "--prefix",
      prefix,
  ]


def build_plugin_install_command(home: Path, plugins_dir: Path) -> list:
  return [
      str(home / "bin" / "grafana"),
      "cli",
      "--homepath",
      str(home),
      "--pluginsDir",
      str(plugins_dir),
      "plugins",
      "install",
      PLUGIN_ID,
      PLUGIN_VERSION,
  ]


def pick_dist(system: str | None = None, machine: str | None = None) -> str:
  system = (system or platform.system()).lower()
  machine = (machine or platform.machine()).lower()
  arch = {
      "arm64": "arm64",
      "aarch64": "arm64",
      "x86_64": "amd64",
      "amd64": "amd64",
  }.get(machine)
  if system not in ("darwin", "linux") or arch is None:
    raise ValueError(
        f"unsupported platform {system}/{machine}: this launcher covers"
        " macOS and Linux on amd64/arm64; on other platforms follow the"
        " manual steps in grafana/README.md."
    )
  return f"{system}-{arch}"


def port_free(port: int) -> bool:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    return probe.connect_ex(("127.0.0.1", port)) != 0


def sha256_of(path: Path) -> str:
  digest = hashlib.sha256()
  with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def verify_sha256(archive: Path, expected: str) -> None:
  expected = expected.strip().split()[0].lower()
  actual = sha256_of(archive)
  if actual != expected:
    raise RuntimeError(
        f"SHA256 mismatch for {archive}: expected {expected}, got {actual};"
        " delete the file and retry."
    )


def _validated_members(tar: tarfile.TarFile, dest: Path) -> list:
  """Containment-checked member list for interpreters without tar filters.

  Mirrors the intent of the stdlib "data" filter: only regular files and
  directories, no absolute paths, no `..` traversal, no links, no special
  files. Anything else is rejected loudly rather than skipped, so a
  tampered archive cannot partially extract.
  """
  dest = dest.resolve()
  safe = []
  for member in tar.getmembers():
    if not (member.isreg() or member.isdir()):
      raise RuntimeError(
          f"refusing to extract {member.name!r}: only regular files and"
          " directories are allowed."
      )
    name = member.name
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
      raise RuntimeError(f"refusing to extract absolute path {name!r}.")
    target = (dest / name).resolve()
    if dest != target and dest not in target.parents:
      raise RuntimeError(
          f"refusing to extract {name!r}: it escapes the destination."
      )
    safe.append(member)
  return safe


def _extract_without_filter(tar: tarfile.TarFile, dest: Path) -> None:
  members = _validated_members(tar, dest)
  if "filter" in inspect.signature(tar.extractall).parameters:
    # Belt and braces where the stdlib filter exists (this path still runs
    # under tests on modern interpreters); legacy interpreters rely on the
    # explicit validation above.
    tar.extractall(dest, members=members, filter="data")
  else:
    tar.extractall(dest, members=members)


def _extractall(tar: tarfile.TarFile, dest: Path) -> None:
  # The "data" filter exists on 3.12+ and the 3.10.12/3.11.4 security
  # backports; older supported patch releases fall back to an explicit
  # containment check — never to unrestricted extraction.
  if "filter" in inspect.signature(tar.extractall).parameters:
    tar.extractall(dest, filter="data")
  else:
    _extract_without_filter(tar, dest)


def ensure_grafana(
    workdir: Path, archive: Path | None, archive_sha256: str | None = None
) -> Path:
  """Downloads (or reuses) and extracts the pinned Grafana; returns homepath.

  Downloads are verified against the .sha256 published alongside the
  release tarball (integrity against corruption/truncation; it shares the
  origin, so pass --grafana-sha256 for an independently pinned hash).
  A verified extraction records its archive hash in .provenance.json; an
  explicitly supplied hash is always honored — a cached extraction whose
  provenance disagrees is discarded and rebuilt, never silently reused.
  """
  extracted = workdir / f"grafana-home-{GRAFANA_VERSION}"
  provenance_file = extracted / ".provenance.json"
  if (extracted / "bin").is_dir():
    if archive_sha256 is None:
      return extracted
    expected = archive_sha256.strip().split()[0].lower()
    recorded = None
    if provenance_file.exists():
      recorded = json.loads(provenance_file.read_text()).get("sha256")
    if recorded == expected:
      return extracted
    print(
        f"cached extraction provenance ({recorded}) does not match the"
        f" requested SHA256 ({expected}); rebuilding from the archive."
    )
    shutil.rmtree(extracted)
  if archive is None:
    dist = pick_dist()
    archive = workdir / f"grafana-{GRAFANA_VERSION}.{dist}.tar.gz"
    url = (
        "https://dl.grafana.com/oss/release/"
        f"grafana-{GRAFANA_VERSION}.{dist}.tar.gz"
    )
    if not archive.exists():
      print(f"downloading {url} (~250 MB, cached for next time)")
      with urllib.request.urlopen(url) as response:
        partial = archive.with_suffix(".partial")
        with open(partial, "wb") as out:
          shutil.copyfileobj(response, out)
        partial.rename(archive)
    if archive_sha256 is None:
      with urllib.request.urlopen(url + ".sha256") as response:
        archive_sha256 = response.read().decode()
  if archive_sha256:
    verify_sha256(archive, archive_sha256)
  else:
    print(
        f"note: no checksum given for {archive}; pass --grafana-sha256 to"
        " verify a locally supplied archive."
    )
  staging = workdir / "extract-staging"
  if staging.exists():
    shutil.rmtree(staging)
  staging.mkdir(parents=True)
  with tarfile.open(archive) as tar:
    _extractall(tar, staging)
  roots = [p for p in staging.iterdir() if p.is_dir()]
  if len(roots) != 1:
    raise RuntimeError(f"unexpected archive layout: {roots}")
  roots[0].rename(extracted)
  staging.rmdir()
  provenance_file.write_text(
      json.dumps({"sha256": sha256_of(archive), "source": str(archive)})
  )
  return extracted


def plugin_needs_install(plugins_dir: Path) -> bool:
  """True unless a cached plugin manifest matches the pinned version.

  A cached directory with a different (or unreadable) manifest would
  silently bypass the version pin across reruns, so mismatches are
  reinstalled rather than trusted.
  """
  manifest = plugins_dir / PLUGIN_ID / "plugin.json"
  if not manifest.exists():
    return True
  try:
    version = json.loads(manifest.read_text())["info"]["version"]
  except (ValueError, KeyError, TypeError):
    return True
  return version != PLUGIN_VERSION


def install_plugin(home: Path, plugins_dir: Path) -> None:
  if not plugin_needs_install(plugins_dir):
    return
  cached = plugins_dir / PLUGIN_ID
  if cached.exists():
    print(f"cached plugin does not match the {PLUGIN_VERSION} pin;"
          " reinstalling.")
    shutil.rmtree(cached)
  subprocess.run(
      build_plugin_install_command(home, plugins_dir), check=True, cwd=home
  )


def maybe_create_views(
    project: str, dataset: str, table: str, prefix: str
) -> None:
  command = build_views_command(project, dataset, table, prefix)
  if shutil.which("bq-agent-sdk"):
    print("creating typed views (idempotent):", " ".join(command))
    subprocess.run(command, check=True)
  else:
    print(
        "bq-agent-sdk not on PATH — the dashboard reads prefixed views, so"
        " before expecting data run:\n  pip install"
        " bigquery-agent-analytics && " + " ".join(command)
    )


def _probe_process(pid: int) -> tuple | None:
  """Returns (start_time, command) for a live pid, or None."""
  try:
    listing = subprocess.run(
        # -ww: unlimited width on both procps and BSD ps — without it,
        # Linux truncates the command at ~80 columns, cutting long
        # --homepath values and making live instances look stale.
        ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
  except subprocess.CalledProcessError:
    return None
  if not listing:
    return None
  # lstart is a fixed-width 24-char timestamp ("Tue Aug 12 09:00:00 2026").
  return listing[:24].strip(), listing[24:].strip()


def _identity_matches(record: dict, start: str, command: str) -> bool:
  """Strong launch identity: exact executable, homepath arg, start time.

  A substring check alone would let --stop signal any process that merely
  mentions the home path in an argument, and PID reuse would defeat a
  command-only check — the start time recorded at launch guards that.
  """
  expected_exe = record["exe"]
  if not (command == expected_exe or command.startswith(expected_exe + " ")):
    return False
  if f"--homepath {record['home']}" not in command:
    return False
  return start == record["start"]


def make_stop_hint(workdir: Path) -> str:
  hint = f"python3 {Path(__file__).name} --stop"
  if workdir != (Path(__file__).parent / ".local").resolve():
    hint += f" --workdir {workdir}"
  return hint


def acquire_launch_lock(workdir: Path):
  """Exclusive, non-blocking ownership of the workdir's launch lifecycle.

  live_instance() alone is a check-then-act race: two overlapping launches
  can both observe no pidfile, and the loser's record overwrite would
  orphan the winner's Grafana. The flock is taken before the liveness
  check and held through process launch and pidfile publication, so
  exactly one contender may own the sequence. Returns the open lock file
  (keep it referenced until publication is done) or None if another
  launcher holds it.
  """
  workdir.mkdir(parents=True, exist_ok=True)
  lock_file = open(workdir / "launch.lock", "w")
  try:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except OSError:
    lock_file.close()
    return None
  return lock_file


def live_instance(workdir: Path) -> dict | None:
  """Returns the pidfile record if it still identifies a live launcher-owned
  Grafana; removes a stale pidfile and returns None otherwise.

  A workdir holds exactly one pidfile, so a second launch that overwrote it
  would orphan the first instance: --stop would tear down only the newer
  process and report success while an admin/admin Grafana backed by the
  operator's credentials kept running with no record left to stop it.
  """
  pidfile = workdir / "grafana.pid"
  if not pidfile.exists():
    return None
  record = json.loads(pidfile.read_text())
  probe = _probe_process(int(record["pid"]))
  if probe is not None and _identity_matches(record, *probe):
    return record
  pidfile.unlink()
  return None


def stop(workdir: Path) -> int:
  lock = acquire_launch_lock(workdir)
  if lock is None:
    print(
        "a launch is in progress in this workdir; retry --stop once it"
        " finishes.",
        file=sys.stderr,
    )
    return 1
  pidfile = workdir / "grafana.pid"
  if not pidfile.exists():
    print(f"nothing to stop (no {pidfile})")
    return 0
  record = json.loads(pidfile.read_text())
  pid = int(record["pid"])

  def matches() -> bool:
    probe = _probe_process(pid)
    return probe is not None and _identity_matches(record, *probe)

  if not matches():
    print(
        f"pid {pid} is not this launcher's grafana anymore (stale pidfile"
        " or reused pid); not sending any signal."
    )
    pidfile.unlink()
    return 0
  os.kill(pid, signal.SIGTERM)
  deadline = time.monotonic() + 15
  while time.monotonic() < deadline:
    if not matches():
      print(f"stopped grafana (pid {pid})")
      pidfile.unlink()
      return 0
    time.sleep(0.5)
  print(f"grafana (pid {pid}) did not exit within 15s", file=sys.stderr)
  return 1


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
  )
  parser.add_argument("--project", help="GCP project holding the BQAA table")
  parser.add_argument("--dataset", help="BigQuery dataset ID")
  parser.add_argument("--table", default="agent_events")
  parser.add_argument("--view-prefix", default="adk_")
  parser.add_argument("--input-price", default="1.25")
  parser.add_argument("--output-price", default="5.00")
  parser.add_argument("--port", type=int, default=3000)
  parser.add_argument(
      "--processing-location",
      help="BigQuery job location (e.g. EU, us-central1); default lets the"
      " plugin select automatically",
  )
  parser.add_argument(
      "--max-bytes-billed",
      default=DEFAULT_MAX_BYTES_BILLED,
      help="per-query BigQuery cost cap in bytes (default 100000000, matching"
      " grafana/datasource.example.yaml)",
  )
  parser.add_argument(
      "--sa-key",
      type=Path,
      help="service-account key JSON for the documented JWT auth path;"
      " default is Application Default Credentials (no key)",
  )
  parser.add_argument(
      "--grafana-archive",
      type=Path,
      help="reuse a local grafana tarball instead of downloading",
  )
  parser.add_argument(
      "--grafana-sha256",
      help="expected SHA256 of the grafana archive (downloads verify against"
      " the published .sha256 automatically)",
  )
  parser.add_argument(
      "--time-from", help="dashboard default range start (ISO), e.g. for demos"
  )
  parser.add_argument("--time-to", help="dashboard default range end (ISO)")
  parser.add_argument(
      "--skip-views", action="store_true", help="do not run views create-all"
  )
  parser.add_argument(
      "--provision-only",
      action="store_true",
      help="write provisioning + patched dashboard, then exit (no download,"
      " no launch)",
  )
  parser.add_argument("--stop", action="store_true", help="stop and exit")
  parser.add_argument(
      "--workdir", type=Path, default=Path(__file__).parent / ".local"
  )
  args = parser.parse_args(argv)

  workdir = args.workdir.resolve()
  if args.stop:
    return stop(workdir)
  launch_lock = acquire_launch_lock(workdir)
  if launch_lock is None:
    print(
        "another launch is already in progress in this workdir; wait for it"
        " or use a different --workdir.",
        file=sys.stderr,
    )
    return 1
  # Test-only seam: lets the concurrency regression widen the race window
  # deterministically. Has no effect unless the variable is set.
  test_hold = float(os.environ.get("_BQAA_RUN_LOCAL_TEST_HOLD_LOCK", 0) or 0)
  if test_hold:
    time.sleep(test_hold)
  running = live_instance(workdir)
  if running is not None:
    print(
        f"a launcher-owned grafana is already running from this workdir"
        f" (pid {running['pid']}, port {running.get('port', '?')});"
        f" stop it first with: {make_stop_hint(workdir)}",
        file=sys.stderr,
    )
    return 1
  if not args.project or not args.dataset:
    parser.error("--project and --dataset are required (except with --stop)")
  if bool(args.time_from) != bool(args.time_to):
    parser.error("--time-from and --time-to must be given together")

  try:
    constants = {
        "project": require_identifier("project", args.project, PROJECT_RE),
        "dataset": require_identifier("dataset", args.dataset, DATASET_RE),
        "table": require_identifier("table", args.table, TABLE_RE),
        "view_prefix": require_identifier(
            "view prefix", args.view_prefix, VIEW_PREFIX_RE
        ),
        "price_per_1m_input_tokens": require_price(
            "input price", args.input_price
        ),
        "price_per_1m_output_tokens": require_price(
            "output price", args.output_price
        ),
    }
    location = (
        require_identifier(
            "processing location", args.processing_location, LOCATION_RE
        )
        if args.processing_location
        else None
    )
    max_bytes = require_max_bytes(args.max_bytes_billed)
  except ValueError as error:
    parser.error(str(error))

  workdir.mkdir(parents=True, exist_ok=True)
  provisioning = workdir / "provisioning"
  dashboards_dir = workdir / "dashboards"
  for sub in (provisioning / "datasources", provisioning / "dashboards",
              dashboards_dir, workdir / "data", workdir / "plugins"):
    sub.mkdir(parents=True, exist_ok=True)

  sa_key = load_service_account_key(args.sa_key) if args.sa_key else None
  datasource_yaml = render_datasource_yaml(
      constants["project"],
      sa_key,
      processing_location=location,
      max_bytes_billed=max_bytes,
  )
  write_private(
      provisioning / "datasources" / "bigquery.yaml", datasource_yaml
  )
  (provisioning / "dashboards" / "bqaa.yaml").write_text(
      render_dashboards_yaml(dashboards_dir)
  )

  source = Path(__file__).parent / "bqaa-dashboard.json"
  patched = patch_dashboard(
      json.loads(source.read_text()),
      constants,
      time_from=args.time_from,
      time_to=args.time_to,
  )
  (dashboards_dir / "bqaa-dashboard.json").write_text(
      json.dumps(patched, indent=1)
  )
  print(f"provisioning written under {workdir}")
  if args.provision_only:
    return 0

  if not port_free(args.port):
    print(
        f"port {args.port} is already in use (another Grafana?); pass"
        " --port to pick a free one.",
        file=sys.stderr,
    )
    return 1

  if not args.skip_views:
    maybe_create_views(
        constants["project"],
        constants["dataset"],
        constants["table"],
        constants["view_prefix"],
    )

  home = ensure_grafana(workdir, args.grafana_archive, args.grafana_sha256)
  install_plugin(home, workdir / "plugins")

  env = launch_env(workdir, provisioning, args.port)
  log = workdir / "grafana.log"
  with open(log, "ab") as log_handle:
    process = subprocess.Popen(
        [str(home / "bin" / "grafana"), "server", "--homepath", str(home)],
        cwd=home,
        env=env,
        stdout=log_handle,
        stderr=log_handle,
    )
  probe = _probe_process(process.pid)
  (workdir / "grafana.pid").write_text(
      json.dumps({
          "pid": process.pid,
          "home": str(home),
          "exe": str(home / "bin" / "grafana"),
          "start": probe[0] if probe else "",
          "port": args.port,
      })
  )

  deadline = time.monotonic() + 90
  url = f"http://127.0.0.1:{args.port}"
  while time.monotonic() < deadline:
    if process.poll() is not None:
      print(f"grafana exited early; see {log}", file=sys.stderr)
      return 1
    try:
      with urllib.request.urlopen(f"{url}/api/health", timeout=2) as response:
        if response.status == 200:
          break
    except OSError:
      pass
    time.sleep(1)
  else:
    print(f"grafana did not become healthy in 90s; see {log}", file=sys.stderr)
    return 1

  stop_hint = make_stop_hint(workdir)
  print(
      f"\nGrafana is up (bound to 127.0.0.1 only): {url}/d/{DASHBOARD_UID}\n"
      "  login: admin / admin (fresh instance; it will offer a password"
      " change)\n"
      "  panels query on first view; allow a few seconds per row.\n"
      f"  stop with: {stop_hint}\n"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
