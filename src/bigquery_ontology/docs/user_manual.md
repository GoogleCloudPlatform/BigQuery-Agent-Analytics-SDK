# Ontology User Manual

## Table of Contents

### Part 1: Guide
- [What Is an Ontology?](#what-is-an-ontology)
- [Why Use an Ontology?](#why-use-an-ontology)
- [How This Project Uses Ontologies](#how-this-project-uses-ontologies)
- [Writing an Ontology](#writing-an-ontology)
  - [Naming and versioning](#naming-and-versioning)
  - [Defining entities](#defining-entities)
  - [Defining relationships](#defining-relationships)
  - [Property types](#property-types)
  - [Derived properties](#derived-properties)
  - [Relationship keys](#relationship-keys)
  - [Metadata](#metadata)
- [Writing a Binding](#writing-a-binding)
  - [Structure](#structure)
  - [Mapping entities](#mapping-entities)
  - [Mapping relationships](#mapping-relationships)
- [Installing the CLI](#installing-the-cli)
- [Validating and Compiling](#validating-and-compiling)
  - [Validate](#validate)
  - [Compile](#compile)
- [Importing from OWL](#importing-from-owl)
  - [Prerequisites](#prerequisites)
  - [Basic usage](#basic-usage)
  - [What maps to what](#what-maps-to-what)
  - [Namespace filtering](#namespace-filtering)
  - [FILL_IN placeholders](#fill_in-placeholders)
  - [Drop annotations](#drop-annotations)
- [Scaffolding a New Project](#scaffolding-a-new-project)
- [End-to-End Walkthrough](#end-to-end-walkthrough)

### Part 2: Reference
- [Ontology YAML Schema](#ontology-yaml-schema)
- [Binding YAML Schema](#binding-yaml-schema)
- [CLI Reference](#cli-reference)
- [Ontology Validation Rules](#ontology-validation-rules)
- [Binding Validation Rules](#binding-validation-rules)
- [Compilation Details](#compilation-details)

---

## Part 1: Guide

### What Is an Ontology?

An **ontology** is a formal, explicit specification of a shared conceptualization of a domain. In practical terms, it defines:

- **Entity types** (classes) — the kinds of things that exist in the domain (Customer, Order, Product)
- **Properties** (attributes) — typed data each entity carries (name: string, amount: decimal)
- **Relationships** — how entity types connect to each other (Customer *places* Order, Order *contains* Product)
- **Constraints** — rules about uniqueness, cardinality, valid values, and type hierarchies

The key distinction from a database schema is that an ontology captures **meaning**, not just structure. It answers "what does this data represent in the real world?" rather than "what columns does this table have?" This makes ontologies portable across different storage backends — the same ontology can be realized on BigQuery, Spanner, or any other data warehouse.

### Why Use an Ontology?

- **Shared vocabulary** — teams agree on what "Account", "Transaction", or "Security" means, preventing ambiguity across systems.
- **Backend independence** — the logical model is defined once and can be mapped to different physical storage systems without changing the model itself.
- **Semantic grounding for AI** — ontologies constrain LLM outputs to well-typed entities and relationships, reducing hallucination and enabling structured multi-hop reasoning over knowledge graphs.
- **Evolvable schemas** — annotations and metadata let you enrich a domain model without breaking existing definitions.

### How This Project Uses Ontologies

In this project, an ontology is authored as a YAML file that describes a domain as a **property graph**: entity types become nodes, relationship types become edges, and both carry typed properties. The ontology is deliberately **backend-neutral** — it defines *what* exists, never *where* the data lives.

A separate **binding** document handles the physical mapping: it attaches ontology entities and properties to concrete BigQuery tables and columns.

Together, the two produce executable DDL:

```
ontology  +  binding  →  CREATE PROPERTY GRAPH DDL
```

This separation means the same ontology can have multiple bindings — one per environment, backend, or data source — without duplicating the logical model.

| Artifact | File convention | Purpose |
|----------|----------------|---------|
| Ontology | `*.ontology.yaml` | Logical graph schema (what exists) |
| Binding | `*.binding.yaml` | Physical table mapping (where it lives) |
| DDL | `*.sql` | BigQuery `CREATE PROPERTY GRAPH` statement |

The `gm` CLI validates and compiles these files.

---

### Writing an Ontology

An ontology YAML file has four main parts: a name, entities, relationships, and optional metadata.

#### Naming and versioning

Every ontology starts with a name. Bindings reference this name to find their companion ontology:

```yaml
ontology: finance
version: 0.1
```

#### Defining entities

Entities are the node types in your graph. Each entity needs a name, a primary key, and its properties:

```yaml
entities:
  - name: Account
    keys:
      primary: [account_id]
    properties:
      - name: account_id
        type: string
      - name: opened_at
        type: timestamp
```

The `primary` key tells the system how to uniquely identify each Account. Every key column must be a declared property.

#### Defining relationships

Relationships connect entities. They have a `from` (source) and `to` (destination) entity, and optionally their own properties:

```yaml
relationships:
  - name: HOLDS
    from: Account
    to: Security
    cardinality: many_to_many
    properties:
      - name: as_of
        type: timestamp
      - name: quantity
        type: double
```

#### Property types

Properties have a `name` and a `type`. The eleven supported types are: `string`, `bytes`, `integer`, `double`, `numeric`, `boolean`, `date`, `time`, `datetime`, `timestamp`, and `json`. These are logical types — backend-specific mapping happens at binding time.

#### Derived properties

A property can compute its value from other properties on the same entity using a BigQuery SQL expression. Mark it with `expr:`:

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

Derived properties do not appear in the binding — the compiler substitutes the expression with bound column names automatically.

#### Relationship keys

Relationships have three options for keys:

- **`primary`** — the edge has its own standalone identity (e.g. a `TRANSFER` keyed by `transaction_id`). The DDL KEY uses the bound primary columns only.
- **`additional`** — the edge is unique within an endpoint pair (e.g. `HOLDS` keyed by `as_of`, meaning for a given account-security pair, no two holdings share the same `as_of` date). The DDL KEY becomes `(from_columns, to_columns, additional_columns)`.
- **No keys** — the DDL defaults to using the endpoint pair `(from_columns, to_columns)` as the KEY. This means each pair of endpoints can have at most one edge. If you actually need multiple edges between the same pair of nodes, declare `keys.additional` with a discriminator property.

`primary` and `additional` are mutually exclusive.

```yaml
# Standalone identity — KEY is (transaction_id)
- name: TRANSFER
  keys:
    primary: [transaction_id]
  from: Account
  to: Account

# Endpoint-extended — KEY is (from, to, as_of)
- name: HOLDS
  keys:
    additional: [as_of]
  from: Account
  to: Security

# No keys — KEY defaults to (from, to), one edge per endpoint pair
- name: RELATED_TO
  from: Party
  to: Party
```

#### Inheritance (under development)

The ontology YAML schema includes an `extends` field on entities and relationships for modeling "is-a" type hierarchies (e.g. `Person extends Party`). This feature is partially implemented: the loader validates inheritance rules, but the compiler does not yet support it. Using `extends` will pass `gm validate` but fail on `gm compile`. For now, define each entity and relationship independently without `extends`.

#### Metadata

You can attach descriptions, synonyms, and free-form annotations to any element:

```yaml
ontology: finance
description: Party, account, and security model for finance domain.
synonyms:
  - finance-core
annotations:
  doc_id: "FIBO-001"
  audit_tags:
    - "HIPAA"
    - "GDPR"
```

Annotations are consumed by downstream tools (catalogs, lineage, search) without the ontology needing to understand them. Values are strings or lists of strings.

---

### Writing a Binding

A binding attaches your ontology to physical BigQuery tables and columns. It answers the question the ontology deliberately leaves open: "where does this data actually live?"

#### Structure

A binding names itself, references an ontology, declares a target, and maps entities and relationships:

```yaml
binding: finance-bq-prod
ontology: finance
target:
  backend: bigquery
  project: my-project
  dataset: finance
```

The `ontology` field is the ontology's name (not a file path).

#### Mapping entities

For each entity you want to realize, specify the BigQuery table and map every non-derived property to a physical column:

```yaml
entities:
  - name: Account
    source: raw.accounts
    properties:
      - name: account_id
        column: acct_id
      - name: opened_at
        column: created_ts
```

You must bind **every** non-derived property — no cherry-picking. Derived properties (those with `expr:`) must **not** appear in the binding. You may omit entire entities from the binding; they simply won't be part of this target.

#### Mapping relationships

Relationships additionally need `from_columns` and `to_columns` to specify which columns in the edge table carry the endpoint keys:

```yaml
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

The arity (number of columns) of `from_columns` must match the source entity's primary key length, and likewise for `to_columns`.

---

### Installing the CLI

Install the package to get the `gm` command:

```bash
pip install bigquery-agent-analytics
```

To verify the installation:

```bash
gm --help
```

For local development, install in editable mode from the repo root:

```bash
pip install -e .
```

If you plan to use `gm import-owl`, install with the OWL extra:

```bash
pip install bigquery-agent-analytics[owl]

# or, in editable mode:
pip install -e '.[owl]'
```

---

### Validating and Compiling

The `gm` CLI validates and compiles your files.

#### Validate

Run `gm validate` on any ontology or binding file. The file kind is auto-detected:

```bash
gm validate finance.ontology.yaml
gm validate finance.binding.yaml
```

Success produces no output (exit code 0). Errors print to stderr with file, location, and rule:

```
finance.ontology.yaml:0:0: ontology-validation — Duplicate entity name: 'Account'
```

When validating a binding, the companion ontology is auto-discovered (`finance.ontology.yaml` next to the binding). Override with `--ontology PATH`.

#### Compile

Run `gm compile` on a binding file to produce `CREATE PROPERTY GRAPH` DDL:

```bash
# Print to stdout
gm compile finance.binding.yaml

# Write to file
gm compile finance.binding.yaml -o graph_ddl.sql

# Pipe directly to BigQuery
gm compile finance.binding.yaml | bq query --use_legacy_sql=false -
```

Compilation validates both files first — any error prevents DDL output.

---

### Importing from OWL

If your domain is already modeled as an OWL ontology, you can import it into `ontology.yaml` format instead of writing it by hand.

#### Prerequisites

The OWL importer requires `rdflib`, which is an optional dependency. Install it with:

```bash
pip install 'bigquery-agent-analytics[owl]'
```

#### Basic usage

```bash
gm import-owl finance.ttl \
  --include-namespace "https://example.com/finance#" \
  -o finance.ontology.yaml
```

This reads the Turtle file, keeps only classes and properties in the given namespace, and writes the resulting ontology YAML. A drop summary of excluded or unsupported OWL features is printed to stderr.

Multiple source files and namespace prefixes are supported:

```bash
gm import-owl core.ttl extensions.ttl \
  --include-namespace "https://example.com/core#" \
  --include-namespace "https://example.com/ext#" \
  -o combined.ontology.yaml
```

Use `--format ttl` or `--format rdfxml` to override parser auto-detection from the file extension.

#### What maps to what

| OWL construct | Ontology equivalent |
|---|---|
| `owl:Class` | Entity |
| `owl:DatatypeProperty` with domain and range | Property on the domain entity |
| `owl:ObjectProperty` with domain and range | Relationship (from domain, to range) |
| `rdfs:subClassOf` (single parent) | `extends` on entity |
| `rdfs:subPropertyOf` (single parent) | `extends` on relationship |
| `owl:hasKey` | `keys.primary` |
| `owl:FunctionalProperty` | `cardinality: many_to_one` (object properties only) |
| `rdfs:label` | `description` |
| `rdfs:comment` | Appended to `description` |
| `skos:altLabel`, `skos:prefLabel` | `synonyms` |

XSD datatypes are mapped to ontology types: `xsd:string` to `string`, `xsd:integer` to `integer`, `xsd:decimal` to `numeric`, `xsd:boolean` to `boolean`, `xsd:date` to `date`, `xsd:dateTime` to `timestamp`, and so on.

#### Namespace filtering

The `--include-namespace` flag is required and acts as an allow-list. Only OWL classes and properties whose IRIs start with one of the given namespace prefixes are included in the output. Everything else is excluded.

This is important because most OWL files import many external vocabularies (upper ontologies, SKOS, Dublin Core) that you typically don't want in your property graph. The namespace filter lets you select just the classes you care about.

For example, a FIBO (Financial Industry Business Ontology) module might import hundreds of classes from foundational vocabularies, but with `--include-namespace "https://spec.edmcouncil.org/fibo/ontology/FBC/"` you get only the financial business classes.

Excluded items are not silently lost:

- The drop summary on stderr counts excluded classes and properties per namespace.
- Cross-boundary references from kept entities are recorded as annotations (e.g., `owl:subClassOf_excluded: https://example.com/upper#Agent`).

#### FILL_IN placeholders

When the OWL source is ambiguous or incomplete, the importer emits `FILL_IN` as a placeholder value with a YAML comment explaining what to do. Three situations produce placeholders:

1. **Missing primary key.** The class has no `owl:hasKey` declaration. The importer lists candidate data properties in a comment:

   ```yaml
   - name: Account
     # no owl:hasKey in OWL source
     # candidate data properties: account_id, external_ref
     keys:
       primary: [FILL_IN]
   ```

2. **Multiple parent classes.** The class has more than one `rdfs:subClassOf` parent. Since the ontology model supports single inheritance, you must pick one:

   ```yaml
   - name: JointAccount
     # multi-parent: rdfs:subClassOf [Account, Organization]
     extends: FILL_IN
   ```

3. **Multiple domain or range.** A property has multiple `rdfs:domain` or `rdfs:range` values:

   ```yaml
   - name: ownedBy
     # multi-range: rdfs:range [Person, Organization]
     from: Asset
     to: FILL_IN
   ```

The output file will fail `gm validate` until all `FILL_IN` markers are replaced with valid values. This is intentional — the importer never silently guesses.

#### Drop annotations

OWL features that don't have a direct ontology equivalent are preserved rather than silently discarded:

- **Structured annotations** (machine-readable): equivalence (`owl:equivalentClass`), disjointness (`owl:disjointWith`), inverse relationships (`owl:inverseOf`), and property characteristics (`owl:characteristics: [Transitive, Symmetric]`) are stored in the entity or relationship's `annotations` map.

  ```yaml
  - name: Person
    extends: Party
    annotations:
      owl:disjointWith: Organization
  ```

- **YAML comments**: restrictions (`someValuesFrom`, `allValuesFrom`, cardinality constraints) and class expressions (`unionOf`, `intersectionOf`) are written as comments above the affected element.

The drop summary printed to stderr provides counts per category so you can quickly see what was preserved as annotations versus what was dropped entirely.

---

### Scaffolding a New Project

If you are starting a greenfield project with no existing BigQuery tables, `gm scaffold` can generate starter `CREATE TABLE` DDL and a matching binding from your ontology:

```bash
gm scaffold \
  --ontology finance.ontology.yaml \
  --dataset my_dataset \
  --out ./graph/
```

This writes two files into the `--out` directory:

- **`table_ddl.sql`** -- `CREATE TABLE` statements for every entity and relationship. Entity tables get a `PRIMARY KEY ... NOT ENFORCED` clause; relationship tables get `FOREIGN KEY` references to their endpoint entity tables.
- **`binding.yaml`** -- a binding stub that maps every non-derived ontology property to the generated column name. This file is immediately valid as input to `gm compile`.

Scaffold is a **one-shot generator**. Its outputs are user-owned: commit them to version control and edit freely. Scaffold will not re-run against an existing output directory (it refuses to write into a non-empty `--out`).

#### Naming

By default (`--naming snake`), scaffold converts PascalCase and camelCase identifiers to snake_case:

| Ontology name | Table/column name |
|---------------|-------------------|
| `Person` | `person` |
| `firstName` | `first_name` |
| `HTTPRequest` | `http_request` |

Use `--naming preserve` to keep identifiers verbatim.

#### Typical greenfield workflow

```bash
# 1. Generate tables and binding from the ontology
gm scaffold --ontology finance.ontology.yaml --dataset my_dataset --out ./graph/

# 2. Create the tables in BigQuery
bq query --use_legacy_sql=false < ./graph/table_ddl.sql

# 3. Edit the binding or DDL as needed (add partitioning, clustering, etc.)

# 4. Compile the property graph DDL
gm compile ./graph/binding.yaml -o ./graph/graph_ddl.sql

# 5. Create the property graph
bq query --use_legacy_sql=false < ./graph/graph_ddl.sql
```

---

### End-to-End Walkthrough

Here is a complete example from YAML to DDL using a finance domain.

#### 1. Define the ontology (`finance.ontology.yaml`)

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

#### 2. Create the binding (`finance.binding.yaml`)

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

#### 3. Validate and compile

```bash
gm validate finance.ontology.yaml
gm validate finance.binding.yaml
gm compile finance.binding.yaml -o graph_ddl.sql
```

#### 4. Generated DDL

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

Notice how:
- Node tables are sorted alphabetically (Account, Person, Security).
- The `full_name` derived property becomes `(given_name || ' ' || family_name) AS full_name` — the compiler substituted bound column names into the expression.
- The HOLDS edge KEY includes the endpoint columns plus the `additional` key column: `(account_id, security_id, snapshot_date)`.
- Properties where the column matches the logical name (like `person_id` in the golden test) render as bare names; renames render as `column AS name`.

---

## Part 2: Reference

### Ontology YAML Schema

#### Top-level fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `ontology` | string | yes | — | Ontology identifier. Bindings reference this name. |
| `version` | string | no | — | Version string. Unquoted numbers (e.g. `0.1`) are coerced to strings. |
| `entities` | list\<Entity\> | yes | — | At least one entity required. |
| `relationships` | list\<Relationship\> | no | `[]` | Edge type definitions. |
| `description` | string | no | — | Human-readable description. |
| `synonyms` | list\<string\> | no | — | Alternative names. |
| `annotations` | map\<string, string \| list\<string\>\> | no | — | Free-form metadata. |

Unknown keys at any level are rejected.

#### Entity fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Unique within the ontology (disjoint from relationship names). |
| `extends` | string | no | — | Parent entity name. Under development — not yet supported by the compiler. |
| `keys` | Keys | yes | — | Primary key declaration. |
| `properties` | list\<Property\> | no | `[]` | Typed attributes. |
| `description` | string | no | — | Human-readable description. |
| `synonyms` | list\<string\> | no | — | Alternative names. |
| `annotations` | map | no | — | Free-form metadata. |

#### Relationship fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Unique within the ontology (disjoint from entity names). |
| `extends` | string | no | — | Parent relationship name. Under development — not yet supported by the compiler. |
| `keys` | Keys | no | — | See key modes below. |
| `from` | string | yes | — | Source entity name. |
| `to` | string | yes | — | Target entity name. |
| `cardinality` | enum | no | unconstrained | `one_to_one`, `one_to_many`, `many_to_one`, `many_to_many` |
| `properties` | list\<Property\> | no | `[]` | Typed attributes. |
| `description` | string | no | — | Human-readable description. |
| `synonyms` | list\<string\> | no | — | Alternative names. |
| `annotations` | map | no | — | Free-form metadata. |

#### Property fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Unique within the parent entity or relationship. |
| `type` | PropertyType | yes | — | See property types table. |
| `expr` | string | no | — | BigQuery SQL expression. Marks the property as derived. |
| `description` | string | no | — | Human-readable description. |
| `synonyms` | list\<string\> | no | — | Alternative names. |
| `annotations` | map | no | — | Free-form metadata. |

#### Property types

| Type | Description |
|------|-------------|
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

#### Keys fields

| Field | Type | Context | Description |
|-------|------|---------|-------------|
| `primary` | list\<string\> | entities (required), relationships (optional) | Row identity. Each entry must name a declared property. |
| `alternate` | list\<list\<string\>\> | both (requires `primary`) | Additional unique tuples. No duplicates with primary or each other. |
| `additional` | list\<string\> | relationships only | Uniqueness within endpoint pair. Mutually exclusive with `primary`. Cannot combine with `alternate`. |

All lists must be non-empty when present. Empty lists (e.g. `primary: []`) are rejected at parse time.

When no keys are declared on a relationship, the compiled DDL defaults to using the endpoint columns (`from_columns + to_columns`) as the KEY, limiting each endpoint pair to one edge.

---

### Binding YAML Schema

#### Top-level fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `binding` | string | yes | — | Binding identifier. |
| `ontology` | string | yes | — | Name of the ontology to realize (not a file path). |
| `target` | Target | yes | — | Deployment target. |
| `entities` | list\<EntityBinding\> | no | `[]` | Entity-to-table mappings. |
| `relationships` | list\<RelationshipBinding\> | no | `[]` | Relationship-to-table mappings. |

#### Target fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `backend` | enum | yes | Only `bigquery` today. |
| `project` | string | yes | GCP project ID. |
| `dataset` | string | yes | BigQuery dataset. |

#### EntityBinding fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Must match a declared entity in the ontology. |
| `source` | string | yes | BigQuery table or view reference. |
| `properties` | list\<PropertyBinding\> | yes | Column mappings. |

#### RelationshipBinding fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Must match a declared relationship in the ontology. |
| `source` | string | yes | — | BigQuery edge table reference. |
| `from_columns` | list\<string\> | yes | — | Columns carrying source entity key. Arity must match source entity PK. |
| `to_columns` | list\<string\> | yes | — | Columns carrying target entity key. Arity must match target entity PK. |
| `properties` | list\<PropertyBinding\> | no | `[]` | Column mappings. |

#### PropertyBinding fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Ontology property name. Must not be a derived property. |
| `column` | string | yes | Physical column name in the source table. |

---

### CLI Reference

#### `gm validate`

```
gm validate <file> [--json] [--ontology PATH]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `file` | yes | — | Path to an ontology or binding YAML file. Kind is auto-detected. |
| `--json` | no | false | Emit structured JSON errors on stderr. |
| `--ontology PATH` | no | auto | Companion ontology path. Default: `<ontology_name>.ontology.yaml` next to the binding. |

Success: no output, exit 0. See [exit codes](#exit-codes) for failure codes.

#### `gm compile`

```
gm compile <file> [--ontology PATH] [-o PATH] [--json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `file` | yes | — | Path to a binding YAML file. Ontology files are rejected. |
| `--ontology PATH` | no | auto | Companion ontology path. |
| `-o` / `--output PATH` | no | stdout | Write DDL to file (overwritten if exists). |
| `--json` | no | false | Emit structured JSON errors on stderr. |

Validates both files before compiling. Any validation error prevents DDL output.

#### `gm import-owl`

```
gm import-owl <source>... --include-namespace <iri>... [-o <out>] [--format ttl|rdfxml] [--json]
```

| Argument/Option | Required | Default | Description |
|-----------------|----------|---------|-------------|
| `source` | yes | — | One or more OWL source files (Turtle `.ttl` or RDF/XML `.owl`/`.rdf`). |
| `--include-namespace <iri>` | yes | — | IRI namespace prefix to include. Repeatable. |
| `-o` / `--out <file>` | no | stdout | Write ontology YAML to file. |
| `--format` | no | auto | Parser override: `ttl` or `rdfxml`. Default is inferred from file extension. |
| `--json` | no | false | Emit structured JSON errors on stderr. |

Output YAML may contain `FILL_IN` placeholders that must be resolved before `gm validate` will pass. A drop summary of excluded and unsupported OWL features is always printed to stderr (not affected by `--json`).

Requires `rdflib`. Install with `pip install 'bigquery-agent-analytics[owl]'`.

#### `gm scaffold`

```
gm scaffold --ontology PATH --dataset NAME --out DIR [--naming {snake|preserve}] [--project ID] [--json]
```

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--ontology PATH` | yes | — | Path to the ontology YAML file. |
| `--dataset NAME` | yes | — | BigQuery dataset name for generated tables. |
| `--out DIR` | yes | — | Output directory. Must not exist or must be empty. |
| `--naming` | no | `snake` | `snake` converts to snake_case; `preserve` keeps ontology names verbatim. |
| `--project ID` | no | omitted | BigQuery project ID. When set, table names are project-qualified. |
| `--json` | no | false | Emit structured JSON errors on stderr. |

Writes `table_ddl.sql` and `binding.yaml` to the output directory. Refuses to overwrite a non-empty directory.

#### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Validation or compilation error (user-fixable) |
| 2 | Usage error (missing file, wrong file kind, missing companion ontology, missing dependency) |
| 3 | Internal error |

#### Error format

**Human-readable (default):**

```
<file>:<line>:<col>: <rule> — <message>
```

**JSON (`--json`):**

```json
[
  {
    "file": "<path>",
    "line": 0,
    "col": 0,
    "rule": "<rule-code>",
    "severity": "error",
    "message": "<message>"
  }
]
```

#### Error rule codes

| Rule | Source |
|------|--------|
| `ontology-shape:<type>` | Pydantic shape validation on ontology |
| `ontology-validation` | Semantic validation on ontology |
| `binding-shape:<type>` | Pydantic shape validation on binding |
| `binding-validation` | Semantic validation on binding |
| `compile-validation` | Compile-time validation (e.g. `extends` not supported in v0) |
| `yaml-parse` | YAML syntax error |
| `cli-missing-file` | File not found or not readable |
| `cli-missing-ontology` | Companion ontology not found or not readable |
| `cli-unknown-kind` | File is neither ontology nor binding |
| `cli-wrong-kind` | Compile invoked on a non-binding file |
| `cli-output-error` | Cannot write output file |
| `cli-missing-dependency` | Required optional dependency not installed (e.g. rdflib for OWL import) |
| `cli-usage` | Invalid flag value (e.g. bad `--naming`, bad `--format`) |
| `cli-non-empty-dir` | Scaffold output directory is not empty |
| `import-validation` | Semantic error during OWL import (e.g. name collision) |
| `scaffold-validation` | Scaffold-time validation (e.g. `extends`, endpoint-column collision) |

---

### Ontology Validation Rules

The ontology loader enforces 14 cross-element semantic rules beyond per-field shape validation:

1. Entity names are unique within the ontology.
2. Relationship names are unique within the ontology.
3. Entity and relationship names are disjoint — no name can be used by both.
4. Property names are unique within their parent entity or relationship.
5. `extends` must reference a declared same-kind element.
6. No cycles in `extends` chains.
7. Redeclaring an inherited property (by name) is an error.
8. Redeclaring inherited keys is an error.
9. Every key column must reference a declared property (including inherited ones).
10. Alternate keys must be non-empty, have no duplicate columns, and not duplicate another key.
11. On entities: `primary` is required (directly or inherited), `additional` is forbidden.
12. On relationships: `primary` and `additional` are mutually exclusive.
13. Relationship endpoints must reference declared entities, and child relationships must covariantly narrow parent endpoints.
14. A child relationship may not redefine the parent's cardinality.

### Binding Validation Rules

The binding loader enforces these semantic rules against the paired ontology:

1. The binding's `ontology` field must match the ontology's `ontology` field.
2. Entity and relationship binding names are unique within the binding and across kinds.
3. Every bound name must reference a declared element in the ontology.
4. Total property coverage within each bound element: all non-derived properties bound, no derived properties bound, no duplicates.
5. Relationship `from_columns` arity matches the source entity's primary key arity.
6. Relationship `to_columns` arity matches the target entity's primary key arity.
7. Both endpoints of a bound relationship must have at least one bound descendant entity in the binding (no dangling edges).

---

### Compilation Details

#### Pipeline

The compiler (`compile_graph`) runs in two stages:

**Stage 1 — Resolve.** Cross-references the ontology and binding to produce a `ResolvedGraph`:
- Rejects `extends` (v0 does not lower inheritance).
- Indexes ontology elements and bindings by name.
- Resolves node tables: entity name becomes the alias, binding supplies source and column mappings.
- Resolves edge tables: wires endpoints to node-table aliases and key columns.
- Substitutes derived expressions with physical column names (recursive, with cycle detection).
- Sorts node and edge tables alphabetically by alias for deterministic output.

**Stage 2 — Emit.** Walks the `ResolvedGraph` and renders DDL text:
- Properties are emitted in ontology declaration order (not sorted).
- Property lists that fit within 80 columns render inline; longer lists wrap to one property per line.
- Output is deterministic: same inputs always produce byte-identical DDL.

#### Derived expression substitution

The compiler replaces property names in `expr` with their bound column names. If a derived property references another derived property, the substitution is recursive, and cycles are detected:

```
expr: "first_name || ' ' || last_name"
        ↓ first_name bound to given_name
        ↓ last_name bound to family_name
sql:  "given_name || ' ' || family_name"
```

Nested derived references are parenthesized: if property `A` has `expr: "B + 1"` and `B` has `expr: "C * 2"`, the result for A is `((bound_C * 2) + 1)`.

#### Edge key resolution

Every edge table gets a `KEY (...)` clause. Three mutually exclusive cases:

| Ontology keys | DDL KEY columns | Effect |
|--------------|-----------------|--------|
| `primary: [cols]` | Bound primary columns | Edge has standalone identity |
| `additional: [cols]` | `from_columns` + `to_columns` + bound additional columns | Multiple edges per endpoint pair, discriminated by additional columns |
| No keys | `from_columns` + `to_columns` | One edge per endpoint pair (endpoint pair is the identity) |

#### Property rendering in DDL

| Case | Rendered as |
|------|-------------|
| Column matches logical name | `column_name` |
| Column differs from logical name | `column_name AS logical_name` |
| Derived property | `(expression) AS logical_name` |

#### Output structure

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

A BigQuery property graph is a logical overlay on existing tables — it does not copy or move data. Once created, the graph can be queried with GQL using `GRAPH_TABLE` or `MATCH` syntax.
