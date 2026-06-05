# `bqaa seed-events` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the codelab's synthetic `agent_events` generator into a maintained `bqaa seed-events` command backed by a deterministic, testable SDK module.

**Architecture:** A new SDK module (`seed_events.py`) owns a pure generator (`generate_seed_events`), a frozen result dataclass (`SeedEventsResult`), and an orchestrator (`run_seed_events`) with injectable `now`/`bq_client`. The `bqaa` CLI command lazily delegates to the orchestrator and maps `result.ok`/exceptions to exit codes. The existing example script becomes a thin shim over the same module.

**Tech Stack:** Python 3.10+, Typer/Click, `google-cloud-bigquery`, pytest, pyink + isort (2-space indent, Google style). Spec: `docs/superpowers/specs/2026-05-28-bqaa-seed-events-design.md`.

---

## File Structure

- **Create** `src/bigquery_agent_analytics/seed_events.py` — `Scenario` enum, `_EVENT_SCHEMA`, id helpers, `_decision_session`, `generate_seed_events`, `SeedEventsResult`, `run_seed_events`. One responsibility: synthetic event generation + insertion.
- **Modify** `src/bigquery_agent_analytics/cli.py` — add `@bqaa_app.command("seed-events")` near the `context-graph` registration (after line ~2040).
- **Create** `tests/test_seed_events.py` — unit tests for the pure generator + orchestrator.
- **Modify** `tests/test_cli_bqaa_app.py` — add CLI flag (param-inspection) + dry-run invocation tests.
- **Rewrite** `examples/codelab/periodic_materialization/seed_events.py` — thin argparse shim over `run_seed_events`.
- **Create** `tests/test_seed_events_wrapper.py` — proves the example shim routes through `run_seed_events`.
- **Modify** docs: `docs/codelabs/periodic_materialization.md`, `examples/codelab/periodic_materialization/README.md`, regenerate `examples/codelab/periodic_materialization/colab_notebook.ipynb`.

Conventions: every new `.py` starts with the Apache 2.0 header used across the repo (copy from `tests/test_cli_bqaa_app.py:1-13`). Indentation is **2 spaces**.

---

### Task 1: Pure generator — `Scenario`, schema, `generate_seed_events`

**Files:**
- Create: `src/bigquery_agent_analytics/seed_events.py`
- Test: `tests/test_seed_events.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_seed_events.py` (prepend the Apache header from `tests/test_cli_bqaa_app.py:1-13`):

```python
"""Tests for the synthetic agent_events generator (issue #246)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bigquery_agent_analytics.seed_events import Scenario
from bigquery_agent_analytics.seed_events import generate_seed_events

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_EVENTS_PER_SESSION = 6  # submit(1) + evaluate(3) + commit(1) + completed(1)


def test_same_seed_and_now_is_byte_identical() -> None:
  a = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  b = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  assert a == b


def test_different_seed_changes_content() -> None:
  a = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  b = generate_seed_events(sessions=3, seed=7, now=_FIXED_NOW)
  assert a != b


def test_seed_none_still_produces_valid_rows() -> None:
  rows = generate_seed_events(sessions=2, seed=None, now=_FIXED_NOW)
  assert len(rows) == 2 * _EVENTS_PER_SESSION


def test_payload_shape_and_terminal_events() -> None:
  rows = generate_seed_events(sessions=4, seed=1, now=_FIXED_NOW)
  assert len(rows) == 4 * _EVENTS_PER_SESSION

  expected_cols = {
      "timestamp", "event_type", "agent", "session_id", "invocation_id",
      "user_id", "trace_id", "span_id", "parent_span_id", "status",
      "error_message", "is_truncated", "content", "attributes", "latency_ms",
  }
  per_session_completed: dict[str, int] = {}
  for row in rows:
    assert set(row) == expected_cols
    assert row["event_type"] in {"TOOL_COMPLETED", "AGENT_COMPLETED"}
    json.loads(row["content"])  # valid JSON string
    if row["event_type"] == "AGENT_COMPLETED":
      per_session_completed[row["session_id"]] = (
          per_session_completed.get(row["session_id"], 0) + 1
      )

  assert len(per_session_completed) == 4
  assert all(count == 1 for count in per_session_completed.values())


@pytest.mark.parametrize("bad", [0, -5])
def test_sessions_must_be_at_least_one(bad: int) -> None:
  with pytest.raises(ValueError, match="sessions must be >= 1"):
    generate_seed_events(sessions=bad, seed=1, now=_FIXED_NOW)


def test_scenario_enum_default_is_decision() -> None:
  assert Scenario.DECISION.value == "decision"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bigquery_agent_analytics.seed_events'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/bigquery_agent_analytics/seed_events.py` (prepend the Apache header from `tests/test_cli_bqaa_app.py:1-13`):

