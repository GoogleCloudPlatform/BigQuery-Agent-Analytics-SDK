#!/usr/bin/env python3
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

"""Report-only mutation testing for a few core SDK modules.

Mutation testing makes small changes to the code (``<=`` to ``<``, ``True``
to ``False``, a string to ``"XX...XX"``) and reruns the tests that execute the
changed function. A mutant that no test notices "survives": the tests run that
code but do not pin down its behavior.

This script runs mutmut (https://github.com/boxed/mutmut) over
``DEFAULT_MODULES`` and writes a Markdown and a JSON report: the mutation
score of each module and every surviving mutant as ``file:line`` plus the code
change. It is report-only. Surviving mutants never change the exit status, and
no pull-request check runs it.

mutmut runs in a scratch copy of the repository (``build/mutation/work`` by
default) whose pyproject.toml gets a generated ``[tool.mutmut]`` table, so the
checkout itself is never modified.

Usage::

  pip install -e ".[dev]" -r scripts/requirements-mutation.txt
  python scripts/mutation_testing.py

Exit status: 0 when a report was written, whatever the survivors; 1 when
mutmut failed before it tested any mutant (the report then holds the log
tail); 2 when mutmut is not installed. See docs/mutation_testing.md.
"""

from __future__ import annotations

import argparse
import ast
import collections
import dataclasses
import datetime
import difflib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "build" / "mutation"
PINNED_MUTMUT_VERSION = "3.8.0"

# The deterministic scoring path: the pure UDF kernels, the SystemEvaluator
# metric factories built on them, and AggregateGrader's composition of
# graders. No BigQuery client or network, and 95-100% branch coverage from
# fast unit tests, so a survivor points at a missing assertion rather than at
# untested code.
DEFAULT_MODULES = (
    "src/bigquery_agent_analytics/udf_kernels.py",
    "src/bigquery_agent_analytics/system_evaluator.py",
    "src/bigquery_agent_analytics/aggregate_grader.py",
)

# Every test file that executes a line of DEFAULT_MODULES, found with
# coverage.py per-test contexts over the whole suite. mutmut maps each mutated
# function to the subset of these tests that call it and runs only those.
DEFAULT_TESTS = (
    "tests/test_adk2_event_types.py",
    "tests/test_aggregate_grader.py",
    "tests/test_cli.py",
    "tests/test_cli_evalbench_score.py",
    "tests/test_client_labels.py",
    "tests/test_event_semantics.py",
    "tests/test_formatter.py",
    "tests/test_pr123_client_regressions.py",
    "tests/test_pr16_fixes.py",
    "tests/test_pr17_fixes.py",
    "tests/test_pr19_fixes.py",
    "tests/test_remote_function.py",
    "tests/test_sdk_client.py",
    "tests/test_streaming_evaluation.py",
    "tests/test_system_evaluator.py",
    "tests/test_trace_identity_u2.py",
    "tests/test_udf_kernels.py",
    "tests/test_udf_sql_generation.py",
)

# mutmut stops a mutant after (estimated seconds of its tests + constant) *
# multiplier of wall-clock time; its CPU-time limit is twice that. mutmut's
# own defaults are 15 and 1.
DEFAULT_TIMEOUT_MULTIPLIER = 10.0
DEFAULT_TIMEOUT_CONSTANT = 1.0
DEFAULT_TIME_BUDGET_MINUTES = 15.0
HEARTBEAT_SECONDS = 30.0
STOP_GRACE_SECONDS = 60.0

WORK_DIR_MARKER = ".bqaa-mutation-work"
JSON_REPORT = "mutation_report.json"
MARKDOWN_REPORT = "mutation_report.md"
LOG_FILE = "mutmut.log"

# Mirrors mutmut.stats.status_by_exit_code in mutmut 3.8.0, where unknown exit
# codes are "suspicious" too. Used when mutmut itself cannot be imported.
_FALLBACK_STATUS_BY_EXIT_CODE = {
    1: "killed",
    3: "killed",
    0: "survived",
    5: "no tests",
    2: "check was interrupted by user",
    None: "not checked",
    33: "no tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "caught by type check",
    -24: "timeout",
    24: "timeout",
    152: "timeout",
    255: "timeout",
    -11: "segfault",
    -9: "segfault",
}
STATUSES = (
    "killed",
    "timeout",
    "caught by type check",
    "survived",
    "no tests",
    "suspicious",
    "segfault",
    "skipped",
    "check was interrupted by user",
    "not checked",
)
DETECTED_STATUSES = ("killed", "timeout", "caught by type check")
UNEVALUATED_STATUSES = (
    "skipped",
    "not checked",
    "check was interrupted by user",
)

