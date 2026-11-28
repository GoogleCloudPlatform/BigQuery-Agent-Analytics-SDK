# Command Line Interface — Core Design (v0)

Status: draft
Scope: the v0 CLI for the `gm` tool. Three commands (`validate`,
`compile`, `import-owl`) plus global conventions for output, errors, and
exit codes, and the later `import-dataplex-glossary` importer (§7).
Out-of-scope items enumerated in §9.

## 1. Goals

- **Minimal surface.** Three commands cover both user workflows end to
  end. `import-dataplex-glossary` (§7) is a second, optional importer:
  it only produces an ontology and adds no new workflow.
- **Composable.** Output is just text, stdout is where results go, exit
  codes are honest. `gm compile … | bq query` works; CI parses errors
  with `--json`.
- **Predictable.** Same inputs → same output. Validation is strict and
  runs implicitly before compilation.

## 2. Workflows

Two starting conditions cover the common cases.

### Condition A: No existing tables (greenfield)

| Step | Action | Command |
|---|---|---|
| 1 | Get an ontology: hand-author `finance.ontology.yaml`, or import from OWL | `gm import-owl fibo.ttl --include-namespace <…> -o finance.ontology.yaml` |
| 2 | Resolve `FILL_IN` placeholders if any | — (text editor) |
| 3 | Check the ontology is valid | `gm validate finance.ontology.yaml` |
| 4 | Design and create warehouse tables | — (external) |
| 5 | Author `finance-bq-prod.binding.yaml` | — (text editor) |
| 6 | Check the binding | `gm validate finance-bq-prod.binding.yaml` |
| 7 | Compile to DDL | `gm compile finance-bq-prod.binding.yaml` |
| 8 | Apply DDL | — (external) |

### Condition B: Existing tables (brownfield)

| Step | Action | Command |
|---|---|---|
| 1 | Inspect existing table schemas | — (external) |
| 2 | Author `finance.ontology.yaml` to describe the tables | — (text editor) |
| 3 | Check the ontology is valid | `gm validate finance.ontology.yaml` |
| 4 | Author `finance-bq-prod.binding.yaml` | — (text editor) |
| 5 | Check the binding | `gm validate finance-bq-prod.binding.yaml` |
| 6 | Compile to DDL | `gm compile finance-bq-prod.binding.yaml` |
| 7 | Apply DDL (property graph only; tables already exist) | — (external) |

## 3. Global conventions

### Invocation

Installed binary `gm`. Subcommand style is flat verb-noun hyphenated
(`gm import-owl`, not `gm import owl`).

### Output destinations

- **stdout** — the primary result: DDL text (`gm compile`), imported
  YAML (`gm import-owl` and `gm import-dataplex-glossary` without
  `-o`), nothing on success (`gm validate`).
- **stderr** — diagnostics, warnings, human-readable errors.
- `-o <file>` — redirect stdout to a file. Where applicable: the long
  form is `--output <file>` on `gm compile` and `--out <file>` on
  `gm import-owl` and `gm import-dataplex-glossary`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. No errors; warnings may have been printed. |
| 1 | Validation or compilation error (user-fixable). |
| 2 | Usage error (bad flag, missing file). |
| 3 | Internal error (unexpected exception). |

### Error format

Default is human-readable, one line per error in the form:

```
<file>:<line>:<col>: <rule> — <message>
```

Example:

```
finance.ontology.yaml:47:5: ontology-r11 — Entity "Account" has no primary key
```

`--json` emits a JSON array of structured error objects with fields
`file`, `line`, `col`, `rule`, `severity` (`error` | `warning`),
`message`. Warnings do not affect the exit code.

### File conventions

- **Suggested suffixes.** `*.ontology.yaml`, `*.binding.yaml`. The
  loader detects file kind by the top-level key (`ontology:` or
  `binding:`), so any extension is accepted.
- **OWL source.** File extension selects the parser: `.ttl` (Turtle),
  `.owl` / `.rdf` (RDF/XML).

## 4. `gm validate`

