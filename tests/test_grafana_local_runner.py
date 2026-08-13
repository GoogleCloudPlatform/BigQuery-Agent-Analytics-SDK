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

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "grafana" / "run_local.py"


def _load_runner():
  spec = importlib.util.spec_from_file_location("grafana_run_local", RUNNER)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


runner = _load_runner()


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("customer-project-123", runner.PROJECT_RE),
        ("agent_analytics", runner.DATASET_RE),
        ("agent_events", runner.TABLE_RE),
        ("events_agent_cur-phenix", runner.TABLE_RE),
        ("adk_", runner.VIEW_PREFIX_RE),
    ],
)
def test_identifiers_accepted(value, pattern):
  assert runner.require_identifier("x", value, pattern) == value


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("UPPERCASE", runner.PROJECT_RE),
        ("short", runner.PROJECT_RE),
        ("project;drop", runner.PROJECT_RE),
        ("bad-dataset", runner.DATASET_RE),
        ("data`set", runner.DATASET_RE),
        ("table,other", runner.TABLE_RE),
        ("pre fix", runner.VIEW_PREFIX_RE),
    ],
)
def test_identifiers_rejected(value, pattern):
  with pytest.raises(ValueError):
    runner.require_identifier("x", value, pattern)


@pytest.mark.parametrize("value", ["1.25", "5.00", "0", "12"])
def test_prices_accepted(value):
  assert runner.require_price("x", value) == value


@pytest.mark.parametrize(
    "value", ["1e3", "-1", "abc", "1.25; DROP", "1/0", "", "NaN", ".5"]
)
def test_prices_rejected(value):
  # These constants are interpolated into panel arithmetic, so anything
  # that is not a plain decimal must fail closed.
  with pytest.raises(ValueError):
    runner.require_price("x", value)


def _real_dashboard():
  return json.loads((ROOT / "grafana" / "bqaa-dashboard.json").read_text())


def test_patch_dashboard_fills_all_six_constants():
  constants = {
      "project": "customer-project-123",
      "dataset": "agent_analytics",
      "table": "agent_events",
      "view_prefix": "adk_",
      "price_per_1m_input_tokens": "1.25",
      "price_per_1m_output_tokens": "5.00",
  }
  patched = runner.patch_dashboard(_real_dashboard(), constants)
  by_name = {v["name"]: v for v in patched["templating"]["list"]}
  for name, value in constants.items():
    assert by_name[name]["type"] == "constant"
    assert by_name[name]["query"] == value
    assert by_name[name]["current"] == {"text": value, "value": value}


def test_patch_dashboard_leaves_query_variables_untouched():
  original = _real_dashboard()
  patched = runner.patch_dashboard(original, {"project": "customer-p-123"})
  originals = {v["name"]: v for v in original["templating"]["list"]}
  for variable in patched["templating"]["list"]:
    if variable["name"] != "project":
      assert variable == originals[variable["name"]]


def test_patch_dashboard_refuses_non_constant_targets():
  # "agent" is a query variable; writing it would break the dashboard's
  # constants-stay-constants injection-safety contract.
  with pytest.raises(ValueError, match="not a patchable constant"):
    runner.patch_dashboard(_real_dashboard(), {"agent": "billing-agent"})


def test_patch_dashboard_refuses_type_mismatch():
  dashboard = _real_dashboard()
  for variable in dashboard["templating"]["list"]:
    if variable["name"] == "project":
      variable["type"] = "textbox"
  with pytest.raises(ValueError, match="not\\s+constant"):
    runner.patch_dashboard(dashboard, {"project": "customer-p-123"})


def test_patch_dashboard_optional_time_window():
  patched = runner.patch_dashboard(
      _real_dashboard(),
      {"project": "customer-p-123"},
      time_from="2026-06-15T00:00:00Z",
      time_to="2026-08-11T00:00:00Z",
  )
  assert patched["time"] == {
      "from": "2026-06-15T00:00:00Z",
      "to": "2026-08-11T00:00:00Z",
  }
  untouched = runner.patch_dashboard(
      _real_dashboard(), {"project": "customer-p-123"}
  )
  assert untouched["time"] == _real_dashboard()["time"]


def test_datasource_yaml_defaults_to_adc():
  rendered = runner.render_datasource_yaml("customer-project-123")
  assert "authenticationType: gce" in rendered
  assert "defaultProject: customer-project-123" in rendered
  assert "privateKey" not in rendered


