#!/usr/bin/env python3
"""Generate a Colab notebook from a codelab markdown source.

The codelab markdown is the source of truth. This script reads it,
applies the ``<!-- colab:... -->`` marker convention, and emits a
runnable Jupyter notebook (``nbformat 4.5``) with mixed markdown and
code cells.

Marker convention
-----------------

Inline markers immediately precede a fenced code block::

    <!-- colab:code bash -->
    ```bash
    gcloud services enable bigquery.googleapis.com
    ```

The fenced block becomes a notebook code cell. ``bash`` blocks have
each non-blank, non-comment line prefixed with ``!`` (Jupyter's shell
magic). ``export VAR="VAL"`` lines become ``os.environ["VAR"] = "VAL"``
so variables persist across cells. ``python`` blocks are copied verbatim.

::

    <!-- colab:markdown -->
    ```sql
    SELECT * FROM agent_decisions_graph
    ```

The fenced block stays as an illustrative markdown fence in the notebook.
This is also the default for any fenced block with no preceding marker.

::

    <!-- colab:skip -->
    ```bash
    export PROJECT_ID="your-project-id"
    ```

The fenced block is omitted from the notebook entirely. Used when an
override directly below replaces this block with a notebook-specific
alternative.

Override-content markers carry their content inside an HTML comment so
the codelab reader does not see it::

    <!-- colab:cell python
    import os
    os.environ["PROJECT_ID"] = "your-project-id"
    -->

The block between ``<!-- colab:cell python`` and ``-->`` becomes a
notebook Python code cell. ``<!-- colab:cell markdown ... -->`` is the
same shape but emits a markdown cell.

Codelab frontmatter
-------------------

The claat-style frontmatter at the top of the codelab (``summary:``,
``id:``, etc., up to but not including the first ``#`` heading) is
dropped from the notebook. The first ``# Title`` heading and everything
after it forms the notebook content.

Usage
-----

::

    # Generate
    python scripts/generate_colab_from_codelab.py \\
        docs/codelabs/periodic_materialization.md \\
        examples/context_graph/codelab/colab_notebook.ipynb

    # Check (CI mode): exit 1 if the notebook on disk differs from
    # what the generator would produce.
    python scripts/generate_colab_from_codelab.py --check \\
        docs/codelabs/periodic_materialization.md \\
        examples/context_graph/codelab/colab_notebook.ipynb
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
from pathlib import Path
import re
import sys
from typing import Iterator

# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

MARKER_INLINE = re.compile(
    r"^<!--\s*colab:(code\s+\w+|markdown|skip)\s*-->\s*$"
)
MARKER_BLOCK_OPEN = re.compile(r"^<!--\s*colab:cell\s+(python|markdown)\s*$")
FENCE_OPEN = re.compile(r"^```(\w*)\s*$")
FENCE_CLOSE = re.compile(r"^```\s*$")
EXPORT_LINE = re.compile(r"^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")

# Claat-specific metadata lines that should not leak into the notebook
# narrative. ``Duration: 03:00`` is the codelab renderer's per-section
# time hint and reads like leaked publishing metadata in Colab.
CLAAT_METADATA_LINE = re.compile(r"^Duration:\s+\d+:\d+\s*$")

# Claat aside markers (``> aside positive`` / ``> aside negative``) open a
# callout box in the rendered codelab. claat consumes that first line; the
# blockquote lines beneath it are the callout body. Colab has no notion of
# claat asides, so the marker line would render as the literal text
# "aside positive" inside a blockquote. Drop it from the notebook and let
# the remaining ``> ...`` lines render as an ordinary Markdown blockquote.
CLAAT_ASIDE_LINE = re.compile(r"^>\s*aside\s+(?:positive|negative)\s*$")


def _strip_frontmatter(lines: list[str]) -> list[str]:
  """Drop claat-style frontmatter (lines before the first ``#`` heading)."""
  for i, line in enumerate(lines):
    if line.startswith("# "):
      return lines[i:]
  return lines


def _read_block_marker_content(
    lines: list[str], start: int
) -> tuple[list[str], int]:
  """Given index of a ``<!-- colab:cell ... `` opening line, return the
  content between it and the ``-->`` close, plus the index of the line
  immediately after ``-->``."""
  i = start + 1
  content: list[str] = []
  while i < len(lines) and lines[i].rstrip() != "-->":
    content.append(lines[i])
    i += 1
  if i >= len(lines):
    raise ValueError(
        f"Unterminated <!-- colab:cell ... --> block starting at line"
        f" {start + 1}: missing closing -->"
    )
  return content, i + 1


def _read_fence(lines: list[str], start: int) -> tuple[str, list[str], int]:
  """Given index of an opening fence line, return ``(lang, body, idx_after_close)``."""
  open_match = FENCE_OPEN.match(lines[start])
  if not open_match:
    raise ValueError(
        f"Expected fence open at line {start + 1}, got: {lines[start]!r}"
    )
  lang = open_match.group(1)
  i = start + 1
  body: list[str] = []
  while i < len(lines) and not FENCE_CLOSE.match(lines[i]):
    body.append(lines[i])
    i += 1
  if i >= len(lines):
    raise ValueError(f"Unterminated fenced block starting at line {start + 1}")
  return lang, body, i + 1


def _skip_blank_to_fence(lines: list[str], start: int) -> int:
  """Advance past blank lines to the next opening fence; return its index.

  Raises if no fence is found before the end of the file.
  """
  i = start
  while i < len(lines):
    if FENCE_OPEN.match(lines[i]):
      return i
    if lines[i].strip() == "":
      i += 1
      continue
    raise ValueError(
        f"Expected fenced block to follow the colab marker, but found"
        f" content at line {i + 1}: {lines[i]!r}"
    )
  raise ValueError("Colab marker not followed by a fenced block")


def parse_blocks(markdown: str) -> Iterator[dict]:
  """Walk the markdown and yield notebook blocks.

  Each yielded dict has either:
    * {"kind": "markdown", "source": str} for narrative markdown
    * {"kind": "code_bash", "source": str} for executable bash → !-prefixed
    * {"kind": "code_python", "source": str} for executable python verbatim
  """
  raw_lines = markdown.split("\n")
  lines = _strip_frontmatter(raw_lines)

  md_buffer: list[str] = []
  i = 0

  def flush_md() -> Iterator[dict]:
    nonlocal md_buffer
    text = "\n".join(md_buffer).strip("\n")
    if text:
      yield {"kind": "markdown", "source": text}
    md_buffer = []

  while i < len(lines):
    line = lines[i]

    # Multi-line override block: <!-- colab:cell LANG ... -->
    block_open = MARKER_BLOCK_OPEN.match(line)
    if block_open:
      lang = block_open.group(1)
      content, i_after = _read_block_marker_content(lines, i)
      yield from flush_md()
      if lang == "python":
        yield {"kind": "code_python", "source": "\n".join(content)}
      elif lang == "markdown":
        yield {"kind": "markdown", "source": "\n".join(content)}
      else:
        raise ValueError(f"Unknown colab:cell language: {lang!r}")
      i = i_after
      continue

    # Inline marker preceding a fenced block
    inline = MARKER_INLINE.match(line)
    if inline:
      directive = inline.group(1)
      # Move past the marker (and any blank lines) to the next fence
      i += 1
      try:
        fence_start = _skip_blank_to_fence(lines, i)
      except ValueError as e:
        raise ValueError(f"colab marker at line {i}: {e}") from e
      # If there are blank lines between the marker and the fence, those
      # are stripped (they would have been narrative spacing).
      lang, body, i_after = _read_fence(lines, fence_start)
      if directive.startswith("code "):
        target_lang = directive.split()[1]
        yield from flush_md()
        if target_lang == "bash":
          yield {"kind": "code_bash", "source": "\n".join(body)}
        elif target_lang == "python":
          yield {"kind": "code_python", "source": "\n".join(body)}
        else:
          raise ValueError(f"Unknown colab:code language: {target_lang!r}")
      elif directive == "markdown":
        # Keep the fence as an illustrative markdown block. Append the
        # full fenced block back into the markdown buffer.
        md_buffer.append(f"```{lang}")
        md_buffer.extend(body)
        md_buffer.append("```")
      elif directive == "skip":
        # Drop the fence entirely from notebook output. Codelab markdown
        # still contains it because the renderer doesn't process this
        # script's markers; the script just omits it from the notebook.
        pass
      else:
        raise ValueError(f"Unknown inline colab marker: {directive!r}")
      i = i_after
      continue

    # Unmarked fenced block → keep as illustrative markdown (safe default).
    if FENCE_OPEN.match(line):
      lang, body, i_after = _read_fence(lines, i)
      md_buffer.append(f"```{lang}")
      md_buffer.extend(body)
      md_buffer.append("```")
      i = i_after
      continue

    # Skip claat-only metadata lines (Duration: 03:00 etc.) so they
    # do not appear as visible prose in the generated notebook.
    if CLAAT_METADATA_LINE.match(line):
      i += 1
      continue

    # Drop claat aside marker lines (``> aside positive``/``> aside
    # negative``) so the callout body renders as a plain blockquote in
    # Colab instead of leaking the literal marker text.
    if CLAAT_ASIDE_LINE.match(line):
      i += 1
      continue

    # Regular narrative line
    md_buffer.append(line)
    i += 1

  yield from flush_md()


# ---------------------------------------------------------------------------
# Bash → notebook code translation
# ---------------------------------------------------------------------------


def bash_to_notebook_code(body: str) -> str:
  """Translate a bash code block into notebook-runnable Python code.

  Rules:
    * ``export VAR="VAL"`` lines become ``os.environ["VAR"] = "VAL"``.
      A single ``import os`` is added at the top of the cell if any
      export translation happens.
    * All other non-blank, non-comment lines are prefixed with ``!``
      so Jupyter runs them in a subshell.
    * Continuation lines (``\\``-terminated) are joined back into one
      logical ``!``-prefixed line so the shell sees one command.
    * Comment lines (``#`` prefix) and blank lines are preserved
      verbatim.
  """
  # First, fold ``\``-continuation lines.
  raw_lines = body.split("\n")
  logical_lines: list[tuple[str, str]] = []  # (kind, content)
  pending: list[str] = []
  for raw in raw_lines:
    stripped = raw.rstrip()
    if pending:
      pending.append(stripped)
      if not stripped.endswith("\\"):
        logical_lines.append(("cmd", " ".join(pending).replace("\\ ", " ")))
        pending = []
      continue
    if stripped == "":
      logical_lines.append(("blank", ""))
      continue
    if stripped.lstrip().startswith("#"):
      logical_lines.append(("comment", stripped))
      continue
    if stripped.endswith("\\"):
      pending.append(stripped)
      continue
    logical_lines.append(("cmd", stripped))
  if pending:
    # Unterminated continuation; treat as single line.
    logical_lines.append(("cmd", " ".join(pending).rstrip("\\").strip()))

  # Translate exports into a leading os.environ block.
  env_assignments: list[str] = []
  other_lines: list[tuple[str, str]] = []
  for kind, content in logical_lines:
    if kind == "cmd":
      m = EXPORT_LINE.match(content)
      if m:
        var, val = m.group(1), m.group(2)
        # Strip surrounding quotes from val for the os.environ assignment.
        val_inner = val
        if (val_inner.startswith('"') and val_inner.endswith('"')) or (
            val_inner.startswith("'") and val_inner.endswith("'")
        ):
          val_inner = val_inner[1:-1]
        env_assignments.append(f'os.environ["{var}"] = {json.dumps(val_inner)}')
        continue
    other_lines.append((kind, content))

  # Join consecutive shell commands with ``&&`` so they run in a single
  # subshell. Without this, a leading ``cd X`` line would change
  # directory in its own subshell and the next ``!`` invocation would
  # run back in the notebook's cwd. Comments and blank lines act as
  # natural separators that flush the in-progress ``&&`` group.
  output: list[str] = []
  if env_assignments:
    output.append("import os")
    output.append("")
    output.extend(env_assignments)
    if other_lines and any(k != "blank" for k, _ in other_lines):
      output.append("")

  cmd_group: list[str] = []

  def flush_cmd_group() -> None:
    if not cmd_group:
      return
    if len(cmd_group) == 1:
      output.append(f"!{cmd_group[0]}")
    else:
      # First command starts with "!", subsequent commands continue on
      # new lines under " && \" line continuations for readability.
      first = cmd_group[0]
      rest = cmd_group[1:]
      output.append(f"!{first} && \\")
      for i, c in enumerate(rest):
        suffix = " && \\" if i < len(rest) - 1 else ""
        output.append(f"    {c}{suffix}")
    cmd_group.clear()

  for kind, content in other_lines:
    if kind == "blank":
      flush_cmd_group()
      output.append("")
    elif kind == "comment":
      flush_cmd_group()
      output.append(content)
    else:  # cmd
      cmd_group.append(content)
  flush_cmd_group()

  return "\n".join(output).rstrip("\n")


# ---------------------------------------------------------------------------
# Notebook construction
# ---------------------------------------------------------------------------


_IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}


