# OWL Import — Core Design (v0)

Status: draft
Scope: converting OWL source ontologies into our `*.ontology.yaml` format
(see `ontology.md`). The importer produces ontology files; bindings are
user-authored separately.

## 1. Goals

- **Faithful.** Preserve as much of the source OWL structure as fits our
  model. Do not silently lose subclass relationships or property
  declarations.
- **No silent drops.** OWL features we cannot map (restrictions,
  equivalence axioms, property characteristics, etc.) are recorded on
  the affected entity or relationship. Simple scalar drops go into
  `annotations` (machine-readable); structured drops that don't fit a
  string value go into YAML comments. The importer also prints a
  summary.
- **Deterministic.** Same input → byte-identical output.
- **User-resolvable ambiguities.** When OWL expresses something our model
  does not (multi-parent subclasses, multi-range properties, missing
  keys), emit a placeholder with an inline comment in the output file
  for the user to resolve rather than silently pick.
- **Output validates.** The emitted ontology.yaml must pass `ontology.md`
  validation. Placeholders that require user input cause a clear,
  actionable validation failure.

## 2. Pipeline

```
OWL file(s)  ──► Parser ──► Triples ──► Filter ──► Mapper ──► ontology.yaml
                                          ▲
                                          │
                                    namespaces
```

Stages:

1. **Parse.** Read OWL source (Turtle or RDF/XML) into an RDF triple store.
2. **Filter.** Keep only triples whose subject IRI matches a user-provided
   namespace list. Follow `owl:imports` to fetch dependencies, but still
   filter by namespace.
3. **Map.** Apply the §5 mapping table to produce entities, relationships,
   and properties. When the source is ambiguous or under-specified, emit
   a placeholder sentinel with an inline YAML comment; see §11.
4. **Emit.** Write ontology.yaml in canonical order (§14).

Resolution happens **in the output file** by the user editing
placeholders — there is no separate hints file.

## 3. Input formats

- **Turtle** (`.ttl`) — primary.
- **RDF/XML** (`.owl`, `.rdf`) — supported; most ontologies ship at least
  one RDF/XML serialization.

Both parse to the same triple store; everything downstream is
format-agnostic. JSON-LD, N3, and N-Triples are deferred until demand
appears.

## 4. Namespace filtering

The importer requires at least one namespace IRI prefix. Only RDF
resources whose IRIs start with an included prefix are mapped into the
output ontology. Everything else — including classes and properties
reached via `owl:imports` — is excluded.

Example: given `--include-namespace https://spec.edmcouncil.org/fibo/ontology/FBC/`,
a FIBO source file imports hundreds of vocabularies; only FBC-namespace
classes and properties are mapped.

Multiple namespaces may be included. No wildcard matching in v0.

### Exclusions are reported, not silent

Namespace filtering is an explicit user choice, but its consequences are
still surfaced:

- **Importer output summary** lists counts of classes and properties
  excluded per namespace. This lets users verify the filter matches
  intent (e.g., catch typos in namespace IRIs).
- **Cross-boundary references from kept entities** are recorded on the
  kept entity. If kept `Person` has `rdfs:subClassOf :upper#Agent`
  where `:upper#` is outside the filter, the `extends` field is not
  set, and an annotation records the severed link:

  ```yaml
  - name: Person
    description: Person
    annotations:
      owl:subClassOf_excluded: https://example.com/upper#Agent
  ```

  Similarly for property domains/ranges that point outside the
  filter (`owl:domain_excluded`, `owl:range_excluded`).

The rule is the same as §13: simple scalar drops become annotations,
structured drops become comments. Filtering is just a different reason
for dropping.

## 5. Mapping table

