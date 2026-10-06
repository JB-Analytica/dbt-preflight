# How preflight works

What a run does, step by step, and how it decides whether a failure is the pull request's
doing. The exact rules, as a consumer of the summary JSON needs them, are in
[integration.md](integration.md#failing-tests-and-the-base-branch); configuration is in
[configuration.md](configuration.md).

## A run, step by step

1. `dbt parse` on the pull request, to learn the sources, models and tests.
2. Fixtures: model2data generates data for every source table, cast to the declared types,
   loaded into a DuckDB file whose catalog is named after the sources' `database`.
3. `dbt parse` on the base branch in a temporary worktree, then `dbt ls --select
   state:modified` to find what changed.
4. Each compiled model is transpiled from the project's dialect to DuckDB with sqlglot,
   when DuckDB does not accept it as written.
5. The selection is closed: downstream models, models whose tests read a changed model,
   and all their ancestors. `dbt build` runs on that set with `--indirect-selection
   cautious`, so every test that runs has all its inputs built.
6. Conventions are checked on the changed models, column rules against the built tables.
7. The changed models and everything downstream are built on the base branch too, into
   their own schemas on the same fixtures, and compared: columns, row counts, differing rows
   and metric values.
8. One Markdown comment, posted or updated through the GitHub API.

Every run is a full build on a fresh DuckDB file that lives for the length of the job.

## What it checks

- The changed models, everything downstream of them, and every model whose tests read them,
  compile and run against a schema-faithful dataset. The seeds and snapshots those models
  read are loaded first, on both branches. A pull request that only adds or edits a test
  runs that test, and builds what it reads.
- Their schema, relationship and accepted-values tests pass or fail, and which rows fail.
- Column renames, dropped `ref()`s and broken joins are caught before merge. dbt unit tests
  run too, and a failing one fails the check.
- What the change did to the output. The base branch is built on the same fixtures, and the
  changed models plus everything downstream are compared: columns added, removed or
  retyped; row counts; rows whose values differ; and every metric the project defines,
  evaluated on both sides, overall and per segment. A refactor that moves net revenue by
  4 percent shows up as a number before a reviewer has to reason about the SQL.
- The change follows the house conventions ([configuration.md](configuration.md#conventions)).

## Whether a failing test is this change's doing

The base branch runs the same tests on the same fixtures, and a test fails the check only
when it is new on the pull request, passed on the base branch, or fails there on fewer rows
than it does now. A test that fails the same way on both branches, often because synthetic
data can never satisfy it, is listed under *Already failing on the base branch*, folded,
and the check passes with warnings.

It does not stop anything downstream from building either: tests that fail on the base are
left out of the build and run after it, so the rest of the pull request is still checked. A
test fails "the same way" when it returns rows on both sides and no more on the pull
request, or errors on both sides with the same error message, line for line, and nothing it
reads was touched by the change (DuckDB stops at the first error, so an old one can hide a
new one). A test that returned rows on the base and errors on the pull request counts
against it, and so does any test the pull request added or edited. `accepted_values` is
judged like every other test. A model that fails to build still skips what depends on it,
and so does a test failure the change caused, including one that fails worse than on the
base.

## Whether a broken model is this change's doing

A model that fails to build is judged the same way. A model that fails on the base branch
too, with the same error message, and has nothing the change touched upstream of it (a
hard-coded relation no branch builds, a fixture type it cannot cast) does not fail the
check: it leads the comment, unfolded, under *Broken on main too*, with its error, the
models it skipped on both branches, and a line pointing at any tests that already fail
there. The check passes with warnings.

A model the change modified, added, or reaches from upstream, one that built on the base,
or one that fails there with a different error still fails the check. One that fails the
same way on the base but sits below something the change touched still fails it, under
*Could not be checked*: DuckDB reports only the first error, so a new one could hide behind
the old, but the change is not known to have broken it either. The same goes for a model
that reads a source column whose type preflight guessed: both branches ran on the guess, so
the shared failure may be the guess's. A model skipped on the pull request is put down to a
broken model only when it was skipped on the base too and the change neither touched it nor
broke anything else above it; otherwise it counts as broken by the change.

## When the base branch is not used

When in doubt preflight counts against the pull request, so some things are never judged
against the base. Downstream of a source whose fixtures changed, neither models nor tests
are, since the base ran on data its code was not written for. A source's fixtures change
when the DBML file changes, when a key of `.dbt-preflight.yml` that shapes the fixtures
changes, when dbt sees the source itself as modified (`sources.yml`), and, with no DBML
file, when the schema preflight derives from the project differs from the one it derives
from the base: a staging model's cast, its `unique`/`not_null`/`accepted_values` tests or
the columns it reads all shape that schema.

And when `dbt_project.yml`, `packages.yml`, `dependencies.yml`, `package-lock.yml`,
`selectors.yml` or a checked-in `profiles.yml` changed (the lock file by commit, since
`dbt deps` rewrites it in the working tree), nothing is judged against the base at all:
dbt's own comparison does not see vars or package versions.

Without `--base-ref` there is nothing to compare against, so every failing test and every
model that fails to build counts.

## The bundled example

The project in `examples/webshop/` is the JB Analytica
[reference architecture](https://github.com/JB-Analytica/reference-architecture)'s dbt
project, made portable with two `adapter.dispatch` macros. It is what the test suite, the
action's self-check and the [recorded scenarios](scenarios/README.md) run against.
