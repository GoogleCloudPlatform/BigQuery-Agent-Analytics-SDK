# Ontology — Core Design (v0)

Status: draft
Scope: the logical ontology spec only. Bindings, compilation, deployment,
measures, and multi-file composition are deliberately out of scope for this
document and will each have their own design.

## 1. Goals

- A **logical ontology** in YAML, independent of any physical warehouse.
- One file, no composition.
- Separate logical modeling (this doc) from physical binding (separate doc).

## 2. File shape

One file per ontology. Suffix: `*.ontology.yaml`. Top-level key: `ontology:`.

All YAML in this document uses **block style** (indented, one field per line)
for consistency. YAML's flow style (`{a: 1, b: 2}` for maps, `[x, y]` for
lists) is equivalent and users may use it freely; the two styles parse to
identical data. Block style is used here to give the spec a single canonical
reading.

```yaml
ontology: finance
version: 0.1

entities:
  - name: Party
    keys:
      primary:
        - party_id
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
      - name: first_name
        type: string
      - name: last_name
        type: string
      - name: full_name
        type: string
        expr: "first_name || ' ' || last_name"

  - name: Organization
    extends: Party
    properties:
      - name: tax_id
        type: string

  - name: Account
    keys:
      primary:
        - account_id
    properties:
      - name: account_id
        type: string
      - name: opened_at
        type: timestamp

  - name: Security
    keys:
      primary:
        - security_id
    properties:
      - name: security_id
        type: string

relationships:
  - name: HOLDS
    keys:
      additional:
        - as_of
    from: Account
    to: Security
    cardinality: many_to_many
    properties:
      - name: as_of
        type: timestamp
      - name: quantity
        type: double

  - name: TRANSFER
    keys:
      primary:
        - transaction_id
    from: Account
    to: Account
    properties:
      - name: transaction_id
        type: string
      - name: amount
        type: double
      - name: executed_at
        type: timestamp

  - name: RELATED_TO
    from: Party
    to: Party

description: Party, account, and security model for finance domain.
synonyms:
  - finance-core
```

## 2a. Type overview

Compact YAML signatures. `<T>` is a placeholder; `, ...` means "list of".

```yaml
# Ontology  (top-level)
ontology: <string>                      # required
version: <string>                       # optional
entities: [<Entity>, ...]               # required, non-empty
relationships: [<Relationship>, ...]    # optional
description: <string>                   # optional
synonyms: [<string>, ...]               # optional
annotations: {<string>: <string> | [<string>, ...]}       # optional
```

```yaml
# Entity
name: <string>                          # required, unique in ontology
extends: <string>                       # optional, name of parent entity
keys: <Keys>                            # required
properties: [<Property>, ...]           # optional
description: <string>                   # optional
synonyms: [<string>, ...]               # optional
annotations: {<string>: <string> | [<string>, ...]}       # optional
```

```yaml
# Relationship
name: <string>                          # required, unique in ontology
extends: <string>                       # optional, name of parent relationship
keys: <Keys>                            # optional
from: <string>                          # required, entity name
to: <string>                            # required, entity name
cardinality: one_to_one | one_to_many | many_to_one | many_to_many  # optional
properties: [<Property>, ...]           # optional
description: <string>                   # optional
synonyms: [<string>, ...]               # optional
annotations: {<string>: <string> | [<string>, ...]}       # optional
```

```yaml
# Property
name: <string>                          # required, unique within parent
type: string | bytes | integer | double | numeric | boolean | date | time | datetime | timestamp | json  # required
expr: <string>                          # optional, BigQuery SQL over sibling properties
description: <string>                   # optional
synonyms: [<string>, ...]               # optional
annotations: {<string>: <string> | [<string>, ...]}       # optional
```

```yaml
# Keys
primary: [<string>, ...]                # required on entities; one of primary/additional on relationships
alternate:                              # optional; only valid with primary
  - [<string>, ...]
additional: [<string>, ...]             # relationships only, mode 2: extends (from, to)
```