def test_datasource_yaml_jwt_uses_block_scalar(tmp_path):
  key_path = tmp_path / "key.json"
  key_path.write_text(
      json.dumps(
          {
              "client_email": "grafana@customer-project-123.iam.example.com",
              "private_key": (
                  "-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB\n-----END PRIVATE"
                  " KEY-----\n"
              ),
              "token_uri": "https://oauth2.example.com/token",
          }
      )
  )
  key = runner.load_service_account_key(key_path)
  rendered = runner.render_datasource_yaml("customer-project-123", key)
  assert "authenticationType: jwt" in rendered
  assert "clientEmail: grafana@customer-project-123.iam.example.com" in (
      rendered
  )
  # Real line breaks under a block scalar, never literal \n escapes
  # (grafana/README.md calls this out explicitly).
  assert "privateKey: |" in rendered
  assert "        -----BEGIN PRIVATE KEY-----\n        AAAA\n" in rendered
  assert "\\n" not in rendered


def test_service_account_key_must_be_complete(tmp_path):
  key_path = tmp_path / "key.json"
  key_path.write_text(json.dumps({"client_email": "x@example.com"}))
  with pytest.raises(ValueError, match="private_key"):
    runner.load_service_account_key(key_path)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "x86_64", "linux-amd64"),
    ],
)
def test_pick_dist(system, machine, expected):
  assert runner.pick_dist(system, machine) == expected


def test_pick_dist_rejects_unsupported():
  with pytest.raises(ValueError, match="manual steps"):
    runner.pick_dist("Windows", "AMD64")


def test_grafana_pin_satisfies_plugin_floor():
  # The plugin's grafanaDependency ranges end unbounded at >=12.2.5-0
  # (issue #421); a pin below 12.2.5 would reintroduce the opaque
  # react/jsx-runtime failure on fresh installs.
  major, minor, patch = (int(x) for x in runner.GRAFANA_VERSION.split("."))
  assert (major, minor, patch) >= (12, 2, 5)


def test_provision_only_writes_everything_without_network(tmp_path):
  result = subprocess.run(
      [
          sys.executable,
          str(RUNNER),
          "--project",
          "customer-project-123",
          "--dataset",
          "agent_analytics",
          "--provision-only",
          "--workdir",
          str(tmp_path),
      ],
      capture_output=True,
      text=True,
      check=True,
  )
  assert "provisioning written" in result.stdout
  datasource = (tmp_path / "provisioning/datasources/bigquery.yaml").read_text()
  assert "authenticationType: gce" in datasource
  dashboard = json.loads(
      (tmp_path / "dashboards/bqaa-dashboard.json").read_text()
  )
  by_name = {v["name"]: v for v in dashboard["templating"]["list"]}
  assert by_name["project"]["query"] == "customer-project-123"
  assert by_name["table"]["query"] == "agent_events"
  # The committed dashboard is a source file and must never be mutated.
  committed = {v["name"]: v for v in _real_dashboard()["templating"]["list"]}
  assert committed["project"]["query"] != "customer-project-123"


def test_rejects_bad_identifiers_before_writing(tmp_path):
  result = subprocess.run(
      [
          sys.executable,
          str(RUNNER),
          "--project",
          "UPPERCASE",
          "--dataset",
          "agent_analytics",
          "--provision-only",
          "--workdir",
          str(tmp_path),
      ],
      capture_output=True,
      text=True,
  )
  assert result.returncode != 0
  assert not (tmp_path / "provisioning").exists()


def test_launch_env_binds_loopback_and_disables_preinstall(tmp_path):
  env = runner.launch_env(tmp_path, tmp_path / "provisioning", 3123)
  # An empty http_addr binds every interface, exposing an admin/admin
  # instance backed by the operator's own credentials to the network.
  assert env["GF_SERVER_HTTP_ADDR"] == "127.0.0.1"
  assert env["GF_SERVER_HTTP_PORT"] == "3123"
  assert env["GF_PLUGINS_PREINSTALL_DISABLED"] == "true"
  assert env["GF_ANALYTICS_REPORTING_ENABLED"] == "false"


def test_views_command_forwards_the_validated_prefix():
  assert runner.build_views_command(
      "customer-project-123", "agent_analytics", "agent_events", "custom_"
  ) == [
      "bq-agent-sdk",
      "views",
      "create-all",
      "--project-id",
      "customer-project-123",
      "--dataset-id",
      "agent_analytics",
      "--table-id",
      "agent_events",
      "--prefix",
      "custom_",
  ]


def test_plugin_install_command_pins_the_version(tmp_path):
  command = runner.build_plugin_install_command(tmp_path, tmp_path / "p")
  assert command[-2:] == [runner.PLUGIN_ID, runner.PLUGIN_VERSION]
  assert runner.PLUGIN_VERSION == "3.3.1"


