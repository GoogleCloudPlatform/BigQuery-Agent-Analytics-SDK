# Ontology User Manual

## Overview

An ontology is a logical, backend-neutral description of a domain as a property graph. It defines **entity types** (nodes), **relationship types** (edges), the **properties** they carry, which properties serve as **keys**, and optional **single-parent inheritance** between types.

An ontology does not know where data physically lives. That is the job of a separate **binding** document, which maps ontology entities and properties to concrete BigQuery tables and columns.

Together, the two produce executable DDL:

```
ontology  +  binding  →  CREATE PROPERTY GRAPH DDL
```

The workflow uses three artifacts:

| Artifact | File convention | Purpose |
|----------|----------------|---------|
| Ontology | `*.ontology.yaml` | Logical graph schema (what) |
| Binding | `*.binding.yaml` | Physical table mapping (where) |
| DDL | `*.sql` | BigQuery `CREATE PROPERTY GRAPH` statement |

The `gm` CLI validates and compiles these files.

---

## Ontology YAML Specification

### Top-Level Structure

```yaml
ontology: <string>                        # required — ontology identifier
version: <string>                         # optional — e.g. "0.1"
entities:                                 # required — at least one entity
  - <Entity>
relationships:                            # optional — defaults to empty
  - <Relationship>
description: <string>                     # optional
synonyms: [<string>, ...]                 # optional
annotations:                              # optional — open-ended metadata
  <key>: <string or list of strings>
```

- `ontology` is the identifier that bindings reference (by name, not file path).
- `version` accepts unquoted numbers in YAML (e.g. `version: 0.1`) — they are coerced to strings automatically.
- `entities` must be non-empty. An ontology with no node types has nothing for relationships or bindings to attach to.
- `relationships` is optional. Pure taxonomies (entities + inheritance, no edges) are legal.
- Unknown top-level keys are rejected.

### Entities (Node Types)

```yaml
- name: <string>                          # required — unique within ontology
  extends: <string>                       # optional — parent entity name
  keys:                                   # required (directly or via inheritance)
    primary: [<property_name>, ...]       # required on entities
    alternate:                            # optional — additional unique tuples
      - [<property_name>, ...]
  properties:                             # optional — defaults to empty
    - <Property>
  description: <string>                   # optional
  synonyms: [<string>, ...]               # optional
  annotations: {<key>: <value>}           # optional
```

**Rules:**

- Entity names must be unique within the ontology and cannot collide with relationship names.
- Every entity must have an effective `keys.primary` — either declared directly or inherited from a parent.
- `keys.additional` is forbidden on entities (it is relationship-only).
- When using `extends`, the child inherits the parent's properties and keys. Redeclaring either is an error.
- `keys` is marked optional in the schema because a child entity inherits its parent's keys and must leave the field unset.

### Relationships (Edge Types)

```yaml
- name: <string>                          # required — unique within ontology
  extends: <string>                       # optional — parent relationship name
  keys:                                   # optional
    primary: [<property_name>, ...]       # XOR with additional
    additional: [<property_name>, ...]    # XOR with primary
    alternate:                            # optional — requires primary
      - [<property_name>, ...]
  from: <entity_name>                     # required — source entity
  to: <entity_name>                       # required — target entity
  cardinality: <Cardinality>              # optional — defaults to unconstrained
  properties:                             # optional — defaults to empty
    - <Property>
  description: <string>                   # optional
  synonyms: [<string>, ...]               # optional
  annotations: {<key>: <value>}           # optional
```

**Rules:**

- `from` and `to` must reference declared entities.
- `primary` and `additional` are mutually exclusive. You may declare one, the other, or neither — but not both.
- When neither is declared, multi-edges are permitted (no uniqueness constraint).
- A child relationship may narrow its endpoints covariantly: the child's `from`/`to` must equal or be a subtype of the parent's corresponding endpoint.
- A child may not redefine the parent's cardinality. It may omit it (inheriting silently) or restate the same value.

