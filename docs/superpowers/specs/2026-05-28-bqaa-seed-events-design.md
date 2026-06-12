# Design: `bqaa seed-events` — promote the codelab seed generator into a maintained CLI (#246)

**Status:** Approved (brainstorming) — ready for implementation plan
**Issue:** #246 (unblocked by #245 / PR #260)
**Follow-ups this unblocks:** #247 (realistic-scale demo data), then #255 (CA-first guide)

## Problem

The synthetic `agent_events` generator lives at
`examples/codelab/periodic_materialization/seed_events.py` (181 lines, argparse,
unseeded `random` + `uuid.uuid4()` + `datetime.now()`). It is download-and-run
example code, not a maintained, testable, reproducible product surface. #246
promotes it into the `bqaa` umbrella as `bqaa seed-events`, backed by a real SDK
module, while keeping the example script working as a compatibility shim.

## Goals

- A maintained `bqaa seed-events` command on the product umbrella.
- Reusable logic in an SDK module, mirroring `materialize_window.py`.
- Deterministic, reproducible content via `--seed` (IDs/content frozen;
  timestamps stay anchored to run time so the demo still works).
- An offline `--dry-run` that reports exactly what would be written.
- Real tests: CLI parsing, deterministic output, payload shape, insert behavior.

## Non-goals (YAGNI)

- Multiple scenarios — `--scenario` ships with a single value behind an
  extensible seam; more scenarios are #247.
- Realistic-scale volume/timing — #247.
- Registration on the internal `bq-agent-sdk` app — product umbrella only.

## Design

### 1. SDK module — `src/bigquery_agent_analytics/seed_events.py`

Mirrors `materialize_window.py`'s shape: pure builders + frozen-dataclass result
+ a top-level orchestrator with injectable dependencies.

**Scenario seam**

```python
class Scenario(str, enum.Enum):
    DECISION = "decision"   # the current 3-step submit/evaluate/commit flow
```
A small dispatch (`_SCENARIO_BUILDERS = {Scenario.DECISION: _decision_session}`)
so #247 adds members without changing the public API.

**Pure generator (no I/O, the core unit under test)**

```python
def generate_seed_events(
    *,
    sessions: int,
    seed: int | None,
    now: datetime,
    scenario: Scenario = Scenario.DECISION,
) -> list[dict]:
```
- A local `rng = random.Random(seed)` drives **everything**: topic/confidence
  choices, option ordering, outcome selection, **and all identifiers**
  (session/request/option/outcome/invocation/span ids derived from `rng`,
  replacing `uuid.uuid4()`).
- `now` is injected; sessions are spaced at fixed offsets from it (e.g. base
  `now - 10min`, `+30s` per session, `+Ns` per step within a session).
- Result: `(seed, now)` fixed → **byte-identical** rows. `seed=None` → a live
  `random.Random()` (non-deterministic content), timestamps anchored to `now`.

**Determinism contract (surfaced in help + docs):** `--seed` freezes
IDs/content and event structure, **not** timestamps. Timestamps default to run
time so seeded events land inside `bqaa context-graph --lookback-hours 24`.
Byte-identical rows require injecting a fixed `now` via the SDK.

**Schema** — `_EVENT_SCHEMA` (the `agent_events` columns + `timestamp` time
partitioning) moves here from the example and becomes the single source of truth.

**Result object**

```python
@dataclasses.dataclass(frozen=True)
class SeedEventsResult:
    table_ref: str
    scenario: str
    sessions: int
    events_generated: int
    events_inserted: int          # 0 when dry_run
    dry_run: bool
    ok: bool                      # False when BigQuery returned insert errors
    event_type_counts: dict[str, int]
    errors: list[dict]            # BigQuery insert errors, empty on success
    def to_json(self) -> dict: ...
```

**Orchestrator**

```python
def run_seed_events(
    *,
    project_id: str,
    dataset_id: str,
    sessions: int = 5,
    seed: int | None = None,
    scenario: Scenario = Scenario.DECISION,
    events_table: str = "agent_events",
    dry_run: bool = False,
    now: datetime | None = None,        # injectable for reproducible fixtures
    bq_client: Any | None = None,       # injectable for tests
) -> SeedEventsResult:
```
- `now` defaults to `datetime.now(timezone.utc)` when not injected.
- **Input validation:** `sessions` must be `>= 1`. `run_seed_events` (and
  `generate_seed_events`) raise `ValueError("sessions must be >= 1")` for `0` or
  negative values **before** any BigQuery call. This is invalid input, not a
  logical insert failure — it is an exception (CLI exit 2), distinct from the
  `ok=False` insert-error path (exit 1). This prevents the bad codelab failure
  mode where `--sessions 0` creates the table, inserts nothing, and the next
  `bqaa context-graph` step finds no sessions and the reader is stuck.