def _embed_local_images(text: str, base_dir: Path) -> str:
  """Rewrite repo-relative markdown image refs into base64 data URIs.

  The codelab markdown references images by relative path (so claat copies
  them into ``img/``), but Colab cannot resolve a repo path. Embedding the
  image as a data URI makes the generated notebook render it inline. Remote
  (``http(s)://``), ``data:``, and ``attachment:`` refs are left unchanged.
  """

  def _replace(match: "re.Match[str]") -> str:
    alt, src = match.group(1), match.group(2).strip()
    if src.startswith(("http://", "https://", "data:", "attachment:")):
      return match.group(0)
    img_path = (base_dir / src).resolve()
    if not img_path.is_file():
      raise FileNotFoundError(
          f"Local image referenced in the codelab markdown was not found:"
          f" {src!r} (resolved to {img_path}). Notebook images must resolve"
          f" so the generated Colab cell is not broken."
      )
    mime = _IMAGE_MIME.get(img_path.suffix.lower())
    if mime is None:
      raise ValueError(
          f"Unsupported image type for notebook embedding: {src!r}."
      )
    encoded = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"![{alt}](data:{mime};base64,{encoded})"

  return _IMAGE_MD.sub(_replace, text)


def build_notebook(markdown: str, base_dir: Path | None = None) -> dict:
  """Return a notebook dict matching nbformat 4.5.

  When ``base_dir`` is given, local markdown image references are embedded
  as base64 data URIs (resolved relative to ``base_dir``) so the generated
  notebook renders them in Colab, which cannot resolve repo-relative paths.
  """
  cells: list[dict] = []
  for block in parse_blocks(markdown):
    if block["kind"] == "markdown":
      cells.append(_markdown_cell(block["source"], base_dir))
    elif block["kind"] == "code_python":
      cells.append(_code_cell(block["source"]))
    elif block["kind"] == "code_bash":
      cells.append(_code_cell(bash_to_notebook_code(block["source"])))
    else:
      raise ValueError(f"Unhandled block kind: {block['kind']!r}")
  return {
      "cells": cells,
      "metadata": {
          "kernelspec": {
              "display_name": "Python 3",
              "language": "python",
              "name": "python3",
          },
          "language_info": {
              "codemirror_mode": {"name": "ipython", "version": 3},
              "file_extension": ".py",
              "mimetype": "text/x-python",
              "name": "python",
              "nbconvert_exporter": "python",
              "pygments_lexer": "ipython3",
              "version": "3.10",
          },
      },
      "nbformat": 4,
      "nbformat_minor": 5,
  }


