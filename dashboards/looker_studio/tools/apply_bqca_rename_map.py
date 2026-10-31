#!/usr/bin/env python3
"""Apply the BQCA live template sync (#559) to a dump, offline.

The live template gives its 65 concepts generic names (``met``, ``dim``,
``m0``, ``d0``, ...). The sync renames each one to the ``qt_`` ID of its
column in ``spec/bqca_chart_manifest.yaml`` and repoints the report-level
``datasource_id``. ``tools/gen_bqca_rename_map.py`` lists that rename. This
tool applies it to a report textproto dump taken before the sync and writes the
result, the dump's remapped twin, so the sync can be reviewed and checked
before the live template is touched. In the dump it:

* renames each ``concept_defs`` ``name`` to its new ``qt_`` ID, and its ``id``
  to ``t0.`` followed by that ID;
* renames each dimension or metric ``concept_names`` and each ``sort_column``
  of the same component that refers to the concept;
* sets ``report_config.report.datasource_id`` to the ``datasource_id`` of
  ``bindings/bqca_report_template.yaml``, when it is another value.

Every other byte of the dump, comments and spacing included, is kept: each edit
replaces one quoted scalar literal in place, and the tool prepends a
``# bqca-offline-preview:`` comment line. The live names repeat across
components, so a rename is keyed by the component and the old name, never by
the name alone. The map is the one ``gen_bqca_rename_map.py`` derives from the
dump and the manifest. ``--map`` also reads a CSV written by that tool, and
fails, writing nothing, unless it matches the derived map exactly.

Before the tool writes anything, it checks the result four ways:

* its tokens equal the dump's, except for the edited literals;
* it parses to the parsed dump with the map applied, by a traversal that does
  not use the edits;
* ``gen_bqca_rename_map.py`` accepts it as the remapped twin of the dump;
* ``tools/validate_contracts.py --profile bqca --live-report-textproto`` exits
  0 on it.

The dump is read once. Every check reads a private, read-only copy of those
bytes, never the path again, so a file that replaces the dump while the tool
runs cannot be mixed in, and the file written is the bytes that passed. It is
written with mode 0600, atomically. The tool refuses an ``--out`` inside this
repository by its path, its directory, or the place a link leads to (a dump of
the live template does not belong in it), one that is the dump, and one that
exists without ``--force``. ``--check`` runs every check and prints the plan
without writing.

A dump that already carries the new IDs maps to itself, so the tool writes a
copy (adding the ``# bqca-offline-preview:`` header comment line once if it is
not already present). The twin previews the sync; it is not a dump of a synced
template. This tool never writes or reads a capture receipt, and
``tools/sign_live_capture.py`` and ``tools/finalize_live_sync.py`` refuse the
twin as the capture of the synced template, and so any edit of its comments or
spacing (the marker included) or of the order of its fields: ``preview_tree``
gives them its parsed content, which they compare, ignoring order, with that
of the capture after the write-back. A listed signer who edits a field value
of the twin and signs it can still get it written; see "Capture receipts" in
``docs/dashboard-implementation.md``. After the rename is applied to the live
template, capture it again with ``tools/sign_live_capture.py``.

Exits 0 when the remapped dump is written, and with ``--check`` when every check
passes, whether or not a write to ``--out`` would be refused; the output says
which. Exits 1 when the dump or the map is rejected, a check fails, or the write
is refused; 2 on bad arguments.
"""

import argparse
import collections
import csv
import dataclasses
import functools
import hashlib
import importlib.util
import io
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import types

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parent.parent
MANIFEST_PATH = "spec/bqca_chart_manifest.yaml"
BINDING_PATH = "bindings/bqca_report_template.yaml"
VALIDATOR_PATH = "tools/validate_contracts.py"
# What an edit may write: no quote, backslash or whitespace, so a new value
# needs no escaping inside the literal it replaces.
_PLAIN_VALUE_RE = re.compile(r"[A-Za-z0-9_.\-]+")
# The kinds of edit, in the order a summary lists them.
KINDS = ("name", "id", "dimension", "metric", "sort", "datasource_id")
MAX_ERRORS = 20
NOT_A_DUMP_OF_A_SYNCED_TEMPLATE = (
    "the remapped dump previews the sync; it is not a dump of a synced"
    " template, and tools/finalize_live_sync.py refuses it as one. After the"
    " rename is applied to the live template, capture it again with"
    " tools/sign_live_capture.py --before <the capture before the write-back>."
)
OFFLINE_PREVIEW_MARKER = (
    "# bqca-offline-preview: written by tools/apply_bqca_rename_map.py;"
    " not a dump of a synced live template."
)


