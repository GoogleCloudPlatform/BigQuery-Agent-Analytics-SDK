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

"""Output formatting for CLI and Remote Function responses.

Supports three modes:

* ``json`` -- pretty-printed JSON via ``serialize()``.
* ``text`` -- calls ``.summary()`` or ``.render()`` if available,
  otherwise falls back to JSON.
* ``table`` -- simple columnar layout for lists of dicts.
"""

from __future__ import annotations

import contextlib
import io
import json
from typing import Any

from .serialization import serialize


def format_output(obj: Any, fmt: str = "json") -> str:
  """Format an SDK result for human or machine consumption.

  Args:
      obj: Any SDK return type.
      fmt: One of ``"json"``, ``"text"``, or ``"table"``.

  Returns:
      Formatted string.

  Raises:
      ValueError: If *fmt* is not recognised.
  """
  if fmt == "json":
    return _format_json(obj)
  if fmt == "text":
    return _format_text(obj)
  if fmt == "table":
    return _format_table(obj)
  raise ValueError(
      f"Unknown format: {fmt!r}. " f"Expected 'json', 'text', or 'table'."
  )


# ------------------------------------------------------------------ #
# Internal helpers                                                     #
# ------------------------------------------------------------------ #


def _format_json(obj: Any) -> str:
  return json.dumps(serialize(obj), indent=2)


def _format_text(obj: Any) -> str:
  """Use .summary() or .render() when available, else JSON.

  When calling ``.render()`` (e.g. on ``Trace``), stdout is
  suppressed because ``Trace.render()`` both prints and returns
  the same string.  The caller of ``format_output`` is
  responsible for printing the returned value.
  """
  if hasattr(obj, "summary") and callable(obj.summary):
    return obj.summary()
  if hasattr(obj, "render") and callable(obj.render):
    with contextlib.redirect_stdout(io.StringIO()):
      return obj.render()
  return _format_json(obj)


def _format_table(obj: Any) -> str:
  """Simple columnar format for list-like results."""
  data = serialize(obj)
  if isinstance(data, list) and data:
    if isinstance(data[0], dict):
      return _dict_list_to_table(data)
    return "\n".join(str(item) for item in data)
  if isinstance(data, dict):
    return _dict_to_table(data)
  return str(data)


def _dict_list_to_table(rows: list[dict[str, Any]]) -> str:
  """Render a list of dicts as a text table."""
  if not rows:
    return ""
  # Collect all keys across all rows to handle heterogeneous dicts.
  seen: dict[str, None] = {}
  for row in rows:
    for k in row:
      seen.setdefault(k, None)
  headers = list(seen)
  col_widths: dict[str, int] = {}
  for h in headers:
    max_val = max(
        (len(str(r.get(h, ""))) for r in rows),
        default=0,
    )
    col_widths[h] = min(max(len(h), max_val), 40)

  header_line = "  ".join(h.ljust(col_widths[h]) for h in headers)
  separator = "  ".join("-" * col_widths[h] for h in headers)
  lines = [header_line, separator]
  for row in rows:
    line = "  ".join(
        str(row.get(h, ""))[: col_widths[h]].ljust(col_widths[h])
        for h in headers
    )
    lines.append(line)
  return "\n".join(lines)


def _dict_to_table(data: dict[str, Any]) -> str:
  """Render a flat dict as key-value pairs."""
  if not data:
    return ""
  max_key = max(len(str(k)) for k in data)
  lines = []
  for k, v in data.items():
    lines.append(f"{str(k).ljust(max_key)}  {v}")
  return "\n".join(lines)
