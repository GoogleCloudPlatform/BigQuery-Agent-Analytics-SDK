# `decision-realistic` Scenario Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a realistic-scale `decision-realistic` seed-events scenario (100 sessions / 72h, fixed 70/10/10/10 outcome mix, multiple agents/users/entities, variable decision length) while leaving the small `decision` scenario byte-identical.

**Architecture:** Lift the `_SCENARIO_BUILDERS` seam from per-session builders to corpus builders `(rng, now, sessions) -> (rows, session_outcome_counts)`. `decision` keeps byte-identical output by wrapping its existing per-session builder; a new `decision-realistic` corpus builder produces the realistic shape. `run_seed_events` reports `session_outcome_counts`; `--sessions` gets overridable per-scenario defaults (decision=5, realistic=100).

**Tech Stack:** Python 3.10+, Typer/Click, `google-cloud-bigquery`, pytest, pyink + isort (2-space indent). Spec: `docs/superpowers/specs/2026-05-29-decision-realistic-scenario-design.md`.

---

## File Structure

- **Modify** `src/bigquery_agent_analytics/seed_events.py` — all generator/orchestrator changes (seam refactor, realistic builder, result field, default resolution).
- **Modify** `src/bigquery_agent_analytics/cli.py` — `--sessions` default becomes `None` (resolved per-scenario in the SDK).
- **Modify** `tests/test_seed_events.py` — generator-level tests (seam, realistic mix/shape/determinism, default resolution).
- **Modify** `tests/test_cli_bqaa_app.py` — CLI dry-run for `decision-realistic`, per-scenario default sizing.
- **Modify** `docs/codelabs/periodic_materialization.md` + regenerate `examples/codelab/periodic_materialization/colab_notebook.ipynb` — optional "Realistic Data" section.

All edits keep 2-space indentation. Run `bash autoformat.sh` after each task and accept its formatting. Commit messages must NOT contain `Co-Authored-By` / "Generated with" trailers.

**Determinism rule (applies throughout):** every random choice — including ids, option counts, confidences, time jitter, outcome assignment, and entity assignment — must flow through the single `random.Random(seed)` instance created by the caller. No `uuid`, no module-level `random.*`.

---

### Task 1: Refactor seam to corpus builders + add `session_outcome_counts`

Lifts the seam without changing `decision` output, and adds the outcome-count field (populated for `decision` as `{"success": N}`).

**Files:**
- Modify: `src/bigquery_agent_analytics/seed_events.py`
- Test: `tests/test_seed_events.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_seed_events.py`:

```python
def test_decision_corpus_is_byte_identical_after_refactor() -> None:
  # Pin decision output so the seam refactor cannot change it.
  rows = generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW)
  assert len(rows) == 3 * _EVENTS_PER_SESSION
  assert rows[0]["agent"] == "demo-agent"
  assert rows[0]["user_id"] == "demo-user"
  assert rows[-1]["event_type"] == "AGENT_COMPLETED"
  # Same (seed, now) still byte-identical.
  assert generate_seed_events(sessions=3, seed=42, now=_FIXED_NOW) == rows


def test_decision_result_reports_success_outcome_counts() -> None:
  result = run_seed_events(
      project_id="p", dataset_id="d", sessions=4, seed=1,
      dry_run=True, now=_FIXED_NOW, bq_client=_FakeBQClient(),
  )
  assert result.session_outcome_counts == {"success": 4}
  assert result.to_json()["session_outcome_counts"] == {"success": 4}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: FAIL — `SeedEventsResult` has no `session_outcome_counts` (AttributeError / unexpected keyword).

- [ ] **Step 3: Refactor the seam** — in `src/bigquery_agent_analytics/seed_events.py`:

(a) Replace the `_SCENARIO_BUILDERS` block (the `_SCENARIO_BUILDERS = {Scenario.DECISION: _decision_session}` line and its following assert) with a corpus builder + registry. Insert `_build_decision_corpus` immediately after `_decision_session`'s definition, then the registry:

```python
def _build_decision_corpus(
    rng: random.Random, now: datetime, sessions: int
) -> tuple[list[dict], dict[str, int]]:
  """Corpus builder for the small ``decision`` scenario.

  Reproduces the exact pre-refactor loop (30s apart from ``now - 10min``,
  delegating to ``_decision_session``) so output stays byte-identical.
  """
  rows: list[dict] = []
  cur = now - timedelta(minutes=10)
  for _ in range(sessions):
    rows.extend(_decision_session(rng, cur))
    cur += timedelta(seconds=30)
  return rows, {"success": sessions}