## 3. Entity

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Unique within the ontology. |
| `extends` | string | no | Name of the parent entity. Single-parent. |
| `keys` | Keys | yes | See §6. |
| `properties` | list\<Property\> | no | See §5. |
| `description` | string | no | Free-form. |
| `synonyms` | list\<string\> | no | Alternate names for search / LLM grounding. |
| `annotations` | map\<string, string \| list\<string\>\> | no | Free-form metadata. Values may be a string or a list of strings. |

## 4. Relationship

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Unique within the ontology. |
| `extends` | string | no | Name of the parent relationship. Single-parent. |
| `keys` | Keys | no | See §6. |
| `from` | string | yes | Source entity name. |
| `to` | string | yes | Target entity name. |
| `cardinality` | enum | no | `one_to_one` \| `one_to_many` \| `many_to_one` \| `many_to_many`. |
| `properties` | list\<Property\> | no | See §5. |
| `description` | string | no | |
| `synonyms` | list\<string\> | no | |
| `annotations` | map\<string, string \| list\<string\>\> | no | |

## 5. Property

A property is an attribute of an entity or relationship.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Unique within its containing entity or relationship. |
| `type` | enum | yes | See §7. |
| `expr` | string | no | BigQuery SQL expression over other properties of the same entity/relationship. See §8. |
| `description` | string | no | |
| `synonyms` | list\<string\> | no | |
| `annotations` | map\<string, string \| list\<string\>\> | no | |

Properties with `expr` are *derived* — their value is computed from other
properties.

## 6. Keys

A single `Keys` shape is used on both entities and relationships. Some
fields are valid only in one context.

```yaml
# Keys
primary: [<string>, ...]                # required on entities; one of primary/additional on relationships
alternate:                              # optional; only valid with primary
  - [<string>, ...]
additional: [<string>, ...]             # relationships only
```

### Rules

- Every name in any list must be a property declared on the same entity
  or relationship.
- `alternate` keys are each non-empty; no duplicates within or across
  primary/alternate.
- **On entities**: `primary` is required; `additional` is not allowed.
- **On relationships**: `primary` XOR `additional` (neither is also
  allowed, meaning no uniqueness constraint — multi-edges permitted).
  `alternate` only applies with `primary`.

### Relationship key modes

- **Mode 1 — standalone primary key.** The edge has its own identity;
  endpoints are foreign keys, not part of the key.

  ```yaml
  keys:
    primary: [transaction_id]
    alternate:
      - [external_ref, source_system]
  ```

- **Mode 2 — endpoint-extended key.** The edge's effective key is
  `(from, to, *additional)`.

  ```yaml
  keys:
    additional: [as_of]
  ```

If a relationship's identity is rich (its own synthetic ID plus outgoing
edges), model it as an entity with two relationships to its endpoints.

## 7. Valid property types

The ontology uses semantic type names that map to the GoogleSQL type system
shared by BigQuery and Spanner.

```
string, bytes, integer, double, numeric,
boolean, date, time, datetime, timestamp, json
```

Any other value is a validation error.

### Backend mapping

| Ontology type | BigQuery | Spanner |
|---|---|---|
| `string` | `STRING` | `STRING` |
| `bytes` | `BYTES` | `BYTES` |
| `integer` | `INT64` | `INT64` |
| `double` | `FLOAT64` | `FLOAT64` |
| `numeric` | `NUMERIC` | `NUMERIC` |
| `boolean` | `BOOL` | `BOOL` |
| `date` | `DATE` | `DATE` |
| `time` | `TIME` | — |
| `datetime` | `DATETIME` | — |
| `timestamp` | `TIMESTAMP` | `TIMESTAMP` |
| `json` | `JSON` | `JSON` |

Backend-unsupported combinations (e.g., `time` on a Spanner target) are
binding/compile-time errors, not ontology-level errors. The ontology stays
backend-neutral.

### Deliberately deferred

