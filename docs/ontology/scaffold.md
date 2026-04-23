# Scaffold — Core Design (v0)

Status: draft
Scope: `gm scaffold` — a one-shot bootstrapper that emits a starter DDL
file and a matching binding stub from an ontology. Intended for greenfield
projects with no existing BigQuery tables.

## 1. What scaffold is, and what it is not

Scaffold is a **generator**, not a compiler. It runs once at the start of
a project. Its outputs are then owned by the user, committed to version
control, and hand-edited freely. Scaffold is never re-run against an
existing output directory, and nothing downstream assumes its outputs are
still in sync with the ontology after the user has edited them.

In particular:

- Scaffold does **not** participate in the compile pipeline. `gm compile`
  treats the emitted binding as any other user-authored binding.
- Scaffold does **not** execute DDL against BigQuery. It emits `.sql`
  text; the user runs it with `bq query`, dbt, Terraform, or whatever
  they already use.
- Scaffold does **not** perform schema evolution. Ontology changes after
  scaffold are handled by editing the binding and writing migrations —
  not by regenerating.
- Scaffold does **not** introspect BigQuery. It has no notion of "some
  tables already exist."

The binding schema stays pure-logical. Physical concerns (partitioning,
clustering, labels, table options) live in the DDL file only, where the
user edits them directly.

## 2. Command shape

```
gm scaffold \
  --ontology path/to/ontology.yaml \
  --dataset my_dataset \
  --out ./graph/
```

Required: `--ontology`, `--dataset`.
Optional: `--naming {snake|preserve}` (default `snake`),
`--project <id>` (omit to emit dataset-qualified names without a project
prefix).

Output directory layout:

```
graph/
  table_ddl.sql    # CREATE TABLE statements, one per entity and relationship
  binding.yaml     # matching binding, consistent with table_ddl.sql by construction
```

The filename `table_ddl.sql` is chosen to contrast with `graph_ddl.sql`,
the conventional output of `gm compile` (which emits the
`CREATE PROPERTY GRAPH` statement). A typical greenfield flow is:

```
gm scaffold --ontology ontology.yaml --dataset my_dataset --out ./graph/
bq query < ./graph/table_ddl.sql
gm compile ./graph/binding.yaml -o ./graph/graph_ddl.sql
bq query < ./graph/graph_ddl.sql
```

Each step produces a file whose name states what kind of DDL it is.

Scaffold refuses to write into a non-empty `--out` directory. The user
deletes or moves the previous output if they want to regenerate.

**Abstract elements** (see `ontology.md` §3a) are skipped entirely.
Abstract entities produce no `CREATE TABLE` statement and no
`entities:` entry in the generated binding; abstract relationships
produce no edge table and no `relationships:` entry. They exist in
the ontology for documentation or taxonomy purposes and have no
physical representation to scaffold.

## 3. Node (entity) table convention

Scaffold derives every structural element from the ontology. No column
names, no key choices, and no types are invented.

- **Columns** are the entity's declared properties, in ontology
  declaration order. Column name is the property name, transformed by
  the active naming rule (see §6).
- **Column types** come from the property's ontology type, mapped via
  §5.
- **Primary key** is the entity's `keys.primary` list. A single-property
  primary key yields one `NOT NULL` column; a compound primary key
  yields several. Primary-key columns are always `NOT NULL`. Non-key
  columns are nullable.
- **Derived properties** — those with `expr:` in the ontology — are
  excluded from the DDL. They are computed by the
  `CREATE PROPERTY GRAPH` layer, not stored.
- **Column order**: primary-key columns first (in the order declared in
  `keys.primary`), then remaining properties in ontology declaration
  order. Deterministic across runs.
- No partitioning, clustering, or table options. Guessing wrong is
  worse than leaving them out.

Primary keys are emitted as a `PRIMARY KEY (...) NOT ENFORCED` table
constraint. BigQuery does not enforce this at write time, but the query
optimizer uses it for join planning, and tooling (including
`CREATE PROPERTY GRAPH` declarations) reads it as the canonical node
identity.

Example. Given an entity `Person` with `keys.primary: [party_id]`,
properties `party_id: string`, `name: string`, `dob: date`:

```sql
CREATE TABLE `my_dataset.person` (
  party_id STRING NOT NULL,
  name STRING,
  dob DATE,
  PRIMARY KEY (party_id) NOT ENFORCED
);
```

Compound example. Entity `AccountDay` with `keys.primary:
[account_id, as_of]`, properties `account_id: string`, `as_of: date`,
`balance: numeric`:

```sql
CREATE TABLE `my_dataset.account_day` (
  account_id STRING NOT NULL,
  as_of DATE NOT NULL,
  balance NUMERIC,
  PRIMARY KEY (account_id, as_of) NOT ENFORCED
);
```

Alternate keys declared in the ontology are not expressed in DDL.
BigQuery has no `UNIQUE` constraint, and a table may declare at most one
`PRIMARY KEY`. Alternate keys remain logical and are enforced at
validation time, not by the warehouse.

## 4. Edge (relationship) table convention

One table per relationship. Endpoint columns reference the source and
target entities' primary-key columns, with a prefix distinguishing the
two endpoints:

- `from_*` columns mirror the `from` entity's primary-key columns.
- `to_*` columns mirror the `to` entity's primary-key columns.

A single-property entity key yields one endpoint column per side; a
compound entity key yields several. Types and `NOT NULL` are inherited
from the referenced primary-key columns. Derived properties on
relationships are excluded from the DDL, just as with entities.

### Relationship key modes

- **No `keys` block.** No uniqueness beyond the endpoint columns.
  Multi-edges are permitted. Emitted table has endpoint columns
  (`NOT NULL`) plus property columns (nullable).
- **Mode 1 — `keys.primary` on the relationship.** The edge has its
  own identity. Primary-key columns come first and are `NOT NULL`,
  followed by endpoint columns (also `NOT NULL`), followed by
  remaining properties.
- **Mode 2 — `keys.additional` on the relationship.** The effective
  key is `(from_*, to_*, *additional)`. Endpoint columns are `NOT NULL`;
  the listed additional columns are also `NOT NULL`. Order: endpoints,
  then additional-key columns, then remaining properties.

Every edge table emits `FOREIGN KEY (...) REFERENCES node_table(...)
NOT ENFORCED` clauses for each endpoint, pointing the `from_*` columns
at the `from` entity's primary key and the `to_*` columns at the `to`
entity's primary key. BigQuery does not enforce these, but the
optimizer uses them and they document the graph topology at the DDL
layer.

Mode 1 and Mode 2 additionally emit a `PRIMARY KEY (...) NOT ENFORCED`
clause over the relationship's effective key columns. Relationships
with no keys block emit only the foreign keys.

Example. Relationship `Follows` with `from: Person`, `to: Person`,
Person's primary key `[party_id]`, property `since: date`, no keys
block:

```sql
CREATE TABLE `my_dataset.follows` (
  from_party_id STRING NOT NULL,
  to_party_id STRING NOT NULL,
  since DATE,
  FOREIGN KEY (from_party_id) REFERENCES `my_dataset.person`(party_id) NOT ENFORCED,
  FOREIGN KEY (to_party_id)   REFERENCES `my_dataset.person`(party_id) NOT ENFORCED
);
```

Compound endpoint example. Relationship `Holding` with `from: Account`
(primary `[account_id, as_of]`) and `to: Security` (primary `[isin]`),
property `quantity: numeric`, `keys.additional: [as_of]`:

```sql
CREATE TABLE `my_dataset.holding` (
  from_account_id STRING NOT NULL,
  from_as_of DATE NOT NULL,
  to_isin STRING NOT NULL,
  as_of DATE NOT NULL,
  quantity NUMERIC,
  PRIMARY KEY (from_account_id, from_as_of, to_isin, as_of) NOT ENFORCED,
  FOREIGN KEY (from_account_id, from_as_of) REFERENCES `my_dataset.account`(account_id, as_of) NOT ENFORCED,
  FOREIGN KEY (to_isin)                     REFERENCES `my_dataset.security`(isin) NOT ENFORCED
);
```

A shared edges-table-with-type-column shape is **not** a scaffold
output. Users who want that refactor after the fact.

### Endpoint-column name collisions

If the `from` and `to` entities share a primary-key property name, the
`from_*` / `to_*` prefixes already disambiguate. If both endpoints are
the same entity (self-edge), the prefixes still suffice. No further
mangling is applied.

If a relationship property name collides with a generated endpoint
column name (e.g. a property named `from_party_id` on a relationship
whose `from` entity has primary key `party_id`), scaffold reports an
error. Renaming either the property or the entity key resolves the
conflict.

## 5. Type mapping

