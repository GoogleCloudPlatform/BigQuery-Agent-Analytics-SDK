# Command Line Interface — Core Design (v0)

Status: draft
Scope: the v0 CLI for the `gm` tool. Three commands (`validate`,
`compile`, `import-owl`) plus global conventions for output, errors, and
exit codes. Out-of-scope items enumerated in §8.

## 1. Goals

- **Minimal surface.** Three commands cover both user workflows end to
  end. Nothing else ships in v0.
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
  YAML (`gm import-owl` without `-o`), nothing on success
  (`gm validate`).
- **stderr** — diagnostics, warnings, human-readable errors.
- `-o <file>` / `--out <file>` — redirect stdout to a file. Where
  applicable (`gm compile`, `gm import-owl`).

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
- **Binding** → checked against `binding.md` §9. The loader also
  attempts to locate the companion ontology (named by `ontology:` in
  the binding, looked up as `<name>.ontology.yaml` in the same
  directory) for cross-validation. If not found, binding validates
  syntactically and the loader emits a warning.

### Flags

| Flag | Purpose |
|---|---|
| `--json` | Structured error output (see §3). |

On success, nothing is written to stdout.

## 5. `gm compile`

Emit DDL from a binding. The companion ontology is located by the same
rule as `gm validate` (§4).

### Usage

```
gm compile <binding> [-o <out>]
```

- Runs validation implicitly on both files before emission. Any error
  fails the compile; no partial DDL is emitted.
- Writes DDL to stdout unless `-o` is given.

### Flags

| Flag | Purpose |
|---|---|
| `-o <file>`, `--out <file>` | Write DDL to file instead of stdout. |
| `--json` | Structured errors for the implicit validation step. |

On any validation or compilation error, no DDL is emitted — even partially.

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
```

- One or more OWL source files (Turtle, RDF/XML).
- At least one `--include-namespace` required; multiple allowed.
- Output uses `FILL_IN` for ambiguities and annotations for dropped OWL
  features (see `owl-import.md` §11, §13). `FILL_IN` causes
  `gm validate` to fail until resolved.

### Flags

| Flag | Purpose |
|---|---|
| `--include-namespace <iri>` | Required, repeatable. |
| `-o <file>`, `--out <file>` | Write YAML to file instead of stdout. |
| `--format ttl\|rdfxml` | Override parser selection from file extension. |
| `--json` | Structured drop-and-placeholder summary. |

Drop summary is printed to stderr regardless of `--json`.

## 7. Open questions

- **Warnings as errors.** A `--strict` flag that turns warnings into
  exit-1 errors is common in similar tools. Defer until CI users ask.
- **Log verbosity.** No `--verbose` / `-v` in v0. Validation and
  compile output are already structured enough; add only if
  debugging demands it.
- **Config file.** A `gm.toml` or similar for per-project defaults
  (namespace filters, target dataset) would simplify repeated
  invocations. Defer until a concrete need surfaces.

## 8. Out of scope

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