```python
"""Synthetic agent_events generator backing ``bqaa seed-events`` (#246).

Writes a small corpus of TOOL_COMPLETED + AGENT_COMPLETED events to a
configured ``agent_events`` table. Each session is a 3-step decision flow
(submit_request -> evaluate_option x3 -> commit_outcome) closed by an
AGENT_COMPLETED row, which is the terminal event the materializer keys on.

``--seed`` freezes IDs and content (and event structure); timestamps stay
anchored to run time so seeded events land inside the materializer's
``--lookback-hours`` window. Inject a fixed ``now`` via the SDK for
byte-identical rows.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import random
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional


class Scenario(str, enum.Enum):
  """Synthetic event scenarios. Extensible seam for #247."""

  DECISION = "decision"


_EVENT_SCHEMA_FIELDS = (
    ("timestamp", "TIMESTAMP", "REQUIRED"),
    ("event_type", "STRING", "REQUIRED"),
    ("agent", "STRING", "NULLABLE"),
    ("session_id", "STRING", "NULLABLE"),
    ("invocation_id", "STRING", "NULLABLE"),
    ("user_id", "STRING", "NULLABLE"),
    ("trace_id", "STRING", "NULLABLE"),
    ("span_id", "STRING", "NULLABLE"),
    ("parent_span_id", "STRING", "NULLABLE"),
    ("status", "STRING", "NULLABLE"),
    ("error_message", "STRING", "NULLABLE"),
    ("is_truncated", "BOOLEAN", "NULLABLE"),
    ("content", "JSON", "NULLABLE"),
    ("attributes", "JSON", "NULLABLE"),
    ("latency_ms", "JSON", "NULLABLE"),
)

_TOPICS = ("approve loan", "schedule maintenance", "grant access", "release budget")


def _hex(rng: random.Random, length: int) -> str:
  """Deterministic hex id of ``length`` chars, driven by ``rng``."""
  return f"{rng.getrandbits(4 * length):0{length}x}"


def _row(
    rng: random.Random,
    event_type: str,
    session_id: str,
    content: dict,
    ts: datetime,
) -> dict:
  return {
      "timestamp": ts.isoformat(),
      "event_type": event_type,
      "agent": "demo-agent",
      "session_id": session_id,
      "invocation_id": _hex(rng, 32),
      "user_id": "demo-user",
      "trace_id": session_id[:16],
      "span_id": _hex(rng, 16),
      "parent_span_id": None,
      "status": "ok",
      "error_message": None,
      "is_truncated": False,
      "content": json.dumps(content),
      "attributes": "{}",
      "latency_ms": "{}",
  }


def _decision_session(rng: random.Random, now: datetime) -> list[dict]:
  session_id = f"sess-{_hex(rng, 8)}"
  request_id = f"req-{_hex(rng, 6)}"
  topic = rng.choice(_TOPICS)
  rows: list[dict] = [
      _row(
          rng,
          "TOOL_COMPLETED",
          session_id,
          {
              "tool": "submit_request",
              "result": {"request_id": request_id, "request_text": f"Should we {topic}?"},
          },
          now,
      )
  ]

  options = [
      {
          "option_id": f"opt-{_hex(rng, 5)}",
          "option_label": label,
          "confidence": round(rng.uniform(0.1, 0.95), 2),
      }
      for label in ("yes", "no", "defer")
  ]
  for i, opt in enumerate(options):
    rows.append(
        _row(
            rng,
            "TOOL_COMPLETED",
            session_id,
            {"tool": "evaluate_option", "result": {"request_id": request_id, **opt}},
            now + timedelta(seconds=i + 1),
        )
    )

  selected = max(options, key=lambda o: o["confidence"])
  rationale = (
      f"Picked '{selected['option_label']}' "
      f"(confidence {selected['confidence']:.2f}) over "
      f"the {len(options) - 1} alternatives."
  )
  rows.append(
      _row(
          rng,
          "TOOL_COMPLETED",
          session_id,
          {
              "tool": "commit_outcome",
              "result": {
                  "request_id": request_id,
                  "outcome_id": f"out-{_hex(rng, 6)}",
                  "status": "committed",
                  "rationale": rationale,
              },
          },
          now + timedelta(seconds=5),
      )
  )
  rows.append(
      _row(rng, "AGENT_COMPLETED", session_id, {"final": True}, now + timedelta(seconds=6))
  )
  return rows


_SCENARIO_BUILDERS = {Scenario.DECISION: _decision_session}


def generate_seed_events(
    *,
    sessions: int,
    seed: Optional[int],
    now: datetime,
    scenario: Scenario = Scenario.DECISION,
) -> list[dict]:
  """Build synthetic agent_events rows. Pure: no I/O.

  ``(seed, now)`` fixed -> byte-identical rows. ``seed=None`` -> live RNG.
  Raises ``ValueError`` if ``sessions < 1``.
  """
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  rng = random.Random(seed)
  builder = _SCENARIO_BUILDERS[scenario]
  rows: list[dict] = []
  cur = now - timedelta(minutes=10)
  for _ in range(sessions):
    rows.extend(builder(rng, cur))
    cur += timedelta(seconds=30)
  return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bigquery_agent_analytics/seed_events.py tests/test_seed_events.py
git commit -m "feat(seed-events): deterministic synthetic agent_events generator (#246)"
```

