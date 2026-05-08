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

# Pool size for the BigQuery fetch. We need a sample that covers
# both span-handling branches (events with ``content.reasoning_text``
# and events without it); a small fetch can fail to span both.
# Pool large enough that partitioning has slack, then cap the
# events we actually feed the LLM at ``_MAX_LIVE_EVENTS``.
_LIVE_QUERY_POOL_SIZE = 50

# Cap on events handed to the LLM compile loop. Keep small —
# the smoke gate just needs both branches represented; pulling
# thousands of events doesn't change what's proven and adds cost.
_MAX_LIVE_EVENTS = 10

# Floor for "minimum coverage of each branch we'll accept." If
# either branch has zero events after filtering, we skip with a
# message naming the missing branch — running the live LLM call
# without proving both branches contradicts the test/doc claim.
_MIN_EVENTS_PER_BRANCH = 1


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
LIMIT @pool_size
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
  """Pull a balanced batch of bka_decision events from BigQuery.

  The live test claims to prove both span-handling branches
  (events with ``content.reasoning_text`` go to
  ``partially_handled``; events without it go to
  ``fully_handled``). Pulling the latest N rows blindly can land
  with all rows in one branch — the LLM call would still
  succeed, but the parity assertion would only exercise one
  branch. To prevent that:

  1. Fetch a larger pool (``_LIVE_QUERY_POOL_SIZE``).
  2. Drop rows without ``content.decision_id`` — the reference
     extractor returns an empty result on those, so they can't
     demonstrate parity. Filtering them first avoids spending
     LLM tokens on events that can't move the assertion.
  3. Partition the remaining rows by ``content.reasoning_text``
     presence.
  4. ``pytest.skip`` if either partition is empty — running the
     LLM compile without both branches represented would leave
     the test/doc claim unproven.
  5. Take a balanced sample (up to ``_MAX_LIVE_EVENTS // 2``
     from each branch) and combine.
  """
  pytest.importorskip("google.cloud.bigquery")
  from google.cloud import bigquery

  client = bigquery.Client(project=live_config["project"], location="US")
  query = _LIVE_BKA_QUERY.format(
      project=live_config["project"], dataset=live_config["dataset"]
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "pool_size", "INT64", _LIVE_QUERY_POOL_SIZE
          )
      ]
  )
  rows = list(client.query(query, job_config=job_config).result())
  if not rows:
    pytest.skip(
        f"No bka_decision events in {live_config['project']}."
        f"{live_config['dataset']}.agent_events; cannot run live compile."
    )

  with_reasoning: list[dict] = []
  without_reasoning: list[dict] = []
  for row in rows:
    content = json.loads(row["content_json"]) if row["content_json"] else {}
    # Skip rows that the reference extractor can't produce a node
    # for — they'd be dead weight in the parity check.
    if not isinstance(content, dict) or not content.get("decision_id"):
      continue
    event = {
        "event_type": row["event_type"],
        "session_id": row["session_id"],
        "span_id": row["span_id"],
        "content": content,
    }
    if content.get("reasoning_text"):
      with_reasoning.append(event)
    else:
      without_reasoning.append(event)

  if len(with_reasoning) < _MIN_EVENTS_PER_BRANCH:
    pytest.skip(
        f"Live BKA sample has 0 events with content.reasoning_text "
        f"out of {_LIVE_QUERY_POOL_SIZE}-row pool; live test can't "
        f"prove the partially-handled span branch. Run with a wider "
        f"pool or against a project containing both branches."
    )
  if len(without_reasoning) < _MIN_EVENTS_PER_BRANCH:
    pytest.skip(
        f"Live BKA sample has 0 events WITHOUT content.reasoning_text "
        f"out of {_LIVE_QUERY_POOL_SIZE}-row pool; live test can't "
        f"prove the fully-handled span branch. Run with a wider pool "
        f"or against a project containing both branches."
    )

  per_branch = max(_MIN_EVENTS_PER_BRANCH, _MAX_LIVE_EVENTS // 2)
  return with_reasoning[:per_branch] + without_reasoning[:per_branch]


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

  Pulls real ``bka_decision`` events from BigQuery (the
  ``bq_events`` fixture filters for ``content.decision_id`` and
  enforces both span-handling branches via ``pytest.skip`` when
  either is absent), runs them through the c.2 retry loop with
  a real Gemini model, and asserts contract-level invariants.
  On success, regenerates the checked-in measurement artifact
  under
  ``tests/fixtures_extractor_compilation/bka_decision_measurement_report.json``.
  """
  # Defense-in-depth: the fixture should already have skipped if
  # either branch is absent, but we verify here so a future
  # fixture refactor that loosens the partition guard fails the
  # test rather than silently weakening the live proof.
  with_reasoning = sum(
      1 for e in bq_events if e["content"].get("reasoning_text")
  )
  without_reasoning = len(bq_events) - with_reasoning
  assert with_reasoning >= 1 and without_reasoning >= 1, (
      f"live sample must cover both span-handling branches; got "
      f"{with_reasoning} with reasoning_text, {without_reasoning} "
      f"without"
  )
  from bigquery_agent_analytics.extractor_compilation import compile_extractor
  from bigquery_agent_analytics.extractor_compilation import measure_compile
  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

  spec_dir = pathlib.Path(tempfile.mkdtemp(prefix="bka_live_compile_spec_"))
  (spec_dir / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
  (spec_dir / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(spec_dir / "ont.yaml"))
  binding = load_binding(str(spec_dir / "bnd.yaml"), ontology=ontology)
  resolved_graph = resolve(ontology, binding)

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
        fingerprint_inputs=BKA_FINGERPRINT_INPUTS,
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