| OWL construct | Our ontology (`ontology.md`) |
|---|---|
| `owl:Class` | Entity |
| `owl:DatatypeProperty` with `rdfs:domain C`, `rdfs:range xsd:T` | Property on entity `C` with `type: T` |
| `owl:ObjectProperty` with `rdfs:domain A`, `rdfs:range B` | Relationship `from: A, to: B` |
| `rdfs:subClassOf` (single parent) | `extends` on entity |
| `rdfs:subPropertyOf` (single parent) | `extends` on relationship |
| `owl:hasKey` | `keys.primary` |
| `rdfs:label` | `description` (first) or appended to `synonyms` |
| `rdfs:comment` | `description` (if no label, or appended) |
| `owl:FunctionalProperty` | Relationship `cardinality: many_to_one` (object prop); ignored for datatype prop |
| `skos:Concept` / `skos:broader` / `skos:definition` / etc. | See §19 |

Everything else is dropped; see §13.

## 6. Type mapping

XSD datatypes → our 11 types (`ontology.md` §7):

| XSD | Ontology |
|---|---|
| `xsd:string`, `xsd:normalizedString`, `xsd:token` | `string` |
| `xsd:hexBinary`, `xsd:base64Binary` | `bytes` |
| `xsd:integer`, `xsd:int`, `xsd:long`, `xsd:short`, `xsd:byte`, `xsd:unsignedInt`, `xsd:unsignedLong`, `xsd:unsignedShort`, `xsd:nonNegativeInteger`, `xsd:positiveInteger`, `xsd:nonPositiveInteger`, `xsd:negativeInteger` | `integer` |
| `xsd:double`, `xsd:float` | `double` |
| `xsd:decimal` | `numeric` |
| `xsd:boolean` | `boolean` |
| `xsd:date` | `date` |
| `xsd:time` | `time` |
| `xsd:dateTime`, `xsd:dateTimeStamp` | `timestamp` |
| `rdf:JSON` | `json` |
| `xsd:anyURI` | `string` (with annotation `xsd_type: anyURI`) |

Unknown or unmapped XSD types produce a warning and default to `string`.

## 7. Inheritance

- **Single `rdfs:subClassOf` parent** → `extends` on the entity. Direct.
- **Multiple `rdfs:subClassOf` parents** → emit `extends: FILL_IN` plus
  a YAML comment listing the candidates. User edits to pick one.
- **Single `rdfs:subPropertyOf` parent** → `extends` on the relationship.
- **Multiple `rdfs:subPropertyOf` parents** → same as above.

The importer preserves `extends` faithfully. The output ontology may use
inheritance that the v0 compiler (see `compilation.md`) does not yet
lower; that is a compiler concern, not an importer concern.

## 8. Domain and range

OWL allows a property to have multiple `rdfs:domain` or `rdfs:range`
values, interpreted as "the intersection of these."

- **Single domain and range** → direct mapping.
- **Multiple domain or range** → emit `from: FILL_IN` or `to: FILL_IN`
  (or `type: FILL_IN` for datatype properties) plus a YAML comment
  listing the candidates. User edits to pick one.

## 9. Annotations

- `rdfs:label` in the selected language (see §19.5, default `en`) →
  `description`. Additional selected-language labels are appended to
  `synonyms`; untagged labels follow as a tiebreaker.
- `rdfs:label` in **other** languages → annotations keyed
  `rdfs:label@<lang>` (one entry per language tag).
- `rdfs:comment` → appended to `description` with a blank-line separator
  when a label is also present.
- SKOS labels (`skos:prefLabel`, `skos:altLabel`, `skos:hiddenLabel`)
  and literal-valued SKOS predicates: see §19.
- Custom annotation properties outside the recognized set (RDF, RDFS,
  OWL, SKOS) → collected as `annotations: { <local_name>: <value> }`
  when the value is a literal. Multiple values for the same predicate
  merge into a sorted list. Non-literal values are dropped.

## 10. Primary keys

- **Class has `owl:hasKey`** → the listed properties become
  `keys.primary` in declaration order.
- **Class has no `owl:hasKey`** → the importer emits
  `keys: { primary: [FILL_IN] }` as a placeholder, with a YAML comment
  explaining. The output fails `ontology.md` validation rule 11 until
  the user edits the placeholder.

This placeholder is intentional: silently guessing a key is worse than
making the user pick, because keys drive binding column mapping and
substitutability.

## 11. Placeholders and in-file resolution

