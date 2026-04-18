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

"""Tests that each SDK entry point stamps the right ``sdk_surface``.

- Direct Python API users get ``python`` (default).
- ``cli._build_client()`` passes ``sdk_surface="cli"``.
- ``_deploy_runtime.resolve_client_options()`` defaults
  ``sdk_surface="remote-function"`` (with override support).
"""

from unittest import mock

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

from bigquery_agent_analytics._deploy_runtime import build_client_from_context
from bigquery_agent_analytics._deploy_runtime import resolve_client_options
from bigquery_agent_analytics._telemetry import LabeledBigQueryClient
from bigquery_agent_analytics.cli import _build_client
from bigquery_agent_analytics.client import Client


def _make_client(**kwargs):
  kwargs.setdefault("project_id", "p")
  kwargs.setdefault("dataset_id", "d")
  kwargs.setdefault("verify_schema", False)
  return Client(**kwargs)


class TestClientDefaultSurface:

  def test_default_surface_is_python(self):
    client = _make_client()
    # Force lazy construction through a known factory.
    with mock.patch(
        "bigquery_agent_analytics.client.make_bq_client"
    ) as factory:
      factory.return_value = mock.MagicMock(spec=LabeledBigQueryClient)
      _ = client.bq_client
    # make_bq_client is called with the surface threaded through.
    factory.assert_called_once()
    assert factory.call_args.kwargs.get("sdk_surface") == "python"

  def test_explicit_surface_flows_into_make_bq_client(self):
    client = _make_client(sdk_surface="cli")
    with mock.patch(
        "bigquery_agent_analytics.client.make_bq_client"
    ) as factory:
      factory.return_value = mock.MagicMock(spec=LabeledBigQueryClient)
      _ = client.bq_client
    assert factory.call_args.kwargs.get("sdk_surface") == "cli"

  def test_jobs_emitted_carry_the_chosen_surface(self):
    # Build a real LabeledBigQueryClient via make_bq_client and dispatch
    # a query. The surface the Client was constructed with must end up
    # on the QueryJobConfig labels.
    with mock.patch(
        "bigquery_agent_analytics.client.make_bq_client"
    ) as factory:
      labeled = LabeledBigQueryClient(
          project="p",
          credentials=AnonymousCredentials(),
          sdk_surface="remote-function",
      )
      factory.return_value = labeled
      client = _make_client(sdk_surface="remote-function")
      with mock.patch.object(bigquery.Client, "query") as parent_query:
        # Trigger any query that uses self.bq_client. get_trace raises
        # when result is empty, so wrap; we only care about the kwargs.
        try:
          client.get_trace("tid")
        except Exception:
          pass
      job_config = parent_query.call_args.kwargs.get("job_config")
      assert job_config is not None
      assert job_config.labels.get("sdk_surface") == "remote-function"


class TestCliEntryPointSurface:

  def test_build_client_passes_sdk_surface_cli(self):
    captured = {}

    def fake_client(**kwargs):
      captured.update(kwargs)
      return mock.MagicMock()

    with mock.patch(
        "bigquery_agent_analytics.client.Client", side_effect=fake_client
    ):
      _build_client(project_id="p", dataset_id="d", table_id="agent_events")

    assert captured.get("sdk_surface") == "cli"


class TestDeployRuntimeSurface:

  def test_resolve_client_options_defaults_remote_function(self):
    opts = resolve_client_options(
        {"project_id": "p", "dataset_id": "d"},
    )
    assert opts["sdk_surface"] == "remote-function"

  def test_resolve_client_options_allows_override(self):
    opts = resolve_client_options(
        {"project_id": "p", "dataset_id": "d"},
        sdk_surface="continuous-query",
    )
    assert opts["sdk_surface"] == "continuous-query"

  def test_build_client_from_context_threads_surface_through(self):
    captured = {}

    def fake_client(**kwargs):
      captured.update(kwargs)
      return mock.MagicMock()

    with mock.patch(
        "bigquery_agent_analytics._deploy_runtime.Client",
        side_effect=fake_client,
    ):
      build_client_from_context({"project_id": "p", "dataset_id": "d"})

    assert captured.get("sdk_surface") == "remote-function"
