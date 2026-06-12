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

"""BigQuery Agent Analytics tracing producers.

Public API:

  * ``BigQueryAgentAnalyticsLogger`` — builds rows in the BQAA
    ``agent_events`` schema and routes them to spool / dry-run /
    direct-write.
  * ``BQAAConfig`` — environment- or constructor-driven configuration.
  * ``bq_schema`` — the canonical schema, used by both the auto-create
    path and the drainer's ``insert_rows_json`` fallback.

Producer modules:

  * ``claude_code`` — Claude Code hook adapter + ``main()`` entry.
    Console script: ``bqaa-claude-hook``.
  * ``setup_check`` — advisory check for env vars + runtime deps.
    Console script: ``bqaa-check-setup``. Used by the ``/bqaa-setup``
    Claude Code slash command.

OpenAI Agents SDK and Codex CLI adapters land in follow-up PRs.
"""

from ._writer_identity import __version__
from .config import BQAAConfig
from .logger import BigQueryAgentAnalyticsLogger
from .schema import bq_schema

__all__ = [
    "BQAAConfig",
    "BigQueryAgentAnalyticsLogger",
    "__version__",
    "bq_schema",
]
