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
# ResolvedLabelAndProperties
label: <string>
properties: [<ResolvedProperty>, ...]
```

```yaml
# ResolvedNodeTable
alias: <string>                   # identifier used after AS and in REFERENCES
source: <string>                  # fully qualified
key_columns: [<string>, ...]
label_and_properties: [<ResolvedLabelAndProperties>, ...]   # one per v0, more reserved for multi-label
```

```yaml
# ResolvedEdgeTable
alias: <string>
source: <string>
key_columns: [<string>, ...]      # row-level identity, always non-empty; see §3
from_key_columns: [<string>, ...]
to_key_columns: [<string>, ...]
from_node_table: <string>         # which node table this edge's source points to
to_node_table: <string>
label_and_properties: [<ResolvedLabelAndProperties>, ...]
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

### Derive table aliases

Every node and edge table gets an alias after `AS` in the emitted
DDL, and edge tables use node-table aliases in their `REFERENCES`
clauses. We use the ontology label verbatim as the alias (`Account`,
`HOLDS`, etc.) so the DDL reads by logical type rather than leaking
physical table basenames. The ontology loader already guarantees
entity and relationship names are unique within the ontology and
disjoint across kinds, so this aliasing is collision-free by
construction — no runtime uniqueness check is required.

### Resolve edge keys

Every edge in the resolved graph carries a non-empty `key_columns`.
BigQuery's property-graph model wants row-level identity on edges
even when the ontology doesn't spell one out, so the resolver picks
a value in all three cases:

- **`keys.primary` declared.** `key_columns` is the bound physical
  columns for those properties. Example: `TRANSFER` with primary
  `[transaction_id]` → `KEY (txn_id)`.
- **`keys.additional` declared.** `key_columns` is the endpoint
  columns followed by the bound additional-key columns — together
  they form a globally-unique row identifier. Example: `HOLDS` with
  additional `[as_of]` → `KEY (account_id, security_id, snapshot_date)`.
- **No keys declared.** `key_columns` is just the endpoint columns,
  expressing "one edge per endpoint pair" as a safe default. Authors
  who need multi-edges should declare `keys.additional` with a
  discriminator property.

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
    raw.accounts AS Account
      KEY (acct_id)
      LABEL Account PROPERTIES (acct_id AS account_id, created_ts AS opened_at),
    raw.persons AS Person
      KEY (person_id)
      LABEL Person PROPERTIES (
        person_id,
        display_name AS name,
        given_name AS first_name,
        family_name AS last_name,
        (given_name || ' ' || family_name) AS full_name
      ),
    ref.securities AS Security
      KEY (cusip)
      LABEL Security PROPERTIES (cusip AS security_id)
  )
  EDGE TABLES (
    raw.holdings AS HOLDS
      KEY (account_id, security_id)
      SOURCE KEY (account_id) REFERENCES Account (acct_id)
      DESTINATION KEY (security_id) REFERENCES Security (cusip)
      LABEL HOLDS PROPERTIES (snapshot_date AS as_of, qty AS quantity)
  );
```

Derived expressions become SQL expressions in the `PROPERTIES` list;
column renames become `AS` clauses.

Formatting rules applied to the output, all in service of determinism:

- `LABEL <name> PROPERTIES (...)` stays bundled as a single clause
  per the GCP grammar's `LabelAndProperties` production. Even though
  each element carries only one label today, keeping label and
  property list paired reserves the shape for future multi-label
  support without another emitter rewrite.
- The bundled clause stays on a single line when the rendered line
  (including its indent) fits within 80 columns; otherwise the
  property list breaks across lines, one property per line, with
  the closing paren on its own line. The `LABEL <name> PROPERTIES (`
  opener remains intact on the first line so the label-property
  association survives the wrap visually.
- Node tables and edge tables are sorted alphabetically by label;
  property lists within each table follow the ontology's declaration
  order.

### Spanner

Same `CREATE PROPERTY GRAPH` / `NODE TABLES` / `EDGE TABLES` form, minor
syntactic differences.

### Relationship to the GCP reference grammar

The resolved model maps to the `CREATE PROPERTY GRAPH` grammar as follows:

| Resolved model | GCP grammar |
|---|---|
| `ResolvedNodeTable.source` + `alias` | `<source> AS <alias>` |
| `ResolvedNodeTable.key_columns` | `KEY (<cols>)` |
| `ResolvedNodeTable.label_and_properties[i]` | `LABEL <name> PROPERTIES (<spec_list>)` (one per entry; `LabelAndPropertiesList` overall) |
| `ResolvedEdgeTable.source` + `alias` | `<source> AS <alias>` |
| `ResolvedEdgeTable.key_columns` | `KEY (<cols>)` |
| `ResolvedEdgeTable.from_key_columns` + `from_node_table` | `SOURCE KEY (<cols>) REFERENCES <node>` |
| `ResolvedEdgeTable.to_key_columns` + `to_node_table` | `DESTINATION KEY (<cols>) REFERENCES <node>` |
| `ResolvedEdgeTable.label_and_properties[i]` | `LABEL <name> PROPERTIES (<spec_list>)` (one per entry) |

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
5. **No abstract elements in bindings.** The binding loader already
   rejects bindings that target abstract entities or relationships
   (`ontology.md` §3a). The compiler guards against this as
   defense-in-depth so a hand-constructed `Binding` object that skips
   the loader still raises rather than emitting unresolvable DDL.

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