---

### Task 2: Result object + `run_seed_events` orchestrator

**Files:**
- Modify: `src/bigquery_agent_analytics/seed_events.py`
- Test: `tests/test_seed_events.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_seed_events.py`:

```python
from bigquery_agent_analytics.seed_events import SeedEventsResult
from bigquery_agent_analytics.seed_events import run_seed_events


class _FakeBQClient:
  """Records create_table / insert_rows_json calls; returns canned errors."""

  def __init__(self, insert_errors: list | None = None) -> None:
    self.created: list = []
    self.inserted: list = []
    self._insert_errors = insert_errors or []

  def create_table(self, table: object, exists_ok: bool = False) -> object:
    self.created.append((table, exists_ok))
    return table

  def insert_rows_json(self, table_ref: str, rows: list) -> list:
    self.inserted.append((table_ref, rows))
    return self._insert_errors


def test_dry_run_generates_without_touching_bigquery() -> None:
  fake = _FakeBQClient()
  result = run_seed_events(
      project_id="p", dataset_id="d", sessions=2, seed=1,
      dry_run=True, now=_FIXED_NOW, bq_client=fake,
  )
  assert fake.created == [] and fake.inserted == []
  assert result.dry_run is True
  assert result.ok is True
  assert result.events_inserted == 0
  assert result.events_generated == 2 * _EVENTS_PER_SESSION
  assert result.event_type_counts["AGENT_COMPLETED"] == 2
  assert result.table_ref == "p.d.agent_events"


def test_insert_success_reports_inserted_count() -> None:
  fake = _FakeBQClient()
  result = run_seed_events(
      project_id="p", dataset_id="d", sessions=3, seed=1,
      now=_FIXED_NOW, bq_client=fake,
  )
  assert len(fake.created) == 1
  table_ref, rows = fake.inserted[0]
  assert table_ref == "p.d.agent_events"
  assert len(rows) == 3 * _EVENTS_PER_SESSION
  assert result.ok is True
  assert result.events_inserted == 3 * _EVENTS_PER_SESSION
  assert result.errors == []


def test_insert_errors_are_explicit_not_exceptions() -> None:
  bq_errors = [{"index": 0, "errors": [{"reason": "invalid"}]}]
  fake = _FakeBQClient(insert_errors=bq_errors)
  result = run_seed_events(
      project_id="p", dataset_id="d", sessions=1, seed=1,
      now=_FIXED_NOW, bq_client=fake,
  )
  assert result.ok is False
  assert result.errors == bq_errors
  assert result.events_inserted == 0


def test_run_seed_events_rejects_bad_sessions() -> None:
  with pytest.raises(ValueError, match="sessions must be >= 1"):
    run_seed_events(project_id="p", dataset_id="d", sessions=0,
                    seed=1, now=_FIXED_NOW, bq_client=_FakeBQClient())


def test_to_json_round_trips() -> None:
  result = run_seed_events(
      project_id="p", dataset_id="d", sessions=1, seed=1,
      dry_run=True, now=_FIXED_NOW, bq_client=_FakeBQClient(),
  )
  payload = result.to_json()
  assert json.loads(json.dumps(payload)) == payload
  assert payload["ok"] is True
  assert payload["events_inserted"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_seed_events'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/bigquery_agent_analytics/seed_events.py`:

