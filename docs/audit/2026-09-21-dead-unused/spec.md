# Spec

## In scope

A symbol is deleted only if all of these hold:

1. `vulture src` flags it as unused.
2. A repo-wide `grep -rnI <symbol> .` (src, tests, examples, scripts,
   docs, plugins) finds no reference other than its own definition /
   import line.
3. It is underscore-private, or a plain `import` that is never
   re-exported (no test or module reads it as `<module>.<name>`).
4. The full hermetic suite passes after removal.

| File | Removed | Evidence |
|------|---------|----------|
| `ontology_schema_compiler.py` | `_compile_entity_schema`, `_compile_relationship_schema` | only hit is the `def` line; `compile_output_schema` builds the schema inline |
| `ontology_schema_compiler.py` | imports `ResolvedEntity`, `ResolvedRelationship` | only used as annotations on the two helpers above |
| `ontology_runtime.py` | `_first_string_value` | only hit is the `def` line; callers use `_all_string_values` |
| `_streaming_evaluation.py`, `system_evaluator.py` | `import udf_kernels` | no `udf_kernels.` use in either module (one comment mention in `system_evaluator.py`); no test patches `<module>.udf_kernels` |
| `client.py` | `import CATEGORICAL_AI_GENERATE_QUERY` | never read in `client.py`; tests import it from `categorical_evaluator` |
| `insights.py` | `from dataclasses import field as dc_field` | no `dc_field` use |
| `trace.py` | `import functools` | only a docstring mentions `functools.wraps` |

## Deliberately kept (vulture false positives)

- `performance_evaluator.py` import of `_extract_json_from_text` — a
  re-export asserted by `tests/test_pr123_performance_regressions.py`.
- `_bqaa_callback`, `_export_callback`, `_root`,
  `_materialize_window_entry` — Typer callbacks / console-script entry
  points.
- `evaluators.__getattr__` — module-level lazy attribute hook.
- `RelationshipBinding._validate_column_entries` — pydantic validator.
- `PerformanceEvaluator._llm_judge_evaluate` — private method with no
  caller, but it is evaluator behavior rather than a leaf helper; left
  for a separate, reviewed change.
- unused parameters `app_name` (`memory_service.search_memory`) and
  `memo` (`__deepcopy__`) — required by the interface signatures.