@functools.cache
def _load_tool(name: str) -> types.ModuleType:
  """Import `<name>.py` from this tool's directory, once."""
  path = pathlib.Path(__file__).resolve().parent / f"{name}.py"
  spec = importlib.util.spec_from_file_location(name, path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@dataclasses.dataclass
class Scalar:
  """A scalar field value and the span of its literal in the text.

  `quote` is the quote of a string literal, and "" for an enum name or a
  number. `pieces` counts the adjacent string literals that make up the value.
  """

  value: str
  start: int
  end: int
  quote: str
  pieces: int = 1


@dataclasses.dataclass(frozen=True)
class Edit:
  """Give the scalar `node`, of the given `kind`, the value `new`."""

  kind: str
  node: Scalar
  old: str
  new: str


def _tokenize(text: str) -> list[tuple[str, str, int, int]]:
  """Return `(kind, literal, start, end)` for every token, skips included."""
  validator = _load_tool("validate_contracts")
  tokens = []
  pos = 0
  while pos < len(text):
    match = validator._TEXTPROTO_TOKEN_RE.match(text, pos)
    if match is None:
      raise ValueError(f"unexpected character {text[pos]!r} at offset {pos}")
    tokens.append((match.lastgroup, match.group(), pos, match.end()))
    pos = match.end()
  return tokens


def read_textproto(text: str) -> list:
  """Parse protobuf text format as the validator does, keeping the spans.

  This follows `validate_contracts.parse_report_textproto` (its tokens, field
  names, escapes and nesting limit), but each scalar is a `Scalar`, so its
  literal can be replaced in place. A message is a list of `(field, Scalar or
  message)` pairs in file order. `text` must not start with a byte order mark.

  Raises:
    ValueError: The text is not well-formed protobuf text format, or uses
      syntax outside the subset the validator reads.
  """
  validator = _load_tool("validate_contracts")
  tokens = [token for token in _tokenize(text) if token[0] != "skip"]
  index = 0

  def peek():
    return tokens[index] if index < len(tokens) else None

  def at(*punctuation: str) -> bool:
    token = peek()
    return token is not None and token[0] == "punct" and token[1] in punctuation

  def parse_value(name: str, depth: int):
    nonlocal index
    token = peek()
    if token is None:
      raise ValueError(f"field {name!r} has no value")
    kind, literal, start, end = token
    if at("{", "<"):
      if depth >= validator._TEXTPROTO_MAX_DEPTH:
        raise ValueError(
            f"messages nest more than {validator._TEXTPROTO_MAX_DEPTH} levels"
            " deep"
        )
      index += 1
      return parse_message("}" if literal == "{" else ">", depth + 1)
    if kind == "string":
      parts = []
      while index < len(tokens) and tokens[index][0] == "string":
        parts.append(validator._decode_textproto_string(tokens[index][1]))
        end = tokens[index][3]
        index += 1
      return Scalar("".join(parts), start, end, literal[0], len(parts))
    if kind == "word":
      index += 1
      return Scalar(literal, start, end, "")
    raise ValueError(f"field {name!r} has unexpected value {literal!r}")

  def parse_message(closer: str | None, depth: int) -> list:
    nonlocal index
    fields = []
    while True:
      token = peek()
      if token is None:
        if closer is None:
          return fields
        raise ValueError(f"unterminated message: missing {closer!r}")
      kind, literal = token[:2]
      if kind == "punct" and literal == closer:
        index += 1
        return fields
      if kind != "word" or not validator._TEXTPROTO_FIELD_NAME_RE.fullmatch(
          literal
      ):
        raise ValueError(f"expected a field name, got {literal!r}")
      name = literal
      index += 1
      has_colon = at(":")
      if has_colon:
        index += 1
      if not has_colon and not at("{", "<"):
        raise ValueError(f"field {name!r} needs ':' before its value")
      if has_colon and at("["):
        index += 1
        while not at("]"):
          fields.append((name, parse_value(name, depth)))
          if at(","):
            index += 1
          elif not at("]"):
            raise ValueError(f"list field {name!r} is missing ',' or ']'")
        index += 1
      else:
        fields.append((name, parse_value(name, depth)))
      if at(",", ";"):
        index += 1

  return parse_message(None, 0)


def _plain(message: list) -> list:
  """Return `message` with each `Scalar` replaced by its value."""
  return [
      (name, _plain(value) if isinstance(value, list) else value.value)
      for name, value in message
  ]


def _scalars(message: list, name: str) -> list[Scalar]:
  """Return the scalar fields `name` of `message`, not those nested in it."""
  return [
      value
      for key, value in message
      if key == name and isinstance(value, Scalar)
  ]


def _edit(kind: str, node: Scalar, new: str, where: str) -> Edit | None:
  """Return the edit that gives `node` the value `new`, or None if it has it.

  Raises:
    ValueError: The edit could not replace the literal of `node` in place.
  """
  if node.value == new:
    return None
  if not node.quote or node.pieces != 1:
    raise ValueError(
        f"{where} {kind} {node.value!r} is not one quoted string, so its"
        " literal cannot be replaced in place"
    )
  if not _PLAIN_VALUE_RE.fullmatch(new):
    raise ValueError(f"{where} would need the {kind} {new!r} escaped")
  return Edit(kind, node, node.value, new)


def plan_edits(
    root: list, renames: dict[tuple[str, str], str], datasource_id: str
) -> list[Edit]:
  """Return the edits that rename the concepts of a parsed dump.

  Each `component_config` of the dump, which may not nest another, must have
  one `component_id`, one quoted `name` and `id` (`t0.<name>`) per concept in
  `renames` (new IDs by `(component_id, old name)`), and at most one
  `property_config` with one `component_property`. Each concept's `name` and
  `id` are renamed. Each `concept_names` and `sort_column` of
  `component_property` must be at a path the live audit reads and name a
  concept of its own component, and is renamed with it, keeping any `t0.`
  prefix. The one `datasource_id` of the report is set to `datasource_id`.
  A value that already has its new value is not edited.

  Args:
    root: The dump, as read by `read_textproto`.
    renames: The new `qt_` ID of each concept, by `(component_id, old name)`.
    datasource_id: The report-level `datasource_id` to set.

  Returns:
    The edits, in the order of the dump.

  Raises:
    ValueError: The dump cannot be renamed one to one, or an edit could not
      replace its literal in place.
  """
  validator = _load_tool("validate_contracts")
  gen = _load_tool("gen_bqca_rename_map")
  edits: list[Edit] = []
  seen: set[str] = set()
  used: set[tuple[str, str]] = set()
  configs = validator._tp_messages(root, "component_config")
  if not configs:
    raise ValueError("the dump has no component_config")
  for config in configs:
    if validator._tp_messages(config, "component_config"):
      raise ValueError("a component_config is nested in a component_config")
    ids = _scalars(config, "component_id")
    if len(ids) != 1 or not ids[0].quote:
      raise ValueError(
          f"a component_config has {len(ids)} component_id fields, not one"
          " quoted"
      )
    component_id = ids[0].value
    where = f"component {component_id}"
    if component_id in seen:
      raise ValueError(f"{where} appears twice")
    seen.add(component_id)

    defined: dict[str, str] = {}
    for concept in (
        value
        for key, value in config
        if key == "concept_defs" and isinstance(value, list)
    ):
      names, concept_ids = _scalars(concept, "name"), _scalars(concept, "id")
      if len(names) != 1 or len(concept_ids) != 1:
        raise ValueError(
            f"{where} has a concept_defs without exactly one name and one id"
        )
      name, concept_id = names[0].value, concept_ids[0].value
      if concept_id != f"t0.{name}":
        raise ValueError(
            f"{where} concept {name!r} has the id {concept_id!r}, not"
            f" 't0.{name}'"
        )
      new = renames.get((component_id, name))
      if new is None:
        raise ValueError(f"{where} concept {name!r} is not in the rename map")
      if name in defined:
        raise ValueError(f"{where} repeats the concept {name!r}")
      defined[name] = new
      used.add((component_id, name))
      edits += filter(
          None,
          [
              _edit("name", names[0], new, where),
              _edit("id", concept_ids[0], f"t0.{new}", where),
          ],
      )

    property_configs = [
        value
        for key, value in config
        if key == "property_config" and isinstance(value, list)
    ]
    properties = [
        value
        for property_config in property_configs
        for key, value in property_config
        if key == "component_property" and isinstance(value, list)
    ]
    if len(property_configs) > 1 or len(properties) > 1:
      raise ValueError(
          f"{where} has more than one property_config or component_property"
      )
    for component_property in properties:
      for path, node in validator._tp_scalar_paths(
          component_property, ("concept_names", "sort_column")
      ):
        if path not in validator._LIVE_REFERENCE_PATHS:
          raise ValueError(
              f"{where} has {path[-1]} {node.value!r} at"
              f" component_property.{'.'.join(path)}, which the live audit"
              " does not read"
          )
        reference = validator._concept_ref(node.value)
        if reference not in defined:
          raise ValueError(
              f"{where} {path[-1]} {node.value!r} names none of its concepts"
          )
        prefix = node.value[: len(node.value) - len(reference)]
        edit = _edit(
            gen._ROLES[path[0]], node, prefix + defined[reference], where
        )
        if edit is not None:
          edits.append(edit)
  unused = sorted(set(renames) - used)
  if unused:
    raise ValueError(f"the rename map has concepts the dump lacks: {unused}")

  report = validator._tp_message(
      validator._tp_message(root, "report_config") or root, "report"
  )
  datasource_ids = _scalars(report, "datasource_id")
  if len(datasource_ids) != 1:
    raise ValueError(
        f"report_config.report has {len(datasource_ids)} datasource_id"
        " fields, not one"
    )
  edit = _edit("datasource_id", datasource_ids[0], datasource_id, "the report")
  if edit is not None:
    edits.append(edit)
  edits.sort(key=lambda edit: edit.node.start)
  return edits


def apply_edits(text: str, edits: list[Edit]) -> str:
  """Return `text` with the literal of each edited scalar replaced."""
  pieces = []
  pos = 0
  for edit in sorted(edits, key=lambda edit: edit.node.start):
    node = edit.node
    if node.start < pos:
      raise ValueError("two edits overlap")
    pieces += [text[pos : node.start], f"{node.quote}{edit.new}{node.quote}"]
    pos = node.end
  pieces.append(text[pos:])
  return "".join(pieces)


def expected_tree(
    tree: list, renames: dict[tuple[str, str], str], datasource_id: str
) -> list:
  """Return the parsed dump `tree` with the rename applied.

  This walks the plain tree of `parse_report_textproto` by field paths. It
  shares no code with `plan_edits`, which edits the literals of the text.

  Raises:
    KeyError: A concept of `tree` is not in `renames`.
  """
  validator = _load_tool("validate_contracts")
  # The path of the message that holds each reference field, from a
  # `component_config`.
  references = {
      ("property_config", "component_property", *path[:-1]): path[-1]
      for path in validator._LIVE_REFERENCE_PATHS
  }
  report = (
      ("report_config", "report")
      if validator._tp_message(tree, "report_config")
      else ("report",)
  )

  def walk(message: list, path: tuple, component_id: str | None) -> list:
    renamed = []
    for key, value in message:
      if isinstance(value, list):
        if key == "component_config":
          value = walk(value, (), validator._tp_scalar(value, "component_id"))
        else:
          value = walk(value, (*path, key), component_id)
      elif component_id is None:
        if path == report and key == "datasource_id":
          value = datasource_id
      elif path == ("concept_defs",) and key == "name":
        value = renames[(component_id, value)]
      elif path == ("concept_defs",) and key == "id":
        value = f"t0.{renames[(component_id, value.removeprefix('t0.'))]}"
      elif references.get(path) == key:
        reference = validator._concept_ref(value)
        value = (
            value[: len(value) - len(reference)]
            + renames[(component_id, reference)]
        )
      renamed.append((key, value))
    return renamed

  return walk(tree, (), None)


def verification_errors(
    text: str,
    remapped: str,
    edits: list[Edit],
    renames: dict[tuple[str, str], str],
    datasource_id: str,
) -> list[str]:
  """Return how `remapped` is not `text` with exactly `edits` applied.

  Neither check uses the spans of `edits`:

  * the tokens of `remapped` equal those of `text`, whitespace and comments
    included, except for one string literal per edit, which has the same quote
    and decodes to the edit's old value in `text` and its new value in
    `remapped`;
  * `remapped` parses to the parsed `text` with `renames` and `datasource_id`
    applied.
  """
  validator = _load_tool("validate_contracts")
  errors = []
  try:
    before, after = _tokenize(text), _tokenize(remapped)
  except ValueError as exc:
    return [f"the remapped dump cannot be read: {exc}"]
  planned = sorted(edits, key=lambda edit: edit.node.start)
  if len(before) != len(after):
    errors.append(
        f"the remapped dump has {len(after)} tokens, the dump {len(before)}"
    )
  else:
    changed = [
        (old, new) for old, new in zip(before, after) if old[:2] != new[:2]
    ]
    if len(changed) != len(planned):
      errors.append(
          f"{len(changed)} tokens of the remapped dump differ from the dump's,"
          f" but {len(planned)} edits were planned"
      )
    for (old, new), edit in zip(changed, planned):
      if (
          (old[0], new[0]) != ("string", "string")
          or old[1][0] != new[1][0]
          or validator._decode_textproto_string(old[1]) != edit.old
          or validator._decode_textproto_string(new[1]) != edit.new
      ):
        errors.append(
            f"the {edit.kind} at offset {old[2]} was not rewritten in place"
            f" from {edit.old!r} to {edit.new!r}"
        )
  try:
    expected = expected_tree(
        validator.parse_report_textproto(text), renames, datasource_id
    )
  except (KeyError, ValueError) as exc:
    errors.append(f"the dump with the map applied cannot be built: {exc}")
    return errors
  try:
    actual = validator.parse_report_textproto(remapped)
  except ValueError as exc:
    errors.append(
        f"remapped dump could not be parsed for tree comparison: {exc}"
    )
    return errors
  if expected != actual:
    errors.append(
        "the remapped dump does not parse to the dump with the map applied"
    )
  return errors


def preview_tree(snapshot: bytes) -> list:
  """Return the parsed content of the remapped dump written for `snapshot`.

  That is the tree that `validate_contracts.parse_report_textproto` reads
  from it, with universal newlines: comments, spacing and quoting are not in
  it, so neither the offline-preview marker nor any edit of them changes it.
  The map is derived from `snapshot` and the manifest, as for a write.

  Raises:
    ValueError: This tool would refuse to remap `snapshot`.
  """
  validator = _load_tool("validate_contracts")
  gen = _load_tool("gen_bqca_rename_map")
  try:
    text = snapshot.decode("utf-8")
  except UnicodeDecodeError as exc:
    raise ValueError(f"it is not UTF-8 text: {exc}") from exc
  manifest = yaml.safe_load((ROOT / MANIFEST_PATH).read_text(encoding="utf-8"))
  binding = yaml.safe_load((ROOT / BINDING_PATH).read_text(encoding="utf-8"))
  datasource_id = (
      binding.get("datasource_id") if isinstance(binding, dict) else None
  )
  if not isinstance(datasource_id, str) or not _PLAIN_VALUE_RE.fullmatch(
      datasource_id
  ):
    raise ValueError(
        f"{BINDING_PATH} datasource_id {datasource_id!r} is not a plain ID"
    )
  with tempfile.TemporaryDirectory(prefix="apply_bqca_rename_map_") as private:
    copy = pathlib.Path(private) / "dump.textproto"
    copy.write_bytes(snapshot)
    components, _, errors = gen.read_dump(validator, copy)
    rows, row_errors = gen.build_rows(validator, manifest, components)
  errors += row_errors
  if errors:
    raise ValueError("; ".join(errors[:MAX_ERRORS]))
  renames = {
      (row["component_id"], row["old_name"]): row["new_qt_id"] for row in rows
  }
  universal = text.replace("\r\n", "\n").replace("\r", "\n")
  try:
    return expected_tree(
        validator.parse_report_textproto(universal), renames, datasource_id
    )
  except KeyError as exc:
    raise ValueError(f"the map has no concept {exc}") from exc


def map_drift(rows: list[dict], text: str) -> list[str]:
  """Return how the rename map CSV `text` differs from the derived `rows`."""
  columns = list(_load_tool("gen_bqca_rename_map").COLUMNS)
  reader = csv.DictReader(io.StringIO(text, newline=""))
  try:
    if reader.fieldnames is None:
      return ["the map is empty"]
    if reader.fieldnames != columns:
      return [f"the map's columns are {reader.fieldnames}, not {columns}"]
    given = list(reader)
  except csv.Error as exc:
    return [f"the map is not valid CSV: {exc}"]
  errors = []
  if len(given) != len(rows):
    errors.append(
        f"the map has {len(given)} rows, but the dump and the manifest give"
        f" {len(rows)}"
    )
  for number, (row, derived) in enumerate(zip(given, rows), start=2):
    if None in row or None in row.values():
      errors.append(f"map line {number} does not have {len(columns)} cells")
      continue
    errors += [
        f"map line {number} ({derived['component_id']},"
        f" {derived['old_name']}): {column} is {row[column]!r}, not"
        f" {derived[column]!r}"
        for column in columns
        if row[column] != derived[column]
    ]
  return errors


def _with_offline_preview_marker(body: str) -> str:
  """Return `body` with `OFFLINE_PREVIEW_MARKER` as its first line."""
  if body.startswith(
      (f"{OFFLINE_PREVIEW_MARKER}\n", f"{OFFLINE_PREVIEW_MARKER}\r\n")
  ):
    return body
  newline = "\r\n" if "\r\n" in body else "\n"
  return f"{OFFLINE_PREVIEW_MARKER}{newline}{body}"


def _inside_repository(out: pathlib.Path) -> bool:
  """Return whether a write to `out` would put a file in this repository.

  The write replaces the entry `out` and never follows it, so the file lands in
  the directory of `out`, reached through the links of its path. `out` is also
  refused if its path, as it is spelled, is in the repository, wherever a link
  on it leads, since the link can be changed, or if it resolves into it, even
  when a case-insensitive filesystem preserves another letter case.
  """
  root = REPOSITORY_ROOT.resolve()
  places = (
      out.resolve(),
      pathlib.Path(os.path.abspath(out)),
      out.parent.resolve(),
  )
  if any(place.is_relative_to(root) for place in places):
    return True
  try:
    root_stat = root.stat()
  except OSError:
    return False
  for place in places:
    for ancestor in (place, *place.parents):
      try:
        if os.path.samestat(ancestor.stat(), root_stat):
          return True
      except OSError:
        continue
  return False


def write_blockers(
    dump: pathlib.Path, out: pathlib.Path, force: bool
) -> list[str]:
  """Return why writing the remapped dump to `out` would be refused."""
  blockers = []
  resolved = out.resolve()
  if _inside_repository(out):
    blockers.append(
        f"{out} is inside this repository, which must not hold a dump of the"
        " live template"
    )
  if resolved == dump.resolve() or (out.exists() and out.samefile(dump)):
    blockers.append(f"{out} is the dump itself")
  elif resolved.is_dir():
    blockers.append(f"{out} is a directory")
  elif os.path.lexists(out) and not force:
    blockers.append(f"{out} exists; --force replaces it")
  # `resolved` folds a `missing/..` away, but the write opens `out.parent` as it
  # is spelled, where `missing/..` is not a directory.
  if not (resolved.parent.is_dir() and out.parent.is_dir()):
    blockers.append(f"the directory of {out} does not exist")
  return blockers


def _write_private(path: pathlib.Path, data: bytes, force: bool) -> None:
  """Write `data` to `path` with mode 0600, atomically.

  The data is written and synced to a staged file next to `path`, which then
  takes its place: with `os.replace` for `force`, else with `os.link`, which
  fails if `path` exists, so a file created meanwhile is never overwritten. On
  any failure the staged file is removed.
  """
  descriptor, staged = tempfile.mkstemp(
      dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
  )
  try:
    with os.fdopen(descriptor, "wb") as handle:
      handle.write(data)
      handle.flush()
      os.fsync(handle.fileno())
    os.chmod(staged, stat.S_IRUSR | stat.S_IWUSR)
    if force:
      os.replace(staged, path)
    else:
      os.link(staged, path)
  finally:
    # After `os.link` this removes only the staging name; the file stays at
    # `path`. After `os.replace` the name is gone already, hence `missing_ok`.
    pathlib.Path(staged).unlink(missing_ok=True)


def _audit(dump: pathlib.Path) -> tuple[int, str]:
  """Run the contract validator with the live audit of `dump`."""
  command = [
      sys.executable,
      str(ROOT / VALIDATOR_PATH),
      "--profile",
      "bqca",
      "--live-report-textproto",
      str(dump),
  ]
  result = subprocess.run(command, capture_output=True, text=True, check=False)
  return result.returncode, result.stderr


def _fail(errors: list[str]) -> int:
  """Print `errors`, say that nothing was written, and return the exit code."""
  for error in errors[:MAX_ERRORS]:
    print(f"ERROR: {error}", file=sys.stderr)
  if len(errors) > MAX_ERRORS:
    print(f"ERROR: ... and {len(errors) - MAX_ERRORS} more", file=sys.stderr)
  print("not remapped: nothing written", file=sys.stderr)
  return 1


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
      "--dump",
      type=pathlib.Path,
      required=True,
      metavar="PATH",
      help="report textproto dump of the live BQCA template, taken before the"
      " sync",
  )
  parser.add_argument(
      "--map",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "a rename map CSV written by gen_bqca_rename_map.py, which must"
          " match the map derived from the dump and the manifest exactly"
      ),
  )
  parser.add_argument(
      "--out",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "where to write the remapped dump: outside this repository, and not"
          " an existing file unless --force"
      ),
  )
  parser.add_argument(
      "--force", action="store_true", help="replace an existing --out"
  )
  parser.add_argument(
      "--check",
      action="store_true",
      help="run every check and print the plan without writing",
  )
  args = parser.parse_args(argv)
  if args.out is None and not args.check:
    parser.error("a write needs --out; --check runs the checks without it")
  if args.force and args.out is None:
    parser.error("--force replaces an existing --out, which is not given")
  # Read the dump once. Everything below uses these bytes (the checks through
  # a private, read-only copy), never the path again.
  dump = args.dump.resolve()
  try:
    snapshot = dump.read_bytes()
  except OSError as exc:
    print(
        f"not remapped: cannot read {args.dump}: {exc.strerror or exc}",
        file=sys.stderr,
    )
    return 1
  map_text = None
  if args.map is not None:
    try:
      map_text = args.map.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
      return _fail([f"cannot read the map {args.map}: {exc}"])
  print(
      f"snapshot of {args.dump}: {len(snapshot)} bytes, sha256"
      f" {hashlib.sha256(snapshot).hexdigest()}"
  )
  blockers = (
      write_blockers(dump, args.out, args.force) if args.out is not None else []
  )
  if blockers and not args.check:
    return _fail(blockers)
  with tempfile.TemporaryDirectory(prefix="apply_bqca_rename_map_") as private:
    return _remap_snapshot(
        args, snapshot, map_text, blockers, pathlib.Path(private)
    )


