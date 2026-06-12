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

"""Pluggability smoke fixture: Simple Request Flow ontology config.

Exists to prove the generic artifact pipeline in
:mod:`ontology_artifacts` accepts any OWL TTL — not just
MAKO's. The TTL is domain-neutral (Request → Action →
Outcome) and intentionally simple (three entities, two
relationships, no cross-namespace imports, no inheritance).

The :data:`SIMPLE_REQUEST_FLOW_CONFIG` plugs into
:func:`ontology_artifacts.regenerate_snapshots` exactly the
same way :data:`mako_artifacts.MAKO_CONFIG` does. No runnable
agent ships with this fixture — it's a smoke test for the
pipeline, not an alternate demo.
"""

from __future__ import annotations

import pathlib

from ontology_artifacts import OntologyConfig

_FIXTURE_DIR = pathlib.Path(__file__).parent

SIMPLE_REQUEST_FLOW_CONFIG = OntologyConfig(
    ttl_path=_FIXTURE_DIR / "simple_request_flow.ttl",
    include_namespace="https://example.com/request-flow/",
    entity_allowlist=("Request", "Action", "Outcome"),
    annotation_prefix="simple_request_flow",
    graph_name="simple_request_flow_graph",
    snapshot_dir=_FIXTURE_DIR,
)