# scenario -> corpus builder ``(rng, now, sessions) -> (rows, outcome_counts)``
_SCENARIO_BUILDERS = {Scenario.DECISION: _build_decision_corpus}
assert set(_SCENARIO_BUILDERS) == set(Scenario), (
    "every Scenario needs a builder; missing: "
    f"{set(Scenario) - set(_SCENARIO_BUILDERS)}"
)
```

(b) Replace the body of `generate_seed_events` so it delegates to the corpus builder and returns rows only:

```python
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
  rows, _counts = _SCENARIO_BUILDERS[scenario](rng, now, sessions)
  return rows
```

(c) Add the `session_outcome_counts` field to `SeedEventsResult` (last field, with a default so existing direct constructions keep working) and include it in `to_json`:

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
  session_outcome_counts: dict[str, int] = dataclasses.field(default_factory=dict)

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
        "session_outcome_counts": dict(self.session_outcome_counts),
        "errors": list(self.errors),
    }
```

(d) In `run_seed_events`, replace the `generate_seed_events(...)` call with a direct builder call (own rng) capturing both rows and outcome counts, and pass `session_outcome_counts` into all three `SeedEventsResult(...)` constructions. The relevant region becomes:

```python
  rng = random.Random(seed)
  rows, session_outcome_counts = _SCENARIO_BUILDERS[scenario](rng, now, sessions)
  counts: dict[str, int] = {}
  for row in rows:
    counts[row["event_type"]] = counts.get(row["event_type"], 0) + 1
  table_ref = f"{project_id}.{dataset_id}.{events_table}"
```

Then add `session_outcome_counts=session_outcome_counts,` to each of the three `SeedEventsResult(...)` returns (dry-run, insert-error, success). Leave the rest of `run_seed_events` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_seed_events.py tests/test_cli_bqaa_app.py -q`
Expected: PASS (all prior tests + the 2 new ones; the existing CLI `insert_errors_exit_1` test still passes because the new field defaults to `{}`).

- [ ] **Step 5: Commit**

```bash
git add src/bigquery_agent_analytics/seed_events.py tests/test_seed_events.py
git commit -m "refactor(seed-events): corpus-builder seam + session_outcome_counts (#247)"
```

---

### Task 2: Helpers — shuffled cycle, outcome allocation, parameterized `_row`

Small, pure helpers the realistic builder needs, plus generalizing `_row` to accept `agent`/`user_id` (defaults keep `decision` byte-identical).

**Files:**
- Modify: `src/bigquery_agent_analytics/seed_events.py`
- Test: `tests/test_seed_events.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_seed_events.py`:

```python
from bigquery_agent_analytics.seed_events import _outcome_allocation
from bigquery_agent_analytics.seed_events import _shuffled_cycle


def test_outcome_allocation_exact_at_100() -> None:
  assert _outcome_allocation(100) == {
      "success": 70, "failed": 10, "orphaned": 10, "truncated": 10,
  }


def test_outcome_allocation_scales_and_keeps_min_one() -> None:
  assert _outcome_allocation(10) == {
      "success": 7, "failed": 1, "orphaned": 1, "truncated": 1,
  }
  assert _outcome_allocation(4) == {
      "success": 1, "failed": 1, "orphaned": 1, "truncated": 1,
  }


def test_outcome_allocation_rejects_too_few_sessions() -> None:
  for bad in (3, 1, 0):
    with pytest.raises(ValueError, match="decision-realistic requires sessions >= 4"):
      _outcome_allocation(bad)


def test_shuffled_cycle_covers_roster_and_is_deterministic() -> None:
  import random as _random

  roster = ("a", "b", "c", "d")
  out1 = _shuffled_cycle(_random.Random(1), roster, 10)
  out2 = _shuffled_cycle(_random.Random(1), roster, 10)
  assert out1 == out2  # deterministic for a fixed rng seed
  assert len(out1) == 10
  assert set(out1) == set(roster)  # every roster member appears
  # >=2 distinct even for small n
  assert len(set(_shuffled_cycle(_random.Random(2), roster, 2))) >= 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: FAIL — `_outcome_allocation` / `_shuffled_cycle` not defined.