Check that a single YAML file conforms to its spec and cross-references
resolve.

### Usage

```
gm validate <file>
```

- Loader detects ontology vs binding from the top-level key.
- **Ontology** → checked against `ontology.md` §10.
- **Binding** → checked against `binding.md` §9. The CLI locates
  the companion ontology (named by `ontology:` in the binding,
  looked up as `<name>.ontology.yaml` in the same directory) for
  cross-validation. If the companion is not found, validation fails
  with exit 2 (`cli-missing-ontology`). Use `--ontology PATH` to
  point at a specific ontology file and skip auto-discovery.

### Flags

| Flag | Purpose |
|---|---|
| `--json` | Structured error output (see §3). |
| `--ontology <path>` | For binding files: path to the companion ontology. Overrides auto-discovery of `<name>.ontology.yaml` next to the binding. |

On success, nothing is written to stdout.

## 5. `gm compile`

Emit DDL from a binding. The companion ontology is located by the same
rule as `gm validate` (§4).

### Usage

```
gm compile <binding> [--ontology <path>] [-o <out>]
```

- Input must be a binding YAML file. Passing an ontology file (or any
  other non-binding YAML) exits 2 with `cli-wrong-kind`.
- Runs validation implicitly on both files before emission. Any error
  fails the compile; no partial DDL is emitted.
- Writes DDL to stdout unless `-o` is given.

### Flags

| Flag | Purpose |
|---|---|
| `-o <file>`, `--output <file>` | Write DDL to file instead of stdout. |
| `--ontology <path>` | Path to the companion ontology. Overrides auto-discovery (same as `gm validate`). |
| `--json` | Structured errors for the implicit validation step. |
| `--emit-concept-index` | Append `CREATE OR REPLACE TABLE` SQL for the concept index + `__meta` sibling. Requires `--concept-index-table`. See [`concept-index.md`](concept-index.md). |
| `--concept-index-table <project.dataset.table>` | Fully-qualified destination for the concept index. Required when `--emit-concept-index` is set; no silent global default. The `__meta` sibling is suffixed automatically. |
| `--compiler-version <str>` | Override the version string flowed into `compile_fingerprint`. Defaults to the installed package version. Only honored with `--emit-concept-index`. |

On any validation or compilation error, no DDL is emitted — even partially.

### Concept-index extension

When `--emit-concept-index` is set, the output combines the property-graph
DDL with two `CREATE OR REPLACE TABLE` statements emitted by the
sibling `compile_concept_index` emitter (the main concept index and its
`__meta` provenance table). The combined output is still a single text
stream; pipe it into `bq query` or write it to a file with `-o`. Without
the flag, output is byte-identical to plain `gm compile`.

The required `--concept-index-table` value must be a fully-qualified
`project.dataset.table` triple. Each segment must match
`^[A-Za-z0-9_-]+$`; backticks are added by the emitter (do not
pre-quote). See [`concept-index.md`](concept-index.md) for the full
schema, provenance contract, and SQL patterns.

Example:

```bash
gm compile finance-bq-prod.binding.yaml \
  --emit-concept-index \
  --concept-index-table my-proj.my_ds.ontology_concept_index \
  -o graph_ddl.sql
```

### Filename convention

When writing to a file, the conventional name is **`graph_ddl.sql`**.
This contrasts with `table_ddl.sql`, the output of `gm scaffold`, so a
directory containing both artifacts is self-describing. The convention
is advisory — `-o` accepts any path.

## 6. `gm import-owl`

Read OWL sources and emit a `*.ontology.yaml` (see `owl-import.md`).

### Usage

```
gm import-owl <source>... --include-namespace <iri>... [-o <out>]
              [--format ttl|rdfxml] [--language <tag>] [--json]
```

- One or more OWL source files (Turtle, RDF/XML).
- At least one `--include-namespace` required; multiple allowed.
- Recognizes both OWL and SKOS constructs; SKOS concepts become
  abstract entities, SKOS graph predicates become abstract
  relationships, and SKOS literals become annotations. See
  `owl-import.md` §19 for the full mapping.