- Builds rows via `generate_seed_events`.
- **`dry_run=True`**: no `create_table`, no insert. Returns
  `events_inserted=0`, `dry_run=True`, `ok=True`, with `events_generated` and
  `event_type_counts` populated so callers see exactly what would be written.
- **`dry_run=False`**: `create_table(exists_ok=True)`, then
  `insert_rows_json`. If BigQuery returns row errors, model it **explicitly** —
  return `ok=False` with `errors=<bq errors>` and `events_inserted=0`. This is
  an expected outcome, **not** an exception. Unexpected failures (auth,
  permissions, programming errors) propagate as exceptions.

### 2. CLI command — `bqaa seed-events` (product umbrella only)

Registered on `bqaa_app`. Lazy-imports the module, calls `run_seed_events`,
emits `typer.echo(format_output(result.to_json(), fmt))`. Exit handling mirrors
`materialize_window` exactly:

```python
typer.echo(format_output(result.to_json(), fmt))
if not result.ok:
    raise typer.Exit(code=1)     # explicit insert-error path
except typer.Exit:
    raise
except Exception as exc:         # noqa: BLE001
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)     # genuinely unexpected
```

Flags:

| Flag | Type / default | Notes |
|---|---|---|
| `--project-id` | str, envvar `BQ_AGENT_PROJECT` | |
| `--dataset-id` | str, envvar `BQ_AGENT_DATASET` | |
| `--events-table` | str, `agent_events` | consistency with `context-graph` |
| `--sessions` | int, `5` | |
| `--seed` | int, optional | help: *"Seed for deterministic IDs/content. Timestamps remain anchored to run time unless using the SDK with an injected now."* |
| `--scenario` | enum, `decision` | extensible seam |
| `--dry-run` | bool, `False` | generate + report, no table create / no insert |
| `--format` | `json\|text\|table`, default `json` | via `format_output` |

### 3. Compatibility shim — `examples/codelab/periodic_materialization/seed_events.py`

Reduced to a thin argparse wrapper that imports `run_seed_events` and calls it
(`--project-id`, `--dataset-id`, `--sessions`, adds `--seed`; `--format`
defaults to `json` so its output matches the CLI and is easy to diff). The
downloaded-kit flow (`python seed_events.py …`) keeps working; the module
docstring points at `bqaa seed-events` as the maintained command.

### 4. Docs

- `docs/codelabs/periodic_materialization.md` + `examples/codelab/.../colab_notebook.ipynb`:
  seed step changes from `python seed_events.py …` → `bqaa seed-events …`.
  Notebook regenerated via `scripts/generate_colab_from_codelab.py`; `--check`
  passes (CI drift guard).
- `examples/codelab/periodic_materialization/README.md`: maintained command is
  `bqaa seed-events`; the local `seed_events.py` remains a thin wrapper for
  downloaded-kit users.

### 5. Tests — `tests/test_seed_events.py` (+ flag test alongside the bqaa CLI tests)

Flag test inspects **Click params**, never rendered help (the P1 lesson from
PR #260 — rich wraps long option names under CI's 80-col non-TTY rendering).

1. **CLI parsing** — `bqaa seed-events` declares
   `--project-id/--dataset-id/--events-table/--sessions/--seed/--scenario/--dry-run/--format`
   (param inspection via `typer.main.get_command(...).get_command(None, "seed-events").params`).
2. **Determinism** — `generate_seed_events(sessions=3, seed=42, now=FIXED)`
   twice → identical; different seed → different; `seed=None` → still valid rows.
3. **Payload shape** — every row carries the `agent_events` columns; `content`
   is valid JSON; event types ∈ {`TOOL_COMPLETED`, `AGENT_COMPLETED`}; each
   session ends with exactly one `AGENT_COMPLETED`; total counts match
   `sessions`.
4. **Orchestrator with injected fake `bq_client`** —
   - `dry_run=True`: no `create_table` / no `insert_rows_json`;
     `events_inserted == 0`, `events_generated > 0`, `ok is True`,
     `event_type_counts` populated.
   - `dry_run=False`, insert succeeds: `insert_rows_json` called with the right
     `table_ref` and row count; `events_inserted` matches; `ok is True`.
   - `dry_run=False`, insert returns errors: `ok is False`, `errors` populated,
     `events_inserted == 0` — no exception raised.
5. **Wrapper** — example `seed_events.py` routes through `run_seed_events`.
6. **Invalid `--sessions`** — `generate_seed_events` / `run_seed_events` raise
   `ValueError` for `sessions=0` and `sessions=-5` before any BigQuery call;
   the CLI surfaces invalid input as exit code 2.

## Required Verification (acceptance checks — not yet performed)

- Full test suite green across Python 3.10–3.14.
- `bash autoformat.sh` reports no changes (pyink + isort).
- Codelab notebook `--check` passes.
- Manual smoke: `bqaa seed-events --help` shows the flags; `--dry-run` prints a
  JSON report with `events_inserted=0`; a real run inserts and reports `ok=true`.
