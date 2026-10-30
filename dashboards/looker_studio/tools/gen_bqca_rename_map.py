#!/usr/bin/env python3
"""Write the rename map of the BQCA live template sync (#559).

The live template gives its 65 concepts generic names (``met``, ``dim``,
``m0``, ``d0``, ...). The sync renames each one to the ``qt_`` ID of its column
in ``spec/bqca_chart_manifest.yaml``. This tool lists that rename, one row per
concept, in manifest page, component and column (slot) order. Each row gives:

* the old name, read from a report textproto dump taken before the sync;
* the new ID, read from the manifest;
* the concept's source field and aggregation, which the sync keeps;
* the chart references to update (``dimension``, ``metric``, ``sort``).

It matches each concept of the dump to the manifest column of its component
with the same ``(source_field_name, aggregation)``, so that every concept and
reference of the dump is in the map: it rejects a component of the dump that
defines concepts or holds references but is not in the manifest, and a
reference, in any component, that names no concept of its own component. It
reports:

* how many concept names the live audit (``validate_contracts.py
  --live-report-textproto``) rejects as not ``qt_`` IDs;
* the report-level ``datasource_id`` that the sync repoints.

With ``--remapped``, it also checks the map against a remapped twin of the
dump: the twin must pass the live audit, have the same components with
concepts or references as the dump, and carry each new ID at its old name's
place.

With no ``--dump``, it reads the checked-in sanitized cut of the 2026-10-29
dump and its remapped twin. ``--csv`` and ``--md`` write the map as CSV and
Markdown; with neither, the CSV goes to stdout. The summary goes to stderr.
Exits 1, writing nothing, when the dump, the twin and the manifest do not line
up.
"""

import argparse
import collections
import csv
import dataclasses
import hashlib
import importlib.util
import io
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parent.parent
MANIFEST_PATH = "spec/bqca_chart_manifest.yaml"
BINDING_PATH = "bindings/bqca_report_template.yaml"
DEFAULT_DUMP = (
    REPOSITORY_ROOT
    / "tests/fixtures/bqca_live_report_42e79a1f_sanitized.textproto"
)
DEFAULT_REMAPPED = (
    REPOSITORY_ROOT
    / "tests/fixtures/bqca_live_report_42e79a1f_sanitized_remapped.textproto"
)
# The checked-in cut replaces one value of the dump it was cut from.
SANITIZED_DATASOURCE_NOTE = (
    " In the checked-in sanitized cut, that ID is a placeholder: the live"
    " template names the source report's retired data source there."
)
COLUMNS = (
    "page",
    "page_name",
    "component_id",
    "component_title",
    "chart_type",
    "old_name",
    "new_qt_id",
    "source_field",
    "aggregation",
    "slot",
    "display_name",
    "references",
)
# The chart reference paths under `component_property` that the live audit
# reads, by the role they give a concept.
_ROLES = {"dimensions": "dimension", "metrics": "metric", "sort": "sort"}