```python
@dataclasses.dataclass(frozen=True)
class SeedEventsResult:
  """Outcome of a seed-events run."""

  table_ref: str
  scenario: str
  sessions: int
  events_generated: int
  events_inserted: int
  dry_run: bool
  ok: bool
  event_type_counts: dict[str, int]
  errors: list[dict]

  def to_json(self) -> dict[str, Any]:
    return {
        "table_ref": self.table_ref,
        "scenario": self.scenario,
        "sessions": self.sessions,
        "events_generated": self.events_generated,
        "events_inserted": self.events_inserted,
        "dry_run": self.dry_run,
        "ok": self.ok,
        "event_type_counts": dict(self.event_type_counts),
        "errors": list(self.errors),
    }


def run_seed_events(
    *,
    project_id: str,
    dataset_id: str,
    sessions: int = 5,
    seed: Optional[int] = None,
    scenario: Scenario | str = Scenario.DECISION,
    events_table: str = "agent_events",
    dry_run: bool = False,
    now: Optional[datetime] = None,
    bq_client: Optional[Any] = None,
) -> SeedEventsResult:
  """Generate synthetic events and (unless ``dry_run``) insert them.

  Invalid input (``sessions < 1``, unknown ``scenario``) raises; the CLI
  maps that to exit 2. BigQuery insert errors are modeled as ``ok=False``
  with ``errors`` populated -- not raised -- so the JSON report stays
  authoritative (CLI exit 1).
  """
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  scenario = Scenario(scenario) if isinstance(scenario, str) else scenario
  if now is None:
    now = datetime.now(timezone.utc)

  rows = generate_seed_events(sessions=sessions, seed=seed, now=now, scenario=scenario)
  counts: dict[str, int] = {}
  for row in rows:
    counts[row["event_type"]] = counts.get(row["event_type"], 0) + 1
  table_ref = f"{project_id}.{dataset_id}.{events_table}"

  if dry_run:
    return SeedEventsResult(
        table_ref=table_ref, scenario=scenario.value, sessions=sessions,
        events_generated=len(rows), events_inserted=0, dry_run=True, ok=True,
        event_type_counts=counts, errors=[],
    )

  from google.cloud import bigquery

  client = bq_client or bigquery.Client(project=project_id)
  table = bigquery.Table(table_ref, schema=_event_schema())
  table.time_partitioning = bigquery.TimePartitioning(field="timestamp")
  client.create_table(table, exists_ok=True)
  errors = client.insert_rows_json(table_ref, rows)
  if errors:
    return SeedEventsResult(
        table_ref=table_ref, scenario=scenario.value, sessions=sessions,
        events_generated=len(rows), events_inserted=0, dry_run=False, ok=False,
        event_type_counts=counts, errors=list(errors),
    )
  return SeedEventsResult(
      table_ref=table_ref, scenario=scenario.value, sessions=sessions,
      events_generated=len(rows), events_inserted=len(rows), dry_run=False, ok=True,
      event_type_counts=counts, errors=[],
  )


def _event_schema() -> list:
  """Build the BigQuery schema lazily (keeps import-time deps minimal)."""
  from google.cloud import bigquery

  return [
      bigquery.SchemaField(name, field_type, mode=mode)
      for name, field_type, mode in _EVENT_SCHEMA_FIELDS
  ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: PASS (12 tests total).

- [ ] **Step 5: Commit**

```bash
git add src/bigquery_agent_analytics/seed_events.py tests/test_seed_events.py
git commit -m "feat(seed-events): run_seed_events orchestrator with explicit insert-error result (#246)"
```

---

### Task 3: `bqaa seed-events` CLI command

**Files:**
- Modify: `src/bigquery_agent_analytics/cli.py` (after the `context-graph` registration, ~line 2040)
- Test: `tests/test_cli_bqaa_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_bqaa_app.py`:

```python
def test_bqaa_seed_events_exposes_expected_flags() -> None:
  """Inspect Click params, not rendered help (rich wraps long flags under CI)."""
  from typer.main import get_command

  command = get_command(bqaa_app).get_command(None, "seed-events")  # type: ignore[arg-type]
  assert command is not None, "bqaa app missing 'seed-events' subcommand"

  declared_flags = set()
  for param in command.params:
    declared_flags.update(getattr(param, "opts", []))
    declared_flags.update(getattr(param, "secondary_opts", []))

  for flag in (
      "--project-id", "--dataset-id", "--events-table", "--sessions",
      "--seed", "--scenario", "--dry-run", "--format",
  ):
    assert flag in declared_flags, f"flag {flag!r} missing from `bqaa seed-events`"