When the importer cannot produce a valid answer from the OWL source
alone, it emits a `FILL_IN` sentinel value at the ambiguous site, plus a
YAML comment explaining the decision. The user resolves each by editing
the file directly.

Three places placeholders appear:

- **Missing primary key** (§10). Value: `FILL_IN`. Comment: source had
  no `owl:hasKey`; list data properties on the class as hints.
- **Multi-parent `extends`** (§7). Value: `FILL_IN`. Comment: lists all
  declared parents.
- **Multi-domain or multi-range property** (§8). Value: `FILL_IN`.
  Comment: lists all declared candidates.

Example emitted fragment:

```yaml
- name: Account
  # no owl:hasKey in OWL source
  # candidate data properties: account_id, external_ref
  keys:
    primary: [FILL_IN]

- name: JointAccount
  # multi-parent: rdfs:subClassOf [Account, Organization]
  extends: FILL_IN
```

Rules:

- `FILL_IN` is a reserved string. Any occurrence in the emitted output
  fails `ontology.md` validation.
- The importer never silently picks. If the source is ambiguous, a
  placeholder is emitted.
- Users resolve by replacing `FILL_IN` with a valid value. Comments can
  be deleted or kept — the loader ignores YAML comments.

## 12. Naming policy

Local names in the emitted ontology are derived from the IRI:

- If the IRI ends with `#<fragment>`, use the fragment.
- Else use the last path segment.
- Strip or rewrite characters not allowed in our names (deferred — see
  §16).

Name collisions across the included namespaces are an error. Users
resolve by narrowing the namespace filter; an in-file override
mechanism is a future concern (see §16).

## 13. What gets dropped

OWL features the importer cannot map to our model:

- **Class expressions.** `owl:unionOf`, `owl:intersectionOf`,
  `owl:complementOf`, `owl:oneOf`.
- **Restrictions.** `owl:someValuesFrom`, `owl:allValuesFrom`,
  `owl:minCardinality`, `owl:maxCardinality`,
  `owl:qualifiedCardinality`, `owl:hasValue`.
- **Equivalence and disjointness.** `owl:equivalentClass`,
  `owl:equivalentProperty`, `owl:disjointWith`,
  `owl:AllDisjointClasses`, `owl:sameAs`.
- **Property characteristics** beyond `owl:FunctionalProperty`:
  `owl:InverseFunctionalProperty`, `owl:TransitiveProperty`,
  `owl:SymmetricProperty`, `owl:AsymmetricProperty`,
  `owl:ReflexiveProperty`, `owl:IrreflexiveProperty`.
- **`owl:inverseOf`.** See §16.
- **Anonymous classes / blank nodes.**
- **Individuals / ABox triples.**
- **Punning** (same IRI as class and instance).

### How drops are surfaced

**Preferred: structured annotations.** Simple drops go into the
entity or relationship's `annotations:` map, where they are
machine-readable and can be round-tripped or queried. Values are
strings for single OWL values, or lists of strings when an OWL property
has multiple values (e.g., a class with two `owl:equivalentClass`
targets):

| Dropped OWL construct | Annotation key | Value |
|---|---|---|
| `owl:equivalentClass` | `owl:equivalentClass` | target name, or list |
| `owl:equivalentProperty` | `owl:equivalentProperty` | target name, or list |
| `owl:disjointWith` | `owl:disjointWith` | target name, or list |
| `owl:sameAs` | `owl:sameAs` | target name, or list |
| `owl:inverseOf` | `owl:inverseOf` | target name |
| `owl:TransitiveProperty`, `owl:SymmetricProperty`, etc. | `owl:characteristics` | list of flags, e.g. `[Transitive, Symmetric]` |
| `owl:InverseFunctionalProperty` | `owl:characteristics` | includes `InverseFunctional` |

```yaml
- name: Person
  description: Person
  extends: Party
  annotations:
    owl:disjointWith: [Organization, Trust]
    owl:equivalentClass: NaturalPerson

relationships:
  - name: heldBy
    from: Account
    to: Party
    annotations:
      owl:inverseOf: holds
      owl:characteristics: [Transitive]
```

Emit a scalar when the OWL source has exactly one value and a list when
it has more than one. The loader accepts both (see `ontology.md` §3).

