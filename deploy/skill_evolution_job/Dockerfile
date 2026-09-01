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

FROM python:3.11-slim

WORKDIR /app

# git: clone the host agent repo + create skill branches.
# curl: fetch the GitHub CLI keyring below.
RUN apt-get update && apt-get install -y git curl \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (gh) for PR/issue creation.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# SDK scripts staged by deploy.sh (skill_evolution.py = the engine,
# quality_report.py = the BigQuery pre-flight, plus eval config).
COPY scripts /app/scripts

# The job component.
COPY main.py .
COPY skill_evolution_job ./skill_evolution_job

ENV PYTHONPATH=/app \
    SDK_SCRIPTS_DIR=/app/scripts

ENTRYPOINT ["python", "main.py"]