def test_datasource_yaml_keeps_the_cost_guard_in_both_shapes(tmp_path):
  adc = runner.render_datasource_yaml("customer-project-123")
  assert f"MaxBytesBilled: {runner.DEFAULT_MAX_BYTES_BILLED}" in adc
  key_path = tmp_path / "key.json"
  key_path.write_text(
      json.dumps(
          {
              "client_email": "g@example.com",
              "private_key": "-----BEGIN PRIVATE KEY-----\nA\n-----END-----\n",
              "token_uri": "https://oauth2.example.com/token",
          }
      )
  )
  jwt = runner.render_datasource_yaml(
      "customer-project-123",
      runner.load_service_account_key(key_path),
      max_bytes_billed="42000000",
  )
  assert "MaxBytesBilled: 42000000" in jwt
  # The cap must sit under jsonData, before the secure block.
  assert jwt.index("MaxBytesBilled") < jwt.index("secureJsonData")


def test_datasource_yaml_omits_processing_location_by_default():
  # Hard-coding a multi-region breaks any dataset outside it; the plugin
  # selects the job location automatically when the field is absent.
  rendered = runner.render_datasource_yaml("customer-project-123")
  assert "processingLocation" not in rendered
  explicit = runner.render_datasource_yaml(
      "customer-project-123", processing_location="EU"
  )
  assert "processingLocation: EU" in explicit


@pytest.mark.parametrize("value", ["100000000", "1", "42000000"])
def test_max_bytes_accepted(value):
  assert runner.require_max_bytes(value) == value


@pytest.mark.parametrize("value", ["0", "-1", "1e6", "abc", "", "1.5"])
def test_max_bytes_rejected(value):
  with pytest.raises(ValueError):
    runner.require_max_bytes(value)


def test_one_sided_time_flags_are_rejected(tmp_path):
  result = subprocess.run(
      [
          sys.executable,
          str(RUNNER),
          "--project",
          "customer-project-123",
          "--dataset",
          "agent_analytics",
          "--time-from",
          "2026-06-15T00:00:00Z",
          "--provision-only",
          "--workdir",
          str(tmp_path),
      ],
      capture_output=True,
      text=True,
  )
  assert result.returncode != 0
  assert "must be given together" in result.stderr


def test_jwt_provisioning_file_is_private(tmp_path):
  key_path = tmp_path / "key.json"
  key_path.write_text(
      json.dumps(
          {
              "client_email": "g@example.com",
              "private_key": "-----BEGIN PRIVATE KEY-----\nA\n-----END-----\n",
              "token_uri": "https://oauth2.example.com/token",
          }
      )
  )
  workdir = tmp_path / "work"
  subprocess.run(
      [
          sys.executable,
          str(RUNNER),
          "--project",
          "customer-project-123",
          "--dataset",
          "agent_analytics",
          "--sa-key",
          str(key_path),
          "--provision-only",
          "--workdir",
          str(workdir),
      ],
      capture_output=True,
      text=True,
      check=True,
  )
  datasource = workdir / "provisioning" / "datasources" / "bigquery.yaml"
  assert (datasource.stat().st_mode & 0o777) == 0o600
  assert (datasource.parent.stat().st_mode & 0o777) == 0o700
  assert not datasource.with_suffix(".yaml.tmp").exists()


def test_sha256_verification(tmp_path):
  archive = tmp_path / "a.tar.gz"
  archive.write_bytes(b"grafana bytes")
  good = runner.sha256_of(archive)
  runner.verify_sha256(archive, good)
  runner.verify_sha256(archive, f"{good}  a.tar.gz\n")
  with pytest.raises(RuntimeError, match="SHA256 mismatch"):
    runner.verify_sha256(archive, "0" * 64)


def _tar_with(tmp_path, name, members):
  import tarfile as tarlib

  archive = tmp_path / name
  with tarlib.open(archive, "w:gz") as tar:
    for member_name, kind in members:
      info = tarlib.TarInfo(member_name)
      if kind == "dir":
        info.type = tarlib.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
      elif kind == "link":
        info.type = tarlib.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
      else:
        payload = b"x"
        info.size = len(payload)
        info.mode = 0o644
        import io

        tar.addfile(info, io.BytesIO(payload))
  return archive


def test_fallback_extraction_rejects_traversal(tmp_path):
  # The pre-3.10.12 code path must never fall back to unrestricted
  # extractall: a member named ../escaped.txt has to fail loudly and
  # write nothing outside the destination.
  import tarfile as tarlib

  archive = _tar_with(tmp_path, "evil.tgz", [("../escaped.txt", "file")])
  dest = tmp_path / "dest"
  dest.mkdir()
  with tarlib.open(archive) as tar:
    with pytest.raises(RuntimeError, match="escapes the destination"):
      runner._extract_without_filter(tar, dest)
  assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize(
    ("member", "kind", "match"),
    [
        ("/abs.txt", "file", "absolute path"),
        ("link", "link", "regular files and directories"),
    ],
)
def test_fallback_extraction_rejects_unsafe_members(
    tmp_path, member, kind, match
):
  import tarfile as tarlib

  archive = _tar_with(tmp_path, "evil2.tgz", [(member, kind)])
  dest = tmp_path / "dest2"
  dest.mkdir()
  with tarlib.open(archive) as tar:
    with pytest.raises(RuntimeError, match=match):
      runner._extract_without_filter(tar, dest)