**Fallback: YAML comments.** Drops that don't fit a string or list-of-
strings value go into a comment above the entity or relationship:

- **Restrictions** (`someValuesFrom`, `allValuesFrom`, `minCardinality`,
  `maxCardinality`, `qualifiedCardinality`, `hasValue`).
- **Class expressions** (`unionOf`, `intersectionOf`, `complementOf`,
  `oneOf`).
- **Anonymous classes / blank nodes** referenced from an otherwise
  mapped class.

```yaml
- name: Person
  # restriction on age: minInclusive 0, maxExclusive 150
  # unionOf: Person, Organization, Trust
  description: Person
  ...
```

**Importer output summary.** Counts per drop category plus a pointer to
each site. CI can check counts; users get a quick overview without
opening the ontology.

Drops not tied to a specific entity (individuals, punning, orphan
blank nodes) appear only in the importer output summary.

## 14. Determinism

- Entities sorted alphabetically by local name.
- Relationships sorted alphabetically by local name.
- Properties within an entity or relationship sorted alphabetically.
- Synonyms sorted alphabetically.

## 15. Validation

The importer validates its own output:

1. Every entity has `keys.primary` (§10 placeholder fails this; user
   must fix).
2. No multi-parent `extends` unresolved (§7).
3. No multi-domain or multi-range properties unresolved (§8).
4. Name collisions resolved (§12).
5. The produced file parses as a valid ontology per `ontology.md` §10.

Failures block output; the importer prints a structured list of
actionable issues.

## 16. Open questions

- **`owl:inverseOf`.** Should the importer synthesize a paired inverse
  relationship (two relationships sharing a source edge, one forward, one
  back)? Currently dropped. Revisit after first real-ontology use.
- **Re-import workflow.** Re-running the importer overwrites user edits
  (including `FILL_IN` resolutions). For v0 the flow is one-shot: import,
  edit, commit. If OWL sources evolve and need re-importing, the user
  re-imports into a fresh file and reconciles via git. A future
  `--merge` mode could preserve user edits, but it is not in v0.
- **Name overrides.** Handling very long or awkward local names
  (CamelCase collisions, reserved keywords). Currently out of scope; an
  inline override mechanism (e.g. a YAML annotation carrying the source
  IRI) could be added later.
- **Profile filtering.** Allow the user to restrict import to OWL EL,
  QL, or RL subsets. Not in v0; most importers don't need it.
- **Identifier escaping policy.** Characters in IRIs that aren't valid
  in our `name` field (`.`, `-`, leading numerics). Current plan: fail
  the import with a clear message pointing at the offending IRI.
- **`owl:imports` fetch policy.** Follow remote imports over HTTP, or
  only local filesystem? Network access raises reproducibility concerns.
- **`skos:ConceptScheme` as ontology-level metadata.** Currently
  dropped. A future extension could surface it under the ontology's
  top-level `annotations`.
- **Promoting SKOS to structural semantics.** `skos:broader` is
  informational by default (§19). An opt-in flag to treat it as
  subsumption was considered and deferred; revisit if real users
  request it.

## 17. Out of scope

- **CLI surface.** Command name, flags, output path conventions — a
  separate doc.
- **Other importers.** Schema.org, FHIR, OpenAPI each get their own
  design doc.
- **Bindings.** The importer produces ontology only. Physical bindings
  are authored separately by users familiar with the target backend.
- **Export back to OWL.** Round-trip is not a goal; see
  `relationship-to-standards.md` OWL section for the lossy gaps.
- **Reasoning over imported ontologies.** We import structure, not
  entailments.
- **Diffing imports across source versions.** A separate concern.

## 18. Worked example

### OWL source (Turtle)

