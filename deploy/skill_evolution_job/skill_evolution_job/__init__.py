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

"""Scheduled skill-evolution Cloud Run Job.

Agentic wrapper around the SDK evolution engine
(``scripts/skill_evolution.py``): an ADK agent orchestrates
quality-report generation, bottleneck detection, skill evolution, and
PR creation against the host agent repository. Host-specific steps
(traffic generation, candidate scoring, publish gates) plug in through
the hook contract in :mod:`skill_evolution_job.hooks`.

The public surface of this package is the environment contract
(:mod:`skill_evolution_job.config`), the agent-registry schema
(:mod:`skill_evolution_job.registry`), and the hook contract
(:mod:`skill_evolution_job.hooks`). Hosts adopt the job by deploying it
and configuring those three seams — not by forking the package.
"""

__version__ = "0.1.0"

__all__ = [
    "agent",
    "bottleneck",
    "coevolve",
    "config",
    "engine",
    "evolve",
    "gcs_utils",
    "hooks",
    "main",
    "registry",
    "skill_loading",
    "tools",
]
