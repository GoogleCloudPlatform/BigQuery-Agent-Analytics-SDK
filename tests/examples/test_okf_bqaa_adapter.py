# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Hermetic tests for examples/okf_bqaa_adapter (no GCP, no google.adk).

Primary input is the COMMITTED live BQAA export written by the ADK observe
agent run. The germany fixture is a SYNTHETIC hashing regression only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

EXAMPLE_DIR = (
    Path(__file__).resolve().parents[2] / "examples" / "okf_bqaa_adapter"
)
FIXTURES = EXAMPLE_DIR / "fixtures"
sys.path.insert(0, str(EXAMPLE_DIR))

import adapter  # noqa: E402
import lookup  # noqa: E402

LIVE_EVENTS = FIXTURES / "live_observe_agent_events.json"
LIVE_META = FIXTURES / "live.json"
LIVE_IDENTITIES = FIXTURES / "live_identities.json"
SYNTHETIC_TRACE = FIXTURES / "synthetic" / "bqaa-germany.json"
SYNTHETIC_IDS = FIXTURES / "synthetic" / "identities.json"

CONSUME_SESSION = "04fa3d56-f2f1-413e-8c2b-ec116835af84"
SHA = "sha256:"


def _load(path: Path) -> dict:
  return json.loads(path.read_text("utf-8"))


@pytest.fixture(scope="module")
def live_trace() -> dict:
  return _load(LIVE_EVENTS)


@pytest.fixture(scope="module")
def live_result(live_trace) -> dict:
  return adapter.adapt(live_trace)


@pytest.fixture(scope="module")
def live_identities(live_result) -> dict:
  return adapter.compute_identities(
      live_result["files"], live_result["constants"], adapter.load_manifests()
  )


@pytest.fixture()
def projected(tmp_path, live_result, live_identities) -> dict:
  return adapter.project(live_result, live_identities, tmp_path / "out")


# ---- 1. committed live export -----------------------------------------------


def test_no_gcp_imports_on_default_path():
  """adapter.py / lookup.py stay stdlib. google.cloud may already be in
  sys.modules from the environment; inspect sources instead."""
  for name in ("adapter.py", "lookup.py"):
    text = (EXAMPLE_DIR / name).read_text("utf-8")
    assert "google.adk" not in text
    assert "google.cloud" not in text
    assert "from google" not in text
    assert "import google" not in text


def test_live_export_is_live_shaped(live_trace):
  meta = _load(LIVE_META)
  assert live_trace["session_id"] == meta["session_id"]
  assert live_trace["session_id"]
  assert live_trace["trace_id"] == meta["trace_id"]
  assert live_trace["agent"]["model"] == "gemini-3.8-flash"
  assert meta["model"] == "gemini-3.8-flash"
  assert meta["vertex_location"] == "global"
  assert live_trace["table"].endswith(".okf_rfc_demo.agent_events")
  assert live_trace["writer"]["label"] == "bigquery-agent-analytics-plugin/live"
  assert "SYNTHETIC" not in live_trace["_fixture"].upper() or (
      "LIVE" in live_trace["_fixture"]
  )
  assert live_trace["_fixture"].startswith("LIVE BQAA export")
  events = live_trace["events"]
  assert len(events) == meta["event_count"]
  assert len(events) >= 100
  assert meta["event_count"] >= 100
  assert {e["session_id"] for e in events} == {meta["session_id"]}
  assert (
      sum(1 for e in events if e["event_type"] == "USER_MESSAGE_RECEIVED") >= 10
  )
  kinds = {adapter.tool_kind(e) for e in events} - {None}
  assert kinds == {adapter.KIND_RETRIEVE, adapter.KIND_RECEIPT}
  # kind lives on the live plugin shape: content.result.kind (not attributes)
  retrieve = next(
      e for e in events if adapter.tool_kind(e) == adapter.KIND_RETRIEVE
  )
  assert retrieve["event_type"] == "TOOL_COMPLETED"
  assert retrieve["content"]["result"]["kind"] == adapter.KIND_RETRIEVE
  assert "tool" not in (retrieve["attributes"] or {})
  models = {
      e["attributes"].get("model")
      for e in events
      if e["event_type"] == "LLM_REQUEST"
  }
  assert models == {"gemini-3.8-flash"}
  assert {e["agent"] for e in events} == {"okf_rfc_observe_agent"}


def test_live_export_tool_payloads_never_emit(live_trace):
  for e in live_trace["events"]:
    if e["event_type"] not in ("TOOL_STARTING", "TOOL_COMPLETED"):
      continue
    assert lookup.never_emit_violations(e["content"]) == []


# ---- 2. observe / adapt / identities -----------------------------------------


