# Binding — Core Design (v0)

Status: draft
Scope: the binding spec only — the YAML format that attaches a logical
ontology (see `ontology.md`) to physical tables and columns on a specific
backend.

## 1. Goals

- **Thin files.** A binding says *where* data lives, not *how* it is
  transformed.
- **One file per target.** One binding file per target, e.g. `(backend, deployment env)` pair. No conditional logic inside a file.
- **Backend-neutral shape.** Entity and relationship binding syntax is
  backend-independent; only the `target:` block differs. Spanner support
  is deferred until the SDK grows Spanner support.
- **Ontology-aware, ontology-unaware.** The binding references ontology
  names and never redeclares logical structure. The ontology file does not
  know bindings exist.

## 2. File shape

One file per target. Suffix: `*.binding.yaml`. Top-level key: `binding:`.
All YAML uses block style; flow style is equivalent.

```yaml
binding: finance-bq-prod
ontology: finance
target:
  backend: bigquery
  project: my-proj
  dataset: finance

entities:
  - name: Person
    source: raw.persons
    properties:
      - name: party_id
        column: person_id
      - name: name
        column: display_name
      - name: dob
        column: date_of_birth
      - name: first_name
        column: given_name
      - name: last_name
        column: family_name
      # full_name is derived (expr in ontology) — not listed here

  - name: Organization
    source: raw.organizations
    properties:
      - name: party_id
        column: org_id
      - name: name
        column: legal_name
      - name: tax_id
        column: ein

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
    from_columns:
      - account_id
    to_columns:
      - security_id
    properties:
      - name: as_of
        column: snapshot_date
      - name: quantity
        column: qty

  - name: TRANSFER
    source: raw.transactions
    from_columns:
      - src_account
    to_columns:
      - dst_account
    properties:
      - name: transaction_id
        column: txn_id
      - name: amount
        column: amount_usd
      - name: executed_at
        column: executed_ts
```

## 2a. Type overview

```yaml
# Binding  (top-level)
binding: <string>                       # required
ontology: <string>                      # required, must match the ontology's `ontology:` field
target: <Target>                        # required
entities: [<EntityBinding>, ...]        # optional
relationships: [<RelationshipBinding>, ...]  # optional
```

```yaml
# Target
backend: bigquery                       # required (spanner deferred)
project: <string>                       # required
dataset: <string>                       # required
```

```yaml
# EntityBinding
name: <string>                          # required, names an entity in the ontology
source: <string>                        # required
properties: [<PropertyBinding>, ...]    # required
```

```yaml
# RelationshipBinding
name: <string>                          # required, names a relationship in the ontology
source: <string>                        # required
from_columns: [<string>, ...]           # required, non-empty, arity matches from-entity primary key
to_columns: [<string>, ...]             # required, non-empty, arity matches to-entity primary key
properties: [<PropertyBinding>, ...]    # optional
```

```yaml
# PropertyBinding
name: <string>                          # required, names a property declared on the entity/relationship
column: <string>                        # required
```

## 3. Target

BigQuery:

```yaml
target:
  backend: bigquery
  project: my-proj
  dataset: finance
```

Source names in entity and relationship bindings resolve relative to the
target: a bare `table` or `dataset.table` uses the target's `project` /
`dataset` as defaults; a fully-qualified `project.dataset.table` overrides
them. Views are valid sources.

## 4. Entity binding

- `name` must name an entity declared in the ontology.
- `source` is the physical table or view. For row filtering (e.g.
  `type = 'customer'`), build a view in the warehouse and bind to it.
- `properties` must list one `PropertyBinding` for every **non-derived**
  ontology property on the entity, including those inherited from parents
  (inheritance is flattened at binding time). Derived properties — those
  with `expr:` in the ontology — **must not** appear; the compiler
  substitutes their referenced property names with bound columns.
- **Primary keys are implicit.** The ontology's `keys.primary` names
  properties; those property bindings supply the physical columns.

## 5. Relationship binding