```turtle
@prefix : <https://example.com/finance#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:Party  a owl:Class ; rdfs:label "Party" ;
    owl:hasKey ( :party_id ) .

:Person  a owl:Class ;
    rdfs:subClassOf :Party ;
    rdfs:label "Person" ;
    owl:disjointWith :Organization .

:Organization  a owl:Class ;
    rdfs:subClassOf :Party ;
    rdfs:label "Organization" .

:party_id  a owl:DatatypeProperty ;
    rdfs:domain :Party ;
    rdfs:range xsd:string .

:name  a owl:DatatypeProperty ;
    rdfs:domain :Party ;
    rdfs:range xsd:string .

:Account  a owl:Class ;
    rdfs:label "Account" .

:heldBy  a owl:ObjectProperty, owl:TransitiveProperty ;
    owl:inverseOf :holds ;
    rdfs:domain :Account ;
    rdfs:range :Party .
```

### Emitted ontology.yaml (with placeholder and drop annotations)

- `Account` has no `owl:hasKey` → `FILL_IN` placeholder (§10).
- Person's `owl:disjointWith Organization` → `annotations`.
- `heldBy`'s `owl:TransitiveProperty` → `annotations.owl:characteristics`.
- `heldBy`'s `owl:inverseOf :holds` → `annotations.owl:inverseOf`.

```yaml
ontology: finance

entities:
  - name: Account
    description: Account
    # no owl:hasKey in OWL source — pick a primary-key property
    keys:
      primary: [FILL_IN]

  - name: Organization
    description: Organization
    extends: Party

  - name: Party
    description: Party
    keys:
      primary: [party_id]
    properties:
      - name: name
        type: string
      - name: party_id
        type: string

  - name: Person
    description: Person
    extends: Party
    annotations:
      owl:disjointWith: Organization

relationships:
  - name: heldBy
    from: Account
    to: Party
    annotations:
      owl:inverseOf: holds
      owl:characteristics: [Transitive]
```

Notes: no YAML comments needed in this example because every drop fit
as an annotation value. Comments would appear if the source had
restrictions, class expressions, or blank-node references.

Running `ontology.md` validation on this file fails rule 11 (`Account`
has no primary key). The user edits the `Account` entry, replacing
`FILL_IN` with a real key (and optionally removing the comment):

```yaml
  - name: Account
    description: Account
    keys:
      primary: [account_id]
    properties:
      - name: account_id
        type: string
```

Notes on this output:
- `Organization` and `Person` inherit Party's key via `extends`; the
  importer does not need a placeholder for them.
- Alphabetical ordering: `Account`, `Organization`, `Party`, `Person`.
- This output uses `extends`, so the v0 compiler will reject it; that
  is expected for a faithful import.

## 19. SKOS support

The importer recognizes SKOS constructs alongside OWL. The guiding
principle is **informational by default**: SKOS and OWL make different
kinds of claims, and the importer preserves the distinction.

- **OWL** makes *formal* claims. `rdfs:subClassOf` means every
  instance of the child is an instance of the parent — drives
  inheritance, keys, substitutability.
- **SKOS** makes *informational* claims. `skos:broader` means "this is
  a narrower topic" — explicitly *not* subsumption per the W3C SKOS
  Primer. Drives documentation, browsing, search.

Consequences for the importer:

1. OWL constructs flow into structural fields (`extends`, `from`/`to`,
   `keys`, `properties`).
2. SKOS graph-shaped predicates (`skos:broader`, `skos:related`,
   `skos:*Match`) flow into **abstract relationships** — edges
   declared in the ontology but not backed by BigQuery tables.
3. SKOS literal predicates (`skos:definition`, `skos:notation`, etc.)
   flow into **annotations**.
4. Nothing from SKOS flows into `extends` or `description`. The
   ontology never claims inheritance or human-readable descriptions
   that the source author did not assert via OWL/RDFS core vocabulary.

### 19.1 Abstract elements

An element with `abstract: true` is declared in the ontology but not
bound to a BigQuery table. Primary keys are not required on abstract
entities.

Downstream behavior:

- **`gm validate`** accepts abstract entities with no primary key.
  Shape rules for declared keys still apply (see §19.4).
- **`gm scaffold`** skips abstract entities and abstract relationships
  entirely; generated `table_ddl.sql` and `binding.yaml` cover only
  concrete elements.
- **`gm compile`** (via `binding_loader`) rejects any binding that
  targets an abstract entity or abstract relationship with a clear
  error message.