- Output uses `FILL_IN` for ambiguities and annotations for dropped OWL
  features (see `owl-import.md` §11, §13). `FILL_IN` causes
  `gm validate` to fail until resolved.

### Flags

| Flag | Purpose |
|---|---|
| `--include-namespace <iri>` | Required, repeatable. |
| `-o <file>`, `--out <file>` | Write YAML to file instead of stdout. |
| `--format ttl\|rdfxml` | Override parser selection from file extension. |
| `--language <tag>` | BCP-47 language tag for label selection (default `en`). Other-language labels become language-suffixed annotations. |
| `--json` | Structured drop-and-placeholder summary. |

Drop summary is printed to stderr regardless of `--json`. It includes
SKOS counts (concepts imported as abstract, relationships imported as
abstract, annotations preserved, labels discarded by language
selection, external match targets, generic literal annotations) and a
hint when every imported entity is abstract.

## 7. `gm import-dataplex-glossary`

Read a Knowledge Catalog (Dataplex) Business Glossary through the
read-only `dataplex.googleapis.com` v1 API and emit a `*.ontology.yaml`.
The user manual has the full mapping table and a worked example.

### Usage

```
gm import-dataplex-glossary <glossary> [--ontology-name <name>]
                            [-o <out>] [--json]
```

- `<glossary>` is a glossary resource name,
  `projects/<project>/locations/<location>/glossaries/<glossary_id>`,
  where the project is a project number or a project ID (lowercase
  letters, digits and hyphens, starting with a letter and not ending
  with a hyphen; legacy domain-scoped IDs such as
  `example.com:my-project` included), and the location is a lowercase
  region or zone such as `us-central1` or `global`. Anything else exits
  2 (`cli-usage`) before any API call.
- Requires the `[dataplex]` extra (`google-cloud-dataplex>=2.12.0`);
  without it the command exits 2 (`cli-missing-dependency`).
  Credentials come from Application Default Credentials. The import is
  read-only: `roles/dataplex.catalogViewer` is enough, and nothing is
  written to the catalog.
- Calls `GetGlossary` and checks that the response names the requested
  glossary: a response without a resource name is rejected, and the
  name must have the same location and glossary ID, and the same
  project or that project under its other form (ID or number). Only
  then does it list every category and term (all pages) under the
  glossary's canonical resource name. A list page that reports any
  unreachable location (even a blank entry), or a response page without
  its `categories` or `terms` field, fails the import
  (`import-api-error`) instead of producing a partial ontology.
- The glossary becomes the concept scheme: the root `ontology`, named
  after the glossary ID unless `--ontology-name` is given, with
  `dataplex:glossary` and `dataplex:uid` annotations. Every category
  and term becomes an abstract entity named `category_<id>` or
  `term_<id>`, with each character outside `[A-Za-z0-9_]` replaced by
  `_`. Names of the same kind that still collide each get a
  `_<sha256(raw id)[:8]>` suffix; if that name is taken, the suffix
  grows up to the full digest and then gains a `_2`, `_3`, … counter,
  so names never depend on API order. The raw ID is kept in
  `skos:notation`, the display name in `skos:prefLabel`, and the
  resource name in `dataplex:resource`.
- A resource whose parent is the glossary gets `skos:topConceptOf` and
  `skos:inScheme`; one whose parent is a category of the glossary gets
  `skos:inScheme` and a `skos_broader` relationship to that category.
  Parents are matched on project, location, glossary ID and category
  ID, and the project must match exactly. A project ID and a project
  number count as one project only when `GetGlossary` answered a
  request by one form with a name in the other (from Python,
  `map_dataplex_glossary(..., project_alias=...)`). Any other parent is
  kept as `dataplex:orphan_parent` and counted in the summary; one that
  might be the same project under its unverified other form is counted
  on its own summary line.