def test_observe_live(live_trace):
  obs = adapter.observe(live_trace)
  assert obs["context_ref"].startswith("okf:env-observe#")
  assert obs["receipt_context_ref"].startswith(obs["context_ref"])
  assert obs["mode"] == "current"
  assert [i["rank"] for i in obs["items"]] == [1, 2, 3, 4, 5, 6]
  assert obs["items"][0]["title"] == "Active-customer revenue"
  assert obs["excluded"][0]["title"] == "Customer revenue (legacy)"
  assert obs["links"] == [
      {
          "from": "Active-customer revenue",
          "to": "Revenue recognition eligibility",
          "rel": "governed_by",
      }
  ]
  assert obs["receipt"]["verdict"] == "UNVERIFIABLE"
  assert obs["receipt"]["receipt_id"] == "rcpt-observe-noexec"
  assert "computation_version_id" not in obs["receipt"]
  assert obs["question"].startswith(
      "What was active-customer revenue in Germany"
  )
  assert obs["error_codes"] == []
  assert obs["observed_publication_id"].startswith(SHA)


def test_adapt_live_bundle(live_result):
  files = live_result["files"]
  assert set(files) == {
      "metrics/active-customer-revenue.md",
      "computations/active-customer-revenue-by-region-and-quarter.md",
      "concepts/active-customer.md",
      "policies/revenue-recognition-eligibility.md",
      "tables/billing-invoice-lines.md",
      "tables/crm-customers.md",
      "metrics/customer-revenue-legacy.md",
      "log.md",
  }
  for path, text in files.items():
    assert "Derived from BQAA observation, not authored" in text
    if path != "log.md":
      fm, _ = adapter.split_frontmatter(text)
      assert any(ln.startswith("type: ") for ln in fm.split("\n"))
  comp = files["computations/active-customer-revenue-by-region-and-quarter.md"]
  assert "verdict `UNVERIFIABLE`" in comp
  assert "runtime: bigquery-named-parameters" in comp
  assert "computation-version" not in comp
  assert "status: deprecated" in files["metrics/customer-revenue-legacy.md"]
  assert "rel: governed_by" in files["metrics/active-customer-revenue.md"]
  c = live_result["constants"]
  assert c["adapter_version"] == "okf-bqaa-adapter:v0"
  assert (
      c["source_uri"].startswith("bqaa://")
      and "okf_rfc_demo" in c["source_uri"]
  )
  assert c["revision"] == "bqaa-trace:" + live_result["observation"]["trace_id"]


def test_live_identities_shape_and_pin(live_identities):
  for key in (
      "observation_id",
      "snapshot_id",
      "publication_id",
      "source_manifest_hash",
  ):
    assert live_identities[key].startswith(SHA)
    assert len(live_identities[key]) == len(SHA) + 64
  assert len(live_identities["concept_version_ids"]) == 7
  assert len(live_identities["file_sha256"]) == 8
  pinned = _load(LIVE_IDENTITIES)
  for key in (
      "observation_id",
      "snapshot_id",
      "publication_id",
      "source_manifest_hash",
  ):
    assert live_identities[key] == pinned[key], key
  assert live_identities["concept_version_ids"] == pinned["concept_version_ids"]
  assert live_identities["file_sha256"] == pinned["file_sha256"]
  assert pinned["inputs"]["adapter_version"] == "okf-bqaa-adapter:v0"
  assert pinned["inputs"]["session_id"] == _load(LIVE_META)["session_id"]


def test_live_identities_are_not_germany(live_identities):
  germany = _load(SYNTHETIC_IDS)
  assert live_identities["publication_id"] != germany["publication_id"]
  assert live_identities["observation_id"] != germany["observation_id"]


# ---- 3/4/5. lookup ------------------------------------------------------------


def test_project_writes_bundle_and_mapping(projected, live_result):
  assert projected["mapping"].exists()
  assert projected["identities"].exists()
  assert (projected["bundle"] / "log.md").exists()
  mapping = _load(projected["mapping"])
  assert mapping["label"].startswith("derived/demo")
  ctx = live_result["observation"]["context_ref"]
  assert ctx in mapping["mapping"]


def test_lookup_known_context_ref(projected, live_result, live_identities):
  ctx = live_result["observation"]["context_ref"]
  result = lookup.lookup(ctx, projected["mapping"])
  assert result == {
      "context_ref": ctx,
      "publication_id": live_identities["publication_id"],
      "label": "derived/demo",
  }
  assert lookup.never_emit_violations(result) == []


def test_lookup_unknown_fails_closed(projected):
  with pytest.raises(lookup.UnknownContextRefError):
    lookup.lookup("okf:env-demo#a25e1c0ccbca", projected["mapping"])
  with pytest.raises(lookup.UnknownContextRefError):
    lookup.lookup("", projected["mapping"])
  assert (
      lookup.main(["okf:env-nope#0", "--mapping", str(projected["mapping"])])
      == 2
  )