### 19.2 Naming convention

- **Pure SKOS entity** (typed only as `skos:Concept`): name prefixed
  `skos_` (e.g., `skos_Banking`).
- **Mixed OWL+SKOS entity** (typed as both `owl:Class` and
  `skos:Concept`): name unprefixed. OWL provides structure, so it wins
  the name; SKOS contributes annotations and synonyms.
- **SKOS relationship** (always abstract): name prefixed `skos_` (e.g.,
  `skos_broader`, `skos_related`, `skos_exactMatch`).
- **SKOS annotation keys**: use `skos:` colon prefix (e.g.,
  `skos:definition`, `skos:notation`), matching the `owl:` convention.

The split between `skos_` in element names and `skos:` in annotation
keys tracks the identifier/metadata boundary: names flow into YAML
parse keys, BigQuery labels, GQL syntax, and SQL code, where colons
are unsafe or already syntactic. Annotation keys are free-form map
keys where colons parse cleanly.

### 19.3 SKOS mapping table

Entity typing:

| SKOS construct | Ontology equivalent | Notes |
|---|---|---|
| `skos:Concept` (no `owl:Class`) | Abstract entity with `skos_` prefix | Keys not required |
| `owl:Class` + `skos:Concept` | Concrete entity, unprefixed | OWL provides structure |
| `skos:ConceptScheme` | Currently dropped | Not emitted; deferred |

Labels and synonyms (language selection applies — see 17.5):

| SKOS construct | Ontology equivalent | Notes |
|---|---|---|
| `skos:prefLabel` (selected language) | `synonyms` (when different from name) | Label data |
| `skos:altLabel` (selected language) | `synonyms` | Label data |
| `skos:hiddenLabel` (selected language) | `synonyms` | Label data |
| `skos:prefLabel` (other language) | Annotation `skos:prefLabel@<lang>` | Preserved verbatim |
| `skos:altLabel` (other language) | Annotation `skos:altLabel@<lang>` | Preserved verbatim |
| `skos:hiddenLabel` (other language) | Annotation `skos:hiddenLabel@<lang>` | Preserved verbatim |

Literal-valued predicates (all preserved as annotations, colon-prefixed):

| SKOS construct | Ontology equivalent | Notes |
|---|---|---|
| `skos:definition` | Annotation `skos:definition` | Not promoted to description |
| `skos:notation` | Annotation `skos:notation` | |
| `skos:scopeNote` | Annotation `skos:scopeNote` | |
| `skos:example` | Annotation `skos:example` | |
| `skos:historyNote` | Annotation `skos:historyNote` | |
| `skos:editorialNote` | Annotation `skos:editorialNote` | |
| `skos:changeNote` | Annotation `skos:changeNote` | |

Reference predicates (IRI target stored as local name):

| SKOS construct | Ontology equivalent | Notes |
|---|---|---|
| `skos:inScheme` | Annotation `skos:inScheme` | |
| `skos:topConceptOf` | Annotation `skos:topConceptOf` | |

Graph-shaped predicates (abstract relationships):

| SKOS construct | Ontology equivalent | Notes |
|---|---|---|
| `skos:broader` | Abstract relationship `skos_broader` | |
| `skos:narrower` | Abstract relationship `skos_broader` (endpoints swapped) | Normalized to broader |
| `skos:related` | Abstract relationship `skos_related` | Emitted once per subject–object pair |

Match predicates (relationship if target imported, annotation otherwise):

| SKOS construct | Ontology equivalent | Notes |
|---|---|---|
| `skos:exactMatch` | Abstract `skos_exactMatch` / annotation `skos:exactMatch` | |
| `skos:closeMatch` | Abstract `skos_closeMatch` / annotation `skos:closeMatch` | |
| `skos:broadMatch` | Abstract `skos_broadMatch` / annotation `skos:broadMatch` | |
| `skos:narrowMatch` | Abstract `skos_narrowMatch` / annotation `skos:narrowMatch` | |
| `skos:relatedMatch` | Abstract `skos_relatedMatch` / annotation `skos:relatedMatch` | External IRI stored verbatim |

