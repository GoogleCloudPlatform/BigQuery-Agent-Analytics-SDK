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

"""Live BigQuery + LLM integration test for the BKA-decision compile path.

This is the **gated** end-to-end proof that PR 4c's compile-with-
LLM pipeline produces a working compiled extractor against real
production telemetry. It exists to catch a class of failure that
mocks can't cover (prompt drift, model regression, schema drift in
``agent_events``) and to regenerate the checked-in measurement
artifact at
``tests/fixtures_extractor_compilation/bka_decision_measurement_report.json``.

Skipped by default. To run, set:

    BQAA_RUN_LIVE_TESTS=1
    BQAA_RUN_LIVE_LLM_COMPILE_TESTS=1
    PROJECT_ID=...
    DATASET_ID=...                 # contains the agent_events table
    BQAA_LLM_COMPILE_MODEL=...     # optional, defaults to gemini-2.5-flash

Assertions are contract-level invariants — ``ok=True``,
``parity_ok=True``, ``n_attempts<=3`` — *not* exact LLM wording.
The artifact captures concrete numbers; the test pins the shape.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

import pytest

_LIVE = (
    os.environ.get("BQAA_RUN_LIVE_TESTS") == "1"
    and os.environ.get("BQAA_RUN_LIVE_LLM_COMPILE_TESTS") == "1"
)

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "Live LLM compile tests skipped. Set BQAA_RUN_LIVE_TESTS=1 plus "
        "BQAA_RUN_LIVE_LLM_COMPILE_TESTS=1 plus PROJECT_ID + DATASET_ID "
        "to opt in. Default CI does NOT run this — the LLM cost and "
        "BigQuery dependency are intentionally opt-in."
    ),
)


_DEFAULT_MODEL = "gemini-2.5-flash"

# Cap on rows pulled from agent_events. Keep small — the compile
# loop's smoke gate just needs both span-handling branches
# represented; pulling thousands of events doesn't change what's
# proven and adds cost.
_MAX_LIVE_EVENTS = 10


_LIVE_BKA_QUERY = """\
SELECT
  event_type,
  session_id,
  span_id,
  TO_JSON_STRING(content) AS content_json
FROM `{project}.{dataset}.agent_events`
WHERE event_type = 'bka_decision'
  AND content IS NOT NULL
