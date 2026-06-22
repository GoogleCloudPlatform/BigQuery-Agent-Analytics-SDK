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

"""Local-image embedding in the Colab notebook generator.

The codelab markdown references images by repo-relative path (e.g.
``images/ca-conversation.png``) so claat copies them into ``img/``. Colab,
however, cannot resolve a repo path, so the generator must embed local images
as base64 data URIs in the notebook. These tests pin that behavior so a future
local image cannot silently drift back to a broken Colab reference.
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

_GEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "generate_colab_from_codelab.py"
)

# A minimal valid 1x1 transparent PNG.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _load_generator():
  spec = importlib.util.spec_from_file_location("_gen_colab", _GEN_PATH)
  module = importlib.util.module_from_spec(spec)
  assert spec and spec.loader
  spec.loader.exec_module(module)
  return module


def _png(tmp_path: Path) -> Path:
  (tmp_path / "images").mkdir(exist_ok=True)
  path = tmp_path / "images" / "pic.png"
  path.write_bytes(_PNG_1x1)
  return path


def test_local_image_becomes_data_uri(tmp_path: Path) -> None:
  gen = _load_generator()
  _png(tmp_path)
  out = gen._embed_local_images("![alt text](images/pic.png)", tmp_path)
  assert out.startswith("![alt text](data:image/png;base64,")
  assert "images/pic.png" not in out
  # The embedded payload round-trips to the original bytes.
  payload = out.split("base64,", 1)[1].rstrip(")")
  assert base64.b64decode(payload) == _PNG_1x1


def test_remote_and_already_embedded_refs_unchanged(tmp_path: Path) -> None:
  gen = _load_generator()
  for src in (
      "![a](https://example.com/x.png)",
      "![a](http://example.com/x.png)",
      "![a](data:image/png;base64,AAAA)",
      "![a](attachment:x.png)",
  ):
    assert gen._embed_local_images(src, tmp_path) == src


def test_missing_local_image_raises(tmp_path: Path) -> None:
  gen = _load_generator()
  with pytest.raises(FileNotFoundError, match="not found"):
    gen._embed_local_images("![a](images/missing.png)", tmp_path)


def test_build_notebook_embeds_local_images(tmp_path: Path) -> None:
  gen = _load_generator()
  _png(tmp_path)
  md = "# Title\n\nSome prose.\n\n![cap](images/pic.png)\n"
  nb = gen.build_notebook(md, base_dir=tmp_path)
  joined = "".join(
      "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "markdown"
  )
  assert "data:image/png;base64," in joined
  assert "images/pic.png" not in joined


def test_build_notebook_without_base_dir_leaves_refs(tmp_path: Path) -> None:
  # Backward-compatible: no base_dir means no embedding.
  gen = _load_generator()
  nb = gen.build_notebook("![cap](images/pic.png)\n")
  joined = "".join(
      "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "markdown"
  )
  assert "images/pic.png" in joined
