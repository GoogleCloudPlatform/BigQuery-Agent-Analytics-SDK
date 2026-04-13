# Compilation — Core Design (v0)

Status: draft
Scope: how an ontology (`ontology.md`) plus a binding (`binding.md`) are
resolved and emitted as backend DDL (`CREATE PROPERTY GRAPH` on BigQuery or
Spanner).

**v0 compiles flat ontologies only.** Ontologies that use `extends` on
entities or relationships are rejected at compile time. Inheritance
lowering — substitutability, per-label property projections, cross-table
identity, overlapping siblings — is the subject of a separate future
design. Deployment, credentials, and measures are out of scope.

## 1. Goals

- **Single-shot compile.** Ontology plus binding in, DDL text out. No
  intermediate on-disk artifact.
- **Deterministic output.** Same inputs → byte-identical DDL.
- **Backend-neutral pipeline, backend-specific emitter.** Resolution is
  shared across backends; emission is per-backend.
- **Output is just text.** What consumes it (deploy tool, `bq query`,
  Terraform, a human) is outside this spec.

## 2. Pipeline

```
ontology.yaml   ──┐
                  ├──► Resolver ──► ResolvedGraph ──► Emitter ──► DDL
binding.yaml    ──┘                                    (BQ|Spanner)
```

Stages:

1. **Load.** Parse and validate ontology and binding independently against
   their specs.
2. **Resolve.** Cross-check names, wire derived expressions to bound
   columns. Produce an in-memory `ResolvedGraph`.
3. **Emit.** Walk the `ResolvedGraph` and produce backend-specific DDL.

## 2a. Type overview (resolved model)

```yaml
# ResolvedGraph
name: <string>                    # graph name, from ontology
target: <Target>                  # from binding
node_tables: [<NodeTable>, ...]
edge_tables: [<EdgeTable>, ...]
```

```yaml
# NodeTable
label: <string>                   # entity name
key_columns: [<string>, ...]
source: <string>                  # fully qualified
properties: [<ResolvedProperty>, ...]
```

```yaml
# EdgeTable
label: <string>                   # relationship name
source: <string>
from_key_columns: [<string>, ...]
to_key_columns: [<string>, ...]
from_node_table: <string>         # which node table this edge's source points to
to_node_table: <string>
properties: [<ResolvedProperty>, ...]
```

```yaml
# ResolvedProperty
name: <string>                    # logical property name
type: <string>                    # GoogleSQL type
sql: <string>                     # column name, or substituted expression for derived
```

## 3. Resolution

### Substitute derived expressions

For each derived property, substitute each name referenced in `expr:` with
the column name from the binding. References to other derived properties
are resolved recursively; cycles are a compile-time error.

### Resolve endpoints

For each relationship, look up the single node table for each endpoint
entity. Because v0 does not lower inheritance, each endpoint entity is
bound to exactly one node table, so endpoint resolution is direct.

## 4. Emission

Both backends produce `CREATE PROPERTY GRAPH` statements. Node tables and
edge tables are listed in deterministic alphabetical order. Property lists
follow the ontology declaration order of the owning entity / relationship.

### BigQuery

#### Worked example

Ontology fragment:

```yaml
entities:
  - name: Person
    keys: { primary: [person_id] }
    properties:
      - { name: person_id,  type: string }
      - { name: name,       type: string }
      - { name: first_name, type: string }
      - { name: last_name,  type: string }
      - { name: full_name,  type: string,
          expr: "first_name || ' ' || last_name" }
  - name: Account
    keys: { primary: [account_id] }
    properties:
      - { name: account_id, type: string }
      - { name: opened_at,  type: timestamp }
```

Binding fragment:

```yaml
entities:
  - name: Person
    source: raw.persons
    properties:
      - { name: person_id,  column: person_id }
      - { name: name,       column: display_name }
      - { name: first_name, column: given_name }
      - { name: last_name,  column: family_name }
  - name: Account
    source: raw.accounts
    properties:
      - { name: account_id, column: acct_id }
      - { name: opened_at,  column: created_ts }
```

Emitted DDL:

```sql
CREATE PROPERTY GRAPH finance
  NODE TABLES (
    raw.accounts AS accounts
      KEY (acct_id)
      LABEL Account PROPERTIES (acct_id AS account_id, created_ts AS opened_at),
    raw.persons AS persons
      KEY (person_id)
      LABEL Person PROPERTIES (
        person_id,
        display_name AS name,
        given_name AS first_name,
        family_name AS last_name,
        (given_name || ' ' || family_name) AS full_name
      )
  )
  EDGE TABLES (
    raw.holdings AS holdings
      SOURCE KEY (account_id) REFERENCES accounts (acct_id)
      DESTINATION KEY (security_id) REFERENCES securities (cusip)
      LABEL HOLDS PROPERTIES (snapshot_date AS as_of, qty AS quantity)
  );
```

Derived expressions become SQL expressions in the `PROPERTIES` list;
column renames become `AS` clauses.

### Spanner

Same `CREATE PROPERTY GRAPH` / `NODE TABLES` / `EDGE TABLES` form, minor
syntactic differences.

### Relationship to the GCP reference grammar

The resolved model maps to the `CREATE PROPERTY GRAPH` grammar as follows:

| Resolved model | GCP grammar |
|---|---|
| `NodeTable.source` | `<source> [AS <alias>]` |
| `NodeTable.key_columns` | `KEY (<cols>)` |
| `NodeTable.label` + `properties` | `LABEL <name> PROPERTIES (<spec_list>)` |
| `EdgeTable.from_key_columns` + `from_node_table` | `SOURCE KEY (<cols>) REFERENCES <node>` |
| `EdgeTable.to_key_columns` + `to_node_table` | `DESTINATION KEY (<cols>) REFERENCES <node>` |

The resolved model collapses the grammar's variant forms to a single
canonical shape. We always emit the explicit
`LABEL <name> PROPERTIES (<list>)` form and do not emit:

- `DEFAULT LABEL` — our properties are always enumerated.
- `PROPERTIES ARE ALL COLUMNS` — same reason.
- `LABEL <name> NO PROPERTIES` — every label projects at least one
  property.
- `DYNAMIC LABEL` / `DYNAMIC PROPERTIES` — our ontology is closed-world
  with declared labels and properties. See §7.

References:
[Spanner graph schema statements](https://cloud.google.com/spanner/docs/reference/standard-sql/graph-schema-statements),
[BigQuery graph creation](https://cloud.google.com/bigquery/docs/graph-create).

## 5. Derived expressions in DDL

Derived properties appear as `<substituted_expr> AS <name>` in the
`PROPERTIES` list. No intermediate view is created. See the `full_name`
example in §4.

## 6. Compile-time validation

On top of ontology-level (`ontology.md` §10) and binding-level
(`binding.md` §9) rules:

1. **No `extends`.** No entity or relationship in the ontology uses
   `extends`. Compilation of hierarchical ontologies is reserved for a
   future design.
2. Every name in a derived expression resolves to a bound or derived
   property on the same entity or relationship.
3. No cycles among derived properties.
4. Every logical property type is supported by the target backend
   (`ontology.md` §7).

Warnings: bound entity referenced by no relationship.

## 7. Determinism and output shape

One `CREATE PROPERTY GRAPH` per compile. Node tables sorted alphabetically,
then edge tables sorted alphabetically. Property lists follow ontology
declaration order.

## 8. Open questions

- **Multi-graph output.** One `CREATE PROPERTY GRAPH` per compile.
  Multi-graph from one ontology is a composition concern.
- **`DYNAMIC LABEL`.** Spanner and BigQuery support a string column as a
  runtime-assigned label (one node table and one edge table per schema).
  We don't emit it today — closed-world ontology with declared labels is
  enough. Revisit if an importer or user surfaces a real need.

## 9. Out of scope

- **Inheritance lowering.** Compilation of ontologies with `extends` —
  substitutability, per-label property projections, fanout vs union-view
  vs label-ref strategies, cross-table identity, overlapping siblings,
  merged-node lowering. Separate future design.
- **CLI surface.** Command names, flag names, output destinations — a
  separate doc.
- **Applying DDL to a live backend.** Credentials, transactions, rollback,
  drift detection. Any tool that can accept DDL text can consume this
  compiler's output.
- **Measures and aggregations.** Not part of the property graph DDL.
- **Composition.** Multi-file ontology assembly, shared binding defaults,
  overlay graphs.
- **Schema evolution and migration.** Diffing two compiled outputs and
  emitting `ALTER` statements — separate concern.