### 19.4 Validation rules for abstract elements

Abstract entities and relationships are legal ontology elements, not
second-class placeholders. Most shape rules still apply:

- **Abstract entities**
  - Primary key not required.
  - If keys are declared, they must follow the same shape rules as
    concrete entities (no `additional`, key columns must reference
    declared properties, alternate-key shape rules).
  - May use `extends` (taxonomy inheritance is allowed).

- **Abstract relationships**
  - Must reference declared entity names in `from` / `to`; endpoint
    existence is enforced.
  - **May point at concrete or abstract entities** in any combination.
  - **Must not use `extends`**. `extends` is a bare name reference,
    but abstract relationship names are not unique on their own — two
    abstract relationships may share a name if their endpoints differ.
    A bare name cannot unambiguously identify the parent, so `extends`
    is forbidden. If keys are declared, they still follow shape rules.
  - Uniqueness is relaxed to `(name, from, to)` instead of `(name,)`
    alone — so multiple `skos_broader` edges with different endpoints
    are legal (see §19.5).

- **Concrete relationships**
  - **Must have concrete endpoints.** A concrete relationship with an
    abstract endpoint is rejected — the binding step has nothing to
    bind on the abstract side.

- **Name collisions**
  - A concrete relationship and an abstract relationship cannot share
    a name, even if endpoints differ.
  - Entity and relationship namespaces remain disjoint: an abstract
    entity cannot share a name with any relationship (concrete or
    abstract), and vice versa.

### 19.5 Relationship uniqueness

Abstract relationships use relaxed uniqueness: `(name, from, to)` must
be unique, rather than `(name)` alone. The relaxation is necessary
because external vocabularies like SKOS use a single predicate name
(`broader`, `related`) across many different node-type pairs. Requiring
name uniqueness alone would force synthetic names
(`skos_broader_1`, `skos_broader_2`, …) that diverge from the source
vocabulary and make the ontology harder to read.

The cost of the relaxation is that `extends` becomes unresolvable for
abstract relationships — a bare name no longer identifies a single
relationship — so `extends` is forbidden on abstract relationships.

Concrete relationships retain strict `(name)` uniqueness. The
relaxation is deliberately narrow: the compiler and binding layer only
operate on concrete elements, so the emitted DDL and GQL surfaces are
unaffected.

### 19.6 Language selection (`--language`)

The `--language` flag (default `en`) selects labels by BCP-47 tag.
The tag is matched by prefix, so `en` covers `en`, `en-US`, `en-GB`.

- Labels matching the selected language populate description and
  synonyms.
- Labels in non-selected languages become language-suffixed
  annotations:
  - `rdfs:label` → `rdfs:label@<lang>`
  - `skos:prefLabel` → `skos:prefLabel@<lang>`
  - `skos:altLabel` → `skos:altLabel@<lang>`
  - `skos:hiddenLabel` → `skos:hiddenLabel@<lang>`
- Untagged labels are treated as a fallback for the selected language.

### 19.7 Generic literal annotations

Unknown literal-valued predicates (not RDF, RDFS, OWL, or SKOS) are
preserved as `annotations: { <local_name>: <value> }`. Multiple values
for the same predicate merge into a sorted list.

This covers Dublin Core (`dc:title`, `dcterms:creator`), Schema.org
literals, and custom annotation properties without special handling.
Full IRI is preserved in the drop summary when the local name is
ambiguous across source vocabularies.

### 19.8 Drop summary additions

The `stderr` drop summary from `gm import-owl` reports, in addition to
the OWL counts:

- `SKOS concepts imported as abstract entities: N`
- `SKOS relationships imported as abstract: N`
- `SKOS predicates mapped to annotations: N`
- `Labels in non-selected languages (preserved as annotations): N`
- `SKOS match targets outside imported namespaces (preserved as annotations): N`
- `Generic literal annotations preserved: N`
- A note when every entity is abstract: *"all entities are abstract
  (SKOS-only). No concrete entities are available for binding.
  Consider representing the taxonomy as dimension columns instead of
  entity types."*

### 19.9 Worked examples

#### Example 1 — Mixed OWL + SKOS

