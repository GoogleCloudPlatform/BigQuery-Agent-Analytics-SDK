# Mutation testing (report-only)

Line coverage says a test *ran* a line. Mutation testing asks whether a test
would *notice* if that line were wrong. [mutmut](https://github.com/boxed/mutmut)
makes small edits to the code (`<=` to `<`, `True` to `False`, a number to the
next one, a string to `"XX…XX"`, an argument to `None`) and reruns the tests
that call the edited function. A mutant that every test still passes
*survives*: the tests execute that code but do not pin down its behavior.

This is a report, not a gate. Surviving mutants never fail a build, no
pull-request check runs mutation testing, and nothing here blocks a merge.

## What runs

`scripts/mutation_testing.py` mutates three modules on the deterministic
scoring path:

| Module | Contents |
|--------|----------|
| `src/bigquery_agent_analytics/udf_kernels.py` | Pure scoring and event-classification kernels shared by the SDK and the BigQuery Python UDFs |
| `src/bigquery_agent_analytics/system_evaluator.py` | `SystemEvaluator` (`CodeEvaluator`) metric factories such as latency, error rate and turn count, built on those kernels |
| `src/bigquery_agent_analytics/aggregate_grader.py` | `AggregateGrader` and its weighted, binary and majority strategies |

They need no BigQuery client or network, and fast unit tests give them 95-100%
branch coverage, so a survivor points at a missing assertion rather than at
untested code. Mutants are checked against the 18 test files that execute
these modules (`DEFAULT_TESTS` in the script). For each mutant, mutmut runs
only the tests that call the mutated function.

The script copies the repository (tracked files plus untracked files that are
not ignored) to `build/mutation/work/`, appends a `[tool.mutmut]` table to the
copy's `pyproject.toml`, and runs mutmut there. Your checkout and its
`pyproject.toml` are never modified, and `build/` is gitignored.

## Run it locally

mutmut needs `fork()`, so use Linux, macOS or WSL.

```bash
pip install -e ".[dev]" -r scripts/requirements-mutation.txt
python scripts/mutation_testing.py
```

The default run has about 970 mutants and takes well under a minute on a
laptop. The work directory is deleted and recreated on every run.

| Option | Default | Meaning |
|--------|---------|---------|
| `--module PATH` | the three modules above | Module to mutate (repeatable) |
| `--test PATH` | `DEFAULT_TESTS` | Test file to check mutants against (repeatable). Pass the tests that exercise any module you add, or its mutants are reported as *no tests* |
| `--time-budget-minutes` | 15 | Wall-clock budget. When it runs out, mutmut is stopped and the partial results are reported |
| `--timeout-multiplier`, `--timeout-constant` | 10, 1 | Per-mutant timeout: wall clock `multiplier × (estimated seconds of the mutant's tests + constant)`, CPU-time limit twice that |
| `--max-children` | CPU count | Parallel workers |
| `--report-only` | off | Rebuild the report from an existing `--work-dir` without running mutmut |

Exit status is `0` whenever a report was written, whatever the survivors. It
is `1` if no mutant got tested, for example because a selected test fails in
the scratch copy or the time budget ran out during setup (the report then
shows the log tail), and `2` if mutmut is not installed.

## Read the report

The script writes three files to `build/mutation/report/`:

- `mutation_report.md`: scores per module, survivors grouped by kind and by
  function, the highest-signal survivors, then every surviving mutant with its
  `file:line`, function, kind and code change.
- `mutation_report.json`: the same data plus the run settings, for tooling.
- `mutmut.log`: mutmut's output.

Each mutant gets one status:

- **killed**: a test failed, so the tests caught the change.
- **timeout**: the mutant ran past its per-mutant timeout. Counted as caught.
- **survived**: every selected test passed.
- **no tests**: no selected test calls the mutated function.
- **not checked**: the time budget ran out first.

Score = (killed + timeout) / (mutants − skipped − not checked), the formula
mutmut uses for its own badge. Compare a module's score across runs, not
across modules: a module with many log messages or SQL strings scores lower
without its tests being weaker.

Survivor kinds, most actionable first: `comparison` (such as `<=` to `<`,
usually a missing boundary test), `boolean`, `arithmetic`, `number`,
`replaced with None`, `removed code`, `other`, `string literal` and
`log/warning call`.

To see one mutant as a diff, pass its `mutmut id` from the report:

```bash
cd build/mutation/work
mutmut show 'bigquery_agent_analytics.udf_kernels.x_score_latency__mutmut_1'
```

## Triage a survivor

A survivor is a question, not a bug. It usually falls into one of three cases:

- **Missing assertion.** Add a test that fails on the mutant; for a
  `comparison` survivor, a test exactly at the boundary. Rerun the script to
  confirm the mutant is now killed.
- **Equivalent mutant.** The change does not alter the result for any finite
  input. For example, `avg_latency_ms >= threshold_ms` to `>` in
  `score_latency` survives because at equality the fall-through
  `1.0 - avg_latency_ms / threshold_ms` also returns `0.0`. Leave it, or
  simplify the code so the mutant disappears.
- **Not worth pinning.** A reworded log message or display string. Leave it.

Do not chase a score target. The report exists to find missing assertions.

## CI

`.github/workflows/mutation-testing.yml` runs the script every Monday at 07:30
UTC and on demand, from **Run workflow** in the Actions tab or with
`gh workflow run mutation-testing.yml --ref <branch>`. It never runs on pull
requests and is not a required check:

- the mutation step has `continue-on-error: true`, so survivors or a mutmut
  failure never fail the job;
- the script stops mutmut after 20 minutes, and the job has a 30-minute limit;
- the selected tests mock BigQuery, so no cloud credentials are needed;
- the Markdown report goes to the job summary, and `build/mutation/report/` is
  uploaded as the `mutation-report` artifact.

## Change the module set

Edit `DEFAULT_MODULES` and `DEFAULT_TESTS` in `scripts/mutation_testing.py`, or
try a module first with `--module` and `--test`. Keep the set small: each
module adds all of its mutants, and `trace.py` alone has about 2,200.

To list the test files that execute a module, run the suite under coverage.py
with per-test contexts:

```bash
pip install coverage
printf '[run]\nsource = src/bigquery_agent_analytics\ndynamic_context = test_function\n' > /tmp/covrc
coverage run --rcfile=/tmp/covrc -m pytest -q tests
coverage json --rcfile=/tmp/covrc --show-contexts -o /tmp/contexts.json \
  --include='src/bigquery_agent_analytics/udf_kernels.py'
```

Each covered line in `/tmp/contexts.json` lists the tests that ran it.
