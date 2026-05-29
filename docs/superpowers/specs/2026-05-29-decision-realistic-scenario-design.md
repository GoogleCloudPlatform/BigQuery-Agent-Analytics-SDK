# Design: `decision-realistic` scenario — realistic-scale demo data (#247)

**Status:** Approved (brainstorming) — ready for implementation plan
**Issue:** #247 (unblocked by #246 / PR #261)
**Builds on:** `src/bigquery_agent_analytics/seed_events.py` (the `bqaa seed-events` SDK module + CLI)
**Follow-up this unblocks:** #255 (CA-first user guide with screenshots)

## Problem

The `decision` scenario shipped in #246 is intentionally tiny and uniform: a single agent/user, all-success sessions, spaced 30s apart from a `now − 10min` base (100 sessions would still span < 1 hour). That is perfect for a fast, predictable first-run codelab, but it does not look like production telemetry. #247 adds a second, realistic-scale scenario — `decision-realistic` — with multi-day spread, multiple agents/users/business entities, and intentional failure/edge-case sessions, while leaving the small `decision` path (and the existing codelab first-run) completely untouched.

## Goals

- A new `decision-realistic` scenario selectable via `--scenario decision-realistic`, using the `_SCENARIO_BUILDERS` seam from #246.
- 100 sessions (default) spread over a 72h window, with a fixed, deterministic **mix/shape**: 70% success, 10% failed, 10% orphaned, 10% truncated.
- Multiple agents/users/business entities; variable decision length (2–6 options) for non-orphan flows.
- Per-outcome reporting via `session_outcome_counts` so the codelab can explain the corpus without inspecting raw rows.
- Determinism preserved: fixed `(seed, now)` → byte-identical rows for `decision-realistic` too.
- An optional codelab "Realistic Data" section that does not change the first-run path.

## Non-goals (YAGNI)

- No new scale flags. Sizing reuses the existing `--sessions` (see §4). Shape/mix is baked in.
- No third scenario, no per-bucket override flags, no configurable time-window flag.
- `decision` behavior and output are unchanged (byte-identical).

## Design

### 1. Scenario seam refactor

The #246 seam maps each `Scenario` to a **per-session** builder `(rng, now) -> rows`, and `generate_seed_events` owns the session loop + fixed 30s spacing. That cannot express multi-day spread or per-session outcome variety, so the seam is lifted to a **corpus builder** that owns count handling, time distribution, and per-session shape, and also returns outcome counts:

```python
# builder(rng, now, sessions) -> (rows, session_outcome_counts)
_SCENARIO_BUILDERS = {
    Scenario.DECISION:           _build_decision_corpus,
    Scenario.DECISION_REALISTIC: _build_decision_realistic_corpus,
}
```

- `_build_decision_corpus(rng, now, sessions)` reproduces the **exact** current rng call order and timestamps (it wraps the untouched `_decision_session(rng, cur)` in the same `cur = now − 10min; +30s` loop) and returns `(rows, {"success": sessions})`. `decision` output stays byte-identical → all existing `decision` tests pass unchanged.
- `generate_seed_events(*, sessions, seed, now, scenario) -> list[dict]` is **unchanged in signature and return type**. Internally it creates `rng = random.Random(seed)`, calls the scenario's corpus builder, and returns **only** the rows (discarding the counts). Existing tests that treat its result as a list keep working.
- `run_seed_events` creates its own `rng` and calls the corpus builder directly so it receives **both** rows and `session_outcome_counts`. (One build per call; `generate_seed_events` and `run_seed_events` do not both build for a single invocation.)

`Scenario` gains `DECISION_REALISTIC = "decision-realistic"`. The existing module-load assert `set(_SCENARIO_BUILDERS) == set(Scenario)` covers the new member.

### 2. `decision-realistic` corpus builder

`_build_decision_realistic_corpus(rng, now, sessions)`. One `random.Random(seed)` drives every choice (no `uuid`/global random), so fixed `(seed, now)` → byte-identical rows.