Ontology property types are fixed (see the ontology spec's type table).
Scaffold emits BigQuery types using the ontology's declared backend
mapping, reproduced here for reference:

| Ontology type | BigQuery type |
|---------------|---------------|
| `string`      | `STRING`      |
| `bytes`       | `BYTES`       |
| `integer`     | `INT64`       |
| `double`      | `FLOAT64`     |
| `numeric`     | `NUMERIC`     |
| `boolean`     | `BOOL`        |
| `date`        | `DATE`        |
| `time`        | `TIME`        |
| `datetime`    | `DATETIME`    |
| `timestamp`   | `TIMESTAMP`   |
| `json`        | `JSON`        |

Any ontology type outside this set is a validation error upstream and
never reaches scaffold. Scaffold itself adds no type inventions.

## 5a. BigQuery DDL features scaffold deliberately does not use

To keep the output minimal and user-editable, scaffold never emits:

- `PARTITION BY` or `CLUSTER BY` — no ontology input determines them.
- Any `OPTIONS(...)` clause on tables or columns. Ontology
  descriptions, labels, and similar metadata belong on the
  `CREATE PROPERTY GRAPH` DDL that `gm compile` produces, not on the
  physical tables. Attaching them here would duplicate semantic
  information across artifacts and invite drift.
- Column `DEFAULT` values.
- `CREATE SCHEMA` for the dataset.
- `CREATE OR REPLACE` or `IF NOT EXISTS` modifiers.
- `CREATE PROPERTY GRAPH` — that is `gm compile`'s job, not scaffold's.
- Views, materialized views, external tables, or snapshot tables.

Users add any of these by hand after scaffold runs.

## 6. Naming

Default `--naming=snake`:

- Entity `Person` → table `person`.
- Relationship `FollowsRelation` or `Follows` → table `follows`.
- Property `firstName` → column `first_name`.

`--naming=preserve` keeps the ontology identifier verbatim. Useful for
users whose warehouses already use `PascalCase` conventions.

The snake-case conversion splits at uppercase-to-lowercase boundaries
and treats consecutive uppercase runs as acronyms:
`firstName` → `first_name`, `HTTPRequest` → `http_request`.

Both the DDL and the generated binding use the same naming choice, so
they agree by construction.

## 7. Generated binding stub

Mirrors the DDL exactly: primary-key columns, `from_*` / `to_*`
endpoint columns, snake-cased property columns, dataset from
`--dataset`. Top of the file carries a comment stating that the file
is user-owned and that scaffold will not re-run against it.

The stub is a valid input to `gm compile` with no edits required.

Example. For the `Person` entity and `Follows` relationship from the
examples above, with `--dataset my_dataset`:

```yaml
# Generated by gm scaffold. This file is user-owned — edit freely.
binding: my_dataset
ontology: <ontology-name>
target:
  backend: bigquery
  dataset: my_dataset
entities:
  - name: Person
    source: my_dataset.person
    properties:
      - {name: party_id, column: party_id}
      - {name: name, column: name}
      - {name: dob, column: dob}
relationships:
  - name: Follows
    source: my_dataset.follows
    from_columns: [from_party_id]
    to_columns: [to_party_id]
    properties:
      - {name: since, column: since}
```

## 8. Determinism

Given the same ontology, flags, and version of `gm`, scaffold produces
byte-identical `ddl.sql` and `binding.yaml`. Ordering follows ontology
declaration order throughout.

## 9. Out of scope for v0

- Hybrid projects (some tables exist, some do not). Revisit with a
  `--only` / `--skip` flag if greenfield users ask.
- `DROP TABLE IF EXISTS` preambles. Scary default; user adds if wanted.
- `CREATE SCHEMA` for the dataset. Assume it exists.
- Migrations or diffs against a prior scaffold output.
- Physical schema knobs in the binding (`partition_by`, `cluster_by`,
  labels, expirations). If a real use case surfaces, extend the binding
  then — not speculatively.
- Spanner output. v0 emits BigQuery DDL only; Spanner scaffold follows
  the same shape but is a separate design.

## 10. Open questions

- Should `--project` default to the ambient `gcloud` project, or always
  be explicit? Leaning explicit — scaffold output is committed to the
  repo, and baking a local default into it tends to surprise.
- How should scaffold handle entities with zero declared properties?
  Emit a table with only `id`, or error? Leaning emit — the user may be
  staging an ontology that gains properties later.
- Should the generated binding carry a version or generator tag, so
  `gm compile` could in principle warn if the binding is out of date?
  Leaning no — the non-idempotent contract means there is no "out of
  date" state.