- Output is canonical (entities, relationships and annotations sorted;
  identical repeated records deduplicated), so re-importing an
  unchanged glossary gives byte-identical YAML whatever order the API
  returns pages in. The YAML is validated before it is written. With
  `-o`, a regular file, new or existing, is written to a temporary file
  in its real directory and then moved into place, so a failed write
  leaves an existing file untouched; that directory must be writable,
  and a read-only file is refused. Directories are resolved strictly,
  as opening the path would: `missing/../out.yaml` fails instead of
  replacing `out.yaml`, a path ending in `/` or `/.` names a directory
  (so `out.yaml/` fails too), and a symlink, dangling or not, is
  followed to the file it names. A new file gets `0o666` minus the
  umask. Descriptor paths (`/dev/stdout`, `/dev/fd/<n>`,
  `/proc/self/fd/<n>`) and other non-regular destinations (`/dev/null`,
  a named pipe) are written directly; a directory is an error.
- Every entity is abstract, so nothing is bound to tables, but
  `gm compile` and `OntologyRuntime` still take a binding; a companion
  binding with no entities validates. `gm compile --emit-concept-index
  --concept-index-table <project.dataset.table>` then emits the concept
  index and its `__meta` table after a bare
  `CREATE PROPERTY GRAPH <ontology>;` header that has no node tables
  (nothing is bound); drop that statement before running the rest.

### Flags

| Flag | Purpose |
|---|---|
| `--ontology-name <name>` | Ontology name (default: the glossary ID). Characters outside `[A-Za-z0-9_]` become `_` and a leading digit gets a `_` prefix; a blank name, or one with no ASCII letter or digit, exits 2 (`cli-usage`). |
| `-o <file>`, `--out <file>` | Write YAML to file instead of stdout. |
| `--json` | Structured errors (see §3): on failure stderr holds exactly one JSON document. Argument-parsing errors (a missing `<glossary>` or option value) are reported by the CLI framework in plain text, as for every `gm` command. |

The import summary is printed to stderr regardless of `--json`, once
the YAML has been written to stdout or the `-o` file: the category and
term counts; when nonzero, the `skos_broader`, sanitized-ID,
hash-suffixed-name, orphan-parent, unpaired-project-parent and
deduplicated-record counts; and a note that every entity is abstract.
Sanitization, hash suffixes and orphan-parent reporting are specific to
this importer:
`gm import-owl` does not replace characters in local names, fails on a
name collision instead of adding a suffix, and leaves out SKOS
hierarchy edges whose other end it did not import.

Exit codes follow §3. Exit 1 covers an empty glossary, conflicting
records for one resource ID, a record from another project, location
or glossary, or a `GetGlossary` response without a resource name or
naming another glossary (`import-validation`); a Dataplex API,
transport, credential, response-decoding or client construction
failure, or a list page with unreachable locations or without its
items (`import-api-error`); and an unwritable `-o` path or stdout
(`cli-output-error`). Exit 3 is an unexpected error, including one
raised while writing the output; with `--json` it is reported as a
`cli-internal-error` object.

## 8. Open questions

- **Warnings as errors.** A `--strict` flag that turns warnings into
  exit-1 errors is common in similar tools. Defer until CI users ask.
- **Log verbosity.** No `--verbose` / `-v` in v0. Validation and
  compile output are already structured enough; add only if
  debugging demands it.
- **Config file.** A `gm.toml` or similar for per-project defaults
  (namespace filters, target dataset) would simplify repeated
  invocations. Defer until a concrete need surfaces.

## 9. Out of scope

- **`gm init`** — scaffold a minimal project. Users can copy from
  `docs/distillation/examples/` once that exists.
- **`gm inspect-schema`** — reverse-engineer a skeleton ontology from
  an existing warehouse dataset. Useful for Workflow B but a
  significant amount of backend-specific code.
- **`gm deploy`** — apply DDL to a live backend. Explicitly off-limits
  per `compilation.md` §1.
- **`gm diff`** — compare two compilations. Text diff of DDL output
  covers the need.
- **Shell completion** — post-v0.
- **Installation and packaging** — separate concern (PyPI, homebrew,
  etc.).
