#!/usr/bin/env python3
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

"""Container entry point: delegates to the skill_evolution_job package."""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
  sys.path.insert(0, _here)

from skill_evolution_job.main import main  # noqa: E402

if __name__ == "__main__":
  main()