Input:

```turtle
@prefix : <https://example.com/finance#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

:Account a owl:Class, skos:Concept ;
    rdfs:label "Account" ;
    skos:altLabel "Acct"@en ;
    skos:definition "A record of financial transactions."@en ;
    skos:related :Ledger ;
    skos:exactMatch <http://fibo.org/ontology/FBC/Account> ;
    owl:hasKey ( :account_id ) .

:Ledger a owl:Class ;
    rdfs:label "Ledger" ;
    owl:hasKey ( :ledger_id ) .
```

Output:

```yaml
entities:
  - name: Account
    description: Account
    keys:
      primary: [account_id]
    properties:
      - name: account_id
        type: string
    synonyms: [Acct]
    annotations:
      skos:definition: A record of financial transactions.
      skos:exactMatch: "http://fibo.org/ontology/FBC/Account"

  - name: Ledger
    description: Ledger
    keys:
      primary: [ledger_id]
    properties:
      - name: ledger_id
        type: string

relationships:
  - name: skos_related
    abstract: true
    from: Account
    to: Ledger
```

Both entities concrete (OWL built the structure, names unprefixed).
`skos:related` becomes an abstract relationship. `skos:definition` and
the external `skos:exactMatch` IRI become annotations with provenance
preserved.

#### Example 2 — Pure SKOS taxonomy

Input:

```turtle
@prefix : <https://example.com/taxonomy#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

:Banking a skos:Concept ;
    skos:prefLabel "Banking"@en ;
    skos:definition "Activities of financial institutions."@en .

:RetailBanking a skos:Concept ;
    skos:prefLabel "Retail Banking"@en ;
    skos:altLabel "Consumer Banking"@en ;
    skos:broader :Banking ;
    skos:notation "RB" .

:InvestmentBanking a skos:Concept ;
    skos:prefLabel "Investment Banking"@en ;
    skos:broader :Banking .
```

Output:

```yaml
entities:
  - name: skos_Banking
    abstract: true
    synonyms: [Banking]
    annotations:
      skos:definition: Activities of financial institutions.

  - name: skos_InvestmentBanking
    abstract: true
    synonyms: [Investment Banking]

  - name: skos_RetailBanking
    abstract: true
    synonyms: [Consumer Banking, Retail Banking]
    annotations:
      skos:notation: RB

relationships:
  - name: skos_broader
    abstract: true
    from: skos_InvestmentBanking
    to: skos_Banking

  - name: skos_broader
    abstract: true
    from: skos_RetailBanking
    to: skos_Banking
```

All entities are prefixed (pure SKOS, informational), all
relationships are `skos_broader` with different endpoints (legal under
the scoped uniqueness relaxation), and `description` is empty on every
entity because the SKOS source offered no `rdfs:label` or
`rdfs:comment`. The drop summary prints the taxonomy hint.

#### Example 3 — Cross-kind reference

A concrete OWL class pointing to a pure-SKOS concept via `skos:broader`:

```turtle
:Account a owl:Class ;
    rdfs:label "Account" ;
    skos:broader :FinancialProduct ;
    owl:hasKey ( :account_id ) .

:FinancialProduct a skos:Concept ;
    skos:prefLabel "Financial Product"@en .
```

Output:

```yaml
entities:
  - name: Account
    description: Account
    keys:
      primary: [account_id]
    properties:
      - name: account_id
        type: string

  - name: skos_FinancialProduct
    abstract: true
    synonyms: [Financial Product]

relationships:
  - name: skos_broader
    abstract: true
    from: Account
    to: skos_FinancialProduct
```

Concrete entity, abstract entity, abstract relationship. The `skos_`
name prefix travels with the element wherever it is referenced, so
provenance is visible at every reference site.

The inverse case — an OWL `owl:ObjectProperty` whose `rdfs:range` is a
pure SKOS concept — is imported faithfully (the endpoint resolves to
the prefixed name) but validation then rejects it: a concrete
relationship cannot have an abstract endpoint (§19.4). The user must
either make the target a proper `owl:Class` in their source or promote
the OWL property to SKOS.
