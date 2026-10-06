# Configuring preflight

Everything has a default. A dbt project at the repository root, written in SQL DuckDB can
run, needs no config file at all. This page is the full reference: the GitHub Action's
inputs, every key of `.dbt-preflight.yml`, where the source schema comes from, how
warehouse SQL runs on DuckDB, where metrics are read from, and the convention rules.

- [The GitHub Action](#the-github-action)
- [`.dbt-preflight.yml`](#dbt-preflightyml)
- [Where the schema comes from](#where-the-schema-comes-from)
- [Keeping the schema](#keeping-the-schema)
- [Loaders, seeds and projects without sources](#loaders-seeds-and-projects-without-sources)
- [Your warehouse's SQL, on DuckDB](#your-warehouses-sql-on-duckdb)
- [Metrics, from wherever the project defines them](#metrics-from-wherever-the-project-defines-them)
- [Conventions](#conventions)

The CLI's flags, the exit code and the summary JSON are in [integration.md](integration.md).
How a run decides what counts against a pull request is in [how-it-works.md](how-it-works.md).

## The GitHub Action

```yaml
# .github/workflows/preflight.yml
name: dbt preflight
on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  preflight:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: JB-Analytica/dbt-preflight@v0
```

`@v0` follows the latest 0.x release; pin a release tag such as `@v0.5.0` for an exact
version. The action installs preflight with uv, runs it against the pull request's base
branch, posts or updates the comment, and copies the comment into the job summary.

| Input | Default | What it does |
| --- | --- | --- |
| `base-ref` | the pull request's base branch | Branch to diff against (preflight is given `origin/<base-ref>`). |
| `config` | `.dbt-preflight.yml` | Path to the config file, relative to the repository root. Used only when the file exists. |
| `post-comment` | `true` | Post or update the review comment on the pull request. |
| `fail-on-error` | `true` | Fail the job when preflight fails. |
| `python-version` | `3.12` | Python version to run preflight with. |
| `github-token` | `${{ github.token }}` | Token used to post the comment. Needs the `pull-requests: write` permission. |

Pull requests from forks run with a read-only token, so for those the report lands in the
job summary only, with a note saying so. To get comments on fork pull requests, run
preflight from a `pull_request_target` workflow, and read its security trade-offs first.

## `.dbt-preflight.yml`

Add the file next to the dbt project, or at the repository root, when the dbt project is
not at the root, its SQL is written for a warehouse and no `profiles.yml` is checked in
(set `dialect:`), or its `sources.yml` reads environment variables:

```yaml
project_dir: dbt                    # folder holding dbt_project.yml (default: .)
schema: source_system/webshop.dbml  # DBML describing the sources (default: derive from the project)
rows: 200                           # rows per source table
rows_for:                           # per-table overrides, for realistic fact-to-dimension ratios
  orders: 800
  order_items: 2000
seed: 42                            # same seed, same data, on every run
locale: nl_BE                       # Faker locale for names and addresses
env:                                # variables your profiles.yml / sources.yml expect
  GCP_PROJECT: preflight
vars:                               # dbt vars, passed to every dbt command as --vars
  shopify_api: rest
dialect: bigquery                   # SQL dialect to transpile from (default: read from profiles.yml)
metrics:                            # extra metrics to compare, for projects with none defined elsewhere
  - name: gross_revenue
    model: fct_orders
    sql: sum(gross_amount_eur)
```

That is the whole setup. Preflight writes its own `profiles.yml`, so the project's real
profile and its credentials are never read.

| Key | Default | What it does |
| --- | --- | --- |
| `project_dir` | `.` | Folder holding `dbt_project.yml`. Must be inside the repository. |
| `schema` | none: derive from the project | A DBML file describing the source tables. See [Where the schema comes from](#where-the-schema-comes-from). |
| `rows` | `200` | Rows per source table. At least 10. |
| `rows_for` | none | Per-table row counts, `table: rows`, for realistic fact-to-dimension ratios. |
| `seed` | `42` | Random seed. Same model, same seed, same data, on every run. |
| `locale` | none (model2data's default) | Faker locale for names and addresses, e.g. `nl_BE`. |
| `env` | none | Environment variables the project's `profiles.yml` or `sources.yml` expect, `NAME: value`. |
| `vars` | none | dbt vars, passed to every dbt command preflight runs as `--vars`. YAML dates are passed as ISO strings; a value JSON cannot carry is a config error. |
| `loader_columns` | `dlt: {_dlt_load_id: varchar, _dlt_id: varchar}` | Columns a loader adds to every table it lands, keyed by the `loader:` a source declares: `loader_name: {column: type}`. |
| `dialect` | read from a checked-in `profiles.yml` | The sqlglot dialect the project's SQL is written in (`bigquery`, `snowflake`, `redshift`, `databricks`, `trino`, ...). `duckdb` or `none` runs the SQL as written. |
| `dialect_failures` | `warn` | `warn`: a model that fails only because DuckDB lacks a warehouse function is reported as *not verified* and does not fail the check. `error`: it does. |
| `check_all` | `false` | Check conventions on every model the run builds, not only the changed ones. |
| `metrics` | none | Extra metrics to compare: each a `name`, a `model` and `sql`, an aggregate expression over that model. |
| `conventions` | absent: the `jba` rules as warnings | Opts in to the convention rules at full strength and adjusts them. See [Conventions](#conventions). |

Relative paths in the file resolve against the file's own folder, so a config kept next to
a dbt project in a subfolder reads the same as one at the repository root. An unknown key,
or a key of the wrong shape (`rows: "lots"`), is an error rather than silently ignored.

A change to the file on a pull request counts as a change to every source only when it
changes a key that shapes the fixtures or what the models compile to: `project_dir`,
`schema`, `rows`, `rows_for`, `seed`, `locale`, `env`, `vars`, `loader_columns` or
`dialect`. A config file that is new, or unreadable on either branch, counts too. Editing
`conventions`, `metrics`, `dialect_failures` or `check_all` leaves the sources as they were.

## Where the schema comes from

Preflight needs to know what the source tables look like. Three options:

1. **A DBML file** (`schema:`). If the repository already describes its source system in
   DBML, point at it. Note hints in the DBML (weighted statuses, skewed foreign keys, null
   rates) carry through to the fixtures, so the data behaves like a business. A change to
   the DBML file counts as a change to every source, so everything is rebuilt. A text
   column some model parses as JSON is filled with JSON holding the keys read, even when
   the DBML says nothing about it; a column note containing `not JSON` keeps the generated
   text.
2. **Derived from `sources.yml`.** With no `schema:` set, preflight reads the project's
   sources. `unique` and `not_null` tests become keys, and `relationships` tests between
   sources become foreign keys.
3. **Inferred from the staging models that read a source**, for any table `sources.yml`
   leaves without columns, or without a `data_type` on them. Real projects (jaffle-shop, for
   one) often declare a source with no columns at all; the staging model that does
   `select id as customer_id, ... from {{ source(...) }}` already names every column it
   needs, so preflight reads that instead of asking for YAML nobody wrote.

### How an untyped column gets its type

An explicit `cast(x as date)` or `x::date` in the staging SQL sets the type first. Failing
that, how the staging SQL uses the column is read next: an operand of `/`, `*`, `+`, `-`
against a numeric literal, or wrapped in `sum(`/`avg(`/`round(`, is numeric, while a
comparison to a string literal or a `lower(`/`upper(`/`trim(`/`concat(` argument is varchar.

Only then does the column's own name decide: `_at`/`_timestamp`/`_datetime` a timestamp,
`_date`/`_on` a date, `id`/`_id` an integer, `is_`/`has_`/`_flag`/`enabled`/`active` a
boolean, a name built from `paid`, `cost`, `tax`, `fee`, `discount`, `revenue`, `amount`,
`price`, `total`, `rate` and the like a decimal, one built from `count`, `number`, `qty`,
`quantity`, `units`, `age`, `year`, `month`, `day` an integer, and a handful of common
attribute names (`email`, `phone`, `name`, `status`, `type`, `sku`, ...) always varchar.

A name shaped like a foreign key (`customer_id`, or a bare `customer` when a
`raw_customers` source exists) is typed as an integer and gets a `ref:` to that table's
`id` when one can be found, the same referential integrity an explicit `relationships`
test would have set up.

The comment says which columns were guessed, so a reviewer can tighten them in
`sources.yml` if a guess is wrong. Nothing untyped stops the run: a declared column no SQL
types is typed by its name when every model reading the source could be followed (so none
of them reads it), and is a `varchar` when one could not; the comment lists both, and says
which were typed `varchar` for that reason. A source nothing reads (no model, snapshot or
test) that `sources.yml` does not fully type gets no fixture at all, and the comment names
it. The run only stops when a source that is read has no column known at all (nothing
declared, nothing a model names), since there is no table to invent; the error points at
`schema:` and at `dbt-preflight schema`, and gives the YAML to fill in.

A model that reads a guessed column and fails the same way on both branches is never
written off as *broken on main*: both branches ran on the guess, so it is listed under
*Could not be checked*, with the guessed columns named, and counts against the run.

### Keys, tests and enums

A source column named `id` is always the primary key, and a table never gets more than
one: without an `id`, the first column carrying both `unique` and `not_null` is the key.

A staging model's own `unique`/`not_null` tests on the alias it gave a source column
(`id as customer_id`, tested as `customer_id`) carry back to that source column too: `pk`
when both are declared and it is the key, `unique`/`not null` otherwise. So a project that
tests its staging models instead of its sources still gets keys in the derived schema. A
`cast()` around the column is seen through, because a staging layer over a schemaless
loader is where types get pinned; `lower(email)` is not, because it changes the value
rather than the type.

An `accepted_values` test carries back the same way, and the column is generated as an
enum of exactly those values, so a column the project treats as a vocabulary is not filled
with placeholder text its own test then rejects. Only a string column becomes an enum,
since an enum's values are strings.

Tests carry back only from a model that reads that one source and keeps its rows: directly
or through an unfiltered pass-through, with no join, grouping, distinct, aggregate, filter,
sampling, paging, unnest/explode/`generate_series` or pivot. A mart's grain test never
becomes a source key.

### Compiled SQL fills the gaps

Before deriving the schema, preflight runs `dbt compile` on the models that read a source,
against an empty DuckDB file, so what a macro hides from the raw SQL is read too: a
project's own `{{ source_or_empty(...) }}`, or Fivetran's `stg_<table>_tmp` models and
`fill_staging_columns`. In compiled SQL a source is matched by its relation name. A model
that is only `select * from` a source (or the empty stand-in a macro renders when the
source does not exist yet) counts as that source, and in a model that reads one source and
nothing else, `cast(null as T) as col` means the package expects `col` of type `T`. A
`numeric` key is taken as an integer, and keeps its foreign-key ref when its name ends in
`_id` and the target's `id` is an integer.

The raw SQL wins wherever it says something; a compiled type that disagrees with it is
listed in the comment. Tests carry back from compiled SQL under the same row-keeping rule
as above. A reader counts as followed when its compiled SQL parsed, names the source,
passes no `*` over it on, puts no `*` inside a join or an expression (`struct_pack(o.*)`),
makes no whole-row reference (`to_json(o)`) and reads no unqualified column next to a join.
A model that does not compile is read from its raw SQL alone.

A source identifier DBML cannot spell (GA4's `events_*`) gets a sanitised table name in the
derived schema; the fixture still loads under the identifier dbt expects.

## Keeping the schema

A derived schema is a starting point that preflight rebuilds on every run. When it had to
guess a type, the comment says so in one line, and `dbt-preflight schema` turns what it
derived into a file you own:

```bash
dbt-preflight schema           # writes source_system/<project name>.dbml
```

It does what a run does on the head (`sources.yml`, the staging models, compiled SQL), with
no warehouse, credentials or base ref, and writes the result as DBML. Every column that
`sources.yml` did not type carries a note saying where its type came from: `type guessed
from the name`, `type read from compiled SQL`, or `inferred from a staging cast`. They are
plain prose, so model2data treats them as descriptions and they change nothing about the
generated data; delete a note once you have checked its column. (A note that is, whole, a
JSON object is how model2data shapes generation: put those next to the prose notes, not in
them.) A column a model reads as JSON carries `JSON, keys read: address.city, weight
(number)`, which preflight does read: it fills the column with JSON holding those keys.
Keep it while a model still parses the column, or write `not JSON` to keep text.

The default path is `source_system/<dbt project name>.dbml` in the folder that holds
`.dbt-preflight.yml`, so the `schema:` line the command prints works as it is. `--output`
chooses another path. The command refuses to overwrite a file without `--force`, and does
nothing when the config already sets `schema:` (`--force` derives anyway; the config is
never edited).

Then:

1. Add `schema: source_system/<project name>.dbml` to `.dbt-preflight.yml`.
2. Commit the file.
3. Refine it in [model2data studio](https://studio.jbanalytica.com/?ref=dbt-preflight), by
   pasting it into the editor or opening the repository as a repository project. Weighted
   statuses, null rates and skewed keys carry through to the fixtures.

A run with `schema:` pointing at the written file builds the same tables, columns and types
as the derived run, and stops reporting guesses. The DBML file is then the schema, and a
change to it counts as a change to every source.

## Loaders, seeds and projects without sources

Sources declared with `loader: dlt` get `_dlt_load_id` and `_dlt_id` added to their
fixtures. Other loaders can be declared under `loader_columns:` in the config.

A project with no sources at all, one whose input is its seeds, needs neither: dbt loads
the seeds during the build and preflight generates nothing. On a pull request only the
seeds the selected models read are loaded, on the base branch and on the pull request
alike, and an edited seed counts as a change to every model that reads it.

## Your warehouse's SQL, on DuckDB

A project written for BigQuery says `timestamp_diff(a, b, hour)` and `initcap(x)`; DuckDB
has neither. Preflight transpiles compiled models from the project's dialect to DuckDB with
[sqlglot](https://github.com/tobymao/sqlglot) before they run, after dbt has resolved every
`ref()` and `source()`. DuckDB gets the first word: a model it accepts as written is left
alone, so a project's own `adapter.dispatch` macros keep their DuckDB branch, and only a
model DuckDB rejects is rewritten.

The dialect is read from a `profiles.yml` checked into the project directory (the adapter
`type` of its default target; `~/.dbt` is never read), or set explicitly:

```yaml
dialect: bigquery   # or snowflake, redshift, databricks, trino, ... ; duckdb/none to disable
```

dbt renders for the DuckDB target, so a macro such as `dbt_utils.star` quotes identifiers
with double quotes, which BigQuery's grammar reads as strings. A double-quoted token is
read as an identifier where only one can stand (next to a `.`, after `as`, or alone as a
select-list item); elsewhere (`status = "paid"`) it stays a string. When DuckDB rejects the
identifier reading for a reason other than one of those columns missing, the string reading
gets a chance; a missing column stays missing rather than becoming a constant.

A model sqlglot cannot parse runs as written and the comment says so. dbt's own SQL, and
generic tests rendered from macros, are never transpiled; only model bodies and singular
tests are. A model that still fails because DuckDB lacks something is reported as *not
verified*, not as broken; `dialect_failures: error` makes that fail the check.

## Metrics, from wherever the project defines them

The comment's "What changed in the output" section evaluates every metric the project
defines, on the base branch and on the pull request, and lists the ones that moved. Three
sources are read, and a project needs only one of them:

1. **dbt's semantic layer.** Semantic models and metrics in the project's YAML: `simple`
   metrics with measure and metric filters, `ratio` and `derived` metrics, including ones
   whose inputs sit on different models (orders per customer reads `fct_orders` and
   `dim_customers`; each side is evaluated on its own model and the comment names both).
   A `cumulative` metric with no window and no grain to date is a running total over all
   time, and its final value is the plain total, so it is evaluated as one. This is the
   route for a project that uses dbt and nothing else. A windowed or grain-to-date
   `cumulative` metric and a `conversion` metric need a time spine and are reported as
   not evaluated.
2. **Lightdash `meta.metrics`.** Aggregate metrics on columns (`sum`, `count_distinct`,
   `average`, ... with their `filters`) and `number` metrics on the model whose `sql`
   references other metrics with `${...}`.
3. **`metrics:` in `.dbt-preflight.yml`.** A name, a model and an aggregate SQL expression,
   for teams with neither of the above.

With no metrics defined, columns, row counts and differing rows are still compared, and the
comment says where metrics can be defined.

## Conventions

The convention checks encode the JB Analytica warehouse conventions. They run on the
changed models only (`check_all: true` widens that to every model the run builds), so
existing debt does not resurface on every pull request.

| Rule | Severity | What it wants |
| --- | --- | --- |
| `naming` | error | `staging/` models named `stg_<source>__<entity>`, `intermediate/` named `int_<entity>__<verb>`, `marts/` named `dim_<entity>` or `fct_<event>` |
| `layering` | error | A staging model reads exactly one `source()` and no `ref()`; nothing outside staging reads a `source()` |
| `primary_key` | error | At least one column tested `unique` and `not_null` |
| `description` | warn | The model has a description |
| `column_naming` | warn | snake_case; timestamps end in `_at`, dates in `_date`, booleans start with `is_` or `has_` (read from the built table's real types) |

Models outside those three folders are exempt from the naming and layering rules.

That is the `jba` preset. By default it runs as warnings only, with or without a
`.dbt-preflight.yml`: a project never signed up for anyone's conventions just by writing a
config file for its paths or dialect, and a warning is advice where an error would be a
demand. A `conventions:` block is the opt-in and runs its preset at full strength, so
`preset: jba` on its own makes the rules above errors. A project with its own conventions
adjusts them there:

```yaml
conventions:
  preset: jba              # jba (default) or none
  rules:
    description: off       # off | warn | error, per rule
    column_naming: warn
  layers:                  # folder under models/ -> regex a model name must match
    staging: "^stg_[a-z0-9]+__[a-z0-9_]+$"
    marts: "^(dim|fct|rpt)_[a-z0-9_]+$"
  source_layer: staging    # the only folder allowed to read source(); null allows any
```

The rules themselves live in `dbt_preflight/conventions.py`; [integration.md](integration.md#where-the-conventions-are-defined)
says where they are checked.