# Survivor kinds, most actionable first. Ordering drives the "highest-signal"
# table: a survived boundary or boolean flip usually means a missing edge-case
# assertion, while a reworded log message rarely matters.
KIND_ORDER = (
    "comparison",
    "boolean",
    "arithmetic",
    "number",
    "replaced with None",
    "removed code",
    "other",
    "string literal",
    "log/warning call",
)
_COMPARISON_TOKENS = {"<", "<=", ">", ">=", "==", "!=", "is", "in"}
_BOOLEAN_TOKENS = {"True", "False", "and", "or", "not"}
_ARITHMETIC_TOKENS = {
    "+",
    "-",
    "*",
    "/",
    "//",
    "%",
    "**",
    "<<",
    ">>",
    "&",
    "|",
    "^",
    "~",
    "+=",
    "-=",
    "*=",
    "/=",
    "//=",
    "%=",
    "**=",
    "=",
}
_TOKEN_RE = re.compile(
    r"""[rRbBuUfF]{0,2}(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
    r"|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?"
    r"|\w+"
    r"|\*\*=?|//=?|<<|>>|[<>=!]=|[-+*/%]="
    r"|\S"
)
_LOG_CALL_RE = re.compile(r"\b(?:logger|logging|warnings)\.\w+\(")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")
_MAX_CELL_CHARS = 110
_SPINNER_RE = re.compile(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] ")


@dataclasses.dataclass
class Mutant:
  """One mutant that the tests did not kill, located in the original file."""

  id: str
  module: str
  status: str
  function: str
  line: Optional[int]
  before: str
  after: str
  kind: str


@dataclasses.dataclass
class ModuleResult:
  """Status counts for one mutated module plus its non-killed mutants."""

  path: str
  counts: dict[str, int]
  mutants: list[Mutant]
  missing: bool = False

  @property
  def score(self) -> Optional[float]:
    return mutation_score(self.counts)


@dataclasses.dataclass
class RunOutcome:
  returncode: Optional[int]
  interrupted: bool
  elapsed_seconds: float
  # .meta path -> readable content taken just before mutmut was stopped.
  snapshots: dict[str, str] = dataclasses.field(default_factory=dict)


def mutation_score(counts: dict[str, int]) -> Optional[float]:
  """Detected / evaluated mutants; mutmut's badge formula for a full run.

  Skipped, never-checked and interrupted mutants are left out of the
  denominator, so a run cut short by the time budget scores only what it
  evaluated.
  """
  total = sum(counts.values())
  evaluated = total - sum(counts.get(s, 0) for s in UNEVALUATED_STATUSES)
  if evaluated <= 0:
    return None
  detected = sum(counts.get(s, 0) for s in DETECTED_STATUSES)
  return detected / evaluated


def status_table() -> dict[Any, str]:
  """mutmut's exit-code-to-status table, from mutmut itself when installed."""
  try:
    from mutmut.stats import status_by_exit_code
  except ImportError:
    return dict(_FALLBACK_STATUS_BY_EXIT_CODE)
  return dict(status_by_exit_code)


def status_for(exit_code: Optional[int], table: dict[Any, str]) -> str:
  return table.get(exit_code, "suspicious")


def _toml_list(values: Sequence[str]) -> str:
  return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def render_mutmut_config(
    modules: Sequence[str],
    tests: Sequence[str],
    also_copy: Sequence[str],
    timeout_multiplier: float,
    timeout_constant: float,
) -> str:
  """Returns the ``[tool.mutmut]`` table appended to the scratch pyproject."""
  return "\n".join(
      [
          "[tool.mutmut]",
          f"source_paths = {_toml_list(['src/'])}",
          f"only_mutate = {_toml_list(modules)}",
          f"also_copy = {_toml_list(also_copy)}",
          f"pytest_add_cli_args_test_selection = {_toml_list(tests)}",
          f"pytest_add_cli_args = {_toml_list(['-p', 'no:cacheprovider'])}",
          f"timeout_multiplier = {float(timeout_multiplier)!r}",
          f"timeout_constant = {float(timeout_constant)!r}",
          # The scratch copy is not a git checkout; hash files instead.
          "use_git_change_detection = false",
          "",
      ]
  )


def _repository_files(repo_root: Path) -> list[str]:
  """Tracked plus untracked-but-not-ignored files, relative to repo_root."""
  try:
    output = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout
  except (OSError, subprocess.CalledProcessError) as error:
    raise SystemExit(f"listing files needs a git checkout: {error}") from error
  return sorted({p for p in output.decode("utf-8").split("\0") if p})


def reset_work_dir(work_dir: Path, repo_root: Path) -> None:
  """Empties work_dir, refusing anything this script did not create."""
  work_dir = work_dir.resolve()
  repo_root = repo_root.resolve()
  if work_dir == repo_root or work_dir in repo_root.parents:
    raise SystemExit(f"refusing to use {work_dir} as the work directory")
  if work_dir.exists():
    if not (work_dir / WORK_DIR_MARKER).exists() and any(work_dir.iterdir()):
      raise SystemExit(
          f"refusing to delete {work_dir}: it is not empty and was not"
          f" created by {Path(__file__).name} (no {WORK_DIR_MARKER} file)"
      )
    shutil.rmtree(work_dir)
  work_dir.mkdir(parents=True)
  (work_dir / WORK_DIR_MARKER).write_text(
      "Scratch copy for scripts/mutation_testing.py; safe to delete.\n"
  )


def prepare_work_dir(
    work_dir: Path,
    repo_root: Path,
    modules: Sequence[str],
    tests: Sequence[str],
    timeout_multiplier: float,
    timeout_constant: float,
    skip_dirs: Sequence[Path] = (),
) -> None:
  """Copies the repository into work_dir and adds the mutmut config."""
  reset_work_dir(work_dir, repo_root)
  skip = [work_dir.resolve()] + [d.resolve() for d in skip_dirs]
  for relative in _repository_files(repo_root):
    source = repo_root / relative
    if not source.is_file():
      continue  # deleted in the working tree, or a submodule
    if any(
        d == source.resolve() or d in source.resolve().parents for d in skip
    ):
      continue  # an output directory of this script
    target = work_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
  for required in list(modules) + list(tests) + ["pyproject.toml"]:
    if not (work_dir / required.split("::")[0]).is_file():
      raise SystemExit(f"{required} is not a file in {repo_root}")
  pyproject = work_dir / "pyproject.toml"
  text = pyproject.read_text(encoding="utf-8")
  if re.search(r"^\[tool\.mutmut\]", text, re.MULTILINE):
    raise SystemExit("pyproject.toml already has a [tool.mutmut] table")
  # Tests read README.md, deploy/, examples/ and more from the repository
  # root, so mutmut's mutants/ tree gets everything, not only src/ and tests/.
  also_copy = sorted(
      entry.name + ("/" if entry.is_dir() else "")
      for entry in work_dir.iterdir()
      if entry.name not in ("src", "mutants", WORK_DIR_MARKER)
  )
  config = render_mutmut_config(
      modules, tests, also_copy, timeout_multiplier, timeout_constant
  )
  pyproject.write_text(text.rstrip("\n") + "\n\n" + config, encoding="utf-8")


def _last_status_line(log_path: Path) -> str:
  try:
    with open(log_path, "rb") as log:
      log.seek(0, os.SEEK_END)
      log.seek(max(0, log.tell() - 4096))
      tail = log.read().decode("utf-8", errors="replace")
  except OSError:
    return ""
  segments = [s.strip() for s in re.split(r"[\r\n]", tail) if s.strip()]
  return segments[-1][:160] if segments else ""


def snapshot_results(
    paths: Sequence[Path], attempts: int = 20, delay_seconds: float = 0.05
) -> dict[str, str]:
  """Copies of the .meta files that parse, taken before mutmut is stopped.

  mutmut rewrites a module's .meta file after every mutant without an atomic
  rename, so the stop signal can land mid-write and leave it truncated.
  """
  snapshots: dict[str, str] = {}
  for path in paths:
    for _ in range(attempts):
      try:
        text = path.read_text(encoding="utf-8")
        json.loads(text)
      except (OSError, ValueError):
        time.sleep(delay_seconds)
        continue
      snapshots[str(path)] = text
      break
  return snapshots


def restore_unreadable_results(snapshots: dict[str, str]) -> list[str]:
  """Puts back the snapshot of every .meta file the stop left unreadable."""
  restored = []
  for path, text in snapshots.items():
    try:
      json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
      Path(path).write_text(text, encoding="utf-8")
      restored.append(path)
  return restored


def _stop(proc: subprocess.Popen) -> None:
  """SIGINT lets mutmut stop its workers and keep finished results."""
  if proc.poll() is not None:
    return
  proc.send_signal(signal.SIGINT)
  try:
    proc.wait(timeout=STOP_GRACE_SECONDS)
  except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def run_mutmut(
    mutmut: str,
    work_dir: Path,
    raw_log: Path,
    max_children: int,
    budget_seconds: float,
    meta_paths: Sequence[Path] = (),
) -> RunOutcome:
  """Runs ``mutmut run`` in work_dir, bounded by budget_seconds."""
  env = dict(os.environ)
  # mutmut puts the mutated copy of src/ first on sys.path; an inherited
  # PYTHONPATH pointing at the checkout's src/ must not shadow it in the
  # subprocesses some tests start.
  env.pop("PYTHONPATH", None)
  env.pop("MUTANT_UNDER_TEST", None)
  command = [mutmut, "run", "--max-children", str(max_children)]
  print(f"[mutation] {' '.join(command)}  (cwd {work_dir})", flush=True)
  start = time.monotonic()
  interrupted = False
  snapshots: dict[str, str] = {}
  with open(raw_log, "wb") as log:
    proc = subprocess.Popen(
        command,
        cwd=work_dir,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
      while True:
        remaining = budget_seconds - (time.monotonic() - start)
        if remaining <= 0:
          print("[mutation] time budget reached; stopping mutmut", flush=True)
          interrupted = True
          snapshots = snapshot_results(meta_paths)
          _stop(proc)
          break
        try:
          proc.wait(timeout=min(HEARTBEAT_SECONDS, remaining))
          break
        except subprocess.TimeoutExpired:
          elapsed = int(time.monotonic() - start)
          print(
              f"[mutation] {elapsed // 60:02d}:{elapsed % 60:02d}"
              f" {_last_status_line(raw_log)}",
              flush=True,
          )
    except KeyboardInterrupt:
      interrupted = True
      snapshots = snapshot_results(meta_paths)
      _stop(proc)
  return RunOutcome(
      returncode=proc.returncode,
      interrupted=interrupted,
      elapsed_seconds=time.monotonic() - start,
      snapshots=snapshots,
  )


def _function_nodes(tree: ast.Module) -> dict[tuple[Optional[str], str], Any]:
  """Top-level functions and methods of top-level classes, by (class, name)."""
  nodes: dict[tuple[Optional[str], str], Any] = {}
  for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      nodes[(None, node.name)] = node
    elif isinstance(node, ast.ClassDef):
      for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
          nodes[(node.name, child.name)] = child
  return nodes


def parse_mutant_id(mutant_id: str) -> tuple[Optional[str], str, str]:
  """Splits a mutmut id into (class name, function name, mutant function).

  ``pkg.mod.x_score__mutmut_3`` is the third mutant of function ``score``;
  ``pkg.mod.xǁGraderǁtotal__mutmut_1`` the first of method ``Grader.total``.
  """
  mutant_function = mutant_id.rpartition(".")[2]
  mangled = mutant_function.partition("__mutmut_")[0]
  if mangled.startswith("xǁ"):
    _, class_name, function_name = mangled.split("ǁ")
    return class_name, function_name, mutant_function
  return None, mangled[len("x_") :], mutant_function


def _join_lines(lines: Sequence[str]) -> str:
  return " ⏎ ".join(line.strip() for line in lines)


def locate_mutation(
    original_lines: Sequence[str],
    original_node: Any,
    mutated_lines: Sequence[str],
    mutated_node: Any,
    function_name: str,
    mutant_function: str,
) -> tuple[int, str, str]:
  """Line in the original file plus the before/after code of one mutant.

  Both functions are compared from their ``def`` line, so decorators and the
  comments mutmut keeps above them do not shift the line numbers.
  """
  before = list(
      original_lines[original_node.lineno - 1 : original_node.end_lineno]
  )
  after = list(mutated_lines[mutated_node.lineno - 1 : mutated_node.end_lineno])
  after[0] = after[0].replace(mutant_function, function_name, 1)
  matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
  hunks = [op for op in matcher.get_opcodes() if op[0] != "equal"]
  if not hunks:
    return original_node.lineno, "", ""
  removed = [line for op in hunks for line in before[op[1] : op[2]]]
  added = [line for op in hunks for line in after[op[3] : op[4]]]
  return (
      original_node.lineno + hunks[0][1],
      _join_lines(removed),
      _join_lines(added),
  )


def _changed_tokens(before: str, after: str) -> tuple[list[str], list[str]]:
  a = _TOKEN_RE.findall(before)
  b = _TOKEN_RE.findall(after)
  removed: list[str] = []
  added: list[str] = []
  matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
  for tag, i1, i2, j1, j2 in matcher.get_opcodes():
    if tag != "equal":
      removed.extend(a[i1:i2])
      added.extend(b[j1:j2])
  return removed, added


def _is_string(token: str) -> bool:
  return token.endswith(("'", '"'))


def classify_mutation(before: str, after: str) -> str:
  """Coarse kind of a mutation, used to rank and group survivors."""
  if _LOG_CALL_RE.search(before):
    return "log/warning call"
  removed, added = _changed_tokens(before, after)
  changed = set(removed) | set(added)
  if not changed:
    return "other"
  if added == ["None"]:
    return "replaced with None"
  if removed and not added:
    return "removed code"
  if any(_is_string(token) for token in changed):
    return "string literal"
  if changed <= _COMPARISON_TOKENS:
    return "comparison"
  if changed - {"(", ")"} <= _BOOLEAN_TOKENS:
    return "boolean"
  if changed <= _ARITHMETIC_TOKENS:
    return "arithmetic"
  if all(_NUMBER_RE.fullmatch(token) for token in changed):
    return "number"
  return "other"


def _function_label(class_name: Optional[str], function_name: str) -> str:
  return f"{class_name}.{function_name}" if class_name else function_name


def collect_module_results(
    work_dir: Path, module: str, table: dict[Any, str]
) -> ModuleResult:
  """Reads mutmut's results for one module from work_dir/mutants."""
  meta_path = work_dir / "mutants" / (module + ".meta")
  try:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    # Never generated, or left truncated by a stop with no snapshot to restore.
    return ModuleResult(path=module, counts={}, mutants=[], missing=True)
  original_lines = (work_dir / module).read_text(encoding="utf-8").splitlines()
  mutated_lines = (
      (work_dir / "mutants" / module).read_text(encoding="utf-8").splitlines()
  )
  original_nodes = _function_nodes(ast.parse("\n".join(original_lines)))
  mutated_nodes = _function_nodes(ast.parse("\n".join(mutated_lines)))

  counts: collections.Counter[str] = collections.Counter()
  mutants: list[Mutant] = []
  for mutant_id, exit_code in meta["exit_code_by_key"].items():
    status = status_for(exit_code, table)
    counts[status] += 1
    if status in DETECTED_STATUSES:
      continue
    class_name, function_name, mutant_function = parse_mutant_id(mutant_id)
    original_node = original_nodes.get((class_name, function_name))
    mutated_node = mutated_nodes.get((class_name, mutant_function))
    line: Optional[int] = None
    before = after = ""
    if original_node is not None and mutated_node is not None:
      line, before, after = locate_mutation(
          original_lines,
          original_node,
          mutated_lines,
          mutated_node,
          function_name,
          mutant_function,
      )
    mutants.append(
        Mutant(
            id=mutant_id,
            module=module,
            status=status,
            function=_function_label(class_name, function_name),
            line=line,
            before=before,
            after=after,
            kind=classify_mutation(before, after),
        )
    )
  mutants.sort(key=lambda m: (m.line or 0, _mutant_number(m.id)))
  return ModuleResult(path=module, counts=dict(counts), mutants=mutants)


def _mutant_number(mutant_id: str) -> int:
  number = mutant_id.rpartition("__mutmut_")[2]
  return int(number) if number.isdigit() else 0


def _git(repo_root: Path, *args: str) -> str:
  try:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
  except (OSError, subprocess.CalledProcessError):
    return ""


def _mutmut_version() -> Optional[str]:
  try:
    return importlib.metadata.version("mutmut")
  except importlib.metadata.PackageNotFoundError:
    return None


def build_report(
    results: Sequence[ModuleResult],
    *,
    status: str,
    tests: Sequence[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
  """JSON-serializable report: settings, per-module scores and mutants."""
  totals: collections.Counter[str] = collections.Counter()
  for result in results:
    totals.update(result.counts)
  return {
      "status": status,
      "report_only": True,
      **settings,
      "tests": list(tests),
      "modules": [
          {
              "path": r.path,
              "missing": r.missing,
              "counts": {**dict.fromkeys(STATUSES, 0), **r.counts},
              "total": sum(r.counts.values()),
              "score": r.score,
          }
          for r in results
      ],
      "totals": {
          "counts": {**dict.fromkeys(STATUSES, 0), **totals},
          "total": sum(totals.values()),
          "score": mutation_score(dict(totals)),
      },
      "mutants": [dataclasses.asdict(m) for r in results for m in r.mutants],
  }


def _cell(text: str) -> str:
  """Inline code for a Markdown table cell (pipes escaped, long text cut)."""
  if len(text) > _MAX_CELL_CHARS:
    text = text[: _MAX_CELL_CHARS - 1] + "…"
  fence = "``" if "`" in text else "`"
  pad = " " if fence == "``" else ""
  return f"{fence}{pad}{text}{pad}{fence}".replace("|", "\\|")


def _mutation_cell(mutant: dict[str, Any]) -> str:
  before = _cell(mutant["before"]) if mutant["before"] else "(not located)"
  after = _cell(mutant["after"]) if mutant["after"] else "(removed)"
  return f"{before} → {after}"


def _percent(score: Optional[float]) -> str:
  return "n/a" if score is None else f"{score * 100:.1f}%"


def _duration(seconds: Optional[float]) -> str:
  if seconds is None:
    return "n/a"
  seconds = int(round(seconds))
  if seconds < 60:
    return f"{seconds} s"
  minutes, seconds = divmod(seconds, 60)
  return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"


def _location(path: str, line: Optional[int]) -> str:
  return f"`{path}:{line}`" if line else f"`{path}`"


def _kind_rank(kind: str) -> int:
  return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def _mutant_table(
    title: str, mutants: list[dict[str, Any]], modules: Sequence[str]
) -> list[str]:
  lines = [f"## {title}", ""]
  for module in modules:
    in_module = [m for m in mutants if m["module"] == module]
    if not in_module:
      continue
    lines += [
        f"### `{module}` ({len(in_module)})",
        "",
        "| Location | Function | Kind | Mutation | mutmut id |",
        "|---|---|---|---|---|",
    ]
    for m in in_module:
      lines.append(
          f"| {_location(Path(module).name, m['line'])} | `{m['function']}` |"
          f" {m['kind']} | {_mutation_cell(m)} | `{m['id']}` |"
      )
    lines.append("")
  return lines


def render_markdown(report: dict[str, Any], top: int = 25) -> str:
  """Human-readable report; every survivor is listed with file:line."""
  lines: list[str] = ["# Mutation testing report", ""]
  lines.append(
      "Report-only: surviving mutants never fail a build or block a merge."
      " How to run and read this report: `docs/mutation_testing.md`."
  )
  lines.append("")
  status_text = {
      "complete": "complete",
      "partial": (
          "partial (stopped by the time budget, an interrupt or a mutmut"
          " error; see the log tail below)"
      ),
      "failed": "failed before any mutant ran (see the log tail below)",
      "report-only": "regenerated from an earlier run",
  }.get(report["status"], report["status"])
  lines.append(
      f"- Run: {status_text}; {_duration(report.get('elapsed_seconds'))}"
      f" (budget {_duration(report.get('time_budget_seconds'))});"
      f" mutmut {report.get('mutmut_version')};"
      f" Python {report.get('python_version')};"
      f" {report.get('max_children')} workers"
  )
  lines.append(
      f"- Commit: `{report.get('commit') or 'unknown'}`"
      f"{' (with uncommitted changes)' if report.get('dirty') else ''};"
      f" generated {report.get('generated_at')}"
  )
  lines.append(
      "- Per-mutant timeout: wall clock"
      f" {report.get('timeout_multiplier')} × (estimated seconds of the"
      f" mutant's tests + {report.get('timeout_constant')} s); CPU limit"
      " twice that"
  )
  lines.append(
      f"- Tests: {len(report['tests'])} files (listed in `{JSON_REPORT}`)"
  )
  for module in report.get("restored_results") or []:
    lines.append(
        f"- `{module}`: the stop left mutmut's results file truncated, so the"
        " copy taken just before the stop was used; mutants finished after"
        " it count as not checked"
    )
  for module in report["modules"]:
    if module["missing"]:
      lines.append(f"- `{module['path']}`: no readable mutmut results")
  lines.append("")

  lines += [
      "## Scores",
      "",
      "| Module | Mutants | Killed | Timeout | Survived | No tests | Other |"
      " Score |",
      "|---|--:|--:|--:|--:|--:|--:|--:|",
  ]
  main_statuses = ("killed", "timeout", "survived", "no tests")
  rows = [(f"`{m['path']}`", m) for m in report["modules"]]
  rows.append(("**Total**", report["totals"]))
  for label, entry in rows:
    counts = entry["counts"]
    other = sum(v for k, v in counts.items() if k not in main_statuses)
    total = entry.get("total", sum(counts.values()))
    score = _percent(entry["score"])
    if counts.get("not checked"):
      score += f" ({counts['not checked']} not checked)"
    lines.append(
        f"| {label} | {total} | {counts['killed']} | {counts['timeout']} |"
        f" {counts['survived']} | {counts['no tests']} | {other} | {score} |"
    )
  lines.append("")
  lines.append(
      "Score = (killed + timeout) / (mutants − skipped − not checked), the"
      " formula of mutmut's own badge. A *no tests* mutant sits in a function"
      " that none of the selected tests calls."
  )
  lines.append("")

  survivors = [m for m in report["mutants"] if m["status"] == "survived"]
  no_tests = [m for m in report["mutants"] if m["status"] == "no tests"]
  others = [
      m
      for m in report["mutants"]
      if m["status"] not in ("survived", "no tests")
  ]
  modules = [m["path"] for m in report["modules"]]
  names = [Path(p).name for p in modules]

  if survivors:
    lines += ["## Survivors by kind", ""]
    lines.append("| Kind | " + " | ".join(names) + " | Total |")
    lines.append("|---|" + "--:|" * (len(names) + 1))
    by_kind = collections.Counter((m["kind"], m["module"]) for m in survivors)
    for kind in sorted({m["kind"] for m in survivors}, key=_kind_rank):
      cells = [str(by_kind.get((kind, p), 0)) for p in modules]
      total = sum(by_kind.get((kind, p), 0) for p in modules)
      lines.append(f"| {kind} | " + " | ".join(cells) + f" | {total} |")
    lines.append("")

  if survivors or no_tests:
    lines += [
        "## Survivors by function",
        "",
        "| Function | Module | Survived | No tests | Kinds |",
        "|---|---|--:|--:|---|",
    ]
    per_function: dict[tuple[str, str], list[dict[str, Any]]] = (
        collections.defaultdict(list)
    )
    for m in survivors + no_tests:
      per_function[(m["module"], m["function"])].append(m)
    ordered = sorted(
        per_function.items(), key=lambda item: (-len(item[1]), item[0])
    )
    for (module, function), items in ordered:
      kinds = collections.Counter(m["kind"] for m in items)
      kind_text = ", ".join(
          f"{k} {n}"
          for k, n in sorted(kinds.items(), key=lambda kv: _kind_rank(kv[0]))
      )
      lines.append(
          f"| `{function}` | {Path(module).name} |"
          f" {sum(m['status'] == 'survived' for m in items)} |"
          f" {sum(m['status'] == 'no tests' for m in items)} | {kind_text} |"
      )
    lines.append("")

  if survivors:
    ranked = sorted(
        survivors,
        key=lambda m: (_kind_rank(m["kind"]), m["module"], m["line"] or 0),
    )
    lines += [
        f"## Highest-signal survivors (top {min(top, len(ranked))})",
        "",
        "Ranked by kind: comparison, boolean and arithmetic changes first;"
        " string-literal and logging changes last.",
        "",
        "| Location | Function | Kind | Mutation |",
        "|---|---|---|---|",
    ]
    for m in ranked[:top]:
      lines.append(
          f"| {_location(m['module'], m['line'])} | `{m['function']}` |"
          f" {m['kind']} |"
          f" {_mutation_cell(m)} |"
      )
    lines.append("")

  if survivors:
    lines += _mutant_table("All surviving mutants", survivors, modules)
  if no_tests:
    lines += _mutant_table("Mutants with no covering test", no_tests, modules)
  if others:
    others_counts = collections.Counter(m["status"] for m in others)
    lines += [
        "## Other statuses",
        "",
        ", ".join(f"{s}: {n}" for s, n in sorted(others_counts.items())),
        "",
    ]
  if report.get("log_tail"):
    lines += [
        "## mutmut log tail",
        "",
        "```text",
        report["log_tail"],
        "```",
        "",
    ]
  lines += [
      "## Reproduce one mutant",
      "",
      "```bash",
      "cd build/mutation/work   # or the --work-dir of this run",
      "mutmut show '<mutmut id>'",
      "```",
      "",
  ]
  return "\n".join(lines)


def _clean_log(raw: bytes) -> str:
  """mutmut redraws a spinner line with carriage returns; keep one per state."""
  text = raw.decode("utf-8", errors="replace").replace("\r", "\n")
  kept: list[str] = []
  for line in text.split("\n"):
    line = _SPINNER_RE.sub("", line).rstrip()
    if line and (not kept or kept[-1] != line):
      kept.append(line)
  return "\n".join(kept) + "\n"


def _log_tail(log_text: str, lines: int = 40) -> str:
  return "\n".join(log_text.splitlines()[-lines:])


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description=(
          "Report-only mutation testing (mutmut) for core SDK modules."
          " Surviving mutants never change the exit status."
      )
  )
  parser.add_argument(
      "--module",
      dest="modules",
      action="append",
      metavar="PATH",
      help=(
          "Repository-relative module to mutate (repeatable). Default:"
          f" {', '.join(DEFAULT_MODULES)}."
      ),
  )
  parser.add_argument(
      "--test",
      dest="tests",
      action="append",
      metavar="PATH",
      help=(
          "Test file the mutants are checked against (repeatable). Pass the"
          " files that exercise any --module you add. Default: the"
          " DEFAULT_TESTS list in this script."
      ),
  )
  parser.add_argument(
      "--work-dir",
      type=Path,
      default=DEFAULT_OUTPUT_ROOT / "work",
      help="Scratch copy mutmut runs in (deleted and recreated each run).",
  )
  parser.add_argument(
      "--report-dir",
      type=Path,
      default=DEFAULT_OUTPUT_ROOT / "report",
      help=f"Where {MARKDOWN_REPORT}, {JSON_REPORT} and {LOG_FILE} go.",
  )
  parser.add_argument(
      "--max-children",
      type=int,
      default=os.cpu_count() or 2,
      help="Parallel mutmut workers (default: CPU count).",
  )
  parser.add_argument(
      "--time-budget-minutes",
      type=float,
      default=DEFAULT_TIME_BUDGET_MINUTES,
      help=(
          "Wall-clock budget for 'mutmut run'; on expiry the run is stopped"
          " and the partial results are reported."
      ),
  )
  parser.add_argument(
      "--timeout-multiplier",
      type=float,
      default=DEFAULT_TIMEOUT_MULTIPLIER,
      help="mutmut timeout_multiplier (per-mutant timeout).",
  )
  parser.add_argument(
      "--timeout-constant",
      type=float,
      default=DEFAULT_TIMEOUT_CONSTANT,
      help="mutmut timeout_constant in seconds (per-mutant timeout).",
  )
  parser.add_argument(
      "--top",
      type=int,
      default=25,
      help="Rows in the highest-signal survivors table.",
  )
  parser.add_argument(
      "--report-only",
      action="store_true",
      help="Do not run mutmut; rebuild the report from --work-dir.",
  )
  return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
  if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")  # mutmut's status uses emoji
  args = parse_args(argv)
  modules = tuple(args.modules or DEFAULT_MODULES)
  tests = tuple(args.tests or DEFAULT_TESTS)
  work_dir = args.work_dir.resolve()
  report_dir = args.report_dir.resolve()
  version = _mutmut_version()

  outcome: Optional[RunOutcome] = None
  restored: list[str] = []
  log_text = ""
  if not args.report_only:
    if os.name != "posix":
      print("mutmut needs fork(); run this on Linux, macOS or WSL.")
      return 2
    mutmut = shutil.which("mutmut", path=str(Path(sys.executable).parent))
    mutmut = mutmut or shutil.which("mutmut")
    if version is None or mutmut is None:
      print(
          "mutmut is not installed in this environment. Install it with:\n"
          '  pip install -e ".[dev]" -r scripts/requirements-mutation.txt'
      )
      return 2
    if version != PINNED_MUTMUT_VERSION:
      print(
          f"warning: mutmut {version} is installed; the report parser"
          f" follows mutmut {PINNED_MUTMUT_VERSION}"
          " (scripts/requirements-mutation.txt)."
      )
    print(f"[mutation] copying {REPO_ROOT} to {work_dir}", flush=True)
    prepare_work_dir(
        work_dir,
        REPO_ROOT,
        modules,
        tests,
        args.timeout_multiplier,
        args.timeout_constant,
        skip_dirs=[report_dir],
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    raw_log = work_dir / "mutmut-run.log"
    outcome = run_mutmut(
        mutmut,
        work_dir,
        raw_log,
        args.max_children,
        args.time_budget_minutes * 60,
        meta_paths=[work_dir / "mutants" / (m + ".meta") for m in modules],
    )
    for path in restore_unreadable_results(outcome.snapshots):
      relative = Path(path).relative_to(work_dir / "mutants").as_posix()
      restored.append(relative.removesuffix(".meta"))
      print(f"[mutation] {path} was truncated by the stop; restored snapshot")
    log_text = _clean_log(raw_log.read_bytes())
    (report_dir / LOG_FILE).write_text(log_text, encoding="utf-8")
  elif not (work_dir / "mutants").is_dir():
    print(f"--report-only: no mutmut results under {work_dir}")
    return 1

  table = status_table()
  results = [collect_module_results(work_dir, m, table) for m in modules]
  # mutmut writes every mutant as "not checked" first; any other status
  # means the mutation phase ran.
  ran = any(
      count
      for r in results
      for s, count in r.counts.items()
      if s != "not checked"
  )
  if outcome is None:
    status = "report-only"
  elif not ran:
    status = "failed"
  elif (
      outcome.interrupted
      or outcome.returncode != 0
      or any(r.missing for r in results)
  ):
    status = "partial"
  else:
    status = "complete"

  settings = {
      "tool": "mutmut",
      "mutmut_version": version,
      "python_version": platform.python_version(),
      "platform": platform.platform(),
      "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime(
          "%Y-%m-%dT%H:%M:%SZ"
      ),
      "commit": _git(REPO_ROOT, "rev-parse", "HEAD"),
      "dirty": bool(_git(REPO_ROOT, "status", "--porcelain")),
      "elapsed_seconds": outcome.elapsed_seconds if outcome else None,
      "mutmut_returncode": outcome.returncode if outcome else None,
      "time_budget_seconds": args.time_budget_minutes * 60,
      "max_children": args.max_children,
      "timeout_multiplier": args.timeout_multiplier,
      "timeout_constant": args.timeout_constant,
      "work_dir": str(work_dir),
      "restored_results": restored,
      "log_tail": (
          _log_tail(log_text) if status in ("failed", "partial") else ""
      ),
  }
  report = build_report(results, status=status, tests=tests, settings=settings)
  report_dir.mkdir(parents=True, exist_ok=True)
  (report_dir / JSON_REPORT).write_text(
      json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
  )
  markdown = render_markdown(report, top=args.top)
  (report_dir / MARKDOWN_REPORT).write_text(markdown, encoding="utf-8")

  print(f"[mutation] status: {status}")
  for module in report["modules"]:
    counts = module["counts"]
    print(
        f"[mutation] {module['path']}: score {_percent(module['score'])}"
        f" ({counts['killed']} killed, {counts['timeout']} timeout,"
        f" {counts['survived']} survived, {counts['no tests']} no tests"
        f" of {module['total']}"
        + (
            f"; {counts['not checked']} not checked)"
            if counts["not checked"]
            else ")"
        )
    )
  print(f"[mutation] report: {report_dir / MARKDOWN_REPORT}")
  if status == "failed":
    print(_log_tail(log_text))
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
