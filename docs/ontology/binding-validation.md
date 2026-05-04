# Binding Validation — Pre-flight Reference

`bq-agent-sdk binding-validate` checks whether the BigQuery tables a binding YAML points at physically exist with the columns and types the binding requires, **before** the SDK starts extraction. Catches the most common authoring error (binding YAML drifted out of sync with physical tables) before extraction wastes `AI.GENERATE` tokens.

## When to run it

- **Before any `ontology-build`** that points at user-pre-defined tables (Terraform / dbt / hand-authored DDL). Use either:
  - `bq-agent-sdk binding-validate ...` standalone, or
  - `bq-agent-sdk ontology-build --validate-binding ...` to gate the build.
- **In CI** as a pre-flight check that runs on every binding YAML change. Strict mode (`--strict`) is appropriate here — it forces every primary-key column to be REQUIRED.
- **Locally during binding authoring** to catch typos and column-name drift before the first build.

## Standalone CLI

```bash
bq-agent-sdk binding-validate \
  --project-id my-project \
  --ontology my.ontology.yaml \
  --binding my-bq-prod.binding.yaml \
  --location US
```

Output is a JSON report on stdout, plus advisory warnings printed to stderr. Exit codes:

| Exit | Meaning |
|---|---|
| `0` | `report.ok` is True. No failures. Warnings (if any) are advisory. |
| `1` | `report.ok` is False. At least one failure. |
| `2` | Unexpected error: missing flags, ontology/binding load failure, etc. |

### Strict mode

```bash
bq-agent-sdk binding-validate \
  --project-id my-project \
  --ontology my.ontology.yaml \
  --binding my-bq-prod.binding.yaml \
  --strict
```

Strict mode escalates `KEY_COLUMN_NULLABLE` warnings into hard failures. Use this in CI when you want every primary-key and endpoint-key column to be REQUIRED. Default mode does **not** require `NOT NULL` on key columns because the SDK's own `OntologyMaterializer.create_tables()` emits NULLABLE keys (`ontology_materializer.py:206`) — a default-mode hard failure here would reject SDK-created tables.

## `ontology-build` integration

Two opt-in flags gate the build on a passing pre-flight:

```bash
bq-agent-sdk ontology-build \
  --project-id my-project \
  --dataset-id my-dataset \
  --ontology my.ontology.yaml \
  --binding my-bq-prod.binding.yaml \
  --session-ids sess-1,sess-2 \
  --validate-binding         # default mode: warnings advisory; failures short-circuit
```

Or in strict mode:

```bash
bq-agent-sdk ontology-build \
  ... \
  --validate-binding-strict  # NULLABLE keys escalate to failures
```

When the validator reports any failure, **the build short-circuits before any `AI.GENERATE` call fires** — no extraction tokens are spent. Default-mode warnings print to stderr but do not block.

The two flags are mutually exclusive. Both are incompatible with the deprecated `--spec-path` form because the validator needs the unresolved `Ontology` + `Binding` pair, not a combined `GraphSpec`.

## Failure-code reference

The validator returns a `BindingValidationReport` with two collections: `failures` (always blocking) and `warnings` (advisory in default mode, escalated under `--strict`). Each entry carries `code`, `binding_element`, `binding_path`, `bq_ref`, `expected`, `observed`, and `detail`.

The CLI emits failure codes as **lowercase strings** in the JSON report (e.g., `"missing_table"`, not `"MISSING_TABLE"`). Use the lowercase form when filtering with `jq` or other JSON tools. The Python `FailureCode` enum exposes both forms — `FailureCode.MISSING_TABLE.value == "missing_table"`.

### Default-mode failure codes (always blocking)

