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
| `skos:altLabel`, `skos:prefLabel` | `synonyms` |
| `owl:FunctionalProperty` | Relationship `cardinality: many_to_one` (object prop); ignored for datatype prop |

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

- `rdfs:label` (prefLabel, if multiple pick English first, else
  alphabetically first) → `description`.
- `rdfs:comment` → appended to `description` with a blank-line separator
  when a label is also present.
- `skos:altLabel` and additional `rdfs:label`s in other languages →
  `synonyms`.
- Custom annotation properties outside our recognized set → collected as
  `annotations: { <iri>: <value> }` when the value is a literal; dropped
  silently when not.

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
- **Annotation property preservation.** Currently kept as
  `annotations: {iri: value}` for literal values; non-literal
  annotations are dropped. Revisit if ontologies lean heavily on SKOS
  hierarchies or Dublin Core metadata.

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