**Sizing + outcome buckets (deterministic).** Given `sessions = N` (default 100, see §4):

```python
edge = max(1, math.floor(0.10 * N + 0.5))   # round half up, min 1
failed = orphaned = truncated = edge
success = N - failed - orphaned - truncated
if success < 1:
    raise ValueError("decision-realistic requires sessions >= 4")
```

- N=100 → 70 success / 10 failed / 10 orphaned / 10 truncated (exact).
- N=10 → 7/1/1/1; N=4 → 1/1/1/1; N<4 → `ValueError` (invalid input → CLI exit 2).

Outcomes are assigned to the N session slots and **rng-shuffled** so failures are interleaved (not clustered), keeping exact bucket counts.

**Time distribution.** Session start times are **distributed across** the window `[now − 72h, now − _MAX_SESSION_SPAN]` by even partitioning plus bounded `rng` jitter within each slot — not pure random draws — so the corpus span always covers the full multi-day range (the multi-day-span test is then exact, not probabilistic), with the earliest session pinned near `now − 72h` and the latest near `now − _MAX_SESSION_SPAN`. The window's upper bound is held back by `_MAX_SESSION_SPAN` (a constant comfortably larger than any single session's per-step offsets, e.g. 60s) so that a session's tool/terminal rows (start + a few seconds) **never land after `now`** — no future timestamps. Within a session, steps are offset by seconds from its start (as in `decision`). Older sessions deliberately fall outside a 24h `--lookback-hours`, which the codelab uses to demonstrate windowing.

**Per-outcome session shape** (built on the existing submit → evaluate×k → commit → terminal flow):
- **success** — `AGENT_COMPLETED`, `status="ok"`. `k` = option count drawn uniformly from 2..6.
- **failed** — terminates with `AGENT_COMPLETED`, `status="error"`, `error_message` populated (e.g. tool failure / policy block). `k` ∈ 2..6.
- **orphaned** — **no terminal event** (session ends after `commit_outcome`, or mid-flow). Exercises the materializer's orphan watchdog (`--max-session-age-hours` / `session_orphaned`); these do not materialize under the default `AGENT_COMPLETED` filter — the intended demonstration. `k` ∈ 2..6.
- **truncated** — `is_truncated=true` and clipped/partial `content` on at least one row; otherwise a normal completed session. `k` ∈ 2..6.

**Entities** (replacing the single `demo-agent`/`demo-user`):
- ~4 named agents (e.g. `loan-advisor`, `ops-scheduler`, `access-broker`, `budget-allocator`), each aligned to a business topic family.
- ~12 named users (e.g. `user-000`..`user-011`).
- Business topics/entities cycled deterministically from an extended topic list.

Agents and users are assigned via a **shuffled cycle**, not independent `rng.choice` (which could pick the same value every time for small N): build a list by repeating the agent (resp. user) roster to length N, `rng.shuffle` it, then index by session slot. This guarantees near-even coverage and **≥2 distinct agents and ≥2 distinct users for any N ≥ 2** (deterministic given the seed), making the coverage test a true guarantee rather than a probabilistic one. Topic is assigned the same way.

The row schema is unchanged (`_EVENT_SCHEMA_FIELDS`); only field *values* vary.

### 3. Result reporting

`SeedEventsResult` gains `session_outcome_counts: dict[str, int]`, surfaced in `to_json()`:
- `decision` → `{"success": N}`.
- `decision-realistic` → `{"success": 70, "failed": 10, "orphaned": 10, "truncated": 10}` (at N=100).

`generate_seed_events`'s public `list[dict]` return is unchanged; outcome counts live behind `run_seed_events` only. Adding a key to `to_json()` is additive — the existing round-trip test still passes.

### 4. CLI / `--sessions` semantics (overridable default)