- [ ] **Step 3: Implement helpers + generalize `_row`** — in `src/bigquery_agent_analytics/seed_events.py`:

(a) Add `import math` to the stdlib import block (keep imports sorted; isort will order it).

(b) Generalize `_row` to accept `agent`/`user_id` keyword args with the current defaults (so `_decision_session` keeps emitting identical rows):

```python
def _row(
    rng: random.Random,
    event_type: str,
    session_id: str,
    content: dict,
    ts: datetime,
    *,
    agent: str = "demo-agent",
    user_id: str = "demo-user",
) -> dict:
  return {
      "timestamp": ts.isoformat(),
      "event_type": event_type,
      "agent": agent,
      "session_id": session_id,
      "invocation_id": _hex(rng, 32),
      "user_id": user_id,
      # One trace per session in the demo corpus; trace_id mirrors session_id.
      "trace_id": session_id,
      "span_id": _hex(rng, 16),
      "parent_span_id": None,
      "status": "ok",
      "error_message": None,
      "is_truncated": False,
      "content": json.dumps(content),
      "attributes": "{}",
      "latency_ms": "{}",
  }
```

(c) Add the two helpers (place them after `_row`, before `_decision_session`):

```python
def _shuffled_cycle(
    rng: random.Random, roster: tuple[str, ...], n: int
) -> list[str]:
  """Return a length-``n`` assignment cycling ``roster``, shuffled in place.

  Repeats the roster to length ``n`` then shuffles, so every member appears
  (for ``n >= len(roster)``) and at least ``min(n, len(roster))`` distinct
  values are present -- a true coverage guarantee, not a probabilistic one.
  """
  reps = -(-n // len(roster))  # ceil division
  cycle = (list(roster) * reps)[:n]
  rng.shuffle(cycle)
  return cycle


def _outcome_allocation(sessions: int) -> dict[str, int]:
  """Exact, deterministic outcome-bucket counts for ``decision-realistic``.

  Each edge bucket (failed/orphaned/truncated) gets ``round-half-up`` of 10%,
  floored at 1; ``success`` takes the remainder. Exact 70/10/10/10 at 100.
  Requires ``sessions >= 4`` (else ``success`` would be < 1).
  """
  edge = max(1, math.floor(0.10 * sessions + 0.5))
  success = sessions - 3 * edge
  if success < 1:
    raise ValueError("decision-realistic requires sessions >= 4")
  return {
      "success": success,
      "failed": edge,
      "orphaned": edge,
      "truncated": edge,
  }
```

- [ ] **Step 4: Run tests to verify they pass (incl. decision byte-identical regression)**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: PASS. In particular `test_decision_corpus_is_byte_identical_after_refactor`, `test_same_seed_and_now_is_byte_identical`, and `test_payload_shape_and_terminal_events` must still pass — proving the `_row` signature change did not alter `decision` output.

- [ ] **Step 5: Commit**

```bash
git add src/bigquery_agent_analytics/seed_events.py tests/test_seed_events.py
git commit -m "feat(seed-events): outcome-allocation + shuffled-cycle helpers; parameterize _row (#247)"
```

---

### Task 3: `decision-realistic` scenario

The realistic corpus builder and its session builder, registered in the seam.

**Files:**
- Modify: `src/bigquery_agent_analytics/seed_events.py`
- Test: `tests/test_seed_events.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_seed_events.py`:

```python
from datetime import timedelta

from bigquery_agent_analytics.seed_events import build_realistic_corpus


def _by_session(rows: list[dict]) -> dict[str, list[dict]]:
  grouped: dict[str, list[dict]] = {}
  for row in rows:
    grouped.setdefault(row["session_id"], []).append(row)
  return grouped


def test_realistic_outcome_mix_exact_at_100() -> None:
  import random as _random

  rows, counts = build_realistic_corpus(_random.Random(42), _FIXED_NOW, 100)
  assert counts == {"success": 70, "failed": 10, "orphaned": 10, "truncated": 10}

  sessions = _by_session(rows)
  assert len(sessions) == 100
  orphaned = [s for s in sessions.values()
              if not any(r["event_type"] == "AGENT_COMPLETED" for r in s)]
  failed = [s for s in sessions.values() if any(
      r["event_type"] == "AGENT_COMPLETED" and r["status"] == "error" for r in s)]
  truncated = [s for s in sessions.values()
               if any(r["is_truncated"] for r in s)]
  assert len(orphaned) == 10
  assert len(failed) == 10
  assert len(truncated) == 10


def test_realistic_terminal_event_invariant() -> None:
  import random as _random

  rows, _ = build_realistic_corpus(_random.Random(7), _FIXED_NOW, 100)
  for session in _by_session(rows).values():
    terminals = [r for r in session if r["event_type"] == "AGENT_COMPLETED"]
    assert len(terminals) in (0, 1)  # orphaned -> 0, others -> exactly 1


def test_realistic_failed_sessions_have_error_message() -> None:
  import random as _random

  rows, _ = build_realistic_corpus(_random.Random(7), _FIXED_NOW, 100)
  for session in _by_session(rows).values():
    for row in session:
      if row["event_type"] == "AGENT_COMPLETED" and row["status"] == "error":
        assert row["error_message"]  # non-empty


def test_realistic_variable_option_count_in_range() -> None:
  import random as _random

  rows, _ = build_realistic_corpus(_random.Random(7), _FIXED_NOW, 100)
  for session in _by_session(rows).values():
    options = [r for r in session
               if '"tool": "evaluate_option"' in r["content"]]
    assert 2 <= len(options) <= 6


def test_realistic_multi_day_span_and_no_future() -> None:
  import random as _random
  from datetime import datetime

  rows, _ = build_realistic_corpus(_random.Random(7), _FIXED_NOW, 100)
  ts = [datetime.fromisoformat(r["timestamp"]) for r in rows]
  span = max(ts) - min(ts)
  assert span > timedelta(hours=24)
  assert span <= timedelta(hours=72)
  assert max(ts) <= _FIXED_NOW  # no future timestamps


def test_realistic_multiple_agents_and_users() -> None:
  import random as _random

  rows, _ = build_realistic_corpus(_random.Random(7), _FIXED_NOW, 100)
  assert len({r["agent"] for r in rows}) >= 2
  assert len({r["user_id"] for r in rows}) >= 2


def test_realistic_is_deterministic() -> None:
  import random as _random

  a, ca = build_realistic_corpus(_random.Random(42), _FIXED_NOW, 100)
  b, cb = build_realistic_corpus(_random.Random(42), _FIXED_NOW, 100)
  assert a == b and ca == cb


def test_realistic_rejects_too_few_sessions() -> None:
  import random as _random

  with pytest.raises(ValueError, match="decision-realistic requires sessions >= 4"):
    build_realistic_corpus(_random.Random(1), _FIXED_NOW, 3)


def test_generate_seed_events_supports_realistic_scenario() -> None:
  rows = generate_seed_events(
      sessions=100, seed=42, now=_FIXED_NOW, scenario=Scenario.DECISION_REALISTIC
  )
  assert len(_by_session(rows)) == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: FAIL — `build_realistic_corpus` / `Scenario.DECISION_REALISTIC` not defined.

- [ ] **Step 3: Implement the realistic scenario** — in `src/bigquery_agent_analytics/seed_events.py`:

(a) Add the enum member:

```python
class Scenario(str, enum.Enum):
  """Synthetic event scenarios. Extensible seam for #247."""

  DECISION = "decision"
  DECISION_REALISTIC = "decision-realistic"