def test_never_emit_scan():
  assert lookup.NEVER_EMIT == adapter.NEVER_EMIT
  bad = {
      "ok": True,
      "nested": [{"sql": "SELECT 1"}, {"deep": {"principal": "x"}}],
  }
  assert lookup.never_emit_violations(bad) == ["principal", "sql"]
  assert lookup.never_emit_violations({"context_ref": "a", "label": "b"}) == []


# ---- --session gate: consume-shaped traces are rejected ----------------------


def _consume_shaped_trace(live_trace: dict) -> dict:
  """Mimic the consume session: a stub echo tool, no OKF kinds."""
  trace = copy.deepcopy(live_trace)
  for e in trace["events"]:
    e["session_id"] = CONSUME_SESSION
    if e["event_type"] in ("TOOL_STARTING", "TOOL_COMPLETED"):
      e["content"] = {
          "tool": "lookup_okf_context",
          "tool_origin": "LOCAL",
          "result": {
              "ok": True,
              "context_ref": "okf:env-demo#a25e1c0ccbca",
              "publication_id": "sha256:" + "a" * 64,
              "note": "derived/demo bundle; not canonical authoring",
          },
      }
  return trace


def test_require_retrieve_shaped_rejects_consume_session(live_trace):
  with pytest.raises(
      adapter.NotRetrieveShapedError, match="okf-context:retrieve"
  ):
    adapter.require_retrieve_shaped(_consume_shaped_trace(live_trace))
  with pytest.raises(adapter.NotRetrieveShapedError):
    adapter.adapt(_consume_shaped_trace(live_trace))


def test_require_retrieve_shaped_needs_receipt(live_trace):
  trace = copy.deepcopy(live_trace)
  trace["events"] = [
      e for e in trace["events"] if adapter.tool_kind(e) != adapter.KIND_RECEIPT
  ]
  with pytest.raises(
      adapter.NotRetrieveShapedError, match="attested-computation"
  ):
    adapter.require_retrieve_shaped(trace)


def test_run_cli_default_path(tmp_path, capsys):
  """Default CLI path must not newly import google.adk (it may already
  be in sys.modules from other tests in the full suite)."""
  import run

  before = set(sys.modules)
  assert run.main(["--out", str(tmp_path / "out")]) == 0
  out = capsys.readouterr().out
  assert "PUBLICATION_ID sha256:" in out
  assert "MODEL gemini-3.8-flash" in out
  assert (
      run.main(["--out", str(tmp_path / "out"), "--lookup", "okf:env-nope#0"])
      == 2
  )
  newly = set(sys.modules) - before
  assert not any(
      m == "google.adk" or m.startswith("google.adk.") for m in newly
  ), sorted(newly)
  assert not any(
      m == "google.cloud" or m.startswith("google.cloud.") for m in newly
  ), sorted(newly)
  src = (EXAMPLE_DIR / "run.py").read_text("utf-8")
  assert "def _load_observe_agent" in src
  assert src.index("def _load_observe_agent") < src.index(
      "import observe_agent"
  )


# ---- 6. SYNTHETIC germany hashing regression (labelled synthetic) -------------


@pytest.mark.skipif(
    not SYNTHETIC_TRACE.exists(), reason="synthetic fixture absent"
)
def test_synthetic_germany_identities_match_pinned_js():
  """SYNTHETIC: pins the hashing port against the JS identities.json."""
  trace = _load(SYNTHETIC_TRACE)
  assert trace["_fixture"].startswith("SYNTHETIC")
  result = adapter.adapt(trace)
  ids = adapter.compute_identities(
      result["files"], result["constants"], adapter.load_manifests()
  )
  pinned = _load(SYNTHETIC_IDS)
  assert ids["observation_id"] == (
      "sha256:9c112b6bcf3ca1a8b82005b470efd2bd489e4517374323d6ed3d1f69ebf8bd87"
  )
  assert ids["snapshot_id"] == (
      "sha256:b8a41d1d2e4ccb21c4a1c342857b38ee8b31e59dbea7ecd9d661edcd0c9ea965"
  )
  assert ids["publication_id"] == (
      "sha256:a25e1c0ccbcad270bf9e3b7a8f792167795381b6880bdc8a1ccde8df40ea52c5"
  )
  assert ids["concept_version_ids"] == pinned["concept_version_ids"]
  assert ids["file_sha256"] == pinned["file_sha256"]
  assert (
      adapter.demo_envelope_id(ids["publication_id"])
      == pinned["demo_envelope_id"]
  )