- `bignumeric` (BQ only, higher-precision decimal).
- `interval` (portability questions between BQ and Spanner semantics).
- `geography` (BQ only; representation questions).
- Composite types `array<T>` and `struct<...>`: model nested data as separate
  entities plus relationships for v0.
- Parameterized types (e.g., `numeric(38, 9)`, `string(100)`): GoogleSQL
  accepts precision/scale and length, but v0 ignores them.

## 8. Expression dialect

Property `expr` uses **BigQuery Standard SQL** syntax. The expression may
reference other properties declared on the same entity or relationship by name;
it may not reference other entities, relationships, or host columns.

Example:
```yaml
- name: full_name
  type: string
  expr: "first_name || ' ' || last_name"
```

No subqueries, window functions, or aggregates.

## 9. Inheritance

Inheritance applies uniformly to entities and relationships.

| Aspect | Entity | Relationship |
|---|---|---|
| `extends` form | single parent | single parent |
| Multi-inheritance | not supported | not supported |
| Cycles | validation error | validation error |
| Properties | inherited and merged; child adds, redeclaration is an error | same |
| Keys | inherited; child cannot redeclare | same |
| Endpoints | — | covariant narrowing: child `from` must equal or extend parent `from`; same for `to` |
| Cardinality | — | inherited unchanged (narrowing deferred) |
| `description`, `synonyms`, `annotations` | not inherited | not inherited |
| Substitutability in queries | `MATCH (:Parent)` matches all descendants | `MATCH -[:parent]-` matches all descendant edges |

Covariant-narrowing example: parent `memberOf: Party → Organization`;
child `alumni: Person → School` is valid iff `Person extends Party` and
`School extends Organization`.

## 10. Validation rules

Enforced by the ontology loader before any downstream stage runs.

1. Every entity and relationship name is unique within the ontology.
2. Every property name is unique within its entity or relationship.
3. `extends` must resolve to a declared entity (entity-to-entity) or
   relationship (relationship-to-relationship) of the correct kind.
4. No cycles in `extends` chains.
5. Redeclaring an inherited property (by name) is an error.
6. Redeclaring inherited keys is an error.
7. Every name in any `keys` list must be a property declared on the same
   entity or relationship.
8. Alternate keys are non-empty and have no duplicates within or across
   primary/alternate.
9. On relationships, `primary` and `additional` are mutually exclusive.
   On entities, `additional` is not allowed.
10. Relationships with `extends`: `from` / `to` satisfy covariant narrowing
    against the parent's endpoints.
11. Every entity must declare `keys.primary`.
12. Property `type` is one of the valid types (§7).
13. Unknown YAML keys anywhere in the spec are an error (`extra="forbid"`).

## 11. Out of scope / future docs

- **Binding** — mapping logical entities and relationships to physical tables
  and columns, per backend/environment.
- **Compilation** — resolving ontology + binding into emitted DDL, including
  lowering strategies for inheritance substitutability.
- **Measures** — aggregations over entity/relationship properties, reintroduced
  as a separate overlay on top of the ontology.
- **Composition** — includes, excludes, multi-file assembly, multi-domain
  directory layouts, `GraphSpec`-style selectors.

## 12. Open questions

- **Reintroduce `abstract`?** Dropped from v0 because it leaks binding-layer
  semantics into the ontology — an unbound entity in a given environment is
  effectively abstract for that environment, and the binding layer can warn
  on "unbound entity with no bound descendants." Revisit if that check proves
  insufficient in practice.
- **Cardinality narrowing under inheritance.** Currently specified as
  "inherited unchanged." Narrowing (e.g., parent `many_to_many`, child
  `one_to_many`) has obvious modeling value but raises enforcement questions;
  revisit after the binding/compilation design lands.
- **Expression dialect scope.** BigQuery Standard SQL is pragmatic but ties
  the ontology to a specific backend's syntax. Worth revisiting if a second
  backend (Spanner) surfaces real incompatibilities.