```

(b) Add realistic constants near `_TOPICS`:

```python
_REALISTIC_AGENTS = (
    "loan-advisor",
    "ops-scheduler",
    "access-broker",
    "budget-allocator",
)
_REALISTIC_USERS = tuple(f"user-{i:03d}" for i in range(12))
_REALISTIC_OPTION_LABELS = (
    "approve",
    "reject",
    "defer",
    "escalate",
    "delegate",
    "hold",
)
_REALISTIC_WINDOW = timedelta(hours=72)
# Held back from ``now`` so a session's per-step offsets never produce a
# timestamp after ``now`` (a session spans at most ~8s; 60s is safe margin).
_MAX_SESSION_SPAN = timedelta(seconds=60)
```

(c) Add the session + corpus builders (place after `_build_decision_corpus`):

```python
def _realistic_session(
    rng: random.Random,
    start: datetime,
    outcome: str,
    agent: str,
    user: str,
    topic: str,
) -> list[dict]:
  """One realistic decision session starting at ``start``.

  ``outcome`` is one of success/failed/orphaned/truncated and shapes the
  terminal event and truncation flag. Option count varies 2..6.
  """
  session_id = f"sess-{_hex(rng, 8)}"
  request_id = f"req-{_hex(rng, 6)}"
  rows: list[dict] = []

  def add(event_type, content, offset, *, status="ok",
          error_message=None, is_truncated=False):
    row = _row(
        rng, event_type, session_id, content,
        start + timedelta(seconds=offset), agent=agent, user_id=user,
    )
    row["status"] = status
    row["error_message"] = error_message
    row["is_truncated"] = is_truncated
    rows.append(row)

  add(
      "TOOL_COMPLETED",
      {
          "tool": "submit_request",
          "result": {"request_id": request_id, "request_text": f"Should we {topic}?"},
      },
      0,
  )

  k = rng.randint(2, 6)
  options = [
      {
          "option_id": f"opt-{_hex(rng, 5)}",
          "option_label": _REALISTIC_OPTION_LABELS[j],
          "confidence": round(rng.uniform(0.1, 0.95), 2),
      }
      for j in range(k)
  ]
  for i, opt in enumerate(options):
    content = {"tool": "evaluate_option", "result": {"request_id": request_id, **opt}}
    # Truncated sessions clip one evaluate row's payload.
    clip = outcome == "truncated" and i == 0
    if clip:
      content["result"]["notes"] = "(payload truncated)"
    add("TOOL_COMPLETED", content, i + 1, is_truncated=clip)

  selected = max(options, key=lambda o: o["confidence"])
  add(
      "TOOL_COMPLETED",
      {
          "tool": "commit_outcome",
          "result": {
              "request_id": request_id,
              "outcome_id": f"out-{_hex(rng, 6)}",
              "status": "committed",
              "selected": selected["option_label"],
          },
      },
      k + 1,
  )

  if outcome == "orphaned":
    return rows  # no terminal event -- exercises the orphan watchdog
  if outcome == "failed":
    add(
        "AGENT_COMPLETED", {"final": True}, k + 2,
        status="error",
        error_message="tool 'commit_outcome' failed: downstream timeout",
    )
  else:  # success or truncated
    add("AGENT_COMPLETED", {"final": True}, k + 2)
  return rows


def build_realistic_corpus(
    rng: random.Random, now: datetime, sessions: int
) -> tuple[list[dict], dict[str, int]]:
  """Corpus builder for ``decision-realistic`` (see spec #247).

  Fixed deterministic mix (70/10/10/10 at 100, scaled otherwise), multi-day
  spread over ``[now - 72h, now - _MAX_SESSION_SPAN]``, multiple
  agents/users/topics. Returns ``(rows, session_outcome_counts)``.
  """
  counts = _outcome_allocation(sessions)  # raises if sessions < 4
  outcomes: list[str] = []
  for name in ("success", "failed", "orphaned", "truncated"):
    outcomes.extend([name] * counts[name])
  rng.shuffle(outcomes)

  window_start = now - _REALISTIC_WINDOW
  window_end = now - _MAX_SESSION_SPAN
  slot = (window_end - window_start).total_seconds() / sessions
  starts = [
      window_start + timedelta(seconds=i * slot + rng.uniform(0, slot))
      for i in range(sessions)
  ]

  agents = _shuffled_cycle(rng, _REALISTIC_AGENTS, sessions)
  users = _shuffled_cycle(rng, _REALISTIC_USERS, sessions)
  topics = _shuffled_cycle(rng, _TOPICS, sessions)

  rows: list[dict] = []
  for i in range(sessions):
    rows.extend(
        _realistic_session(
            rng, starts[i], outcomes[i], agents[i], users[i], topics[i]
        )
    )
  return rows, counts
