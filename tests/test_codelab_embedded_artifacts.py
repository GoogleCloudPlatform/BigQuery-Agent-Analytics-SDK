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

"""The codelab embeds its artifacts inline so the notebook is self-contained.

Those embedded copies must stay byte-for-byte in sync with the canonical files
in ``examples/codelab/periodic_materialization/``; otherwise the codelab would
write stale artifacts. This test fails if either side drifts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CODELAB = _REPO / "docs" / "codelabs" / "periodic_materialization.md"
_ARTIFACTS = _REPO / "examples" / "codelab" / "periodic_materialization"


@pytest.mark.parametrize(
    "name",
    ["table_ddl.sql", "property_graph.sql", "ontology.yaml", "binding.yaml"],
)
def test_embedded_artifact_matches_canonical(name: str) -> None:
  canonical = (_ARTIFACTS / name).read_text(encoding="utf-8").rstrip("\n")
  codelab = _CODELAB.read_text(encoding="utf-8")
  assert canonical in codelab, (
      f"The codelab's embedded copy of {name} has drifted from"
      f" examples/codelab/periodic_materialization/{name}. Re-run the embed so"
      f" the self-contained notebook writes the current artifact."
  )
