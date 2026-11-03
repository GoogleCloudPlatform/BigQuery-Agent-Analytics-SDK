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

"""Deploy a generated Power BI project to a Fabric workspace via REST.

This tool creates or updates a semantic model and report in a Power BI
workspace using the Fabric REST API.  It reads a bearer token from a file
or environment variable and never prints or logs the token.
"""

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import time
import urllib.error
import urllib.request

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
PBIR_SCHEMA_URL = (
    "https://developer.microsoft.com/json-schemas/fabric/"
    "item/report/definitionProperties/2.0.0/schema.json"
)
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAX_LRO_SECONDS = 600
DEFAULT_RETRY_AFTER = 10
MODEL_DIR_NAME = "BQAA_Agent_Analytics.SemanticModel"
REPORT_DIR_NAME = "BQAA_Agent_Analytics.Report"


def validate_guid(value, label="workspace ID"):
  if not GUID_RE.match(value):
    raise ValueError(f"invalid {label}")
  return value


def read_token_file(path):
  p = pathlib.Path(path)
  if not p.is_file():
    raise ValueError(f"token file not found: {path}")
  mode = p.stat().st_mode
  if stat.S_IMODE(mode) & 0o077:
    raise ValueError("token file must not be readable by group or other")
  content = p.read_text().strip()
  if not content or any(c.isspace() for c in content):
    raise ValueError("token file must contain a single non-whitespace line")
  return content


def get_token(token_file, env_var="FABRIC_BEARER_TOKEN"):
  if token_file:
    return read_token_file(token_file)
  env_token = os.environ.get(env_var, "").strip()
  if not env_token:
    raise ValueError("no token: provide --token-file or set " f"{env_var}")
  return env_token


def collect_parts(item_dir):
  """Collect all files under *item_dir* except .platform and .pbi/."""
  item_dir = pathlib.Path(item_dir)
  if not item_dir.is_dir():
    raise ValueError(f"directory not found: {item_dir}")
  parts = []
  for path in sorted(item_dir.rglob("*")):
    if not path.is_file():
      continue
    rel = path.relative_to(item_dir).as_posix()
    if rel == ".platform" or rel.startswith(".pbi/"):
      continue
    parts.append({"path": rel, "data": path.read_bytes()})
  return parts


def encode_parts(parts):
  encoded = []
  for part in parts:
    encoded.append(
        {
            "path": part["path"],
            "payload": base64.b64encode(part["data"]).decode("ascii"),
            "payloadType": "InlineBase64",
        }
    )
  return encoded


def rewrite_pbir(pbir_text, model_id):
  """Rewrite definition.pbir to the byConnection form for deploy."""
  return (
      json.dumps(
          {
              "$schema": PBIR_SCHEMA_URL,
              "version": "4.0",
              "datasetReference": {
                  "byConnection": {
                      "connectionString": f"semanticmodelid={model_id}"
                  }
              },
          },
          indent=2,
      )
      + "\n"
  )


ALLOWED_API_PREFIXES = (
    "https://api.fabric.microsoft.com/",
    "https://api.powerbi.com/",
)


def validate_operation_url(url):
  """Validate that an operation/request URL targets Fabric or Power BI REST."""
  if not isinstance(url, str) or not url.startswith(ALLOWED_API_PREFIXES):
    raise ValueError(f"untrusted operation URL: {url!r}")
  return url


def http_request(url, method, token, body=None):
  """Make an HTTP request.  Returns (status, headers, body_bytes)."""
  validate_operation_url(url)
  headers = {"Authorization": f"Bearer {token}"}
  data = None
  if body is not None:
    headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode("utf-8")
  req = urllib.request.Request(url, data=data, headers=headers, method=method)
  try:
    with urllib.request.urlopen(req, timeout=60) as resp:
      return resp.status, dict(resp.headers), resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, dict(exc.headers), exc.read()


def _extract_error(body_bytes):
  if not body_bytes:
    return "unknown", "no response body"
  try:
    error = json.loads(body_bytes)
  except (json.JSONDecodeError, ValueError):
    return "unknown", "non-JSON response"
  code = error.get("errorCode", error.get("error", {}).get("code", "unknown"))
  msg = error.get("message", error.get("error", {}).get("message", ""))
  return code, msg


