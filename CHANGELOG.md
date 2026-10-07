# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **A project that declares `require-dbt-version: ">=2.0.0"` runs.** dbt stopped before
  parsing a line, and preflight pins dbt 1.x, so the current dbt-labs/jaffle-shop (the
  project most people reach for first) could not be checked at all. Every dbt command
  preflight runs now passes `--no-version-check`. The check guards against building a
  project on a dbt that cannot build it correctly, which is a real risk for a deployment
  and none here: preflight builds into a throwaway DuckDB file and never writes to a
  warehouse. A project that genuinely needs dbt 2 still fails, later, on the syntax it
  uses, with an error that says so.
- **Warehouse-only layout settings no longer lose a model.** A model written for BigQuery
  carries `partition_by={'field': ..., 'granularity': ...}`; dbt-duckdb rejects it rather
  than ignoring it, so a project with partitioned marts lost every one of those models
  before a row was compared. On dbt-ga4 that was 54 models. Settings that describe how a
  warehouse lays a table out, and nothing about the rows a model returns, are now dropped
  while building on DuckDB: BigQuery's `partition_by`, `cluster_by` and
  `require_partition_filter`, Snowflake's `transient` and `automatic_clustering`,
  Databricks' `zorder`, Redshift's `sort` and `dist`, and their relatives. Anything that
  changes a model's output, `materialized` and `unique_key` among them, is untouched. The
  comment names the models and the settings, and the summary JSON carries
  `warehouse_configs_dropped`.
- **`profiles.yml` is found above the project directory.** A repository that keeps its dbt
  project in a subdirectory usually keeps the profile at the repository root, next to the
  workflow that uses it. Only the project directory was looked at, so no dialect was
  detected for those projects and every warehouse-specific function went untranspiled.
  The project directory still wins when both exist.
- **Every unset `env_var()` is listed at once.** dbt fails on the first one it happens to
  render, so finding them all took one run per variable. A run that stops on an unset
  variable now scans the project and lists all of them, with the `env:` block to paste into
  `.dbt-preflight.yml`.

### Added

- A two-minute path at the top of the README: clone dbt-labs/jaffle_shop, rename a column,
  run preflight, see what it says. No warehouse, no credentials, no config file.
- The 52-second demo in the README, under that path. The Marketplace listing renders this
  README, so it shows there from the next release too.


## [0.5.2] - 2026-10-07

Maintenance only: nothing about a run changes.

### Fixed

- `scripts/draft_release.sh` stopped without a message when the local `v0` tag was behind
  the one on GitHub, which is the case after every release. It now updates local tags to
  match.

## [0.5.1] - 2026-10-07

The first release listed on the GitHub Marketplace. Nothing about a run changes.

### Changed

- The action's description, which the GitHub Marketplace shows, now says what a run does:
  builds main and the pull request on synthetic data, compares them, and leaves one comment.

## [0.5.0] - 2026-10-06

### Added

- A one-line summary directly under the verdict heading of the comment: new failures, the
  metrics moved (naming the biggest, with its delta), models or marts touched, and what
  could not be checked or is broken on the base branch, e.g. `No new failures · moves 3
  metrics (Total lifetime value +4.6%) · touches 6 marts`. Parts that are zero are left out.
  The summary JSON gains `headline` with the same fields (schema version unchanged).
- `dbt-preflight schema` writes the schema a run derives from the project as a DBML file to
  keep and refine in model2data studio. It does the head's derivation (sources.yml, staging
  SQL, compiled SQL) with no warehouse, credentials or base ref. Columns `sources.yml` did
  not type carry a prose note (`type guessed from the name`, `type read from compiled SQL`,
  `inferred from a staging cast`), which model2data reads as a description, never as a
  generation hint. Default path `source_system/<dbt project name>.dbml` beside
  `.dbt-preflight.yml`; `--output` overrides it, `--force` overwrites and also derives when
  the config already has `schema:`. A run with `schema:` pointing at the file builds the
  same fixtures as the derived run.
- When a run had to guess a column's type, the comment gets one line, outside the folded
  fixtures block, pointing at `dbt-preflight schema` and model2data studio. It never
  appears on a clean run, and the link carries no schema content. The summary JSON gains
  `fixtures.guessed_sources` (schema version unchanged).