### Properties

```yaml
- name: <string>                          # required — unique within parent
  type: <PropertyType>                    # required
  expr: <string>                          # optional — marks a derived property
  description: <string>                   # optional
  synonyms: [<string>, ...]               # optional
  annotations: {<key>: <value>}           # optional
```

A property without `expr` is a **stored property** — its value comes from a physical column specified in the binding.

A property with `expr` is a **derived property** — its value is computed from a BigQuery SQL expression that references sibling properties on the same entity or relationship. Derived properties:

- Must NOT appear in a binding (the compiler substitutes the expression).
- May only reference other properties on the same entity/relationship.
- Must not use subqueries, window functions, or aggregates.
- Circular references between derived properties are detected and rejected.

Example:

```yaml
properties:
  - name: first_name
    type: string
  - name: last_name
    type: string
  - name: full_name
    type: string
    expr: "first_name || ' ' || last_name"
```

### Property Types

Eleven logical types are supported, each mapping to a GoogleSQL type:

| Logical type | Description |
|-------------|-------------|
| `string` | Variable-length text |
| `bytes` | Binary data |
| `integer` | 64-bit integer |
| `double` | 64-bit floating point |
| `numeric` | Arbitrary-precision decimal |
| `boolean` | True/false |
| `date` | Calendar date |
| `time` | Time of day |
| `datetime` | Date and time without timezone |
| `timestamp` | Date and time with timezone |
| `json` | JSON document |

These types are backend-neutral. Backend-specific gaps (e.g. Spanner not supporting `time` or `datetime`) are surfaced at binding time, not ontology time.

### Keys

Keys define uniqueness constraints. There are three roles:

**`primary`** — the identity of a row.

- Required on entities (directly or inherited).
- Optional on relationships.
- Each entry must name a declared property.
- List must be non-empty.

**`alternate`** — additional unique tuples beyond the primary key.

- Only meaningful alongside a `primary` key.
- Each alternate key is a list of property names.
- No alternate key may duplicate the primary key or another alternate key.
- Each alternate key list must be non-empty with no duplicate columns.

**`additional`** — uniqueness without picking a primary. Relationship-only.

- Mutually exclusive with `primary`.
- Cannot be combined with `alternate` keys.
- The effective key becomes `(from_endpoint, to_endpoint, additional_columns)`.

**Entity key modes:**

```yaml
# Entity: primary required, additional forbidden
keys:
  primary: [account_id]
  alternate:                    # optional
    - [email]
    - [tenant_id, external_id]
```

**Relationship key modes:**

```yaml
# Mode A: standalone identity
keys:
  primary: [transaction_id]

# Mode B: endpoint-extended identity
keys:
  additional: [as_of]
# Effective key = (from_entity_pk, to_entity_pk, as_of)

# Mode C: no keys (multi-edges permitted)
# Simply omit the keys field entirely
```

### Inheritance

Ontologies support single-parent inheritance via `extends`.

```yaml
entities:
  - name: Party
    keys:
      primary: [party_id]
    properties:
      - name: party_id
        type: string
      - name: name
        type: string

  - name: Person
    extends: Party
    properties:
      - name: dob
        type: date
```

**Inheritance rules:**

- Entities extend entities. Relationships extend relationships. Cross-kind inheritance is not allowed.
- A child inherits all properties and keys from its parent chain.
- Redeclaring an inherited property (by name) is an error.
- Redeclaring inherited keys is an error.
- Cyclic inheritance chains are detected and rejected.
- For relationships: child endpoints must covariantly narrow the parent's endpoints (i.e. child's `from`/`to` must be the same entity as, or a subtype of, the parent's).
- A child relationship may not redefine the parent's cardinality.

**Current limitation:** The v0 compiler does not support `extends`. Ontologies with inheritance pass validation but fail at compile time. Inheritance lowering is planned for a future version.