def test_fallback_extraction_allows_benign_archives(tmp_path):
  import tarfile as tarlib

  archive = _tar_with(
      tmp_path,
      "ok.tgz",
      [("top", "dir"), ("top/bin", "dir"), ("top/bin/file.txt", "file")],
  )
  dest = tmp_path / "dest3"
  dest.mkdir()
  with tarlib.open(archive) as tar:
    runner._extract_without_filter(tar, dest)
  assert (dest / "top" / "bin" / "file.txt").read_bytes() == b"x"


def test_stop_identity_rejects_lookalike_and_reused_pids():
  record = {
      "pid": 12345,
      "home": "/work/grafana-home-12.3.0",
      "exe": "/work/grafana-home-12.3.0/bin/grafana",
      "start": "Tue Aug 12 09:00:00 2026",
  }
  good = (
      "/work/grafana-home-12.3.0/bin/grafana server --homepath"
      " /work/grafana-home-12.3.0"
  )
  assert runner._identity_matches(record, "Tue Aug 12 09:00:00 2026", good)
  # An unrelated process that merely mentions the home path as an
  # argument must never be signalled (the reviewer's sleep repro).
  lookalike = "python3 -c sleep /work/grafana-home-12.3.0"
  assert not runner._identity_matches(
      record, "Tue Aug 12 09:00:00 2026", lookalike
  )
  # Same command shape but a different start time means the pid was
  # recycled into a new process.
  assert not runner._identity_matches(record, "Tue Aug 12 10:30:00 2026", good)
  # Prefix-spoofed executables do not match the exact-exe rule.
  spoofed = good.replace("bin/grafana ", "bin/grafana-evil ")
  assert not runner._identity_matches(
      record, "Tue Aug 12 09:00:00 2026", spoofed
  )


def test_stop_never_signals_live_lookalike_process(tmp_path):
  # End-to-end: a real unrelated process whose argv contains the home
  # path survives a stop() call against a pidfile pointing at it.
  home = tmp_path / "grafana-home-12.3.0"
  victim = subprocess.Popen(
      [sys.executable, "-c", f"import time; '{home}'; time.sleep(30)"]
  )
  try:
    probe = runner._probe_process(victim.pid)
    assert probe is not None
    (tmp_path / "grafana.pid").write_text(
        json.dumps(
            {
                "pid": victim.pid,
                "home": str(home),
                "exe": str(home / "bin" / "grafana"),
                "start": probe[0],
            }
        )
    )
    assert runner.stop(tmp_path) == 0
    assert victim.poll() is None, "stop() signalled an unrelated process"
  finally:
    victim.kill()
    victim.wait()


def test_plugin_pin_enforced_against_cached_manifests(tmp_path):
  plugin_dir = tmp_path / runner.PLUGIN_ID
  plugin_dir.mkdir()
  assert runner.plugin_needs_install(tmp_path)  # no manifest
  manifest = plugin_dir / "plugin.json"
  manifest.write_text(json.dumps({"info": {"version": "0.0.1"}}))
  assert runner.plugin_needs_install(tmp_path)  # version mismatch
  manifest.write_text("{corrupt")
  assert runner.plugin_needs_install(tmp_path)  # unreadable manifest
  manifest.write_text(json.dumps({"info": {"version": runner.PLUGIN_VERSION}}))
  assert not runner.plugin_needs_install(tmp_path)


def test_cached_extraction_honors_explicit_checksum(tmp_path):
  # Build a tiny valid "grafana" archive.
  import io
  import tarfile as tarlib

  archive = tmp_path / "grafana.tgz"
  with tarlib.open(archive, "w:gz") as tar:
    info = tarlib.TarInfo("grafana-x/bin/grafana")
    payload = b"#!/bin/sh\n"
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))
  good = runner.sha256_of(archive)

  workdir = tmp_path / "work"
  workdir.mkdir()
  home = runner.ensure_grafana(workdir, archive, good)
  assert (home / "bin" / "grafana").exists()
  provenance = json.loads((home / ".provenance.json").read_text())
  assert provenance["sha256"] == good

  # Reuse with the matching hash: same home, no rebuild needed.
  assert runner.ensure_grafana(workdir, archive, good) == home
  # A cached home must not satisfy a DIFFERENT explicit hash: the stale
  # extraction is discarded and the archive re-verified — here the
  # mismatch fails closed before anything is reused.
  with pytest.raises(RuntimeError, match="SHA256 mismatch"):
    runner.ensure_grafana(workdir, archive, "0" * 64)
  assert not home.exists()