| Python `FailureCode` | JSON `code` value | What it means | Typical fix |
|---|---|---|---|
| `MISSING_TABLE` | `"missing_table"` | The bound `source` table doesn't exist in BigQuery. | Create the table, or fix the binding's `source`. |
| `MISSING_COLUMN` | `"missing_column"` | A bound column (property, key, or SDK metadata column like `session_id` / `extracted_at`) doesn't exist on the table. | Add the column, or fix the binding's `column` mapping. |
| `TYPE_MISMATCH` | `"type_mismatch"` | A bound column exists but its BigQuery type doesn't match the ontology-derived expected type. | Change the BQ column's type, or change the ontology property's type. |
| `ENDPOINT_TYPE_MISMATCH` | `"endpoint_type_mismatch"` | An edge's `from_columns` or `to_columns` entry disagrees with the referenced node's primary-key column type. Two flavors: spec-level (edge vs ontology) and physical (edge vs node's actual storage). | Align the edge endpoint's type with the node's primary-key type. |
| `UNEXPECTED_REPEATED_MODE` | `"unexpected_repeated_mode"` | A scalar property/key column is in REPEATED (ARRAY) mode in BigQuery. | Restructure the table — the SDK can't bind scalar properties to ARRAY columns. |
| `MISSING_DATASET` | `"missing_dataset"` | The bound table's dataset doesn't exist. | Create the dataset, or fix the binding's `target.dataset` / fully-qualified `source`. |
| `INSUFFICIENT_PERMISSIONS` | `"insufficient_permissions"` | The calling identity can't read the table. | Grant `bigquery.tables.get` on the dataset, or run as an identity that already has it. |

### Strict-only code (warning by default, failure under `--strict`)

| Python `FailureCode` | JSON `code` value | What it means | Why it's strict-only |
|---|---|---|---|
| `KEY_COLUMN_NULLABLE` | `"key_column_nullable"` | A primary-key or endpoint-key column is in NULLABLE mode. | The SDK's own `CREATE TABLE IF NOT EXISTS` DDL emits NULLABLE key columns. A default-mode hard failure here would reject SDK-created tables. Use `--strict` in CI to enforce REQUIRED keys when you control the DDL. |

Example `jq` filter for failed builds:

```bash
bq-agent-sdk binding-validate ... --format=json \
  | jq '.failures[] | select(.code == "missing_column") | .bq_ref'
```

## CI usage pattern

```yaml
# .github/workflows/binding-validation.yml
- name: Pre-flight binding validation
  run: |
    bq-agent-sdk binding-validate \
      --project-id ${{ secrets.GCP_PROJECT }} \
      --ontology my.ontology.yaml \
      --binding my-bq-prod.binding.yaml \
      --location US \
      --strict
```

A failed validation exits 1 and the CI step fails. Combine with the matching `ontology-build --validate-binding-strict` in your deploy step so the same gate runs at build time.

## Python API

The CLI is a thin wrapper around `validate_binding_against_bigquery`. For programmatic use:

```python
from google.cloud import bigquery
from bigquery_ontology import load_ontology, load_binding
from bigquery_agent_analytics.binding_validation import (
    validate_binding_against_bigquery,
    FailureCode,
)

ontology = load_ontology("my.ontology.yaml")
binding = load_binding("my-bq-prod.binding.yaml", ontology=ontology)
client = bigquery.Client(project="my-project", location="US")

report = validate_binding_against_bigquery(
    ontology=ontology,
    binding=binding,
    bq_client=client,
    strict=False,
)

if not report.ok:
    for f in report.failures:
        print(f"{f.code.value} at {f.binding_path} ({f.bq_ref}): {f.detail}")
    raise SystemExit(1)

for w in report.warnings:
    print(f"WARN: {w.code.value} at {w.binding_path}")
```

`report.ok` returns `True` iff `report.failures` is empty — warnings do not flip it. Under `strict=True`, `KEY_COLUMN_NULLABLE` warnings become failures with the same code and `report.warnings` is empty (escalated, not duplicated).

## Related

- `docs/ontology/ontology-build.md` — the orchestrator the validator gates.
- `docs/ontology/binding.md` — the binding model the validator validates.
- Issue [#76](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/76) — the planned post-extraction `validate_extracted_graph` validator (different phase, different inputs).