def test_bqaa_seed_events_dry_run_reports_without_bigquery() -> None:
  """--dry-run runs end-to-end with no BigQuery client and exits 0."""
  result = runner.invoke(
      bqaa_app,
      [
          "seed-events", "--project-id", "p", "--dataset-id", "d",
          "--sessions", "2", "--seed", "1", "--dry-run", "--format", "json",
      ],
  )
  assert result.exit_code == 0, result.output
  payload = json.loads(result.output)
  assert payload["dry_run"] is True
  assert payload["events_inserted"] == 0
  assert payload["events_generated"] == 12


def test_bqaa_seed_events_invalid_sessions_exits_2() -> None:
  result = runner.invoke(
      bqaa_app,
      ["seed-events", "--project-id", "p", "--dataset-id", "d",
       "--sessions", "0", "--dry-run"],
  )
  assert result.exit_code == 2, result.output
```

Add `import json` to the test file's imports if not already present (it imports `sys`, `textwrap` at `tests/test_cli_bqaa_app.py:29-30` — add `import json` alongside).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli_bqaa_app.py -q`
Expected: FAIL — `seed-events` subcommand not found / `AssertionError`.

- [ ] **Step 3: Write minimal implementation**

In `src/bigquery_agent_analytics/cli.py`, immediately after the `bqaa_app.command("context-graph")(materialize_window)` block (ends ~line 2040), add:

```python
@bqaa_app.command("seed-events")
def seed_events(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    events_table: str = typer.Option(
        "agent_events",
        "--events-table",
        help="Destination telemetry table name (in --dataset-id).",
    ),
    sessions: int = typer.Option(
        5, "--sessions", help="Number of synthetic decision sessions (>= 1)."
    ),
    seed: Optional[int] = typer.Option(
        None,
        "--seed",
        help=(
            "Seed for deterministic IDs/content. Timestamps remain anchored to"
            " run time unless using the SDK with an injected now."
        ),
    ),
    scenario: str = typer.Option(
        "decision",
        "--scenario",
        help="Synthetic scenario to generate. Currently: decision.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Generate and report events without creating the table or inserting.",
    ),
    fmt: str = typer.Option(
        "json", "--format", help="Output format: json|text|table."
    ),
) -> None:
  """Seed a dataset with synthetic agent_events for the context graph.

  Generates completed decision sessions (TOOL_COMPLETED + AGENT_COMPLETED)
  so ``bqaa context-graph`` has terminal-event-closed sessions to process.

  Exit codes:
      0 — events generated (and inserted, unless --dry-run).
      1 — BigQuery returned insert errors (reported in the JSON output).
      2 — invalid input or unexpected internal error.
  """
  try:
    from .seed_events import run_seed_events

    result = run_seed_events(
        project_id=project_id,
        dataset_id=dataset_id,
        sessions=sessions,
        seed=seed,
        scenario=scenario,
        events_table=events_table,
        dry_run=dry_run,
    )
    typer.echo(format_output(result.to_json(), fmt))
    if not result.ok:
      raise typer.Exit(code=1)
  except typer.Exit:
    raise
  except Exception as exc:  # noqa: BLE001
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_bqaa_app.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bigquery_agent_analytics/cli.py tests/test_cli_bqaa_app.py
git commit -m "feat(cli): add bqaa seed-events command (#246)"
```