### Cardinality

Cardinality describes the multiplicity of a relationship's endpoints:

| Value | Meaning |
|-------|---------|
| `one_to_one` | Each `from` entity connects to at most one `to`, and vice versa |
| `one_to_many` | Each `from` entity connects to many `to` entities, each `to` connects to at most one `from` |
| `many_to_one` | Each `from` entity connects to at most one `to`, each `to` connects to many `from` entities |
| `many_to_many` | No multiplicity constraint |

Cardinality is optional. When absent, the relationship is treated as unconstrained.

### Annotations and Synonyms

**Annotations** are an open-ended bag of string metadata attached to any ontology element. Values are either a single string or a list of strings:

```yaml
annotations:
  doc_id: "FIBO-001"
  pii_category: "PERSONAL"
  audit_tags:
    - "HIPAA"
    - "GDPR"
```

Annotations are consumed by downstream tools (catalogs, lineage, search) without the ontology itself needing to understand them.

**Synonyms** are alternative names for the element:

```yaml
synonyms:
  - finance-core
  - fin-domain
```

### Validation Rules

The ontology loader enforces 13 cross-element semantic rules (beyond the per-field shape validation that Pydantic handles):

1. Entity names are unique within the ontology.
2. Relationship names are unique within the ontology.
3. Entity and relationship names are disjoint — no name can be used by both.
4. Property names are unique within their parent entity or relationship.
5. `extends` must reference a declared same-kind element (entity extends entity, relationship extends relationship).
6. No cycles in `extends` chains.
7. Redeclaring an inherited property (by name) is an error.
8. Redeclaring inherited keys is an error.
9. Every key column must reference a declared property (including inherited ones).
10. Alternate keys must be non-empty, have no duplicate columns, and not duplicate another key.
11. On entities: `primary` is required (directly or inherited), `additional` is forbidden.
12. On relationships: `primary` and `additional` are mutually exclusive.
13. Relationship endpoints (`from`, `to`) must reference declared entities, and child relationships must covariantly narrow parent endpoints.

Unknown YAML keys at any level are rejected (via Pydantic `extra="forbid"`).

---

## Binding YAML Specification

A binding attaches a logical ontology to physical BigQuery tables and columns. One binding file describes one deployment target.

### Top-Level Structure

```yaml
binding: <string>                         # required — binding identifier
ontology: <string>                        # required — ontology name to realize
target:                                   # required — deployment target
  backend: bigquery                       # required — only "bigquery" today
  project: <string>                       # required — GCP project ID
  dataset: <string>                       # required — BigQuery dataset
entities:                                 # optional — defaults to empty
  - <EntityBinding>
relationships:                            # optional — defaults to empty
  - <RelationshipBinding>
```

- `ontology` is the ontology's name (its `ontology:` field), not a file path. The loader resolves it to a file.
- A binding may realize a subset of the referenced ontology — any entity or relationship left out is simply absent from this target.

### Target

```yaml
target:
  backend: bigquery
  project: my-gcp-project
  dataset: analytics
```

`project` and `dataset` serve as the default namespace for resolving bare table names in entity/relationship source references.

### Entity Bindings

```yaml
entities:
  - name: Account                         # must match ontology entity name
    source: raw.accounts                  # BigQuery table or view
    properties:
      - name: account_id                  # ontology property name
        column: acct_id                   # physical column name
      - name: opened_at
        column: created_ts
```

**Rules:**

- `name` must reference a declared entity in the ontology.
- `source` is the physical BigQuery table or view.
- Every non-derived property on the entity (including inherited ones) must have a `PropertyBinding`. No cherry-picking.
- Derived properties (`expr:` in ontology) must NOT appear in the binding.
- The primary key is implicit: the ontology names the key properties, and the matching `PropertyBinding` entries supply the physical columns.

### Relationship Bindings