- Schema inference reads dbt's compiled SQL as well as the raw SQL, so a source read
  through a macro is no longer invisible. The models that read a source are compiled
  against an empty DuckDB file on both branches; a pure `select *` (or a macro's empty
  stand-in) counts as its source, and a typed null in a single-source model types the
  column. Fivetran's dbt_shopify now derives a schema without a hand-written DBML file
  (87 sources). The raw SQL wins where it already says something, and the fixtures block
  says how many columns came from compiled SQL.
  - Tests carry back from compiled SQL only from a model that reads that one source and
    keeps its rows (no join, grouping, distinct, filter or aggregate), and only through a
    column that resolves to the source in its own scope. A mart's grain test never
    becomes a key on the source.
  - A model that fails to compile is excluded by its exact selector and read from raw SQL.
    When only the head compiles, every source counts as reshaped.

- `vars:` in `.dbt-preflight.yml`: a mapping passed to every dbt command preflight runs as
  `--vars`. A change to it counts as reshaping the fixtures, like `env:`. YAML dates are
  passed as ISO strings, and a value JSON cannot carry is a config error.

### Changed

- **No untyped column stops the run any more.** A column still untyped after the raw and
  compiled SQL is typed by its name when every reader could be followed, and is a
  `varchar` when one could not. Both are listed as guessed, and the comment and the
  summary (`unknown_columns`) say which are `varchar` for that reason. The run stops only
  when a read source has no column known at all, and the error points at `schema:` and
  `dbt-preflight schema`.
- **A failure over a guessed column is never "broken on main".** A model that reads a
  source column whose type preflight guessed, directly or upstream, and fails the same
  way on both branches, is listed under "Could not be checked" with the guessed columns
  named (`guessed_inputs` in the summary), and counts against the run, when the change
  reaches it; when it does not, it is a warning (see "preflight's own data" below). A
  compilation error is exempt, since it happens before any data is read. A test over such
  a model, or over a
  guessed source column, names the guessed columns it reads. When it *errors* (a type
  mismatch, a failed cast) it is not judged against the base and counts. When it fails on
  rows on both branches it stays pre-existing, annotated: an invariant that random data
  breaks is not a typing question. The guesses are tied to the verdict instead of only appearing in the folded
  Fixtures block.
- **A failure on preflight's own data is never "broken on main".** A model that fails the
  same way on both branches with an error about the shape of a value - malformed JSON, a
  timestamp, date or time that does not parse, a failed cast of a string - fails on a value
  preflight generated (`baseline.FIXTURE_SHAPED_ERRORS`, DuckDB's error text in one list;
  a compilation error and a model with no source upstream are exempt). Such a model, and
  one failing over a guessed column, is split by whether the change reaches it (modifies
  it, reshapes its fixtures, adds or edits a test on it, or modifies something upstream):
  reached, it is "Could not be checked" and counts; not reached, it builds from identical
  SQL on identical data on both sides, so it goes in a new warning section, "Preflight's
  generated data cannot build this model", with the reason, and what is skipped only
  because of it is not counted either, unless the change reaches that too. A guessed,
  unreached model that used to count in `counts.models.failed` (as unverified) now counts
  in `counts.models.fixture_limited`, and the headline adds `N cannot be built on
  generated data` (`headline.fixture_limited`). The summary gains
  `fixture_limited_models`, `counts.models.fixture_limited` and
  `counts.models.skipped_by_fixture_limited`, per-model `fixture_limited` and
  `skipped_by_fixture_limited`, and `fixture_error` on `unverified_broken_on_base_models`
  (schema version unchanged).
