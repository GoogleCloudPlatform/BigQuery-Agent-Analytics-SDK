# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for scripts/mutation_testing.py, the report-only mutation run.

mutmut is not installed in the regular test environment, so these tests feed
the report code a hand-written run laid out the way mutmut 3.8 writes it: the
original module, a mutated copy under mutants/ and its .meta results file.
"""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mutation_testing.py"


def _load_script():
  spec = importlib.util.spec_from_file_location("mutation_testing", SCRIPT)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  # dataclasses resolve string annotations through sys.modules.
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


mt = _load_script()

ORIGINAL = '''\
"""Toy module."""


def score(value, limit):
  if value >= limit:
    return 0.0
  return 1.0


class Grader:

  def total(self, a, b):
    return a + b

  @staticmethod
  def label(name):
    logger.warning("graded %s", name)
    return name
'''

# Mutants sit next to an "orig" copy, and mutmut keeps the comments and
# decorators that preceded the original function, so a survivor's line has
# to be counted from the def line, not from the top of the generated block.
MUTATED = '''\
"""Toy module."""
from mutmut.mutation.trampoline import wrap_in_trampoline as _mutmut_mutated


def x_score__mutmut_orig(value, limit):
  if value >= limit:
    return 0.0
  return 1.0


def x_score__mutmut_1(value, limit):
  if value > limit:
    return 0.0
  return 1.0


def x_score__mutmut_2(value, limit):
  if value >= limit:
    return 1.0
  return 1.0


class Grader:

  def xǁGraderǁtotal__mutmut_orig(self, a, b):
    return a + b

  def xǁGraderǁtotal__mutmut_1(self, a, b):
    return a - b

  # A comment mutmut kept above the function.
  @staticmethod
  def xǁGraderǁlabel__mutmut_1(name):
    logger.warning("XXgraded %sXX", name)
    return name
'''

EXIT_CODES = {
    "toy.x_score__mutmut_1": 0,  # survived
    "toy.x_score__mutmut_2": 1,  # killed
    "toy.xǁGraderǁtotal__mutmut_1": 33,  # no tests
    "toy.xǁGraderǁlabel__mutmut_1": 0,  # survived
}
MODULE = "src/toy.py"


@pytest.fixture
def work_dir(tmp_path):
  work = tmp_path / "work"
  (work / "src").mkdir(parents=True)
  (work / "mutants" / "src").mkdir(parents=True)
  (work / MODULE).write_text(ORIGINAL, encoding="utf-8")
  (work / "mutants" / MODULE).write_text(MUTATED, encoding="utf-8")
  meta = {
      "exit_code_by_key": EXIT_CODES,
      "hash_by_function_name": {},
      "type_check_error_by_key": {},
      "durations_by_key": {},
      "estimated_durations_by_key": {},
  }
  (work / "mutants" / (MODULE + ".meta")).write_text(
      json.dumps(meta), encoding="utf-8"
  )
  (work / mt.WORK_DIR_MARKER).write_text("", encoding="utf-8")
  return work


def _by_id(result):
  return {m.id: m for m in result.mutants}


def test_survivors_are_located_in_the_original_file(work_dir):
  result = mt.collect_module_results(
      work_dir, MODULE, mt._FALLBACK_STATUS_BY_EXIT_CODE
  )

  assert result.counts == {"survived": 2, "killed": 1, "no tests": 1}
  assert result.score == pytest.approx(0.25)
  mutants = _by_id(result)
  assert "toy.x_score__mutmut_2" not in mutants  # killed mutants are omitted

  boundary = mutants["toy.x_score__mutmut_1"]
  assert (boundary.status, boundary.function, boundary.line) == (
      "survived",
      "score",
      5,
  )
  assert ORIGINAL.splitlines()[boundary.line - 1].strip() == boundary.before
  assert (boundary.before, boundary.after) == (
      "if value >= limit:",
      "if value > limit:",
  )
  assert boundary.kind == "comparison"

  untested = mutants["toy.xǁGraderǁtotal__mutmut_1"]
  assert (untested.status, untested.function, untested.line) == (
      "no tests",
      "Grader.total",
      13,
  )
  assert untested.kind == "arithmetic"

  logged = mutants["toy.xǁGraderǁlabel__mutmut_1"]
  assert (logged.function, logged.line) == ("Grader.label", 17)
  assert logged.after == 'logger.warning("XXgraded %sXX", name)'
  assert logged.kind == "log/warning call"


@pytest.mark.parametrize(
    "before, after, kind",
    [
        ("if x <= 0:", "if x < 0:", "comparison"),
        ("if a in b:", "if a not in b:", "boolean"),
        ("ok = True", "ok = False", "boolean"),
        ("return x if y else z", "return x if (y) or True else z", "boolean"),
        ("return a + b", "return a - b", "arithmetic"),
        ("limit: float = 5000.0,", "limit: float = 5001.0,", "number"),
        (
            "observed = s.get(key, 0)",
            "observed = s.get(key, None)",
            "replaced with None",
        ),
        ("context = { ⏎ 'k': v, ⏎ }", "context = None", "replaced with None"),
        ("budget=limit, ⏎ )", ")", "removed code"),
        ('observed_key="turn_count",', "", "removed code"),
        ('status: str = "OK",', 'status: str = "XXOKXX",', "string literal"),
        (
            'logger.debug("failed %s", e)',
            'logger.debug("failed %s", None)',
            "log/warning call",
        ),
        ("name.lower()", "name.upper()", "other"),
    ],
)
def test_classify_mutation(before, after, kind):
  assert mt.classify_mutation(before, after) == kind


@pytest.mark.parametrize(
    "mutant_id, expected",
    [
        ("pkg.mod.x_score__mutmut_3", (None, "score", "x_score__mutmut_3")),
        (
            "pkg.mod.xǁGraderǁtotal__mutmut_12",
            ("Grader", "total", "xǁGraderǁtotal__mutmut_12"),
        ),
        (
            "pkg.mod.xǁ_Entryǁ__init____mutmut_1",
            ("_Entry", "__init__", "xǁ_Entryǁ__init____mutmut_1"),
        ),
    ],
)
def test_parse_mutant_id(mutant_id, expected):
  assert mt.parse_mutant_id(mutant_id) == expected


def test_score_leaves_out_mutants_that_never_ran():
  # A run stopped by the time budget scores only what it evaluated.
  assert mt.mutation_score(
      {"killed": 6, "timeout": 2, "survived": 2, "not checked": 90}
  ) == pytest.approx(0.8)
  assert mt.mutation_score({"not checked": 5, "skipped": 1}) is None
  assert mt.mutation_score({}) is None


def test_mutmut_config_is_valid_toml():
  tomllib = pytest.importorskip("tomllib")
  text = mt.render_mutmut_config(
      mt.DEFAULT_MODULES,
      mt.DEFAULT_TESTS,
      ["README.md", "deploy/", "tests/"],
      timeout_multiplier=10,
      timeout_constant=1,
  )

  config = tomllib.loads(text)["tool"]["mutmut"]
  assert config["source_paths"] == ["src/"]
  assert config["only_mutate"] == list(mt.DEFAULT_MODULES)
  assert config["pytest_add_cli_args_test_selection"] == list(mt.DEFAULT_TESTS)
  assert config["also_copy"] == ["README.md", "deploy/", "tests/"]
  assert (config["timeout_multiplier"], config["timeout_constant"]) == (
      10.0,
      1.0,
  )


def test_default_modules_and_tests_exist():
  # A rename would otherwise surface only in the scheduled workflow.
  for relative in mt.DEFAULT_MODULES + mt.DEFAULT_TESTS:
    assert (ROOT / relative).is_file(), relative


def test_report_only_run_exits_zero_with_survivors(work_dir, tmp_path):
  report_dir = tmp_path / "report"

  exit_code = mt.main(
      [
          "--report-only",
          "--work-dir",
          str(work_dir),
          "--report-dir",
          str(report_dir),
          "--module",
          MODULE,
      ]
  )

  # Surviving mutants are reported, never turned into a failing exit status.
  assert exit_code == 0
  report = json.loads((report_dir / mt.JSON_REPORT).read_text("utf-8"))
  assert report["status"] == "report-only"
  assert report["report_only"] is True
  assert report["totals"]["counts"]["survived"] == 2
  assert report["modules"][0]["score"] == pytest.approx(0.25)
  markdown = (report_dir / mt.MARKDOWN_REPORT).read_text("utf-8")
  assert "Report-only: surviving mutants never fail a build" in markdown
  assert "| `toy.py:5` | `score` | comparison |" in markdown
  assert "## Mutants with no covering test" in markdown


@pytest.mark.parametrize(
    "exit_codes, returncode, interrupted, status, expected_exit",
    [
        (EXIT_CODES, 0, False, "complete", 0),
        # Time budget hit mid-run: what ran is reported, still exit 0.
        ({**EXIT_CODES, "toy.x_score__mutmut_2": None}, 0, True, "partial", 0),
        # mutmut stopped before testing any mutant (e.g. a selected test
        # failed in the scratch copy): the only non-zero exit.
        (dict.fromkeys(EXIT_CODES), 1, False, "failed", 1),
    ],
)
def test_run_status_and_exit_code(
    work_dir,
    tmp_path,
    monkeypatch,
    exit_codes,
    returncode,
    interrupted,
    status,
    expected_exit,
):
  meta_path = work_dir / "mutants" / (MODULE + ".meta")
  meta = json.loads(meta_path.read_text("utf-8"))
  meta["exit_code_by_key"] = exit_codes
  meta_path.write_text(json.dumps(meta), encoding="utf-8")

  def fake_run(mutmut, work, raw_log, max_children, budget, meta_paths=()):
    # mutmut redraws a spinner status line with carriage returns.
    raw_log.write_bytes(
        "⠋ Running stats\r⠙ Running stats\rfailed to collect stats\n".encode()
    )
    return mt.RunOutcome(returncode, interrupted, 3.0)

  monkeypatch.setattr(mt, "_mutmut_version", lambda: mt.PINNED_MUTMUT_VERSION)
  monkeypatch.setattr(mt.shutil, "which", lambda *args, **kwargs: "mutmut")
  monkeypatch.setattr(mt, "prepare_work_dir", lambda *args, **kwargs: None)
  monkeypatch.setattr(mt, "run_mutmut", fake_run)
  report_dir = tmp_path / "report"

  exit_code = mt.main(
      [
          "--work-dir",
          str(work_dir),
          "--report-dir",
          str(report_dir),
          "--module",
          MODULE,
      ]
  )

  assert exit_code == expected_exit
  report = json.loads((report_dir / mt.JSON_REPORT).read_text("utf-8"))
  assert report["status"] == status
  log = (report_dir / mt.LOG_FILE).read_text("utf-8")
  assert log == "Running stats\nfailed to collect stats\n"
  markdown = (report_dir / mt.MARKDOWN_REPORT).read_text("utf-8")
  assert ("failed to collect stats" in markdown) == (status != "complete")
  # A partial run says how much of each score is still unchecked.
  assert ("(1 not checked)" in markdown) == (status == "partial")


def test_snapshot_skips_truncated_results_and_restore_puts_it_back(
    tmp_path,
):
  # mutmut rewrites .meta without an atomic rename; a stop signal can land
  # mid-write and leave the JSON truncated.
  good = tmp_path / "good.meta"
  good.write_text('{"exit_code_by_key": {"a": 1}}', encoding="utf-8")
  cut = tmp_path / "cut.meta"
  cut.write_text('{"exit_code_by_key": {"a": 1', encoding="utf-8")

  snapshots = mt.snapshot_results([good, cut], attempts=2, delay_seconds=0)
  assert list(snapshots) == [str(good)]

  good.write_text('{"exit_code_by_key": {"a": 0}}', encoding="utf-8")
  stale = {str(good): '{"exit_code_by_key": {}}'}
  assert mt.restore_unreadable_results(stale) == []  # readable: kept as is
  assert json.loads(good.read_text("utf-8")) == {"exit_code_by_key": {"a": 0}}

  snapshot = cut.read_text("utf-8") + "}}"
  assert mt.restore_unreadable_results({str(cut): snapshot}) == [str(cut)]
  assert json.loads(cut.read_text("utf-8")) == {"exit_code_by_key": {"a": 1}}


def test_truncated_results_are_reported_not_raised(work_dir, tmp_path):
  meta_path = work_dir / "mutants" / (MODULE + ".meta")
  meta_path.write_text(meta_path.read_text("utf-8")[:40], encoding="utf-8")

  result = mt.collect_module_results(
      work_dir, MODULE, mt._FALLBACK_STATUS_BY_EXIT_CODE
  )
  assert result.missing and result.counts == {}

  report_dir = tmp_path / "report"
  assert (
      mt.main(
          [
              "--report-only",
              "--work-dir",
              str(work_dir),
              "--report-dir",
              str(report_dir),
              "--module",
              MODULE,
          ]
      )
      == 0
  )
  markdown = (report_dir / mt.MARKDOWN_REPORT).read_text("utf-8")
  assert f"`{MODULE}`: no readable mutmut results" in markdown


def test_stopped_run_restores_truncated_results(
    work_dir, tmp_path, monkeypatch
):
  meta_path = work_dir / "mutants" / (MODULE + ".meta")
  snapshot = meta_path.read_text("utf-8")

  def fake_run(mutmut, work, raw_log, max_children, budget, meta_paths=()):
    assert [Path(p) for p in meta_paths] == [meta_path.resolve()]
    meta_path.write_text(snapshot[:40], encoding="utf-8")  # cut mid-write
    raw_log.write_bytes(b"Stopping...\n")
    return mt.RunOutcome(0, True, 600.0, {str(meta_path.resolve()): snapshot})

  monkeypatch.setattr(mt, "_mutmut_version", lambda: mt.PINNED_MUTMUT_VERSION)
  monkeypatch.setattr(mt.shutil, "which", lambda *args, **kwargs: "mutmut")
  monkeypatch.setattr(mt, "prepare_work_dir", lambda *args, **kwargs: None)
  monkeypatch.setattr(mt, "run_mutmut", fake_run)
  report_dir = tmp_path / "report"

  exit_code = mt.main(
      [
          "--work-dir",
          str(work_dir),
          "--report-dir",
          str(report_dir),
          "--module",
          MODULE,
      ]
  )

  assert exit_code == 0
  report = json.loads((report_dir / mt.JSON_REPORT).read_text("utf-8"))
  assert report["status"] == "partial"
  assert report["restored_results"] == [MODULE]
  assert report["totals"]["counts"]["survived"] == 2
  markdown = (report_dir / mt.MARKDOWN_REPORT).read_text("utf-8")
  assert "the stop left mutmut's results file truncated" in markdown


def test_report_only_without_results_fails(tmp_path):
  assert mt.main(["--report-only", "--work-dir", str(tmp_path)]) == 1


def test_markdown_escapes_pipes_in_code():
  mutant = {
      "id": "toy.x_f__mutmut_1",
      "module": MODULE,
      "status": "survived",
      "function": "f",
      "line": 3,
      "before": "return a | b",
      "after": "return a & b",
      "kind": "arithmetic",
  }
  report = mt.build_report(
      [], status="complete", tests=[], settings={"max_children": 1}
  )
  report["modules"] = [
      {
          "path": MODULE,
          "missing": False,
          "counts": {s: 0 for s in mt.STATUSES},
          "total": 1,
          "score": 0.0,
      }
  ]
  report["mutants"] = [mutant]

  markdown = mt.render_markdown(report)

  assert "`return a \\| b` → `return a & b`" in markdown


def test_reset_work_dir_refuses_foreign_directories(tmp_path):
  foreign = tmp_path / "foreign"
  foreign.mkdir()
  (foreign / "keep.txt").write_text("not ours", encoding="utf-8")

  with pytest.raises(SystemExit, match="refusing to delete"):
    mt.reset_work_dir(foreign, ROOT)
  assert (foreign / "keep.txt").exists()
  with pytest.raises(SystemExit, match="refusing to use"):
    mt.reset_work_dir(ROOT, ROOT)


def test_missing_mutmut_exits_2_without_touching_work_dir(
    tmp_path, monkeypatch
):
  monkeypatch.setattr(mt, "_mutmut_version", lambda: None)
  work = tmp_path / "work"

  assert mt.main(["--work-dir", str(work)]) == 2
  assert not work.exists()
