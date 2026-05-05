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

"""Hand-authored compile fixtures for the extractor-compilation
scaffolding (issue #75 PR 4b.1).

PR 4b.1 ships compile-pipeline plumbing only; the LLM-driven
template-fill step lands in PR 4b.2. The fixtures here let the
pipeline be exercised end-to-end with hand-written source so AST
validation, smoke-test runner, manifest write-out, and the #76
validator gate can be tested in isolation from any LLM behavior.
"""