- "Could not be checked" gives each model its own reason (a generated value, a guessed
  column, or a change upstream hiding behind DuckDB's first error) under a generic intro,
  instead of saying of every model that the change reaches it from upstream.
- A snapshot or a singular test reading a source counts as a reader that cannot be
  followed, so that source's unread untyped columns are flagged `varchar` rather than
  typed by name.
- **Sources nothing reads are skipped.** A source no model, snapshot or test reads, and
  that `sources.yml` does not fully type, gets no fixture and no error. One line in the
  fixtures block names it, and the summary lists it as `fixtures.skipped_sources`. A fully
  typed source keeps its fixture, since another source's foreign key may point at it.
- **Tests carry back only from a model that has the source's rows.** This applies to raw
  SQL too, which changes 0.4.0's behaviour. A `unique`/`not_null`/`accepted_values` test
  carries back only from a model that reads that one source, directly or through an
  unfiltered pass-through, with no join, grouping, distinct, aggregate, filter, sampling,
  paging, unnest/explode/`generate_series` or pivot. A staging model with a `where` or a
  join no longer hands its tests to the source; a mart reading a source directly never
  did legitimately.
- A reader of a source is "followed" only without a `*` inside a join or an expression
  and without whole-row references. A CTE counts as reading the source only through what
  its FROM and JOINs select, no longer through every earlier CTE it could see.
- A source identifier DBML cannot spell (GA4's `events_*`) gets a sanitised table name in
  the derived schema. The fixture still loads under the identifier dbt expects.
- Compiled SQL falls back to the project's own dialect when DuckDB's grammar cannot parse
  it or finds column names no warehouse would use. Under DuckDB's grammar, BigQuery's
  `replace(x, " ", "_")` reads as two columns named ` ` and `_`. For BigQuery-style
  dialects, dbt's relation names are re-quoted for that attempt. An inferred column name
  DBML cannot spell is dropped.
- **Unread declared columns are typed by their name.** A column `sources.yml` declares
  without a `data_type` used to fail the run unless a model read it. Now it gets a type
  from its name and is listed as guessed, but only when every model reading the source is
  accounted for: its compiled SQL parsed (in DuckDB's grammar, the default one or the
  project's own dialect), names the source's relation (in full, or as an unambiguous
  `schema.table`), passes no `*` over it to its output, and reads no unqualified column
  next to a join. With anything less, the column is a flagged `varchar` (see above).
  Fivetran documents more columns than its staging macros select.
- **Numeric keys from a typed null become integers with a ref.** A column typed by a
  compiled `cast(null as numeric(...))` and named `id` or `*_id` is an `int`. Fivetran
  types every id `numeric(28,6)`, and decimal keys neither joined nor took a ref. A `*_id`
  column typed this way gets a foreign-key ref when the target's `id` is an integer too.
- `timestampntz` and `timestampltz` now read as `timestamp`. This also changes the raw-SQL
  path: a Snowflake staging model casting to either spelling used to give the fixture
  column that literal type name, and now gives it a timestamp.

### Fixed

- **A column a model parses as JSON gets valid JSON.** It used to get model2data's
  placeholder sentences, so a model doing `json_extract` failed on both branches and was
  reported "broken on main" over preflight's own data (Fivetran's `shopify__orders` and
  `shopify__transactions`: `Malformed JSON ... Input: "Weight reason."`). Preflight now finds
  the source columns read with a JSON function (DuckDB's `json_extract*`, `->`, `->>`,
  `json_value`, `json_valid`, `json_keys`, `from_json`, `::json`; BigQuery's
  `json_extract*`, `json_value`, `parse_json` where they survive), in raw and compiled SQL,
  traced back through staging aliases, `select *` and pass-throughs, together with the
  paths read and the type a cast after the extraction implies. The fixture step fills those
  columns with JSON objects holding every path (nested objects, arrays where the SQL
  indexes one), derived from the seed, table and column name, with the column's nulls kept.
  Only a text column qualifies; model2data is untouched. Models whose raw SQL calls a macro
  mentioning JSON are compiled too, in a second compile that cannot cost the inference
  anything, also on a run with a DBML file. The derived schema, and the file
  `dbt-preflight schema` writes, note such a column as `JSON, keys read: ...`, and a run on
  that file builds the same fixtures. The fixtures block lists the columns, and the summary
  JSON gains `fixtures.json_columns` (schema version unchanged).
  - The paths are the base branch's and the head's together: both build on one fixture,
    so a difference in paths alone does not reshape a source. A key only the pull request
    reads, on a column the base parses too, is a JSON null, so a renamed key (`$.amount` to
    `$.amout`) reads NULL as on real data instead of validating itself; the comment and
    `fixtures.json_new_keys` list such keys. When only one branch compiled, keys are
    compared in raw SQL alone, and the comment and `fixtures.json_keys_partly_compared`
    say a key renamed inside a macro is not caught.
  - A qualified column (`o.payload` in a join) fills only the source its qualifier names.
  - A DBML column whose note contains `not JSON` keeps its generated text.
  - Leaf strings and integers carry the row number, so a `unique` test on a JSON column or
    a key read from it holds; a column read only as a whole gets `{"id": <row>}`.
- Transpiling from BigQuery (and Spark, Databricks, Hive) turned identifiers dbt rendered
  in double quotes for the DuckDB target into string literals: `dbt_utils.star` became
  `SELECT 'customer_id', 'email'`, and Fivetran's `shopify__customers` failed with
  `Values list "customers" does not have a column named "customer_id"`. A double-quoted
  token is now an identifier where only an identifier can stand (next to a `.`, after
  `as`, or alone as a select-list item outside a function call); BigQuery's own
  double-quoted strings elsewhere (`status = "paid"`, `concat(a, " ", b)`) stay strings,
  and single-quoted strings and comments are untouched. When DuckDB cannot plan that
  reading for a reason other than one of those columns missing, the string reading is
  tried; a missing column is never turned into a constant.
- A model skipped on both branches behind a model broken on the base was excused even when
  the change reached it through another parent (a model reading both a broken model and a
  modified one). It counts now, listed as skipped and never checked.
- A model the pull request added or edited a test on (generic, unit, or a singular test
  reading it) could be "broken on main" when it failed the same way there, which skipped
  the new test unseen and excused what the model skips. It is "Could not be checked" now,
  naming the test.
- A derived table could get two `pk` columns (`id`, and a column whose staging alias was
  tested `unique` and `not_null`), which model2data reads as one composite key, so neither
  was unique and the staging model's `unique` test failed on the fixtures
  (audience-analytics' `stg_billtobox__creditors`). `id`, or failing that the first such
  column, is now the only key; the others are `unique, not null`.

## [0.4.0] - 2026-10-05

### Added

- A real-world regression suite (`scripts/realworld/`, `uv run poe realworld --compare`):
  four public dbt projects pinned to a commit, each run through a harmless change, a
  column rename and a wider change, compared with a committed baseline. Not part of
  `poe check`, since it needs network; a weekly workflow runs it.

### Fixed

- Editing `.dbt-preflight.yml` made every source count as modified, whatever changed.
  Now only a key that shapes the fixtures or what the models compile to does that
  (`project_dir`, `schema`, `rows`, `rows_for`, `seed`, `locale`, `env`,
  `loader_columns`, `dialect`). Adding a `conventions:` block to audience-analytics had
  rebuilt all 33 models and failed on a fixture weakness the change had nothing to do with.
  A config that is new, or unreadable on either side, still counts as a change.
- Seeds were never loaded on a pull-request run. The selection held models only and the
  base branch was built with `dbt run`, so a project whose models `ref()` a seed (classic
  jaffle_shop; Tuva-style lookup seeds beside sources) built none of them, with `Table
  raw_customers does not exist`, on a harmless one-line change. The seed-only fix of
  11 September covered full builds alone. The selection is now closed over the seeds and
  snapshots the selected models read, on both branches, and an edited seed counts as a
  change to the models that read it.
- Three schema-derivation bugs found on a real Snowflake project: two sources declaring the
  same table name crashed the run (the clash now gets `<source>__<identifier>`), a bare
  `date` column was guessed as a foreign key to a `dates` table, and in a model reading
  several sources every unqualified column was attributed to every source. A model2data
  error while reading the derived schema is now a readable `SchemaError`. On a fork of
  mattermost-data-warehouse a run went from 0 of 10 models built to 7.

### Changed

- **The house conventions are opt-in.** A `.dbt-preflight.yml` no longer turns the `jba`
  preset on at full strength by itself: without a `conventions:` block the rules run as
  warnings, exactly as they do with no config file at all. Add `conventions: {preset: jba}`
  to keep them as errors. A config file written only for `project_dir` and `dialect` had
  turned fivetran/dbt_shopify into 263 convention errors.
- A model that fails to build is judged against the base branch too. One that fails on
  the base with the same error message, and has nothing the change touched upstream of
  it, no longer fails the check: it leads the comment in an unfolded *Broken on main too
  (N)* section, with its error, the models it skipped on both branches, and a pointer to
  any tests already failing there, and the verdict is `passed_with_warnings`. A model the
  change modified, added or reaches from upstream, one that built on the base, or one
  that fails there differently still fails. A skipped model is put down to a broken one
  only when the base skipped it too and the change did not touch it. Found on mattermost
  (`account_daily_arr_deltas` reads a hard-coded `finance.account_daily_arr`) and
  fivetran shopify (two marts that cannot cast fixture values).
- A model that fails on the base the same way, but that the change reaches from upstream,
  is reported as *Could not be checked*, naming what the change modified upstream,
  instead of under *Unchanged models this change breaks*. It still fails the run. The
  summary adds `unverified_broken_on_base_models`, per-model `unverified_broken_on_base`
  and `skipped_by_unverified`, and `counts.models.unverified_broken_on_base`.
- A "table does not exist" error reads as "was not built, it failed or was skipped
  upstream" only when the table is a model, seed, snapshot or source of the project; a
  table nothing in the project builds reads as a hard-coded table.
- When in doubt, a failure counts against the pull request. Errors are compared as whole
  messages (an enforced contract always opens with the same line), and an error is never
  pre-existing when anything upstream changed, since DuckDB reports only the first error
  in a statement. Downstream of a source whose fixtures changed nothing is judged against
  the base, tests included; with no DBML file that covers a staging model's cast or test
  that changes the schema preflight derives, compared against the one derived from the
  base. A change to `dbt_project.yml`, `packages.yml`, `dependencies.yml`,
  a committed `package-lock.yml`, `selectors.yml` or a checked-in `profiles.yml` turns
  the base comparison off.
- Summary JSON `schema_version` is 2. `failing_tests`, `counts.tests.failed`,
  `counts.models.failed` and `counts.models.skipped` now hold only what the change answers
  for; `preexisting_failing_tests`, `broken_on_base_models`, `counts.tests.failed_on_base`,
  `counts.models.failed_on_base`, `counts.models.skipped_by_base`, per-model
  `tests_failed_on_base`, `broken_on_base` and `skipped_by_base`, and `base_failures` are
  new. See docs/integration.md for the full list.
- A failing test is judged against the base branch. The base now runs the same tests on
  the same fixtures, before the pull request is built, and a test fails the check only
  when it is new, passed on the base, or fails on more rows than it did there. One that
  fails the same way on both branches, usually because synthetic data cannot satisfy it,
  is listed under a folded *Already failing on the base branch (N)* and the verdict is
  `passed_with_warnings`. Such tests are left out of the build and run after it, so they
  no longer skip every model downstream: on dbt-labs/jaffle-shop one
  `expression_is_true` test that fails on both branches used to skip `customers`,
  `orders` and `order_items` and list them as broken by the change. *Unchanged models
  this change breaks* lists only models with failures the change caused. A model that
  fails to build, and a test failure the change caused (new, edited, or worse than on the
  base), still skip what depends on them. A test the pull request edited is never
  compared with its base result, since dbt keeps its unique id through an edit to a
  singular test's SQL, a unit test's rows or a generic test's config.
  Runs without `--base-ref` behave as before.
- A pull request that only adds or edits a test now runs it: tests count toward
  `state:modified`, and the model a modified test is declared on is a changed model.
  Before, such a pull request reported that nothing had changed. The same holds for a test
  added on a seed and for an edited snapshot no model reads.
- A snapshot with a legacy `target_schema` writes to the same table on both branches, so
  it is no longer built on the base: it, and every model reading it, is built for the pull
  request only, not compared with the base, and the comment says so.
- The base branch is built once, before the pull request, and covers the whole selection
  rather than only what the diff compares. A pull-request run now makes one more dbt
  invocation on the base (its tests), and one more on the head when any test already
  fails on the base.
- Requires model2data 1.10.3 or newer, the first release that promises byte-identical
  output for the same schema and seed, and the lock now tests against 1.11.0 (it had stayed
  on 1.7.1, while a fresh install already resolved the newest release).

## [0.3.0] - 2026-09-16

### Added

- An `accepted_values` test on a staging column now carries back to its source column, the
  way `unique` and `not_null` already did, and the column is generated as a DBML enum of
  exactly those values. Without it the generator filled an enum-shaped column with
  placeholder text and the project's own test rejected every row, which on a project with
  a few enum columns was the difference between a green run and a red one. The values are
  the test's own arguments, so unlike the other two carry-backs this one arrives with its
  answer attached. A test declared on the source column itself is used the same way, and
  wins over one carried back from staging.

  Only a string column becomes an enum: an enum's values are strings, so turning a numeric
  column into one would change its type to satisfy a test. A test whose values could not
  survive a DBML enum block - empty, or carrying a quote, a brace or a newline - leaves the
  column exactly as it would have been rather than emitting a schema that does not parse.

## [0.2.1] - 2026-09-16

### Fixed

- A `unique`/`not_null` test on a staging column stopped carrying back to its source
  column as soon as the staging model wrapped the column in a `cast()`. A staging layer
  over a schemaless loader is where types get pinned, so such a project casts every column
  it selects, which meant it carried nothing at all: every source column came out
  nullable, the generator put its usual share of nulls in, and every staging `not_null`
  test failed against the fixtures. Found in a project where that skipped every model
  downstream of staging, so the models a pull request actually touched were never built.
  A cast preserves identity and nullness, which is exactly what the carry-back needs, so
  it is now seen through - `cast(x as t)`, `try_cast(x as t)` and `x::t`, nested or not.
  `lower(email)` keeps nullness but not identity and `coalesce(x, 0)` turns a nullable
  column non-null; neither carries back, nor does a cast wrapped around either.

- `dbt-preflight --version` reported 0.1.0 on the 0.2.0 release, and the run's first
  progress line with it. The version was hardcoded in `dbt_preflight/__init__.py` and had
  never been bumped alongside `pyproject.toml`. It is now read from the installed package
  metadata, so there is one source of truth and the two cannot drift again. A test asserts
  the two agree.

## [0.2.0] - 2026-09-16

### Added

- Metrics whose inputs live on different models are evaluated. A `ratio` or `derived`
  metric in dbt's semantic layer that divides orders (on `fct_orders`) by customers (on
  `dim_customers`) used to be reported as "inputs live on different models"; now each
  input is evaluated on its own model, on both sides of the diff, and the scalars are
  combined in DuckDB with the metric's own arithmetic. An input model the change did not
  reach was not built on the base branch, and being untouched its head value stands for
  both sides. The metric is listed under the first of its models the change reached, with
  the label saying which models it reads (`Orders per customer (across `fct_orders`,
  `dim_customers`)`), and the summary JSON carries the same list under `spans`. A
  `cumulative` metric with no window and no grain to date is a running total over all
  time, whose final value is exactly the plain aggregate, so it is evaluated as one; a
  windowed or grain-to-date cumulative metric still says it needs a time spine, now naming
  the window, and a conversion metric says it joins two events over a window. The bundled
  example gained a `customers` semantic model and an orders-per-customer ratio to show it.
- README links are absolute, so the page renders with working links on PyPI.
- The comment's size budget now covers metric breakdowns: only the first three moved
  metrics show their per-dimension breakdown in full, the rest fold under one `<details>`
  block. A change to a fact table moves every metric on it at once, and fifteen moved
  metrics over two dimensions were thirty bullets, longer than the rest of the comment.
- `dbt-preflight run --summary-file PATH` writes a JSON document alongside the comment:
  `verdict`, `exit_code`, counts, and every model, failing test, violation, diff and
  fixture as structured data, for a hook, a bot or a plugin to act on without parsing
  Markdown. It is derived from the same `PreflightReport` the comment renders from, in one
  function (`dbt_preflight/summary.py`), so the two can never disagree. `docs/integration.md`
  is the contract for anything wiring preflight in: every flag, the exit code, the JSON
  schema, and a worked pre-pull-request hook.
- The output diff detects a rename (a dropped and an added column of the same type with the
  same values) and reports it as one, lists where a removed or renamed column was referenced
  on the base branch (YAML entry, Lightdash meta, semantic layer, tests, downstream models),
  and profiles added columns (type, nulls, value distribution when low-cardinality). All
  three came from watching a coding agent use the comment.
- A size budget on the comment: only the first three failing tests are shown in full, the
  rest fold under one `<details>` block; "Also rebuilt" lists at most five model names and
  says how many more; a build error past the first folds too, one model per block.
- Generic tests (`unique`, `not_null`, `accepted_values`, `relationships`) are rendered from
  their own metadata instead of dbt's generated name, e.g. `unique` on
  `stg_webshop__customers.customer_id`, or for `relationships`, both sides of the join.
  dbt's raw test name is kept inside the details block.
- A one-line plain-English reading of common DuckDB error shapes (a dropped column, a
  renamed column, a model that was never built, an unknown function, a parse failure),
  shown above the raw message on a build error and on an errored test. A shape not covered
  falls through to the raw message unchanged.
- Row counts and rows-with-different-values in "What changed in the output" carry their
  percentage change, e.g. `rows 800 → 742 (-7.3%)` and `30 rows with different values (20%)`.
- Per-step timing on the run's stderr progress lines (parse, fixtures, build, base parse,
  base build, diff), each tagged with seconds since the previous step. Added while
  scale-testing preflight against a 120-model project; the review comment itself is
  unchanged. On that project, head parse and build together account for most of a run, and
  the base build (which rebuilds ancestors already built on head) is not the dominant cost
  it was expected to be, so no build-time optimisation landed alongside it.
- Sources with no `data_type` in `sources.yml` - or no columns declared at all, as
  jaffle-shop and other real projects do - get their columns inferred from the staging
  models that read them, instead of failing with a SchemaError. An explicit `cast(x as T)`
  or `x::T` sets the type; the rest is guessed from the column name. The fixtures block in
  the comment says which sources were inferred and which columns were guessed.
- model2data's own warnings about the fixtures it generated - columns filled with generic
  placeholder text, tables stuck in an unresolved foreign-key cycle, composite keys left
  with duplicate combinations, DBML lines it could not parse - are surfaced in the
  comment's fixtures block, so a reviewer can tell a weak fixture from a strong one.
- Rows-with-different-values is now computed even when the schema changed, over the columns
  both sides share (matching name and type), so an added or renamed column no longer hides a
  value change on every other column: `30 rows with different values (20%, on the 12 columns
  both sides share)`.
- A metric that moved is broken down by the model's categorical dimensions (the dbt semantic
  layer's `categorical` dimensions, or Lightdash `dimension.type: string` meta when there is
  no semantic model), showing the rows with the largest contribution to the move, e.g.
  `Net revenue (EUR) by sales_channel: web 5,210 → 4,980 (-4.4%), mobile_app ...`. Capped at
  three dimensions and three rows each, and only computed for metrics that actually moved, so
  the cost stays proportional to what changed. The model's own primary key and any dimension
  Lightdash marks `hidden` are skipped, since neither makes a meaningful breakdown. A
  cardinality gate (one cheap `count(distinct ...)` per candidate) then skips a dimension
  with only one distinct value or more than twelve — `full_name` and `city` say nothing a
  reviewer can use — and orders what is left by fewest distinct values first.