"Fixed baked-in" means fixed **mix/shape**, not fixed count.
- `--sessions` default becomes scenario-specific: **`decision` → 5, `decision-realistic` → 100**. Explicit `--sessions N` overrides either.
- Mechanically: the CLI `--sessions` Typer option default becomes `None`; `run_seed_events` resolves `None` to the scenario default via `_SCENARIO_DEFAULT_SESSIONS = {DECISION: 5, DECISION_REALISTIC: 100}`. `decision` with no flag still yields 5 (identical observable behavior; help documents the per-scenario defaults).
- `--scenario decision-realistic` accepts the new value (string coerced to the enum; unknown values still raise → exit 2).

No other CLI flags change.

### 5. Codelab integration

First-run path unchanged (`bqaa seed-events --sessions 5`). Add an optional **"Realistic Data"** section to `docs/codelabs/periodic_materialization.md`:

```bash
bqaa seed-events --project-id "$PROJECT_ID" --dataset-id "$DATASET" \
    --scenario decision-realistic --sessions 100 --seed 42
```

with two checks: (a) an outcome-distribution query, and (b) orphan-watchdog behavior (`bqaa context-graph` skips the 10 orphaned sessions under the default filter; `--max-session-age-hours` flags them as `session_orphaned`). Regenerate `colab_notebook.ipynb` via `scripts/generate_colab_from_codelab.py`; the CI drift `--check` must pass.

The outcome-distribution check must be **concrete SQL**, not just an event-type tally — event-type counts don't reveal the outcome mix. The implementation plan provides the exact query that classifies each `session_id` by joining/aggregating over its rows: a session is *orphaned* if it has no `AGENT_COMPLETED` row, *failed* if its `AGENT_COMPLETED` has `status = 'error'`, *truncated* if any of its rows has `is_truncated = true`, else *success* — and counts sessions per class, so the reader sees roughly 70/10/10/10 at the default 100. (Classification precedence resolves any overlap; the generator assigns one outcome per session, so overlaps should not occur.)

### 6. Tests (`tests/test_seed_events.py`, `tests/test_cli_bqaa_app.py`)

`decision` regression (byte-identical output + existing assertions) stays green unchanged. New `decision-realistic` tests:
- **Outcome mix:** exact `{"success":70,"failed":10,"orphaned":10,"truncated":10}` at default 100; proportional scaling at other N (e.g. N=10 → 7/1/1/1); `sessions < 4` raises `ValueError`.
- **Terminal-event invariant:** every non-orphan session has exactly one `AGENT_COMPLETED`; every orphaned session has zero.
- **Failure markers:** failed sessions carry `status="error"` + non-empty `error_message`; truncated sessions carry `is_truncated=true`.
- **Variable length:** non-orphan option counts ∈ [2, 6].
- **Multi-day spread:** `max(timestamp) − min(timestamp)` is > 24h and ≤ 72h.
- **Entities:** ≥ 2 distinct `agent` values and ≥ 2 distinct `user_id` values.
- **Determinism:** fixed `(seed, now)` → byte-identical rows; different seed → different.
- **Reporting:** `session_outcome_counts` matches the actual rows; `run_seed_events(dry_run=True, scenario="decision-realistic")` returns the counts with `events_inserted=0`.
- **CLI:** `--scenario decision-realistic --dry-run` exits 0 and the JSON payload includes `session_outcome_counts`; default (no `--sessions`) yields 100 sessions for realistic and 5 for `decision`.

## Required Verification (acceptance checks — not yet performed)

- Full test suite green across Python 3.10–3.14.
- `bash autoformat.sh` reports no changes (pyink + isort).
- Codelab notebook `--check` passes (markdown ↔ notebook in sync).
- Manual smoke: `bqaa seed-events --scenario decision-realistic --dry-run --seed 42` reports `sessions: 100`, `session_outcome_counts: {success:70, failed:10, orphaned:10, truncated:10}`, multi-day timestamps; `decision` dry-run output is byte-identical to pre-#247.