ORDER BY event_timestamp DESC
LIMIT @max_events
"""


@pytest.fixture(scope="module")
def live_config():
  project = os.environ.get("PROJECT_ID")
  dataset = os.environ.get("DATASET_ID")
  if not project or not dataset:
    pytest.skip(
        "PROJECT_ID and DATASET_ID env vars are required for live compile tests."
    )
  return {
      "project": project,
      "dataset": dataset,
      "model": os.environ.get("BQAA_LLM_COMPILE_MODEL", _DEFAULT_MODEL),
  }


@pytest.fixture(scope="module")
def bq_events(live_config):
  """Pull a small batch of bka_decision events from BigQuery."""
  pytest.importorskip("google.cloud.bigquery")
  from google.cloud import bigquery

  client = bigquery.Client(project=live_config["project"], location="US")
  query = _LIVE_BKA_QUERY.format(
      project=live_config["project"], dataset=live_config["dataset"]
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("max_events", "INT64", _MAX_LIVE_EVENTS)
      ]
  )
  rows = list(client.query(query, job_config=job_config).result())
  if not rows:
    pytest.skip(
        f"No bka_decision events in {live_config['project']}."
        f"{live_config['dataset']}.agent_events; cannot run live compile."
    )

  events: list[dict] = []
  for row in rows:
    content = json.loads(row["content_json"]) if row["content_json"] else {}
    events.append(
        {
            "event_type": row["event_type"],
            "session_id": row["session_id"],
            "span_id": row["span_id"],
            "content": content,
        }
    )
  return events


class _GenaiLLMAdapter:
  """Thin in-test adapter wrapping ``google.genai`` to satisfy the
  :class:`LLMClient` Protocol.

  Adapter choice is out of scope for the SDK core (per the c.2
  docs); this in-test wrapper is intentionally minimal. If multiple
  call sites end up needing the same shape, this is the right
  thing to extract into a public adapter — until then it stays
  test-private.
  """

  def __init__(self, *, model: str) -> None:
    pytest.importorskip("google.genai")
    from google import genai

    self._model = model
    # Default Application Default Credentials path. Live test
    # invocation is responsible for ensuring the runtime can
    # authenticate (gcloud auth application-default login or a
    # service-account key on GOOGLE_APPLICATION_CREDENTIALS).
    self._client = genai.Client()

  def generate_json(self, prompt: str, schema: dict) -> dict:
    from google.genai import types

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
    )
    response = self._client.models.generate_content(
        model=self._model,
        contents=prompt,
        config=config,
    )
    text = response.text
    if not text:
      raise RuntimeError(
          "Live LLM returned an empty response; cannot parse plan JSON."
      )
    return json.loads(text)


def test_live_bka_compile_with_parity(bq_events, live_config, tmp_path):
  """End-to-end live proof.

  Pulls real ``bka_decision`` events from BigQuery, runs them
  through the c.2 retry loop with a real Gemini model, and
  asserts contract-level invariants. On success, regenerates the
  checked-in measurement artifact under
  ``tests/fixtures_extractor_compilation/bka_decision_measurement_report.json``.
  """
  from bigquery_agent_analytics.extractor_compilation import compile_extractor
  from bigquery_agent_analytics.extractor_compilation import measure_compile
  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE
  # Reuse the inline BKA YAML from the deterministic test module.
  # Importing from a sibling test file is fine — both are in
  # tests/, and the YAML is small enough that duplication would
  # be its own kind of drift risk.
  from tests.test_extractor_compilation_measurement import _BKA_BINDING_YAML
  from tests.test_extractor_compilation_measurement import _BKA_ONTOLOGY_YAML

  spec_dir = pathlib.Path(tempfile.mkdtemp(prefix="bka_live_compile_spec_"))
  (spec_dir / "ont.yaml").write_text(_BKA_ONTOLOGY_YAML, encoding="utf-8")
  (spec_dir / "bnd.yaml").write_text(_BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(spec_dir / "ont.yaml"))
  binding = load_binding(str(spec_dir / "bnd.yaml"), ontology=ontology)
  resolved_graph = resolve(ontology, binding)

  fingerprint_inputs = {
      "ontology_text": _BKA_ONTOLOGY_YAML,
      "binding_text": _BKA_BINDING_YAML,
      "event_schema": {"bka_decision": BKA_EVENT_SCHEMA["content"]},
      "event_allowlist": ("bka_decision",),
      "transcript_builder_version": "v0.1",
      "content_serialization_rules": {"strip_ansi": True},
      "extraction_rules": {
          "bka_decision": {
              "entity": "mako_DecisionPoint",
              "key_field": "decision_id",
          }
      },
  }

  def compile_source(plan, source: str):
    return compile_extractor(
        source=source,
        module_name="bka_live_extractor",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=bq_events,
        spec=None,
        resolved_graph=resolved_graph,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=fingerprint_inputs,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )

  llm_client = _GenaiLLMAdapter(model=live_config["model"])

  measurement = measure_compile(
      extraction_rule=BKA_EXTRACTION_RULE,
      event_schema=BKA_EVENT_SCHEMA,
      sample_events=bq_events,
      reference_extractor=extract_bka_decision_event,
      spec=None,
      llm_client=llm_client,
      compile_source=compile_source,
      max_attempts=5,
      model_name=live_config["model"],
      source=(
          f"live:{live_config['project']}.{live_config['dataset']}.agent_events"
      ),
  )

  # Regenerate the checked-in artifact whether or not the contract
  # invariants pass — a failed live run with the actual numbers is
  # the most useful artifact when it happens.
  artifact_path = (
      pathlib.Path(__file__).parent
      / "fixtures_extractor_compilation"
      / "bka_decision_measurement_report.json"
  )
  artifact_path.write_text(measurement.to_json() + "\n", encoding="utf-8")

  # Contract-level invariants only.
  assert measurement.ok, (
      f"live compile failed: ok={measurement.ok}, "
      f"reason={measurement.reason}, "
      f"attempt_failures={measurement.attempt_failures}, "
      f"parity_divergences={measurement.parity_divergences}"
  )
  assert measurement.parity_ok
  assert measurement.parity_divergences == ()
  assert measurement.n_attempts <= 3, (
      f"live compile took {measurement.n_attempts} attempts "
      f"(expected <= 3); attempt_failures={measurement.attempt_failures}"
  )
  assert measurement.n_events >= 2, (
      f"need at least 2 sample events to exercise both span-handling "
      f"branches; got {measurement.n_events}"
  )
  assert measurement.bundle_fingerprint is not None
  assert measurement.model_name == live_config["model"]
  assert measurement.source.startswith("live:")
