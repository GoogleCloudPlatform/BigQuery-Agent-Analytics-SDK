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

"""Escaping for outside text that is printed into a CI log.

The GitHub Actions runner reads each stdout and stderr line of a step;
``\\n``, ``\\r`` and ``\\r\\n`` all end a line. It runs a line as a
workflow command when the line starts with ``::name::`` after leading
whitespace, or when it contains ``##[name]`` anywhere (actions/runner
``ActionCommand.TryParseV2`` and ``TryParse``). Session IDs, judge
replies and the metric names a judge chooses come from outside the CI
job, so the evaluation gates escape them here before they print or log
them.
"""

from __future__ import annotations


def escaped_log_pieces(text: str, quote: bool = False) -> list[str]:
  """Escape *text* for one CI log line, one piece per character.

  A backslash, and a double quote when *quote* is set, gets a backslash.
  A non-printable character, such as a line break or an ANSI escape,
  becomes its Python escape (``\\n``, ``\\x1b``). The ``[`` after ``##``
  becomes ``\\x5b``, and the first ``:`` of a ``::`` that opens the text,
  after any spaces, becomes ``\\x3a``. So the escaped text never breaks
  the line, never contains ``##[`` and cannot start a line with ``::``.
  A caller can truncate between pieces without splitting an escape.
  """
  pieces = []
  indent = len(text) - len(text.lstrip(" "))
  for index, char in enumerate(text):
    if char == "\\" or (quote and char == '"'):
      pieces.append("\\" + char)
    elif not char.isprintable():
      pieces.append(ascii(char)[1:-1])
    elif char == "[" and text[max(index - 2, 0) : index] == "##":
      pieces.append("\\x5b")
    elif index == indent and text.startswith("::", index):
      pieces.append("\\x3a")
    else:
      pieces.append(char)
  return pieces


def escape_log_text(text: str, quote: bool = False) -> str:
  """Return *text* escaped for one CI log line by escaped_log_pieces."""
  return "".join(escaped_log_pieces(text, quote))


def escape_encoded_log_text(encoded: str) -> str:
  """Escape ``##[`` in JSON or ``repr()`` output, keeping every value.

  ``json.dumps`` and ``repr()`` escape line breaks and other controls
  inside a string, so text inside a string can never begin a line, and
  ``##[`` is the one workflow command it can still carry. That ``[``
  becomes ``\\u005b``, an escape that JSON and Python string literals
  both decode back to ``[``.
  """
  return encoded.replace("##[", "##\\u005b")
