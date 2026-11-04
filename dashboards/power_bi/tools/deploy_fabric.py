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
import datetime
import email.utils
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
PBIR_SCHEMA_URL = (
    "https://developer.microsoft.com/json-schemas/fabric/"
    "item/report/definitionProperties/2.0.0/schema.json"
)
# `\Z` (not `$`) so that a trailing newline is never accepted, even by callers
# that use `.match()` instead of `.fullmatch()`.
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
MAX_LRO_SECONDS = 600
DEFAULT_RETRY_AFTER = 10
MODEL_DIR_NAME = "BQAA_Agent_Analytics.SemanticModel"
REPORT_DIR_NAME = "BQAA_Agent_Analytics.Report"


def validate_guid(value, label="workspace ID"):
  """Return *value* when it is exactly one GUID string, else raise."""
  if not isinstance(value, str) or not GUID_RE.fullmatch(value):
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
  if not isinstance(error, dict):
    return "unknown", "unexpected response body"
  nested = error.get("error")
  if not isinstance(nested, dict):
    nested = {}
  code = error.get("errorCode", nested.get("code", "unknown"))
  msg = error.get("message", nested.get("message", ""))
  return code, msg


def _header(headers, name):
  """Case-insensitive HTTP header lookup."""
  wanted = name.lower()
  for key, value in (headers or {}).items():
    if str(key).lower() == wanted:
      return value
  return None


def _load_json_object(body, label, allow_empty=True):
  """Parse *body* as a JSON object.

  A blank body is an empty object unless *allow_empty* is false, in which case
  it is an error: a listing that came back without a body is not evidence that
  the scan was complete.
  """
  if not body or not body.strip():
    if allow_empty:
      return {}
    raise RuntimeError(f"{label} returned an empty body")
  try:
    parsed = json.loads(body)
  except ValueError:
    raise RuntimeError(f"{label} returned a non-JSON body") from None
  if not isinstance(parsed, dict):
    raise RuntimeError(f"{label} returned an unexpected JSON shape")
  return parsed


def _parse_retry_after(value, max_seconds=MAX_LRO_SECONDS):
  """Convert a Retry-After header into whole seconds.

  RFC 9110 allows either delta-seconds or an HTTP-date.  The result is clamped
  to [0, *max_seconds*] so a date in the past or a hostile value can neither
  spin the poller nor stall it; an unparsable value falls back to the default
  polling interval.
  """
  if value is None:
    return DEFAULT_RETRY_AFTER
  text = str(value).strip()
  try:
    seconds = int(text)
  except ValueError:
    try:
      when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
      return DEFAULT_RETRY_AFTER
    if when.tzinfo is None:
      when = when.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    seconds = int((when - now).total_seconds())
  return max(0, min(max_seconds, seconds))


def poll_lro(token, location_url, operation_id, max_seconds=MAX_LRO_SECONDS):
  """Poll a long-running operation until Succeeded or Failed.

  Returns the operation result as a dict; an operation that succeeds with an
  empty result body (HTTP 200 with no content, or 204) yields an empty dict.
  """
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
    result = _load_json_object(body, "LRO poll")
    op_status = result.get("status", "Running")
    if op_status == "Succeeded":
      result_url = f"{FABRIC_API_BASE}/operations/{operation_id}/result"
      r_status, _, r_body = http_request(result_url, "GET", token)
      if r_status not in (200, 204):
        code, msg = _extract_error(r_body)
        raise RuntimeError(f"LRO result fetch failed: {code}: {msg}")
      return _load_json_object(r_body, "LRO result fetch")
    if op_status == "Failed":
      error = result.get("error", {})
      if not isinstance(error, dict):
        error = {}
      code = error.get("errorCode", "unknown")
      msg = error.get("message", "operation failed")
      raise RuntimeError(f"LRO failed: {code}: {msg}")
    retry_after = _parse_retry_after(
        _header(headers, "Retry-After"), max_seconds
    )
  raise RuntimeError(f"LRO timed out after {max_seconds}s")


MAX_LIST_PAGES = 1000


def _next_page_url(base_url, result):
  """Return the URL of the next listing page, or None on the last page."""
  continuation_uri = result.get("continuationUri")
  if continuation_uri:
    return continuation_uri
  continuation_token = result.get("continuationToken")
  if continuation_token:
    quoted = urllib.parse.quote(str(continuation_token))
    return f"{base_url}?continuationToken={quoted}"
  return None


def find_existing_item(token, workspace_id, item_type, display_name):
  """Return the id of the one item named *display_name*, or None.

  Follows listing pagination so an item beyond the first page is found, fails
  closed when the listing itself fails (so a transient error can never turn an
  update into a duplicate create), and refuses to guess between several items
  that share the same display name.
  """
  base_url = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/{item_type}"
  url = base_url
  seen_urls = set()
  matches = []
  while url:
    if url in seen_urls or len(seen_urls) >= MAX_LIST_PAGES:
      raise RuntimeError(f"list {item_type} pagination did not terminate")
    seen_urls.add(url)
    status, _, body = http_request(url, "GET", token)
    if status != 200:
      code, msg = _extract_error(body)
      raise RuntimeError(f"list {item_type} failed ({status}): {code}: {msg}")
    result = _load_json_object(body, f"list {item_type}", allow_empty=False)
    items = result.get("value", [])
    if not isinstance(items, list):
      raise RuntimeError(
          f"list {item_type} returned an unexpected response shape"
      )
    for item in items:
      if not isinstance(item, dict):
        continue
      if item.get("displayName") == display_name and item.get("id"):
        if item["id"] not in matches:
          matches.append(item["id"])
    url = _next_page_url(base_url, result)
  if len(matches) > 1:
    raise RuntimeError(
        f"Ambiguous duplicate {item_type} items named {display_name!r}"
    )
  return matches[0] if matches else None


def deploy_item(
    token,
    workspace_id,
    item_type,
    parts,
    display_name,
    update=False,
):
  """Create or update an item.  Returns the item id.

  When *update* is true and an item with the same display name exists, its
  definition is updated in place and its existing id is returned even if the
  service replies with no body.  A newly created item must come back with a
  well-formed id, otherwise the call fails rather than returning an empty one.
  """
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
  verb = "update" if item_id else "create"
  status, headers, resp_body = http_request(url, "POST", token, body)
  if status == 202:
    location = _header(headers, "Location") or ""
    operation_id = _header(headers, "x-ms-operation-id") or ""
    if not location or not operation_id:
      raise RuntimeError(f"{verb} accepted but missing LRO headers")
    result = poll_lro(token, location, operation_id)
    returned_id = result.get("id") if isinstance(result, dict) else None
  elif status in (200, 201):
    try:
      body_obj = (
          json.loads(resp_body) if resp_body and resp_body.strip() else {}
      )
    except ValueError:
      body_obj = {}
    returned_id = body_obj.get("id") if isinstance(body_obj, dict) else None
  else:
    code, msg = _extract_error(resp_body)
    raise RuntimeError(f"deploy failed: {code}: {msg}")
  if item_id:
    return returned_id or item_id
  if not returned_id:
    raise RuntimeError(f"create {item_type} returned no item id")
  validate_guid(returned_id, f"{item_type}_id")
  return returned_id


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
  if not model_id:
    raise RuntimeError("semantic model id is empty")
  validate_guid(model_id, "semantic_model_id")
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