---

### Task 4: Convert the example script to a thin wrapper

**Files:**
- Rewrite: `examples/codelab/periodic_materialization/seed_events.py`
- Test: `tests/test_seed_events_wrapper.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_seed_events_wrapper.py` (prepend the Apache header from `tests/test_cli_bqaa_app.py:1-13`):

```python
"""The codelab example seed_events.py stays a thin shim over the SDK (#246)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_WRAPPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples/codelab/periodic_materialization/seed_events.py"
)


def _load_wrapper():
  spec = importlib.util.spec_from_file_location("_codelab_seed_events", _WRAPPER_PATH)
  module = importlib.util.module_from_spec(spec)
  assert spec and spec.loader
  spec.loader.exec_module(module)
  return module


def test_wrapper_forwards_parsed_args_to_run_seed_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
  module = _load_wrapper()
  captured: dict = {}

  class _Result:
    def to_json(self) -> dict:
      return {"ok": True}

  def fake_run_seed_events(**kwargs):
    captured.update(kwargs)
    return _Result()

  monkeypatch.setattr(module, "run_seed_events", fake_run_seed_events)
  monkeypatch.setattr(
      "sys.argv",
      ["seed_events.py", "--project-id", "p", "--dataset-id", "d",
       "--sessions", "4", "--seed", "9"],
  )
  module.main()

  assert captured["project_id"] == "p"
  assert captured["dataset_id"] == "d"
  assert captured["sessions"] == 4
  assert captured["seed"] == 9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_seed_events_wrapper.py -q`
Expected: FAIL — wrapper still uses its own generation logic; `run_seed_events` attribute missing or args mismatch.

- [ ] **Step 3: Rewrite the wrapper**

Replace the entire contents of `examples/codelab/periodic_materialization/seed_events.py` with:

```python
"""Synthetic agent_events generator for the BQAA codelab (compatibility shim).

The maintained command is now ``bqaa seed-events``. This wrapper forwards to
the same SDK module (``bigquery_agent_analytics.seed_events``) that backs the
CLI, so the downloaded codelab kit keeps working:

    python seed_events.py \\
        --project-id "$PROJECT_ID" \\
        --dataset-id "$DATASET" \\
        --sessions 5

Prefer ``bqaa seed-events`` once the SDK is installed.
"""

from __future__ import annotations

import argparse

from bigquery_agent_analytics.formatter import format_output
from bigquery_agent_analytics.seed_events import run_seed_events


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--project-id", required=True)
  parser.add_argument("--dataset-id", required=True)
  parser.add_argument("--sessions", type=int, default=5)
  parser.add_argument("--seed", type=int, default=None)
  parser.add_argument("--format", dest="fmt", default="json")
  args = parser.parse_args()

  result = run_seed_events(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      sessions=args.sessions,
      seed=args.seed,
  )
  print(format_output(result.to_json(), args.fmt))


if __name__ == "__main__":
  main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_seed_events_wrapper.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/codelab/periodic_materialization/seed_events.py tests/test_seed_events_wrapper.py
git commit -m "refactor(codelab): make seed_events.py a thin wrapper over the SDK (#246)"
```

---

### Task 5: Update codelab docs + regenerate notebook

**Files:**
- Modify: `docs/codelabs/periodic_materialization.md`
- Modify: `examples/codelab/periodic_materialization/README.md`
- Regenerate: `examples/codelab/periodic_materialization/colab_notebook.ipynb`

- [ ] **Step 1: Update the codelab run command**

In `docs/codelabs/periodic_materialization.md` around line 264, replace the seeding invocation:

Old:
```bash
python seed_events.py \
```
New:
```bash
bqaa seed-events \
```
(Keep the surrounding `--project-id` / `--dataset-id` / `--sessions 5` flags unchanged.)

- [ ] **Step 2: Update the artifact README**