```yaml
relationships:
  - name: HOLDS                           # must match ontology relationship name
    source: raw.holdings                  # BigQuery edge table
    from_columns: [account_id]            # columns carrying source entity key
    to_columns: [security_id]             # columns carrying target entity key
    properties:                           # optional — defaults to empty
      - name: as_of
        column: snapshot_date
      - name: quantity
        column: qty
```

**Rules:**

- `name` must reference a declared relationship in the ontology.
- `from_columns` and `to_columns` are the columns in the edge table that carry the source and target entity keys.
- The arity (number of columns) of `from_columns` must match the source entity's primary key length.
- The arity of `to_columns` must match the target entity's primary key length.
- Both `from_columns` and `to_columns` must be non-empty lists.
- Property coverage follows the same total-coverage rule as entities: every non-derived property must be bound.

### Coverage Rules

The binding follows a **partial-at-ontology-level, total-within-each-element** model:

- You may omit entire entities or relationships from the binding. Anything absent is not realized on this target.
- Once you include an entity or relationship, you must bind **every** non-derived property (including inherited ones).
- Derived properties must **never** appear in a binding.

### Validation Rules

The binding loader enforces these semantic rules against the paired ontology:

1. The binding's `ontology` field must match the ontology's `ontology` field.
2. Entity and relationship binding names are unique within the binding and across kinds.
3. Every bound name must reference a declared element in the ontology.
4. Total property coverage within each bound entity/relationship (all non-derived properties bound, no derived properties bound, no duplicates).
5. Relationship `from_columns` arity matches the source entity's primary key arity.
6. Relationship `to_columns` arity matches the target entity's primary key arity.
7. Both endpoints of a bound relationship must have at least one bound descendant entity in the binding (no dangling edges).

---

## CLI Reference

The `gm` command-line tool validates and compiles ontology and binding YAML files.

### Installation

The `gm` command is installed as a console script entry point from the `bigquery_ontology` package:

```bash
pip install -e .
gm --help
```

### `gm validate`

Validate a single ontology or binding YAML file. The file kind is auto-detected by checking for a top-level `ontology:` or `binding:` key.

```
gm validate <file> [--json] [--ontology PATH]
```

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to an ontology or binding YAML file |

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--json` | false | Emit structured JSON errors on stderr instead of human-readable text |
| `--ontology PATH` | auto | For binding files: path to the companion ontology YAML. By default, looks for `<ontology_name>.ontology.yaml` in the same directory as the binding |

**Behavior:**

- For ontology files: validates shape and cross-element semantics. Success produces no output.
- For binding files: auto-discovers or uses `--ontology` to locate the companion ontology, validates both, then validates the binding against the ontology. Success produces no output.

**Examples:**

```bash
# Validate an ontology
gm validate finance.ontology.yaml

# Validate a binding (auto-discovers companion ontology)
gm validate finance.binding.yaml

# Validate a binding with explicit ontology path
gm validate finance.binding.yaml --ontology /path/to/finance.ontology.yaml

# Get JSON-formatted errors
gm validate finance.binding.yaml --json
```

### `gm compile`

Compile a binding YAML file into BigQuery `CREATE PROPERTY GRAPH` DDL. The input must be a binding file, not an ontology — ontologies are backend-neutral and need a binding to resolve physical tables and columns.

```
gm compile <file> [--ontology PATH] [-o PATH] [--json]
```

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to a binding YAML file |

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--ontology PATH` | auto | Path to companion ontology YAML |
| `-o PATH` / `--output PATH` | stdout | Write DDL to this file (overwritten if exists) |
| `--json` | false | Emit structured JSON errors on stderr |

**Behavior:**

- Validates both the binding and its companion ontology before compiling. Any validation error fails the compile — no partial DDL is emitted.
- On success: writes the DDL to stdout (or to `-o` file).
- On failure: writes errors to stderr.

**Examples:**

