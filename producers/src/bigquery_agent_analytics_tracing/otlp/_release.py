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

"""Released receiver image reference (issue #349 release contract).

Written by scripts/release_image_tool.py during the release pipeline,
AFTER the image is built and its digest captured — a digest cannot appear
inside the artifact it is computed from, so this constant exists only in
the Python packaging artifacts, never in the image itself.

``None`` in development checkouts: bootstrap then requires an explicit
``--image`` or the source-build path.
"""

RELEASE_IMAGE = None