In `examples/codelab/periodic_materialization/README.md`:
- Line ~13 (`seed_events.py` table row): change description to note it is a thin wrapper — e.g. `Synthetic agent_events generator. Thin wrapper over the maintained \`bqaa seed-events\` command (SDK module \`bigquery_agent_analytics.seed_events\`).`
- Line ~21 (step 5): change `python seed_events.py --project-id ... --sessions 5` to `bqaa seed-events --project-id "$PROJECT_ID" --dataset-id "$DATASET" --sessions 5` and add: `(the bundled \`seed_events.py\` remains as a compatibility shim if you are running from the downloaded kit).`

- [ ] **Step 3: Regenerate the notebook**

Run:
```bash
python scripts/generate_colab_from_codelab.py \
    docs/codelabs/periodic_materialization.md \
    examples/codelab/periodic_materialization/colab_notebook.ipynb
```

- [ ] **Step 4: Verify the drift check passes**

Run:
```bash
python scripts/generate_colab_from_codelab.py --check \
    docs/codelabs/periodic_materialization.md \
    examples/codelab/periodic_materialization/colab_notebook.ipynb
```
Expected: exit 0 (notebook in sync with markdown).

- [ ] **Step 5: Commit**

```bash
git add docs/codelabs/periodic_materialization.md \
        examples/codelab/periodic_materialization/README.md \
        examples/codelab/periodic_materialization/colab_notebook.ipynb
git commit -m "docs(codelab): seed via bqaa seed-events; note compatibility shim (#246)"
```

---

### Task 6: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Format check**

Run: `bash autoformat.sh`
Expected: "All Python files formatted" with **no** modified files (`git status --short` clean for tracked files). If it reformats anything, `git add -A` the formatting-only changes and amend the most recent relevant commit.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`
Expected: all pass, including the new `tests/test_seed_events.py`, `tests/test_seed_events_wrapper.py`, and the added `tests/test_cli_bqaa_app.py` cases.

- [ ] **Step 3: Manual smoke**

Run:
```bash
python -c "from typer.testing import CliRunner; from bigquery_agent_analytics.cli import bqaa_app; \
r = CliRunner().invoke(bqaa_app, ['seed-events','--project-id','p','--dataset-id','d','--dry-run','--seed','1','--sessions','3']); \
print(r.exit_code); print(r.output)"
```
Expected: exit code `0`; JSON with `"dry_run": true`, `"events_inserted": 0`, `"events_generated": 18`, `"ok": true`.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin feat/bqaa-seed-events
gh pr create --title "feat(cli): add bqaa seed-events command (#246)" --body "<summary referencing the spec + closes #246>"
```
(Do not add Co-Authored-By / generated-by signatures — repo convention.)

---

## Self-Review

**Spec coverage:**
- SDK module owns generator + schema + result + orchestrator → Tasks 1–2. ✓
- `bqaa seed-events` product-umbrella-only command → Task 3. ✓
- `--seed` determinism (IDs/content frozen, timestamps run-time anchored; injected `now` for byte-identical) → Task 1 tests + module docstring + flag help. ✓
- `--dry-run` returns `events_generated` + `event_type_counts`, `events_inserted=0` → Task 2 + Task 3 tests. ✓
- Explicit insert-error model (`ok=False`, not exception) → Task 2 `test_insert_errors_are_explicit_not_exceptions`. ✓
- `--sessions >= 1` validation → exit 2 → Tasks 1/2/3 tests. ✓
- `--scenario` single value, extensible seam → `Scenario` enum + `_SCENARIO_BUILDERS`, Task 1. ✓
- Thin wrapper → Task 4. ✓
- Codelab/notebook move to maintained command → Task 5. ✓
- Tests inspect Click params, not rendered help → Task 3 `test_bqaa_seed_events_exposes_expected_flags`. ✓
- `--format` defaults to `json` (CLI + wrapper) → Task 3 + Task 4. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `generate_seed_events` / `SeedEventsResult` (fields: `table_ref, scenario, sessions, events_generated, events_inserted, dry_run, ok, event_type_counts, errors`) / `run_seed_events` signatures are identical across Tasks 1–4 and the tests. `_EVENTS_PER_SESSION = 6` matches the 6 rows emitted by `_decision_session`. `bigquery.Client` import is deferred inside `run_seed_events`/`_event_schema` so Task 1's pure generator stays import-light. ✓