def poll_lro(token, location_url, operation_id, max_seconds=MAX_LRO_SECONDS):
  """Poll a long-running operation until Succeeded or Failed."""
  validate_operation_url(location_url)
  deadline = time.monotonic() + max_seconds
  retry_after = DEFAULT_RETRY_AFTER
  while time.monotonic() < deadline:
    remaining = deadline - time.monotonic()
    time.sleep(min(retry_after, max(0, remaining)))
    status, headers, body = http_request(location_url, "GET", token)
    if status != 200:
      code, msg = _extract_error(body)
      raise RuntimeError(f"LRO poll failed: {code}: {msg}")
    result = json.loads(body)
    op_status = result.get("status", "Running")
    if op_status == "Succeeded":
      result_url = f"{FABRIC_API_BASE}/operations/{operation_id}/result"
      r_status, _, r_body = http_request(result_url, "GET", token)
      if r_status != 200:
        code, msg = _extract_error(r_body)
        raise RuntimeError(f"LRO result fetch failed: {code}: {msg}")
      return json.loads(r_body)
    if op_status == "Failed":
      error = result.get("error", {})
      code = error.get("errorCode", "unknown")
      msg = error.get("message", "operation failed")
      raise RuntimeError(f"LRO failed: {code}: {msg}")
    retry_after = int(headers.get("Retry-After", DEFAULT_RETRY_AFTER))
  raise RuntimeError(f"LRO timed out after {max_seconds}s")


def find_existing_item(token, workspace_id, item_type, display_name):
  url = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/{item_type}"
  status, _, body = http_request(url, "GET", token)
  if status != 200:
    return None
  result = json.loads(body)
  for item in result.get("value", []):
    if item.get("displayName") == display_name:
      return item.get("id")
  return None


def deploy_item(
    token,
    workspace_id,
    item_type,
    parts,
    display_name,
    update=False,
):
  """Create or update an item.  Returns the item id."""
  encoded = encode_parts(parts)
  format_type = "TMDL" if item_type == "semanticModels" else "PBIR"
  item_id = None
  if update:
    item_id = find_existing_item(token, workspace_id, item_type, display_name)
  if item_id:
    url = (
        f"{FABRIC_API_BASE}/workspaces/{workspace_id}/{item_type}"
        f"/{item_id}/updateDefinition"
    )
    body = {"definition": {"format": format_type, "parts": encoded}}
  else:
    url = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/{item_type}"
    body = {
        "displayName": display_name,
        "definition": {"format": format_type, "parts": encoded},
    }
  status, headers, resp_body = http_request(url, "POST", token, body)
  if status == 202:
    location = headers.get("Location", "")
    operation_id = headers.get("x-ms-operation-id", "")
    if not location or not operation_id:
      raise RuntimeError("create accepted but missing LRO headers")
    result = poll_lro(token, location, operation_id)
    return result.get("id", "")
  if status in (200, 201):
    result = json.loads(resp_body) if resp_body else {}
    return result.get("id", "")
  code, msg = _extract_error(resp_body)
  raise RuntimeError(f"deploy failed: {code}: {msg}")


def _print_dry_run_parts(label, parts):
  print(f"{label} parts ({len(parts)}):")
  for part in parts:
    sha = hashlib.sha256(part["data"]).hexdigest()
    print(f"  {part['path']}  {len(part['data'])} bytes" f"  sha256:{sha[:16]}")


def main(argv=None):
  parser = argparse.ArgumentParser(
      description="Deploy a Power BI project to a Fabric workspace"
  )
  parser.add_argument("--workspace-id", required=True)
  parser.add_argument("--token-file")
  parser.add_argument("--project-dir", required=True)
  parser.add_argument("--display-name", default="BQAA Agent Analytics")
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--update", action="store_true")
  args = parser.parse_args(argv)

  validate_guid(args.workspace_id)
  project_dir = pathlib.Path(args.project_dir)
  model_dir = project_dir / MODEL_DIR_NAME
  report_dir = project_dir / REPORT_DIR_NAME

  model_parts = collect_parts(model_dir)

  if args.dry_run:
    _print_dry_run_parts("Semantic Model", model_parts)
    report_parts = collect_parts(report_dir)
    for i, part in enumerate(report_parts):
      if part["path"] == "definition.pbir":
        rewritten = rewrite_pbir(
            part["data"].decode("utf-8"), "<pending-model-id>"
        )
        report_parts[i] = {
            "path": part["path"],
            "data": rewritten.encode("utf-8"),
        }
    _print_dry_run_parts("Report", report_parts)
    return 0

  token = get_token(args.token_file)

  model_id = deploy_item(
      token,
      args.workspace_id,
      "semanticModels",
      model_parts,
      args.display_name,
      args.update,
  )
  print(f"semantic model id: {model_id}")

  report_parts = collect_parts(report_dir)
  for i, part in enumerate(report_parts):
    if part["path"] == "definition.pbir":
      rewritten = rewrite_pbir(part["data"].decode("utf-8"), model_id)
      report_parts[i] = {
          "path": part["path"],
          "data": rewritten.encode("utf-8"),
      }

  report_id = deploy_item(
      token,
      args.workspace_id,
      "reports",
      report_parts,
      args.display_name,
      args.update,
  )
  print(f"report id: {report_id}")
  print(
      f"report url: https://app.powerbi.com/groups/"
      f"{args.workspace_id}/reports/{report_id}"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