```

(d) Register the builder:

```python
_SCENARIO_BUILDERS = {
    Scenario.DECISION: _build_decision_corpus,
    Scenario.DECISION_REALISTIC: build_realistic_corpus,
}
```

(The existing `assert set(_SCENARIO_BUILDERS) == set(Scenario)` now covers both members.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_seed_events.py -q`
Expected: PASS (all realistic tests + the decision regression tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/bigquery_agent_analytics/seed_events.py tests/test_seed_events.py
git commit -m "feat(seed-events): add decision-realistic scenario (#247)"
```

---

### Task 4: Overridable per-scenario `--sessions` defaults

`decision` defaults to 5, `decision-realistic` to 100; explicit `--sessions N` overrides.

**Files:**
- Modify: `src/bigquery_agent_analytics/seed_events.py` (`run_seed_events`)
- Modify: `src/bigquery_agent_analytics/cli.py` (`--sessions` default → `None`)
- Test: `tests/test_seed_events.py`, `tests/test_cli_bqaa_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_seed_events.py`:

```python
def test_default_sessions_per_scenario() -> None:
  decision = run_seed_events(
      project_id="p", dataset_id="d", seed=1, dry_run=True,
      now=_FIXED_NOW, bq_client=_FakeBQClient(),
  )
  assert decision.sessions == 5

  realistic = run_seed_events(
      project_id="p", dataset_id="d", seed=1, dry_run=True,
      scenario="decision-realistic", now=_FIXED_NOW, bq_client=_FakeBQClient(),
  )
  assert realistic.sessions == 100
  assert realistic.session_outcome_counts == {
      "success": 70, "failed": 10, "orphaned": 10, "truncated": 10,
  }


def test_explicit_sessions_overrides_scenario_default() -> None:
  result = run_seed_events(
      project_id="p", dataset_id="d", sessions=40, seed=1, dry_run=True,
      scenario="decision-realistic", now=_FIXED_NOW, bq_client=_FakeBQClient(),
  )
  assert result.sessions == 40
  assert sum(result.session_outcome_counts.values()) == 40
```

Append to `tests/test_cli_bqaa_app.py`:

```python
def test_bqaa_seed_events_realistic_dry_run_reports_outcomes() -> None:
  """--scenario decision-realistic defaults to 100 sessions and reports the mix."""
  result = runner.invoke(
      bqaa_app,
      ["seed-events", "--project-id", "p", "--dataset-id", "d",
       "--scenario", "decision-realistic", "--seed", "42", "--dry-run"],
  )
  assert result.exit_code == 0, result.output
  payload = json.loads(result.output)
  assert payload["sessions"] == 100
  assert payload["session_outcome_counts"] == {
      "success": 70, "failed": 10, "orphaned": 10, "truncated": 10,
  }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_seed_events.py tests/test_cli_bqaa_app.py -q`
Expected: FAIL — `run_seed_events` requires/defaults `sessions=5` regardless of scenario; realistic default is not 100.

- [ ] **Step 3: Implement default resolution**

(a) In `src/bigquery_agent_analytics/seed_events.py`, add the default map after `_SCENARIO_BUILDERS`:

```python
_SCENARIO_DEFAULT_SESSIONS = {
    Scenario.DECISION: 5,
    Scenario.DECISION_REALISTIC: 100,
}
```

(b) Change `run_seed_events`'s `sessions` parameter to `Optional[int] = None` and resolve it per scenario AFTER coercing the scenario. Replace the signature default and the opening of the body:

```python
def run_seed_events(
    *,
    project_id: str,
    dataset_id: str,
    sessions: Optional[int] = None,
    seed: Optional[int] = None,
    scenario: Scenario | str = Scenario.DECISION,
    events_table: str = "agent_events",
    dry_run: bool = False,
    now: Optional[datetime] = None,
    bq_client: Optional[Any] = None,
) -> SeedEventsResult:
  """Generate synthetic events and (unless ``dry_run``) insert them.

  ``sessions`` defaults per scenario (decision=5, decision-realistic=100) when
  not given; an explicit value overrides. Invalid input (resolved
  ``sessions < 1``, unknown ``scenario``) raises; the CLI maps that to exit 2.
  BigQuery insert errors are modeled as ``ok=False`` with ``errors`` populated
  -- not raised -- so the JSON report stays authoritative (CLI exit 1).
  """
  scenario = Scenario(scenario) if isinstance(scenario, str) else scenario
  if sessions is None:
    sessions = _SCENARIO_DEFAULT_SESSIONS[scenario]
  if sessions < 1:
    raise ValueError("sessions must be >= 1")
  if now is None:
    now = datetime.now(timezone.utc)
```

(Delete the old `if sessions < 1` / `scenario = Scenario(...)` lines that previously opened the body — they are replaced by the block above. The rest of `run_seed_events`, from `rng = random.Random(seed)` onward, is unchanged.)

(c) In `src/bigquery_agent_analytics/cli.py`, change the `seed-events` command's `sessions` option default from `5` to `None` and update its help to document the per-scenario defaults:

```python
    sessions: Optional[int] = typer.Option(
        None,
        "--sessions",
        help=(
            "Number of synthetic sessions (>= 1). Default depends on"
            " --scenario: 5 for decision, 100 for decision-realistic."
        ),
    ),
```

(`Optional` is already imported in `cli.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_seed_events.py tests/test_cli_bqaa_app.py -q`
Expected: PASS, including the existing `test_dry_run_generates_without_touching_bigquery` (passes `sessions=2` explicitly) and `test_bqaa_seed_events_dry_run_reports_without_bigquery`.

- [ ] **Step 5: Commit**

```bash
git add src/bigquery_agent_analytics/seed_events.py src/bigquery_agent_analytics/cli.py tests/test_seed_events.py tests/test_cli_bqaa_app.py
git commit -m "feat(cli): per-scenario --sessions defaults (decision=5, realistic=100) (#247)"
```

---

### Task 5: Codelab "Realistic Data" section + notebook

**Files:**
- Modify: `docs/codelabs/periodic_materialization.md`
- Regenerate: `examples/codelab/periodic_materialization/colab_notebook.ipynb`

- [ ] **Step 1: Add the optional section**

In `docs/codelabs/periodic_materialization.md`, immediately AFTER the existing event-verification block (the section that ends with the `25 TOOL_COMPLETED rows and 5 AGENT_COMPLETED rows` paragraph, before `## Phase 3: Materialize the Decision Graph`), insert a new optional subsection. Use the same `<!-- colab:code bash -->` fence markers the file already uses so the notebook generator picks them up:

````markdown
### Optional: Realistic-scale data

The 5-session corpus above is intentionally tiny so the first run is fast. When you want production-shaped data — multiple agents and users spread over several days, with failed, orphaned, and truncated sessions — use the `decision-realistic` scenario. It defaults to 100 sessions over a 72-hour window; the first-run path above is unchanged.

<!-- colab:code bash -->
```bash
bqaa seed-events \
    --project-id "$PROJECT_ID" \
    --dataset-id "$DATASET" \
    --scenario decision-realistic \
    --sessions 100 \
    --seed 42
```

The JSON report's `session_outcome_counts` shows the mix — roughly `{"success": 70, "failed": 10, "orphaned": 10, "truncated": 10}`.

Confirm the outcome distribution by classifying each session from its rows (orphaned = no `AGENT_COMPLETED`; failed = `AGENT_COMPLETED` with `status = 'error'`; truncated = any row with `is_truncated = true`; otherwise success). A first pass classifies each session, then a second aggregates per outcome:

<!-- colab:code bash -->
```bash
bq query --use_legacy_sql=false \
    "WITH per_session AS (
       SELECT
         session_id,
         CASE
           WHEN COUNTIF(event_type = 'AGENT_COMPLETED') = 0 THEN 'orphaned'
           WHEN COUNTIF(event_type = 'AGENT_COMPLETED' AND status = 'error') > 0 THEN 'failed'
           WHEN COUNTIF(is_truncated) > 0 THEN 'truncated'
           ELSE 'success'
         END AS outcome
       FROM \`$PROJECT_ID.$DATASET.agent_events\`
       GROUP BY session_id
     )
     SELECT outcome, COUNT(*) AS sessions
     FROM per_session GROUP BY outcome ORDER BY outcome"
```

You should see roughly 70 success, 10 failed, 10 orphaned, and 10 truncated.

The 10 orphaned sessions never emitted `AGENT_COMPLETED`, so the default `bqaa context-graph` run skips them (it materializes only terminal-event-closed sessions). To surface them as `session_orphaned` instead of silently retrying forever, add `--max-session-age-hours` when you materialize — see the orphan-watchdog discussion later in this codelab.
````

- [ ] **Step 2: Regenerate the notebook**

Run:
```bash
python scripts/generate_colab_from_codelab.py \
    docs/codelabs/periodic_materialization.md \
    examples/codelab/periodic_materialization/colab_notebook.ipynb
```

- [ ] **Step 3: Verify the drift check passes**

Run:
```bash
python scripts/generate_colab_from_codelab.py --check \
    docs/codelabs/periodic_materialization.md \
    examples/codelab/periodic_materialization/colab_notebook.ipynb
```
Expected: exit 0.

- [ ] **Step 4: Sanity-check the markdown**

Run: `git diff --check` (expect no whitespace errors) and confirm the first-run seed step (`bqaa seed-events --sessions 5`) is unchanged: `grep -n "sessions 5" docs/codelabs/periodic_materialization.md` should still show the original first-run command.

- [ ] **Step 5: Commit**

```bash
git add docs/codelabs/periodic_materialization.md examples/codelab/periodic_materialization/colab_notebook.ipynb
git commit -m "docs(codelab): optional realistic-scale data section + outcome SQL (#247)"
```

---

### Task 6: Final verification + PR

**Files:** none (verification only)

- [ ] **Step 1: Format check**

Run: `bash autoformat.sh`
Expected: no changes to tracked files (`git status --short` shows no modified tracked files). If it reformats anything, `git add -A` and amend the most recent relevant commit.

- [ ] **Step 2: Full test suite**

Run: `python -m pytest -q`
Expected: all pass, including the new `decision-realistic` tests and unchanged `decision` regression tests.

- [ ] **Step 3: Manual smoke**

Run:
```bash
python -c "from typer.testing import CliRunner; from bigquery_agent_analytics.cli import bqaa_app; \
r = CliRunner().invoke(bqaa_app, ['seed-events','--project-id','p','--dataset-id','d','--scenario','decision-realistic','--dry-run','--seed','42']); \
print(r.exit_code); print(r.output)"
```
Expected: exit `0`; JSON with `"sessions": 100`, `"dry_run": true`, `"events_inserted": 0`, `"ok": true`, and `"session_outcome_counts": {"success": 70, "failed": 10, "orphaned": 10, "truncated": 10}`.

- [ ] **Step 4: Push and open PR**

```bash
git push -u myfork-fork feat/seed-events-realistic-scale
gh pr create --repo GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --base main --head caohy1988:feat/seed-events-realistic-scale \
  --title "feat(cli): add decision-realistic seed-events scenario (#247)" \
  --body "<summary referencing the spec + closes #247>"
```
(Do not add Co-Authored-By / generated-by signatures.)

---

## Self-Review

**Spec coverage:**
- Seam refactor to corpus builders → Task 1. ✓
- `decision` byte-identical → Task 1 (`_build_decision_corpus` reproduces the loop) + Task 2 (`_row` defaults) regression tests. ✓
- `session_outcome_counts` on result, behind `run_seed_events`; `generate_seed_events` returns `list[dict]` unchanged → Task 1. ✓
- `decision-realistic`: 100/72h default, 70/10/10/10 mix, exact bucket rule (round-half-up/min-1, success=remainder, `sessions>=4`) → Tasks 2 (`_outcome_allocation`) + 3. ✓
- Failed (status=error+message) / orphaned (no terminal) / truncated (is_truncated) / variable 2–6 options → Task 3 + tests. ✓
- Time distribution by partition+jitter in `[now-72h, now-_MAX_SESSION_SPAN]`, no future timestamps, multi-day span → Task 3 + `test_realistic_multi_day_span_and_no_future`. ✓
- ≥2 agents/users via shuffled cycle → Task 2 (`_shuffled_cycle`) + Task 3 + `test_realistic_multiple_agents_and_users`. ✓
- Determinism → Task 3 `test_realistic_is_deterministic`. ✓
- Overridable per-scenario `--sessions` defaults → Task 4. ✓
- Codelab optional section + concrete outcome SQL + orphan-watchdog note + notebook regen → Task 5. ✓

**Placeholder scan:** No TBD/TODO. Every code step shows complete code. Task 5 explicitly instructs removing the illustrative wrong-query block (the only place a "draft then correct" appears) so the committed markdown contains exactly one correct query. ✓

**Type consistency:** `build_realistic_corpus`, `_build_decision_corpus`, and the registry all use the corpus signature `(rng, now, sessions) -> tuple[list[dict], dict[str,int]]`. `_outcome_allocation`/`_shuffled_cycle` signatures match their call sites in Task 3. `SeedEventsResult.session_outcome_counts` (added Task 1) is produced by the builders (Tasks 1/3) and consumed by `run_seed_events` (Tasks 1/4) and tests. `_SCENARIO_DEFAULT_SESSIONS` keys match the `Scenario` members. `_row`'s new keyword-only `agent`/`user_id` default to the original literals, keeping `_decision_session` byte-identical. ✓