def _remap_snapshot(
    args: argparse.Namespace,
    snapshot: bytes,
    map_text: str | None,
    blockers: list[str],
    private: pathlib.Path,
) -> int:
  """Remap the dump `snapshot`, check the result, and write or preview it.

  Every reader and the audit use files in `private`, copies of `snapshot` and
  of the remapped dump.
  """
  validator = _load_tool("validate_contracts")
  gen = _load_tool("gen_bqca_rename_map")
  try:
    text = snapshot.decode("utf-8")
  except UnicodeDecodeError as exc:
    return _fail([f"{args.dump} is not UTF-8 text: {exc}"])
  # A leading byte order mark is kept as it is; the spans start after it.
  body = text.removeprefix("\ufeff")
  head = text[: len(text) - len(body)]
  copy = private / "dump.textproto"
  copy.write_bytes(snapshot)
  copy.chmod(stat.S_IRUSR)

  manifest = yaml.safe_load((ROOT / MANIFEST_PATH).read_text(encoding="utf-8"))
  binding = yaml.safe_load((ROOT / BINDING_PATH).read_text(encoding="utf-8"))
  datasource_id = (
      binding.get("datasource_id") if isinstance(binding, dict) else None
  )
  if not isinstance(datasource_id, str) or not _PLAIN_VALUE_RE.fullmatch(
      datasource_id
  ):
    return _fail(
        [f"{BINDING_PATH} datasource_id {datasource_id!r} is not a plain ID"]
    )

  # The map: one row per concept, derived as gen_bqca_rename_map.py does.
  try:
    components, old_datasource_id, errors = gen.read_dump(validator, copy)
    rows, row_errors = gen.build_rows(validator, manifest, components)
  except (OSError, ValueError) as exc:
    return _fail([f"cannot read the dump: {exc}"])
  errors += row_errors
  if errors:
    return _fail(errors)
  if map_text is not None:
    errors = map_drift(rows, map_text)
    if errors:
      return _fail([f"{args.map}: {error}" for error in errors])
  renames = {
      (row["component_id"], row["old_name"]): row["new_qt_id"] for row in rows
  }
  if len(renames) != len(rows):
    return _fail(["the map renames a concept more than once"])
  matched = f"; {args.map} matches it exactly" if args.map is not None else ""
  component_count = len({component_id for component_id, _ in renames})
  print(
      f"map: {len(rows)} concepts in {component_count} components, from the"
      f" dump and {MANIFEST_PATH}{matched}"
  )

  # The edits, and the remapped dump they make.
  try:
    root = read_textproto(body)
    universal_body = copy.read_text(encoding="utf-8").removeprefix("\ufeff")
    if _plain(root) != validator.parse_report_textproto(body) or _plain(
        root
    ) != validator.parse_report_textproto(universal_body):
      raise ValueError(
          f"the dump reads differently here than in {VALIDATOR_PATH}"
      )
    edits = plan_edits(root, renames, datasource_id)
    remapped_body = apply_edits(body, edits)
  except ValueError as exc:
    return _fail([str(exc)])
  remapped = (head + _with_offline_preview_marker(remapped_body)).encode(
      "utf-8"
  )
  counts = collections.Counter(edit.kind for edit in edits)
  lines = sum(
      old != new
      for old, new in zip(body.split("\n"), remapped_body.split("\n"))
  )
  if edits:
    print(
        f"edits: {len(edits)} scalars in {lines} lines: "
        + ", ".join(f"{kind} {counts[kind]}" for kind in KINDS if counts[kind])
    )
  else:
    print("edits: none; the dump already carries every new ID")
  print(
      f"report datasource_id: {old_datasource_id!r} -> {datasource_id!r}"
      if counts["datasource_id"]
      else f"report datasource_id: already {datasource_id!r}"
  )

  # Four checks of the result, before anything is written.
  errors = verification_errors(
      body, remapped_body, edits, renames, datasource_id
  )
  twin = private / "remapped.textproto"
  twin.write_bytes(remapped)
  twin.chmod(stat.S_IRUSR)
  try:
    twin_components, _, twin_read_errors = gen.read_dump(validator, twin)
    errors += [f"remapped dump: {error}" for error in twin_read_errors]
    errors += [
        f"remapped dump: {error}"
        for error in gen.twin_errors(rows, components, twin_components)
    ]
  except (OSError, ValueError) as exc:
    errors.append(f"remapped dump: cannot read it: {exc}")
  code, audit_errors = _audit(twin)
  if code != 0:
    errors.append(f"{VALIDATOR_PATH} exited {code} on the remapped dump:")
    errors += [
        "  "
        + line.removeprefix("ERROR: ").replace(str(twin), "the remapped dump")
        for line in audit_errors.splitlines()
        if line.strip()
    ]
  if errors:
    return _fail(errors)
  print(
      "verified: only the planned scalars differ, and the remapped dump is the"
      " dump with the map applied"
  )
  print(
      f"audit: {VALIDATOR_PATH} --profile bqca --live-report-textproto exits 0"
      " on the remapped dump, which gen_bqca_rename_map.py accepts as its twin"
  )

  if args.check:
    refusal = (
        f", but a write would refuse it: {'; '.join(blockers)}"
        if blockers
        else ""
    )
    print(f"check: every check passed{refusal}; nothing written")
    print(NOT_A_DUMP_OF_A_SYNCED_TEMPLATE)
    return 0
  try:
    _write_private(args.out, remapped, args.force)
  except OSError as exc:
    return _fail([f"cannot write {args.out}: {exc.strerror or exc}"])
  print(
      f"written: {args.out} ({len(remapped)} bytes, sha256"
      f" {hashlib.sha256(remapped).hexdigest()}, mode 600)"
  )
  print(NOT_A_DUMP_OF_A_SYNCED_TEMPLATE)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
