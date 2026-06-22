# classify_sessions_via_api and _infer_corrections should run concurrently

**Labels:** `enhancement`, `performance`

## Problem

`classify_sessions_via_api` in `categorical_evaluator.py:831` processes sessions sequentially:

```python
for sid, transcript in transcripts.items():
    response = await client.aio.models.generate_content(...)
```

Additionally, `_infer_corrections` in `quality_report.py` is called per-session in a loop inside `_build_resolved_map_from_conversations` and `run_evaluation` (lines 908-920).

For 205 multi-turn sessions this results in **410 sequential Gemini API calls** (~7-8s per call = ~25 minutes total). Each call is independent — there's no reason they can't run concurrently.

## Benchmarks

| Sessions | Sequential (current) | Expected with concurrency=10 |
|----------|---------------------|-------------------------------|
| 5 | 38.8s | ~4s |
| 205 | ~25min | ~2.5min |

## Proposed fix

### 1. `classify_sessions_via_api` — add semaphore-bounded concurrency

```python
async def classify_sessions_via_api(transcripts, config, endpoint, concurrency=10):
    semaphore = asyncio.Semaphore(concurrency)

    async def _classify_one(sid, transcript):
        async with semaphore:
            # existing per-session logic (lines 860-895)
            ...

    tasks = [_classify_one(sid, t) for sid, t in transcripts.items()]
    results = await asyncio.gather(*tasks)
    return list(results)
```

### 2. `_infer_corrections` — batch with gather

In `_build_resolved_map_from_conversations` and `run_evaluation`, collect all multi-turn sessions and infer corrections concurrently:

```python
async def _infer_corrections_batch(sessions, model, concurrency=10):
    semaphore = asyncio.Semaphore(concurrency)

    async def _infer_one(conv):
        async with semaphore:
            return _infer_corrections(conv, model)

    return await asyncio.gather(*[_infer_one(s) for s in sessions])
```

### 3. Wire `--concurrency` flag

The `score_conversations.py` CLI already has a `--concurrency` flag (currently ignored). Pass it through to both functions.

## Files to change

- `src/bigquery_agent_analytics/categorical_evaluator.py` — `classify_sessions_via_api`
- `scripts/quality_report.py` — `_infer_corrections` batching, `_build_resolved_map_from_conversations`, `run_evaluation`

## Notes

- Default concurrency of 10 should be safe for Gemini API rate limits
- The `client.aio.models.generate_content` API is already async — just needs gather
- Backwards compatible — sequential behavior preserved with `concurrency=1`
