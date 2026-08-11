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
a pinned Grafana, installs the BigQuery datasource plugin, provisions the
datasource (Application Default Credentials by default, --sa-key for the
documented JWT path), writes a copy of bqaa-dashboard.json with the six
constant variables filled in, and launches with the plugin preinstaller
disabled. `--stop` tears it down. Everything generated lives under
grafana/.local/ and is disposable; the committed dashboard and example
files are never modified.

Requires only the Python standard library. Views (`adk_*`) are created via
`bq-agent-sdk views create-all` when that CLI is on PATH; otherwise the
command to run is printed and the dashboard will show "No data" until the
views exist.
"""

from __future__ import annotations

import argparse
import copy
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
# unbounded ">=12.2.5-0" range.
GRAFANA_VERSION = "12.3.0"
PLUGIN_ID = "grafana-bigquery-datasource"
DASHBOARD_UID = "bqaa-dashboard"
DATASOURCE_UID = "bqaa-bigquery"

# Identifier rules mirror dashboard/looker_studio/tools/hydrate_dashboard.py
# (the repository's source of truth for BigQuery identifier hygiene).
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$")
VIEW_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# The two price constants are interpolated into panel arithmetic
# (grafana/README.md warns a Textbox there is an injection risk), so only a
# strict decimal literal is accepted.
PRICE_RE = re.compile(r"^\d{1,9}(\.\d{1,9})?$")

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
    default_project: str, sa_key: dict | None = None
) -> str:
  """Provisioning YAML for the BigQuery datasource.

  With no key: `gce` authentication, which the plugin resolves through
  Application Default Credentials when Grafana runs off-GCE — no
  service-account key needed for local evaluation. With a key: the JWT path
  documented in grafana/README.md, with the private key as a real YAML
  block scalar (never literal \\n escapes).
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
  if sa_key is None:
    return header + (
        "      authenticationType: gce\n"
        f"      defaultProject: {default_project}\n"
        "      processingLocation: US\n"
    )
  private_key = "".join(
      f"        {line}\n" for line in sa_key["private_key"].splitlines()
  )
  return header + (
      "      authenticationType: jwt\n"
      f"      clientEmail: {sa_key['client_email']}\n"
      f"      defaultProject: {default_project}\n"
      f"      tokenUri: {sa_key['token_uri']}\n"
      "      processingLocation: US\n"
      "    secureJsonData:\n"
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


def ensure_grafana(workdir: Path, archive: Path | None) -> Path:
  """Downloads (or reuses) and extracts the pinned Grafana; returns homepath."""
  extracted = workdir / f"grafana-home-{GRAFANA_VERSION}"
  if (extracted / "bin").is_dir():
    return extracted
  if archive is None:
    dist = pick_dist()
    archive = workdir / f"grafana-{GRAFANA_VERSION}.{dist}.tar.gz"
    if not archive.exists():
      url = (
          "https://dl.grafana.com/oss/release/"
          f"grafana-{GRAFANA_VERSION}.{dist}.tar.gz"
      )
      print(f"downloading {url} (~250 MB, cached for next time)")
      with urllib.request.urlopen(url) as response:
        partial = archive.with_suffix(".partial")
        with open(partial, "wb") as out:
          shutil.copyfileobj(response, out)
        partial.rename(archive)
  staging = workdir / "extract-staging"
  if staging.exists():
    shutil.rmtree(staging)
  staging.mkdir(parents=True)
  with tarfile.open(archive) as tar:
    tar.extractall(staging, filter="data")
  roots = [p for p in staging.iterdir() if p.is_dir()]
  if len(roots) != 1:
    raise RuntimeError(f"unexpected archive layout: {roots}")
  roots[0].rename(extracted)
  staging.rmdir()
  return extracted


def install_plugin(home: Path, plugins_dir: Path) -> None:
  if (plugins_dir / PLUGIN_ID / "plugin.json").exists():
    return
  subprocess.run(
      [
          str(home / "bin" / "grafana"),
          "cli",
          "--homepath",
          str(home),
          "--pluginsDir",
          str(plugins_dir),
          "plugins",
          "install",
          PLUGIN_ID,
      ],
      check=True,
      cwd=home,
  )


def maybe_create_views(project: str, dataset: str, table: str) -> None:
  command = [
      "bq-agent-sdk",
      "views",
      "create-all",
      "--project-id",
      project,
      "--dataset-id",
      dataset,
      "--table-id",
      table,
  ]
  if shutil.which("bq-agent-sdk"):
    print("creating adk_* views (idempotent):", " ".join(command))
    subprocess.run(command, check=True)
  else:
    print(
        "bq-agent-sdk not on PATH — the dashboard reads adk_* views, so"
        " before expecting data run:\n  pip install"
        " bigquery-agent-analytics && " + " ".join(command)
    )


def stop(workdir: Path) -> int:
  pidfile = workdir / "grafana.pid"
  if not pidfile.exists():
    print(f"nothing to stop (no {pidfile})")
    return 0
  pid = int(pidfile.read_text().strip())
  try:
    os.kill(pid, signal.SIGTERM)
    print(f"stopped grafana (pid {pid})")
  except ProcessLookupError:
    print(f"grafana (pid {pid}) was not running")
  pidfile.unlink()
  return 0


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
  if not args.project or not args.dataset:
    parser.error("--project and --dataset are required (except with --stop)")

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
  except ValueError as error:
    parser.error(str(error))

  workdir.mkdir(parents=True, exist_ok=True)
  provisioning = workdir / "provisioning"
  dashboards_dir = workdir / "dashboards"
  for sub in (provisioning / "datasources", provisioning / "dashboards",
              dashboards_dir, workdir / "data", workdir / "plugins"):
    sub.mkdir(parents=True, exist_ok=True)

  sa_key = load_service_account_key(args.sa_key) if args.sa_key else None
  datasource_yaml = render_datasource_yaml(constants["project"], sa_key)
  (provisioning / "datasources" / "bigquery.yaml").write_text(datasource_yaml)
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
        constants["project"], constants["dataset"], constants["table"]
    )

  home = ensure_grafana(workdir, args.grafana_archive)
  install_plugin(home, workdir / "plugins")

  env = dict(
      os.environ,
      GF_PATHS_DATA=str(workdir / "data"),
      GF_PATHS_PLUGINS=str(workdir / "plugins"),
      GF_PATHS_PROVISIONING=str(provisioning),
      GF_SERVER_HTTP_PORT=str(args.port),
      GF_ANALYTICS_REPORTING_ENABLED="false",
      # A failed background preinstall can break ALL plugin frontend
      # loading with an opaque react/jsx-runtime 404 (issue #421).
      GF_PLUGINS_PREINSTALL_DISABLED="true",
  )
  log = workdir / "grafana.log"
  with open(log, "ab") as log_handle:
    process = subprocess.Popen(
        [str(home / "bin" / "grafana"), "server", "--homepath", str(home)],
        cwd=home,
        env=env,
        stdout=log_handle,
        stderr=log_handle,
    )
  (workdir / "grafana.pid").write_text(str(process.pid))

  deadline = time.monotonic() + 90
  url = f"http://localhost:{args.port}"
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

  print(
      f"\nGrafana is up: {url}/d/{DASHBOARD_UID}\n"
      "  login: admin / admin (fresh instance; it will offer a password"
      " change)\n"
      "  panels query on first view; allow a few seconds per row.\n"
      f"  stop with: python3 {Path(__file__).name} --stop\n"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