def _load_validator():
  """Import `validate_contracts.py` from this tool's directory."""
  path = pathlib.Path(__file__).resolve().parent / "validate_contracts.py"
  spec = importlib.util.spec_from_file_location("validate_contracts", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@dataclasses.dataclass(frozen=True)
class Concept:
  """One `concept_defs` entry of a dump."""

  name: str | None
  source_field: str | None
  aggregation: str


@dataclasses.dataclass
class DumpComponent:
  """The concepts of one `component_config` and the references to them."""

  concepts: list[Concept]
  # `(path under component_property, referenced name)`, in dump order.
  references: list[tuple[tuple[str, ...], str]]


def read_dump(validator, path: pathlib.Path):
  """Return `(components by component_id, report datasource_id, errors)`."""
  report = validator.parse_report_textproto(path.read_text(encoding="utf-8"))
  components: dict[str, DumpComponent] = {}
  errors: list[str] = []
  for config in validator._tp_messages(report, "component_config"):
    component_id = validator._tp_scalar(config, "component_id")
    concepts = []
    for key, value in config:
      if key != "concept_defs" or not isinstance(value, list):
        continue
      transform = validator._tp_message(
          validator._tp_message(value, "query_time_transformation"),
          "data_transformation",
      )
      concepts.append(
          Concept(
              validator._tp_scalar(value, "name"),
              validator._tp_scalar(transform, "source_field_name"),
              validator._tp_scalar(transform, "aggregation") or "",
          )
      )
    component_property = validator._tp_message(
        validator._tp_message(config, "property_config"), "component_property"
    )
    references = [
        (path, validator._concept_ref(ref))
        for path, ref in validator._tp_scalar_paths(
            component_property, ("concept_names", "sort_column")
        )
    ]
    if component_id in components:
      errors.append(f"component {component_id} appears twice")
      continue
    components[component_id] = DumpComponent(concepts, references)
  report_message = validator._tp_message(
      validator._tp_message(report, "report_config") or report, "report"
  )
  return (
      components,
      validator._tp_scalar(report_message, "datasource_id"),
      errors,
  )


def _role(validator, path: tuple[str, ...]) -> str:
  if path in validator._LIVE_REFERENCE_PATHS:
    return _ROLES[path[0]]
  return ".".join(path)


def build_rows(validator, manifest: dict, components: dict):
  """Return `(rows, errors)`: one row per manifest column, in order.

  Each row is a dict of `COLUMNS` plus `_where`, which names its component
  in error messages.
  """
  rows: list[dict] = []
  errors: list[str] = []
  labels = {
      component.get("component_id"): (
          f"component {component.get('id')} ({component.get('component_id')})"
      )
      for page in manifest.get("pages", [])
      for component in page.get("components", [])
  }
  # Every reference of the dump, in any component, must name a concept of
  # its own component, or the map could not rename it.
  for component_id, dumped in components.items():
    names = {concept.name for concept in dumped.concepts}
    for path, ref in dumped.references:
      if ref not in names:
        errors.append(
            f"{labels.get(component_id, f'component {component_id}')}"
            f" {path[-1]} {ref!r} at component_property.{'.'.join(path)}"
            " names none of its concepts"
        )
  for page in manifest.get("pages", []):
    for component in page.get("components", []):
      component_id = component.get("component_id")
      where = labels[component_id]
      dumped = components.get(component_id)
      if dumped is None:
        errors.append(f"{where} of the manifest is not in the dump")
        continue
      names = [concept.name for concept in dumped.concepts]
      if len(set(names)) != len(names):
        errors.append(f"{where} repeats a concept name: {names}")
        continue
      by_binding = collections.defaultdict(list)
      for index, concept in enumerate(dumped.concepts):
        key = (
            concept.source_field,
            validator._normalized_aggregation(concept.aggregation),
        )
        by_binding[key].append(index)
      matched: set[int] = set()
      for slot, column in enumerate(validator._bqca_spec_columns(component)):
        key = (
            column.get("fieldName"),
            validator._normalized_aggregation(column.get("aggregation")),
        )
        indexes = by_binding.get(key, [])
        if len(indexes) != 1:
          errors.append(
              f"{where} column {slot} {key} matches {len(indexes)} concepts"
              " of the dump, not 1"
          )
          continue
        (index,) = indexes
        matched.add(index)
        concept = dumped.concepts[index]
        roles = [
            _role(validator, path)
            for path, ref in dumped.references
            if ref == concept.name
        ]
        rows.append(
            {
                "page": str(page.get("index")),
                "page_name": page.get("name"),
                "component_id": component_id,
                "component_title": component.get("title"),
                "chart_type": component.get("chart_type"),
                "old_name": concept.name,
                "new_qt_id": column.get("id"),
                "source_field": concept.source_field,
                "aggregation": concept.aggregation,
                "slot": str(slot),
                "display_name": column.get("displayName"),
                "references": "; ".join(roles),
                "_where": where,
            }
        )
      for index, concept in enumerate(dumped.concepts):
        if index not in matched:
          errors.append(
              f"{where} concept {concept.name!r} ({concept.source_field},"
              f" {concept.aggregation or 'no aggregation'}) matches no column"
              " of the manifest"
          )
  # A concept or reference outside the manifest would be left out of the map.
  for component_id, dumped in components.items():
    if component_id in labels:
      continue
    if dumped.concepts:
      names = [concept.name for concept in dumped.concepts]
      errors.append(
          f"component {component_id} of the dump defines concepts {names} but"
          f" is not in {MANIFEST_PATH}"
      )
    elif dumped.references:
      refs = [ref for _, ref in dumped.references]
      errors.append(
          f"component {component_id} of the dump references {refs} but is not"
          f" in {MANIFEST_PATH}"
      )
  return rows, errors


def twin_errors(rows: list[dict], components: dict, twin: dict) -> list[str]:
  """Return how a remapped twin differs from the dump renamed by `rows`.

  The twin must have the same components with concepts or references as the
  dump, the dump's concepts, in order, each named by its new ID with the same
  source field and aggregation, and the dump's references, in order, renamed
  the same way.
  """
  errors: list[str] = []
  dumped_ids, twinned_ids = (
      {key for key, value in dump.items() if value.concepts or value.references}
      for dump in (components, twin)
  )
  if dumped_ids != twinned_ids:
    errors.append(
        "remapped twin: its components with concepts or references differ"
        f" from the dump's: dump only {sorted(dumped_ids - twinned_ids)}, twin"
        f" only {sorted(twinned_ids - dumped_ids)}"
    )
  new_names = collections.defaultdict(dict)
  where = {}
  for row in rows:
    new_names[row["component_id"]][row["old_name"]] = row["new_qt_id"]
    where[row["component_id"]] = row["_where"]
  for component_id, renames in new_names.items():
    dumped, twinned = components[component_id], twin.get(component_id)
    label = f"remapped twin: {where[component_id]}"
    if twinned is None:
      errors.append(f"{label} is missing")
      continue
    expected = [
        dataclasses.replace(concept, name=renames.get(concept.name))
        for concept in dumped.concepts
    ]
    if len(twinned.concepts) != len(expected):
      errors.append(
          f"{label} has {len(twinned.concepts)} concepts, not"
          f" {len(expected)}"
      )
      continue
    for index, (got, want) in enumerate(zip(twinned.concepts, expected)):
      if got.name != want.name:
        errors.append(
            f"{label} concept {index} is {got.name!r}, not {want.name!r}"
        )
      elif got != want:
        errors.append(f"{label} concept {index} is {got}, not {want}")
    renamed = [(path, renames.get(ref, ref)) for path, ref in dumped.references]
    if twinned.references != renamed:
      errors.append(f"{label} references {twinned.references}, not {renamed}")
  return errors


def _display_path(path: pathlib.Path) -> str:
  """Return `path` relative to the repository, or its name."""
  resolved = path.resolve()
  if resolved.is_relative_to(REPOSITORY_ROOT.resolve()):
    return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
  return path.name


def _sha256(path: pathlib.Path) -> str:
  return hashlib.sha256(path.read_bytes()).hexdigest()


def render_csv(rows: list[dict]) -> str:
  out = io.StringIO()
  writer = csv.DictWriter(
      out, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n"
  )
  writer.writeheader()
  writer.writerows(rows)
  return out.getvalue()


def _cell(value: str) -> str:
  return value.replace("|", "\\|")


def render_markdown(
    rows: list[dict],
    summary: list[str],
    inputs: list[tuple[str, str, str]],
    repoint: tuple[str, str],
    repoint_note: str = "",
) -> str:
  lines = [
      "# BQCA live template sync (#559): rename map",
      "",
      "Generated by `python3 dashboards/looker_studio/tools/"
      "gen_bqca_rename_map.py` from:",
      "",
  ]
  for role, path, digest in inputs:
    lines.append(f"- {role}: `{path}` (sha256 `{digest}`)")
  lines += ["", *summary, ""]
  lines += [
      "For each row, rename the concept from the old name to the new `qt_`"
      " ID, set its `id` to `t0.` followed by the new ID, keep its source"
      " field and aggregation, and rename each reference listed (dimension"
      " or metric `concept_names`, `sort_column`). An empty aggregation is a"
      " dimension.",
      "",
      f"Also repoint the report-level `datasource_id` from `{repoint[0]}` to"
      f" `{repoint[1]}`, the `datasource_id` of `{BINDING_PATH}`."
      + repoint_note,
  ]
  page = None
  for row in rows:
    if row["page"] != page:
      page = row["page"]
      lines += [
          "",
          f"## Page {page}: {row['page_name']}",
          "",
          "| Component ID | Component | Chart type | Slot | Old name | New"
          " `qt_` ID | Source field | Aggregation | References |",
          "|---|---|---|---|---|---|---|---|---|",
      ]
    lines.append(
        f"| `{row['component_id']}` | {_cell(row['component_title'])} |"
        f" {row['chart_type']} | {row['slot']} | `{row['old_name']}` |"
        f" `{row['new_qt_id']}` | `{row['source_field']}` |"
        f" {row['aggregation']} | {row['references']} |"
    )
  return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
      "--dump",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "report textproto dump of the live template taken before the sync"
          " (default: the checked-in sanitized cut of the 2026-10-29 dump)"
      ),
  )
  parser.add_argument(
      "--remapped",
      type=pathlib.Path,
      metavar="PATH",
      help=(
          "remapped twin of the dump to check the map against (default with"
          " no --dump: the checked-in twin of the sanitized cut)"
      ),
  )
  parser.add_argument("--csv", type=pathlib.Path, metavar="PATH")
  parser.add_argument("--md", type=pathlib.Path, metavar="PATH")
  args = parser.parse_args(argv)
  if args.dump is None:
    args.dump = DEFAULT_DUMP
    if args.remapped is None:
      args.remapped = DEFAULT_REMAPPED

  validator = _load_validator()
  manifest = yaml.safe_load((ROOT / MANIFEST_PATH).read_text(encoding="utf-8"))
  binding = yaml.safe_load((ROOT / BINDING_PATH).read_text(encoding="utf-8"))
  errors: list[str] = []
  try:
    components, old_datasource_id, read_errors = read_dump(validator, args.dump)
    errors += read_errors
    rows, row_errors = build_rows(validator, manifest, components)
    errors += row_errors
    if args.remapped is not None and not errors:
      # The live audit also checks the twin's report-level datasource_id.
      errors += [
          f"remapped twin: {error}"
          for error in validator.validate_live_report_textproto(args.remapped)
      ]
      twin, _, twin_read_errors = read_dump(validator, args.remapped)
      errors += [f"remapped twin: {error}" for error in twin_read_errors]
      errors += twin_errors(rows, components, twin)
  except (OSError, ValueError) as exc:
    errors.append(f"cannot read a dump: {exc}")
  if errors:
    for error in errors:
      print(f"ERROR: {error}", file=sys.stderr)
    return 1

  categories = collections.Counter(
      component.get("category")
      for page in manifest.get("pages", [])
      for component in page.get("components", [])
  )
  manifest_columns = sum(
      len(validator._bqca_spec_columns(component))
      for page in manifest.get("pages", [])
      for component in page.get("components", [])
  )
  not_qt = sum(
      "is not a qt_ column ID" in error
      for error in validator.validate_live_report_textproto(args.dump)
  )
  roles = collections.Counter(
      role for row in rows for role in row["references"].split("; ") if role
  )
  concept_names = roles["dimension"] + roles["metric"]
  summary = [
      f"{len(rows)} concepts in {len({row['component_id'] for row in rows})}"
      f" components on {len({row['page'] for row in rows})} pages; the"
      f" manifest has {manifest_columns} columns in {sum(categories.values())}"
      f" components ({categories['scorecard']} scorecards +"
      f" {categories['chart']} charts).",
      f"The live audit reports {not_qt} concept names in the dump that are"
      " not qt_ IDs.",
      f"{sum(roles.values())} references to rename: {concept_names}"
      f" concept_names ({roles['dimension']} dimension, {roles['metric']}"
      f" metric) and {roles['sort']} sort_column.",
      f"Report-level datasource_id: {old_datasource_id!r} ->"
      f" {binding.get('datasource_id')!r}.",
  ]
  other_roles = sorted(set(roles) - set(_ROLES.values()))
  if other_roles:
    summary.append(
        "References outside the audited paths, which the sync must rename"
        f" too: {', '.join(other_roles)}."
    )
  if args.remapped is not None:
    summary.append(
        "Cross-checked with the remapped twin"
        f" {_display_path(args.remapped)}: the live audit accepts it, and it"
        " carries every new ID."
    )
  for line in summary:
    print(line, file=sys.stderr)

  csv_text = render_csv(rows)
  # Every file the map is built from, the binding (the repointed
  # datasource_id) included.
  inputs = [
      ("dump", _display_path(args.dump), _sha256(args.dump)),
      *(
          (role, _display_path(ROOT / path), _sha256(ROOT / path))
          for role, path in (
              ("manifest", MANIFEST_PATH),
              ("binding", BINDING_PATH),
          )
      ),
  ]
  if args.remapped is not None:
    inputs.append(
        (
            "remapped twin",
            _display_path(args.remapped),
            _sha256(args.remapped),
        )
    )
  markdown_summary = [
      summary[0],
      "",
      *(f"- {line}" for line in summary[1:]),
  ]
  if args.csv is None and args.md is None:
    sys.stdout.write(csv_text)
  if args.csv is not None:
    args.csv.write_text(csv_text, encoding="utf-8")
  if args.md is not None:
    args.md.write_text(
        render_markdown(
            rows,
            markdown_summary,
            inputs,
            (old_datasource_id, binding.get("datasource_id")),
            SANITIZED_DATASOURCE_NOTE if args.dump == DEFAULT_DUMP else "",
        ),
        encoding="utf-8",
    )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