def _markdown_cell(source: str, base_dir: Path | None = None) -> dict:
  if base_dir is not None:
    source = _embed_local_images(source, base_dir)
  return {
      "cell_type": "markdown",
      "metadata": {},
      "source": _splitlines_keep(source),
  }


def _code_cell(source: str) -> dict:
  return {
      "cell_type": "code",
      "execution_count": None,
      "metadata": {},
      "outputs": [],
      "source": _splitlines_keep(source),
  }


def _splitlines_keep(text: str) -> list[str]:
  """Split text into the list-of-strings shape nbformat expects.

  Each entry in the returned list ends in a newline, except the last
  entry which is the final partial line (no trailing newline). Empty
  text returns an empty list.
  """
  if not text:
    return []
  parts = text.split("\n")
  return [p + "\n" for p in parts[:-1]] + [parts[-1]]


# ---------------------------------------------------------------------------
# I/O + check mode
# ---------------------------------------------------------------------------


def _normalize_json(obj: dict) -> str:
  """Serialize the notebook with stable indentation for byte comparison."""
  return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description="Generate a Colab notebook from a codelab markdown source."
  )
  parser.add_argument(
      "markdown", type=Path, help="Path to the codelab markdown."
  )
  parser.add_argument(
      "notebook",
      type=Path,
      help="Path to the output notebook (.ipynb).",
  )
  parser.add_argument(
      "--check",
      action="store_true",
      help=(
          "Do not write the notebook. Exit 1 if the on-disk notebook"
          " differs from what would be generated."
      ),
  )
  args = parser.parse_args(argv)

  markdown_text = args.markdown.read_text(encoding="utf-8")
  generated = build_notebook(markdown_text, base_dir=args.markdown.parent)
  generated_text = _normalize_json(generated)

  if args.check:
    if not args.notebook.exists():
      print(
          f"ERROR: {args.notebook} does not exist; run without --check"
          " first.",
          file=sys.stderr,
      )
      return 1
    on_disk = args.notebook.read_text(encoding="utf-8")
    if on_disk == generated_text:
      print(
          f"OK: {args.notebook} matches what"
          f" {args.markdown} would generate."
      )
      return 0
    print(
        f"DRIFT: {args.notebook} is out of sync with {args.markdown}.",
        file=sys.stderr,
    )
    print(
        "Run without --check to regenerate, then commit both files.",
        file=sys.stderr,
    )
    # Print a small diff to help diagnose.
    diff = difflib.unified_diff(
        on_disk.splitlines(),
        generated_text.splitlines(),
        fromfile=str(args.notebook) + " (on disk)",
        tofile="(what the generator would produce)",
        lineterm="",
        n=3,
    )
    sys.stderr.write("\n".join(list(diff)[:80]))
    sys.stderr.write("\n")
    return 1

  args.notebook.parent.mkdir(parents=True, exist_ok=True)
  args.notebook.write_text(generated_text, encoding="utf-8")
  cell_count = len(generated["cells"])
  md_count = sum(1 for c in generated["cells"] if c["cell_type"] == "markdown")
  code_count = sum(1 for c in generated["cells"] if c["cell_type"] == "code")
  print(
      f"Wrote {args.notebook} ({cell_count} cells:"
      f" {md_count} markdown + {code_count} code)."
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
