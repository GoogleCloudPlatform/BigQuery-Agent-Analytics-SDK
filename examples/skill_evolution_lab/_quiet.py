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

"""Silence noisy third-party warnings and loggers for the lab's CLI scripts.

Import this BEFORE any google/genai imports (``import _quiet``) -- the noisy
loggers must be muted before the SDKs configure them, and authlib forces
``simplefilter("always")`` at its own import time.

Shared by ``run_agent.py`` and ``analyze_and_evolve.py`` so the suppression
list lives in exactly one place.
"""

import logging
import warnings

warnings.filterwarnings("ignore")

# authlib forces simplefilter("always") at import time; neutralise early.
try:
  import authlib.deprecate

  warnings.filterwarnings(
      "ignore", category=authlib.deprecate.AuthlibDeprecationWarning
  )
except ImportError:
  pass

# Suppress noisy SDK loggers before any google imports ("AFC is enabled",
# "...will take precedence", "HTTP Request: POST ...").
for _noisy in (
    "google.genai",
    "google_genai",
    "google.auth",
    "google_auth",
    "google.adk",
    "google_adk",
    "httpx",
    "httpcore",
):
  logging.getLogger(_noisy).setLevel(logging.ERROR)
