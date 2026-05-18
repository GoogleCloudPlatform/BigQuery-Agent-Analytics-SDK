#!/usr/bin/env bash
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

# Auto-format Python files before every PR.
# Usage: bash autoformat.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "==> Running isort (import sorting)..."
isort "${REPO_ROOT}/src/" "${REPO_ROOT}/tests/" "${REPO_ROOT}/examples/" "${REPO_ROOT}/scripts/"

echo "==> Running pyink (code formatting)..."
pyink --config "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/src/" "${REPO_ROOT}/tests/" "${REPO_ROOT}/examples/" "${REPO_ROOT}/scripts/"

echo "==> Done. All Python files formatted."