```bash
# Compile and print DDL to stdout
gm compile finance.binding.yaml

# Compile with explicit ontology path
gm compile finance.binding.yaml --ontology /path/to/finance.ontology.yaml

# Write DDL to a file
gm compile finance.binding.yaml -o graph_ddl.sql

# Pipe directly to BigQuery
gm compile finance.binding.yaml | bq query --use_legacy_sql=false -
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Validation or compilation error (user-fixable) |
| 2 | Usage error (bad flag, missing file, missing companion ontology, compile invoked on non-binding) |
| 3 | Internal error |

### Error Formats

**Human-readable (default):**

```
finance.ontology.yaml:0:0: ontology-validation — Duplicate entity name: 'Account'
```

Format: `<file>:<line>:<col>: <rule> — <message>`

**JSON (with `--json`):**

```json
[
  {
    "file": "finance.ontology.yaml",
    "line": 0,
    "col": 0,
    "rule": "ontology-validation",
    "severity": "error",
    "message": "Duplicate entity name: 'Account'"
  }
]
```

**Error rule codes:**

| Rule | Source |
|------|--------|
| `ontology-shape:<type>` | Pydantic shape validation on ontology |
| `ontology-validation` | Semantic validation on ontology |
| `binding-shape:<type>` | Pydantic shape validation on binding |
| `binding-validation` | Semantic validation on binding |
| `compile-validation` | Compile-time validation (e.g. `extends` not supported) |
| `yaml-parse` | YAML syntax error |
| `cli-missing-file` | File not found or not readable |
| `cli-missing-ontology` | Companion ontology not found |
| `cli-unknown-kind` | File is neither ontology nor binding |
| `cli-wrong-kind` | Compile invoked on a non-binding file |
| `cli-output-error` | Cannot write output file |

---

## Compilation

### How It Works

The compiler (`compile_graph`) takes a validated ontology and binding and produces a BigQuery `CREATE PROPERTY GRAPH` DDL string. It runs in two stages:

**Stage 1 — Resolve.** Cross-references the ontology and binding to produce an in-memory `ResolvedGraph`:
- Rejects `extends` (v0 does not lower inheritance).
- Substitutes derived expressions with physical column names.
- Wires relationship endpoints to node-table aliases and key columns.
- Computes edge key columns from ontology key declarations.
- Sorts node and edge tables alphabetically by name for deterministic output.

**Stage 2 — Emit.** Walks the `ResolvedGraph` and renders the DDL:
- Properties are emitted in ontology declaration order (not sorted).
- Property lists that fit within 80 columns render inline; longer ones wrap to one property per line.
- Output is deterministic: same inputs always produce byte-identical DDL.

### Derived Expression Substitution

When a derived property references other properties by name, the compiler replaces each property name with its physical column (for stored properties) or recursively substituted expression (for other derived properties):

```yaml
# Ontology
properties:
  - name: first_name
    type: string
  - name: last_name
    type: string
  - name: full_name
    type: string
    expr: "first_name || ' ' || last_name"
```

```yaml
# Binding
properties:
  - name: first_name
    column: given_name
  - name: last_name
    column: family_name
  # full_name is derived — must NOT appear here
```

```sql
-- Compiled DDL output
LABEL Person PROPERTIES (
  given_name AS first_name,
  family_name AS last_name,
  (given_name || ' ' || family_name) AS full_name
)
```

Circular references between derived properties are detected and rejected.

### Edge Key Resolution

Every edge table gets a `KEY (...)` clause in the DDL. The compiler determines the key columns using three mutually exclusive cases:

**Case 1 — `primary` declared.** The relationship has a standalone identity (e.g. `TRANSFER` keyed by `transaction_id`). The KEY uses the bound primary-key columns.

**Case 2 — `additional` declared.** The relationship identifies rows within an endpoint pair (e.g. `HOLDS` with `additional: [as_of]`). The KEY is `from_columns + to_columns + bound additional columns`.

**Case 3 — No keys declared.** The default is one edge per endpoint pair. The KEY is `from_columns + to_columns`.

### Output Format

The compiled DDL follows this structure:

```sql
CREATE PROPERTY GRAPH <name>
  NODE TABLES (
    <source> AS <entity_name>
      KEY (<key_columns>)
      LABEL <entity_name> PROPERTIES (<property_list>),
    ...
  )
  EDGE TABLES (
    <source> AS <relationship_name>
      KEY (<key_columns>)
      SOURCE KEY (<from_columns>) REFERENCES <from_entity> (<from_key_columns>)
      DESTINATION KEY (<to_columns>) REFERENCES <to_entity> (<to_key_columns>)
      LABEL <relationship_name> PROPERTIES (<property_list>),
    ...
  );
