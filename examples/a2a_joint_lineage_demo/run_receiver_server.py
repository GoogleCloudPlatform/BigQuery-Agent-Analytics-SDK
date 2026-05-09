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

"""Run the receiver A2A server with the BQ AA Plugin attached.

`to_a2a()`'s default-runner path builds its own ``Runner`` with no
``plugins=`` argument — using it as-is would silently drop receiver
telemetry. This driver constructs the runner explicitly with the BQ
AA Plugin in ``plugins=[...]`` and passes it via ``runner=``, which
is the only mechanically correct way to wire the receiver-side
plugin today.

The bound URL prints to stdout. Smoke-test it with::

    ./.venv/bin/python3 smoke_receiver.py

Stop the server with Ctrl-C; ADK's lifespan flushes the plugin on
shutdown.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from receiver_agent import APP_NAME
from receiver_agent import build_receiver_plugin
from receiver_agent import root_agent
import uvicorn

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_HERE, ".env")
if os.path.exists(_ENV_PATH):
  load_dotenv(dotenv_path=_ENV_PATH)


def main() -> int:
  receiver_url = os.getenv("RECEIVER_A2A_URL", "http://127.0.0.1:8000")
  parsed = urlparse(receiver_url)
  if not parsed.hostname or not parsed.port:
    print(
        f"ERROR: RECEIVER_A2A_URL={receiver_url!r} must include host "
        "and port, e.g. http://127.0.0.1:8000",
        file=sys.stderr,
    )
    return 2
  host = parsed.hostname
  port = parsed.port
  protocol = parsed.scheme or "http"

  receiver_plugin = build_receiver_plugin()

  # Build the runner explicitly with the BQ AA Plugin attached.
  # Using ``to_a2a(receiver_agent)`` without ``runner=`` would build a
  # plugin-free Runner and zero rows would land in the receiver's
  # agent_events table. ``InMemorySessionService`` honors explicit
  # session ids, which is what makes
  # ``caller.A2A_INTERACTION.a2a_context_id == receiver.session_id``
  # hold for the demo's standard path.
  runner = Runner(
      app_name=APP_NAME,
      agent=root_agent,
      plugins=[receiver_plugin],
      artifact_service=InMemoryArtifactService(),
      session_service=InMemorySessionService(),
      memory_service=InMemoryMemoryService(),
      credential_service=InMemoryCredentialService(),
  )

  app = to_a2a(
      root_agent,
      host=host,
      port=port,
      protocol=protocol,
      runner=runner,
  )

  print(f"Receiver A2A server starting on {protocol}://{host}:{port}")
  print(f"Agent card: {protocol}://{host}:{port}/.well-known/agent-card.json")
  print(
      "BQ AA Plugin attached; receiver spans will land in "
      f"{receiver_plugin.project_id}.{receiver_plugin.dataset_id}."
      f"{receiver_plugin.table_id}"
  )
  print("Smoke test in another shell:  ./.venv/bin/python3 smoke_receiver.py")
  print("Stop with Ctrl-C.")
  uvicorn.run(app, host=host, port=port, log_level="info")
  return 0


if __name__ == "__main__":
  sys.exit(main())