- Schema inference for a column without an explicit cast now reads how the staging SQL
  uses it before falling back to its name: an operand of `/`, `*`, `+`, `-` against a
  numeric literal, or wrapped in `sum(`/`avg(`/`round(`, is numeric; compared to a string
  literal, or passed to `lower(`/`upper(`/`trim(`/`concat(`, is varchar. The name
  heuristics themselves grew - `paid`, `cost`, `tax`, `fee`, `discount`, `revenue`,
  `subtotal`, `balance`, `margin`, `weight`, `score` now read as decimal, and `count`,
  `number`, `num`, `qty`, `quantity`, `units`, `age`, `year`, `month`, `day`, each matched
  as a whole underscore-separated word so "package" and "average" don't become integers by
  accident, now read as integer - and a column named `email`, `phone`, `name`, `city`,
  `country`, `address`, `sku`, `description`, `status`, `type`, or `category` always stays
  varchar. A column shaped like a foreign key - `customer_id`, or a bare `customer` when a
  `raw_customers` source exists - is typed as an integer and gets a `ref:` to that table's
  `id` when a matching source table can be found, unless an explicit `relationships` test
  already set one. Against a clone of dbt-labs/jaffle-shop with no config file, this took
  preflight from 6 of 13 models built to 10 of 13; the remaining 3 are skipped by dbt
  itself because of one upstream test - `order_total - tax_paid = subtotal` on
  `stg_orders` - that checks an arithmetic relationship between three independently
  generated columns, which no schema inference can satisfy on synthetic data.
- A staging model's own `unique`/`not_null` tests on the alias it gave a source column
  (`id as customer_id`, tested as `customer_id`) now carry back to that source column in
  the derived DBML - `pk` when both are declared on the same alias, `unique`/`not null`
  alone otherwise - so a project that tests its staging models rather than its sources
  still gets keys in the derived schema. A source column named `id` was already the
  primary key regardless.

### Fixed

- The comment no longer reshuffles between runs. Model lists (the changed-models table,
  "Unchanged models this change breaks", "Also rebuilt") and convention violations followed
  dbt's thread-completion order, so a push that changed nothing could still rewrite the
  comment's lists, and the summary JSON with them. Everything a reader sees is ordered by
  model name, and failing tests by model and test name.

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