```

Property rendering:
- Stored property where the column matches the logical name: `column_name`
- Stored property with a rename: `column_name AS logical_name`
- Derived property: `(expression) AS logical_name`

---

## End-to-End Example

### Step 1: Define the Ontology

Create `finance.ontology.yaml`:

```yaml
ontology: finance
version: 0.1

entities:
  - name: Person
    keys:
      primary: [party_id]
    properties:
      - name: party_id
        type: string
      - name: name
        type: string
      - name: first_name
        type: string
      - name: last_name
        type: string
      - name: full_name
        type: string
        expr: "first_name || ' ' || last_name"

  - name: Account
    keys:
      primary: [account_id]
    properties:
      - name: account_id
        type: string
      - name: opened_at
        type: timestamp

  - name: Security
    keys:
      primary: [security_id]
    properties:
      - name: security_id
        type: string

relationships:
  - name: HOLDS
    keys:
      additional: [as_of]
    from: Account
    to: Security
    cardinality: many_to_many
    properties:
      - name: as_of
        type: timestamp
      - name: quantity
        type: double

description: Party, account, and security model for finance domain.
```

### Step 2: Create the Binding

Create `finance.binding.yaml` in the same directory:

```yaml
binding: finance-bq-prod
ontology: finance
target:
  backend: bigquery
  project: my-project
  dataset: finance

entities:
  - name: Person
    source: raw.persons
    properties:
      - name: party_id
        column: pid
      - name: name
        column: display_name
      - name: first_name
        column: given_name
      - name: last_name
        column: family_name
      # full_name is derived — do NOT include it here

  - name: Account
    source: raw.accounts
    properties:
      - name: account_id
        column: acct_id
      - name: opened_at
        column: created_ts

  - name: Security
    source: ref.securities
    properties:
      - name: security_id
        column: cusip

relationships:
  - name: HOLDS
    source: raw.holdings
    from_columns: [account_id]
    to_columns: [security_id]
    properties:
      - name: as_of
        column: snapshot_date
      - name: quantity
        column: qty
```

### Step 3: Validate

```bash
# Validate the ontology
gm validate finance.ontology.yaml

# Validate the binding (auto-discovers finance.ontology.yaml next to it)
gm validate finance.binding.yaml
```

Both commands produce no output on success (exit code 0).

### Step 4: Compile to DDL

```bash
gm compile finance.binding.yaml
```

Output:

```sql
CREATE PROPERTY GRAPH finance
  NODE TABLES (
    raw.accounts AS Account
      KEY (acct_id)
      LABEL Account PROPERTIES (acct_id AS account_id, created_ts AS opened_at),
    raw.persons AS Person
      KEY (pid)
      LABEL Person PROPERTIES (
        pid AS party_id,
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
      KEY (account_id, security_id, snapshot_date)
      SOURCE KEY (account_id) REFERENCES Account (acct_id)
      DESTINATION KEY (security_id) REFERENCES Security (cusip)
      LABEL HOLDS PROPERTIES (snapshot_date AS as_of, qty AS quantity)
  );
```

To write to a file:

```bash
gm compile finance.binding.yaml -o graph_ddl.sql
```
