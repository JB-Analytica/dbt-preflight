# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- The output diff detects a rename (a dropped and an added column of the same type with the
  same values) and reports it as one, lists where a removed or renamed column was referenced
  on the base branch (YAML entry, Lightdash meta, semantic layer, tests, downstream models),
  and profiles added columns (type, nulls, value distribution when low-cardinality). All
  three came from watching a coding agent use the comment.
- Sources with no `data_type` in `sources.yml` - or no columns declared at all, as
  jaffle-shop and other real projects do - get their columns inferred from the staging
  models that read them, instead of failing with a SchemaError. An explicit `cast(x as T)`
  or `x::T` sets the type; the rest is guessed from the column name. The fixtures block in
  the comment says which sources were inferred and which columns were guessed.
- model2data's own warnings about the fixtures it generated - columns filled with generic
  placeholder text, tables stuck in an unresolved foreign-key cycle, composite keys left
  with duplicate combinations, DBML lines it could not parse - are surfaced in the
  comment's fixtures block, so a reviewer can tell a weak fixture from a strong one.

### Changed

- A repository with no `.dbt-preflight.yml` gets the house conventions as warnings, not
  errors. Full strength needs a config file (a `conventions:` block is not required).

## [0.1.0] - 2026-09-11

First tagged release. Private repository; the GitHub Action installs from the repository
and `@v0` follows the latest 0.x release.

### Added

- `dbt-preflight run`: generate synthetic source data with model2data, build and test the
  models changed in a pull request against DuckDB, check house conventions, and write one
  review comment. No warehouse credentials required.
- Schema from a DBML file, or derived from the project's `sources.yml` when every source
  column declares a `data_type`.
- Loader columns: sources declared with `loader: dlt` get `_dlt_load_id` and `_dlt_id`
  added to their fixtures automatically.
- Convention checks: layer naming, staging reads one source, no `source()` outside staging,
  model descriptions, primary-key `unique` + `not_null` tests, snake_case columns, timestamp
  and date and boolean column suffixes.
- Dialect-aware failures: a model that fails only because DuckDB lacks a warehouse-specific
  function is reported as *not verified*, not as broken.
- Composite GitHub Action that installs the tool and posts or updates the comment.
- Output diff against the base branch: the changed models and everything downstream are
  built on the base too, on the same fixtures, and compared on columns (added, removed,
  retyped), row counts, rows with different values, and every metric the project defines.
  Metrics come from dbt's semantic layer (simple, ratio, derived), Lightdash `meta.metrics`,
  or a `metrics:` list in the preflight config. A removed or retyped column is a warning.
- dbt unit tests are reported like data tests; a failing one fails the check. A changed
  model that was skipped or never built fails the check too.
- The bundled example gained a dbt semantic layer (with the time spine it requires) beside
  its Lightdash metrics, and two more recorded scenarios: a change a unit test catches, and
  a silent one only the diff sees.
- Model SQL is transpiled from the project's warehouse dialect to DuckDB with sqlglot, hooked
  into dbt's compiler after Jinja rendering. The dialect comes from a checked-in
  `profiles.yml` or the `dialect:` config key; models sqlglot cannot parse run as written and
  are listed in the comment.
- Change detection covers sources: a modified source (or a change to the DBML schema file or
  the preflight config) rebuilds everything that reads it, and the comment says why.
- Convention findings about descriptions and primary-key tests point at the model's YAML
  file rather than its SQL, because that is where the fix goes.
- Conventions are configurable: a `conventions:` block selects the `jba` or `none` preset,
  sets each rule to off, warn or error, replaces the layer patterns, and names the folder
  allowed to read `source()`.
- A project without sources runs on its seeds alone; nothing is generated.
- Untyped source columns are reported with a YAML fragment to paste into the sources file.
- Pull requests from forks get their report in the job summary with a note, instead of a
  silent failure to post.
- Release workflow: a version tag builds, tests the wheel, creates the GitHub release and
  moves the `v0` tag; PyPI publishing waits on a repository variable.

### Fixed

- With a dialect set, a model with a genuine syntax error was filed as "not verified"
  instead of failed. A parser error on transpiled SQL is now the pull request's failure.
- `dbt deps` was given flags it does not accept, so projects with packages could not run.
- `scripts/scenarios.py` records four pull-request shapes against the bundled example into
  `docs/scenarios/`; `docs/agents.md` says how to put preflight in front of a coding agent.
