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

"""WSGI entrypoint for the OTLP receiver on Cloud Run (issue #316, PR 3).

Deploy with gunicorn using the app factory::

    gunicorn --factory bigquery_agent_analytics_tracing.otlp.app:make_app

Config comes from environment variables; the real Pub/Sub publisher is
lazy-constructed (needs the ``receiver`` extra). The request logic lives in
``receiver.py`` and is fully unit-tested without a server or network.
"""

from __future__ import annotations

import http.client
import os
from typing import Any, Callable, Iterable

from bigquery_agent_analytics_tracing import _utils
from bigquery_agent_analytics_tracing.otlp import receiver


class PubSubPublisher:
  """``receiver.Publisher`` backed by google-cloud-pubsub (lazy import)."""

  def __init__(self) -> None:
    from google.cloud import pubsub_v1  # noqa: PLC0415 - optional dependency

    self._client = pubsub_v1.PublisherClient()

  def publish(self, topic: str, message: bytes) -> None:
    self._client.publish(topic, message).result()


def config_from_env() -> receiver.ReceiverConfig:
  return receiver.ReceiverConfig(
      expected_token=os.environ["BQAA_OTLP_TOKEN"],
      main_topic=os.environ["BQAA_OTLP_MAIN_TOPIC"],
      dlq_topic=os.environ["BQAA_OTLP_DLQ_TOPIC"],
      enable_traces=os.environ.get("BQAA_OTLP_ENABLE_TRACES", "0") == "1",
      source_product=os.environ.get("BQAA_OTLP_SOURCE_PRODUCT", "claude_code"),
  )


def make_app(
    config: receiver.ReceiverConfig | None = None,
    publisher: receiver.Publisher | None = None,
) -> Callable[[dict[str, Any], Callable], Iterable[bytes]]:
  """Build the WSGI app. Tests pass an explicit config + fake publisher."""
  config = config or config_from_env()
  publisher = publisher or PubSubPublisher()

  def app(environ: dict[str, Any], start_response: Callable) -> Iterable[bytes]:
    path = environ.get("PATH_INFO", "")
    if path == "/healthz":
      start_response("200 OK", [("Content-Type", "text/plain")])
      return [b"ok"]

    try:
      length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
      length = 0
    body = environ["wsgi.input"].read(length) if length else b""

    result = receiver.handle_export(
        path=path,
        body=body,
        content_type=environ.get("CONTENT_TYPE"),
        auth_header=environ.get("HTTP_AUTHORIZATION"),
        ingest_time=_utils.iso_timestamp(),
        config=config,
        publisher=publisher,
    )

    reason = http.client.responses.get(result.status, "")
    start_response(
        f"{result.status} {reason}", [("Content-Type", "application/json")]
    )
    return [
        receiver._encode(
            {
                "status": result.status,
                "published": result.published,
                "dead_lettered": result.dead_lettered,
                "message": result.message,
            }
        )
    ]

  return app
