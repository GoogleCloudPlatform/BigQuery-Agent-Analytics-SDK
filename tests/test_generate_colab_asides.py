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

"""Claat aside markers must not leak into the generated Colab notebook.

The codelab follows the claat callout convention (``> aside positive`` /
``> aside negative``) so the published codelab renders proper callout boxes.
Colab has no notion of those markers, so the generator drops the marker line
and keeps the callout body as a plain Markdown blockquote. These tests pin
that behavior so the marker text can never reappear inside the notebook.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_GEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "generate_colab_from_codelab.py"
)


def _load_generator():
  spec = importlib.util.spec_from_file_location("_gen_colab", _GEN_PATH)
  module = importlib.util.module_from_spec(spec)
  assert spec and spec.loader
  spec.loader.exec_module(module)
  return module


def _markdown_text(nb: dict) -> str:
  return "".join(
      "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "markdown"
  )


def test_aside_marker_dropped_body_kept() -> None:
  gen = _load_generator()
  md = (
      "# Title\n\n"
      "Some prose.\n\n"
      "> aside positive\n"
      "> **Tip:** keep this body text.\n\n"
      "More prose.\n"
  )
  nb = gen.build_notebook(md)
  text = _markdown_text(nb)
  assert "aside positive" not in text
  assert "> **Tip:** keep this body text." in text


def test_aside_negative_marker_dropped() -> None:
  gen = _load_generator()
  md = "# Title\n\n> aside negative\n> Watch out for this.\n"
  nb = gen.build_notebook(md)
  text = _markdown_text(nb)
  assert "aside negative" not in text
  assert "> Watch out for this." in text


def test_duration_line_still_stripped() -> None:
  # The MM:SS duration format used to match #96 must still be treated as
  # claat publishing metadata and stripped from the notebook narrative.
  gen = _load_generator()
  md = "# Title\n\n## Introduction\n\nDuration: 05:00\n\nReal prose.\n"
  nb = gen.build_notebook(md)
  text = _markdown_text(nb)
  assert "Duration: 05:00" not in text
  assert "Real prose." in text
