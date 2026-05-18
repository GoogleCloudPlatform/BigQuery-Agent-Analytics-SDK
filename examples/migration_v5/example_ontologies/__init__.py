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

"""Pluggability smoke fixtures for the migration v5 artifact pipeline.

Each module in this package defines a small
:class:`ontology_artifacts.OntologyConfig` paired with a TTL
file. The configs aren't meant for production deploys — they
exist to prove the pipeline in
:mod:`ontology_artifacts` is genuinely ontology-agnostic.

For the canonical reference example, see
:mod:`mako_artifacts` in the parent package.
"""
