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

"""Writer identity stamped onto every row's `attributes.writer` block.

Two surfaces read this:

  * Every row's `attributes.writer.{plugin,version,label,agent,mode}` block
    — the queryable surface for self-service adoption analytics.
  * `AppendRowsRequest.trace_id` on Storage Write API batches — recorded
    server-side by Google for diagnostics. Not exposed as a column in
    `INFORMATION_SCHEMA.WRITE_API_TIMELINE_BY_*`, so it cannot power
    adoption queries; it lets Google Cloud support attribute traffic back
    to this package during throughput/quota investigations.

Override per deployment with the `BQAA_WRITER_LABEL` env var (e.g. when
running multiple distinct deployments against one dataset).
"""

from __future__ import annotations

from importlib import metadata

WRITER_PLUGIN_NAME = "bigquery-agent-analytics-tracing"


def get_writer_version() -> str:
  """Resolve the writer version from package metadata.

  Falls back to ``"0.0.0+local"`` for vendored installs (e.g. a Claude Code
  plugin that ships the package source without a wheel install) and for
  source checkouts where the distribution is not installed.
  """
  try:
    return metadata.version(WRITER_PLUGIN_NAME)
  except metadata.PackageNotFoundError:
    return "0.0.0+local"


__version__ = get_writer_version()
DEFAULT_WRITER_LABEL = f"{WRITER_PLUGIN_NAME}/{__version__}"