- `name` must name a relationship declared in the ontology.
- `source` is the physical edge table or view.
- `from_columns` and `to_columns` name the columns in `source` that hold
  the source/target endpoint keys. Their arity must equal the endpoint
  entity's `keys.primary` arity.
- `properties` binds the relationship's own non-derived properties, same
  rules as §4.

How endpoint substitutability (e.g. `HOLDS.from = Account` with both
`Account` and `SavingsAccount` bound) lowers to backend DDL — one edge
table, many edge tables, label-referenced edges, or a union view — is a
compilation concern, out of scope here.

## 6. Derived properties

Properties with `expr:` in the ontology are never listed in the binding.
At DDL emission the compiler substitutes each referenced property name
with its bound column. A reference to a property that is not bound in this
environment is a compile-time error.

## 7. Partial bindings

A binding realizes a **subset** of the ontology. An entity or relationship
may be:

1. **Absent from the binding** — not realized in this target; no DDL emitted.
2. **Listed with a `source`** — realized.

A logical parent may remain unbound while concrete children are bound;
queries against the parent's label resolve against the bound children via
substitutability (lowering is the compilation layer's job).

Constraint: if a relationship is bound, both endpoint entities must have
at least one bound descendant (including themselves) in this binding —
otherwise the edge has nothing to point at.

## 8. Type compatibility with the target

The ontology is backend-neutral; the binding enforces target compatibility.

BigQuery supports all 11 logical types defined in the ontology spec.

No implicit coercion. If the physical column type does not match the
logical property type, fix it upstream (a view, or land the data
correctly).

## 9. Validation rules

1. `binding` and `ontology` are non-empty strings.
2. The ontology named by `ontology:` exists and loads without errors.
3. `target.backend` is supported; backend-specific required fields are
   present.
4. Every `EntityBinding.name` names an entity declared in the ontology,
   and the entity must not be abstract (see `ontology.md` §3a).
5. Every `RelationshipBinding.name` names a relationship declared in the
   ontology, and the relationship must not be abstract.
6. No duplicate entity or relationship names within the binding.
7. For every bound entity, every non-derived property (including
   inherited) has exactly one `PropertyBinding` with a non-empty `column`.
8. No `PropertyBinding` names a derived property.
9. No `PropertyBinding` names a property not declared on the entity or
   relationship (after inheritance flattening).
10. For every bound relationship: `from_columns` length equals the `from`
    entity's `keys.primary` length; same for `to_columns` and `to`.
11. For every bound relationship: the `from` and `to` entities each have
    at least one bound descendant (including themselves).
12. Unknown YAML keys anywhere are a validation error (`extra="forbid"`).

## 10. Relationship to ontology

- Binding references ontology entities and relationships by name only; no
  schema redeclaration.
- The binding's `ontology: <name>` resolves, by default, to
  `<name>.ontology.yaml` in the same directory. A CLI flag can override
  the lookup path.

## 11. Open questions

- **Light casts in bindings.** Narrow `cast: <type>` field (→ `CAST(column
  AS type)`) vs. forcing users to a view. Revisit after first real use.
- **Strict vs. loose property coverage.** Currently strict: every
  non-derived property must be bound if the entity is bound. Loosening
  would allow exposing a subset. Wait for concrete demand.
- **Sources as SQL subqueries.** R2RML allows arbitrary SQL as a logical
  table. We disallow it here to keep transformation out of YAML.
  Reconsider if the rule proves onerous.
- **Multi-target-in-one-file.** Reconsider if users consistently ask for
  it.

## 12. Out of scope

- **Compilation and DDL emission**, including lowering strategies for
  inheritance substitutability (label-referenced edges, fan-out, union
  views).
- **Credentials** — authentication is out-of-band.
- **Transformation logic** — no arbitrary `expr:` in bindings; use views or
  dbt.
- **Composition of bindings** — shared defaults, multi-file assembly.
- **Measures** and **deployment** — separate docs.
