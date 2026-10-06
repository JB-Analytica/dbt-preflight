# Integrating preflight into a hook, a bot or a plugin

This is the contract for anything that runs `dbt-preflight run` and has to act on the
result without a person reading the comment first: a pre-pull-request hook that blocks a
coding agent, a bot that comments on a pull request from its own credentials, or a plugin
that wraps preflight for another tool. It covers the invocation, the exit code, the two
files preflight writes, and where the house conventions live so a description of them is
never duplicated by hand.

## Running it

```bash
dbt-preflight run \
  --base-ref origin/main \
  --comment-file preflight-comment.md \
  --summary-file preflight-summary.json
```

Every flag on `run`. The `schema` command's flags follow.

| Flag | Default | What it does |
| --- | --- | --- |
| `--base-ref` | none | Git ref to diff against, e.g. `origin/main`. Without it every model counts as changed and the whole project is built; no diff against a base branch is computed, and every failing test counts against the run, since there is no base to have already failed on. |
| `--config` | `.dbt-preflight.yml` at the repo root | Path to the config file. |
| `--repo-root` | the git root of the current directory | Repository root; the base branch is checked out relative to this. |
| `--comment-file` | none | Write the review comment (Markdown) here instead of stdout. |
| `--summary-file` | none | Write a JSON summary of the run here (see below). Not written if omitted. |
| `--post` | off | Post or update the comment on the pull request through the GitHub API. Needs `GITHUB_TOKEN`, `GITHUB_REPOSITORY` and a pull request number. |
| `--pr` | none | Pull request number, for `--post` when it cannot be read from the Actions event payload. |
| `--fail-on-error` / `--no-fail-on-error` | `--fail-on-error` | Whether a failed check exits 1. |
| `--keep-workdir` | off | Keep `.preflight/` (fixtures, the DuckDB file, dbt artefacts) after the run, for inspection. |
| `--version` | — | Print the version and exit. |

`dbt-preflight schema` writes the schema a run would derive from the project as a DBML file to
keep and edit (see "Keeping the schema" in the README). It needs no warehouse, credentials or
base ref, and exits 0 on success, when `.dbt-preflight.yml` already has `schema:` (nothing is
written), and 1 when it cannot derive a schema or the output exists.

| Flag | Default | What it does |
| --- | --- | --- |
| `--config` | `.dbt-preflight.yml` at the repo root | Path to the config file. |
| `--repo-root` | the git root of the current directory | Repository root. |
| `--output` | `source_system/<dbt project name>.dbml`, in the folder holding `.dbt-preflight.yml` | Where to write the DBML. A `schema:` line copied from the command's output works as printed. |
| `--force` | off | Overwrite an existing output file, and derive a schema even when the config already has `schema:` (the config is never edited). |

## Exit code

- **0** — the check passed (`verdict` is `passed`, `passed_with_warnings` or
  `nothing_changed`).
- **1** — the check failed (`verdict` is `failed`), or preflight could not run at all
  (`verdict` is `could_not_run`), and `--fail-on-error` was in effect.
- **`--no-fail-on-error`** makes the process exit 0 regardless of `verdict`. Read
  `verdict` from the summary file when a caller needs to know pass or fail under this flag;
  the exit code alone will not tell you. `--summary-file`'s own `exit_code` field records
  what the process actually returned, which is 0 in this case even for a failed run.

## The two output files

- **`--comment-file`** (Markdown): what a person reads, in the pull-request sidebar or a
  job summary. Built for that context: it leads with the verdict, folds detail behind
  `<details>` once a section grows past a few items, and reads DuckDB errors into plain
  English.
- **`--summary-file`** (JSON): what a hook or an agent reads. Everything in it is derived
  from the same report the comment renders from (`dbt_preflight/summary.py`), so the two
  can never disagree; reading the summary never requires parsing the Markdown.

| Read this from... | ...for |
| --- | --- |
| Summary `verdict` | Pass/fail/could-not-run, without reading the comment at all. |
| Summary `counts` | Whether anything needs a look, before rendering a single line of the comment. |
| Summary `models` / `failing_tests` / `violations` / `diffs` | The specific things to fix, each with a path. |
| Comment file | What to show a person, or paste into a pull-request description. |

## The summary JSON

`schema_version` is `2` (see "Changes in schema version 2" below). Top-level keys:

| Key | Type | What it holds |
| --- | --- | --- |
| `schema_version` | integer | Bumped when a key's meaning or shape changes, not when a key is only added. |
| `verdict` | string | One of `passed`, `passed_with_warnings`, `failed`, `could_not_run`, `nothing_changed`. A run whose only failures, tests or model builds, also fail on the base branch is `passed_with_warnings` (see below). |
| `exit_code` | integer | What the process actually exited with (0 or 1; see above). |
| `base_ref` | string or null | The `--base-ref` this run was given. |
| `head` | string or null | The head commit's SHA. |
| `elapsed_seconds` | number | Total run time. |
| `fatal` | string or null | Set only on `could_not_run`: why preflight could not run at all. |
| `note` | string or null | One line of context also shown under the comment's summary line, e.g. why every source counted as modified. |
| `counts` | object | `models` (built/failed/skipped/not_verified/no_result/failed_on_base/skipped_by_base/unverified_broken_on_base/fixture_limited/skipped_by_fixture_limited), `tests` (passed/failed/warned/failed_on_base), `violations` (error/warn), `metrics` (defined/moved). `models.failed`, `models.skipped` and `tests.failed` count only what the change answers for; `models.failed_on_base` counts models that fail to build on the base branch too, `models.skipped_by_base` the models skipped only because of one of those, `models.fixture_limited` the models the change does not reach that preflight's generated data cannot build, `models.skipped_by_fixture_limited` what only those skipped (neither is in `failed` or `skipped`), and `tests.failed_on_base` the tests that also fail on the base branch. |
| `models` | array | Every model in the run's selection: name, path, status, changed, rows, tests_passed/failed/warned, tests_failed_on_base, broken_on_base, skipped_by_base, unverified_broken_on_base, skipped_by_unverified, fixture_limited, skipped_by_fixture_limited, dialect_function. `status` stays dbt's (`failed`, `skipped`); the two booleans say it is not this change's doing. |
| `failing_tests` | array | Every failing or warning test the change answers for: name (dbt's own), readable_name (a generic test's own name and target, when it has one), model, status, failures, base_failures (the base branch's failing-row count whenever the same, unedited test also failed there with rows; null when it passed, errored or is new or edited. In this list a non-null value is always smaller than `failures`: the test got worse), reading (a one-line plain-English reading of the DuckDB error, when there is one), guessed_inputs (source columns it reads, directly or upstream, whose type preflight guessed; an error there is not judged against the base, failing rows still can be pre-existing). A test that fails the same way on the base branch is not here but in `preexisting_failing_tests`. |
| `preexisting_failing_tests` | array | Failing tests that fail on the base branch the same way, on at least as many rows: same keys as `failing_tests`. They do not fail the run. Empty without `--base-ref`. |
| `fixture_limited_models` | array | Models the change does not reach that fail the same way on the base branch over preflight's generated data: name, unique_id, error, reason (the kind of value, e.g. `malformed JSON`, or "reads `x.y`, whose type preflight guessed"), fixture_error, guessed_inputs. A warning: they do not fail the run (`passed_with_warnings`). Empty without `--base-ref`. |
| `unverified_broken_on_base_models` | array | Models that fail on the base branch the same way, but that the change reaches, so they could not be checked: name, unique_id, error, reached_from (what the change modified upstream of it), guessed_inputs (the source columns it reads, as `table.column`, whose type preflight guessed; when set, that is why it could not be checked), fixture_error (when the shared error is about a generated value, what kind: `malformed JSON`, `an invalid timestamp`, `an invalid date`, `an invalid time`, `a date or time format it could not parse`, `a failed cast`; null otherwise). They do fail the run, and are counted in `counts.models.failed`. Empty without `--base-ref`. |
| `broken_on_base_models` | array | Models that fail to build on the base branch too, the same way, without this change touching them: name, unique_id, error (DuckDB's error line). They do not fail the run. Empty without `--base-ref`. |
| `violations` | array | Convention violations: rule, severity, model, path, message. |
| `diffs` | array | Base-versus-head comparison for changed models and everything downstream: rows, added/removed/retyped/renamed columns, moved metrics with base and head values (`spans` names the models a metric reads when it reads more than one, e.g. a ratio of orders to customers; empty otherwise), and where a removed or renamed column was referenced on the base branch. |
| `fixtures` | object or null | The synthetic data generated: tables and rows, sources whose columns were inferred rather than declared (`inferred_sources`; each lists `guessed_columns`, the subset typed `varchar` because a reader could not be followed as `unknown_columns`, `compiled_columns` and `type_conflicts`), sources skipped because nothing reads them (`skipped_sources`), the text columns filled with JSON because a model parses them as JSON (`json_columns`, as `identifier.column`), and model2data's own warnings. `guessed_sources` is the number of sources with at least one guessed column (the count behind the comment's pointer to `dbt-preflight schema`); 0 when nothing was guessed. |
| `comment_file` | string or null | The `--comment-file` path this run was given, or null if none. |

### Failing tests and the base branch

With `--base-ref`, the base branch runs the same tests on the same fixtures before the pull
request is built, and each failing test is matched to its base result by dbt's unique id. A test the pull
request added or edited never is: the unique id survives an edit to a singular test's SQL,
a unit test's rows or a generic test's config (`where`, `severity`, `error_if`, ...), so a
test that `state:modified` selects is always judged as new. Otherwise:

| On the base branch | On the pull request | Counts against the run |
| --- | --- | --- |
| absent, passed or skipped | fails or errors | yes, in `failing_tests` |
| fails on *n* rows | fails on more than *n* rows | yes, in `failing_tests`, with `base_failures: n` |
| fails on *n* rows | fails on *n* rows or fewer | no, in `preexisting_failing_tests` |
| errors | errors with the same error message, and nothing the test reads was touched by the change | no, in `preexisting_failing_tests` |
| errors | errors with the same message, but something it reads was touched | yes, in `failing_tests` |
| fails | errors, or the reverse | yes, in `failing_tests` |

Pre-existing failures make the verdict `passed_with_warnings` rather than `passed`: they
are a real finding about the project, only not this change's. Tests already failing on the
base are run after the build rather than inside it, so a pre-existing failure never skips
the models downstream of it. One that turns out worse on the pull request is the change's
failure, and is reported the way a single `dbt build` would have: the models downstream of
what it tests show as skipped.

Rows are what dbt reports as `failures`, so for a test that counts groups rather than rows
(`accepted_values` counts distinct rejected values) "more rows" means more of those. The
comparison is by count: a change that fixes some failing rows and breaks as many others
reads as pre-existing.

"The same error" means the whole message, line for line, after normalising what differs
between the two sides without meaning anything: the base target's schema names, the
caret line under the failing column, timings, and the base checkout's path. Not the first
line alone: an enforced contract always opens with the same sentence and names the wrong
columns below it. And DuckDB stops at the first error in a statement, so an error that
reads the same can hide a new one behind it: an error is never pre-existing when anything
the test reads, directly or upstream, was touched by the change.

### Model builds and the base branch

A model that fails to build is judged the same way. It is *broken on the base too*, and
does not fail the run, when all of these hold: it failed to build on the base branch; the
error message is the same on both sides, normalised as above; and nothing the change
touched is the model itself or upstream of it, including an ephemeral model inlined into
it. A model the change modified or added, one below anything it modified, one that built
on the base, and one that fails there with a different error count against the run.

A model skipped on the pull request has `skipped_by_base: true`, and stays out of
`counts.models.skipped`, only when all of these hold: a model broken on the base too is
upstream of it; it was skipped on the base branch as well; the change did not modify it,
add it, or change its fixtures; and nothing else the change broke is upstream of it (a
model failing only on head, a seed or snapshot that failed, or a test the change made
fail). Anything else skipped counts. The comment lists broken models and what they skip
in an unfolded *Broken on main too* section above *Changed models*, and points there at
the tests already failing on the base, whose details stay folded further down.

A model that fails on the base the same way over preflight's own data is not *broken on the
base too* either, since main is not what failed. Over preflight's data means one of two
things: it reads a source column whose type preflight guessed (no DBML, and the column
untyped in `sources.yml` and in the SQL), directly or upstream (`guessed_inputs`); or the
error is about a value rather than the SQL - malformed JSON, a timestamp, date or time that
does not parse, a failed cast of a string - in a model with a source upstream
(`fixture_error`; the patterns are DuckDB's error text, listed in one place,
`baseline.FIXTURE_SHAPED_ERRORS`). A compilation error is never one: it happens before any
data is read. Where it goes depends on whether the change reaches the model (it modified
it, the model reads fixtures the change reshaped, or either is upstream of it,
`cli._reached`):

- **Reached:** *Could not be checked*, with the reason named. It counts against the run
  (`unverified_broken_on_base: true`, in `counts.models.failed`).
- **Not reached:** the model builds from identical SQL on identical data on both branches,
  so the change cannot have affected it. It goes in an unfolded *Preflight's generated data
  cannot build this model* section next to *Broken on main too*, with the reason, and is a
  warning (`fixture_limited: true`, in `fixture_limited_models` and
  `counts.models.fixture_limited`). What only it skips is listed with it
  (`skipped_by_fixture_limited`) and does not count either.

A test over a model reading a guessed column, or over a guessed source column, carries
`guessed_inputs` too. If it errors on the base (a type mismatch or failed cast on guessed
data), its base result is dropped, so it counts. If it fails on rows on both branches, it
stays pre-existing, with the guessed columns named. "Reads" is column by column, as far as
inference can tell: the models whose SQL names the column (for a column typed `varchar`
because a reader could not be followed, those readers), and everything downstream of them.

A model that fails on the base the same way but has something the change modified upstream
of it is neither (below a source whose fixtures changed the base ran on the change's data,
so there the error is the change's own and reads as such): it counts against the run (`status: failed`, in `counts.models.failed`),
but the comment does not claim the change broke it. It is listed under *Could not be
checked*, naming what the change modified upstream, with `unverified_broken_on_base: true`
in the summary and in `counts.models.unverified_broken_on_base`. What only it skips is
listed there as skipped because of it (`skipped_by_unverified: true`), still counted in
`counts.models.skipped`. A model that built on the base and fails on the pull request keeps
the plain *Unchanged models this change breaks* and *Build errors* wording.

### When the base is not used at all

When in doubt, preflight counts against the pull request. Downstream of a source whose
fixtures changed, neither tests nor model builds are judged against the base, because the
base is built on the head's fixtures and there it runs on data its own code was not written
for. A source's fixtures count as changed when:

- the DBML file named by `schema:` changed, or `.dbt-preflight.yml` changed (every source);
- dbt's `state:modified` selects the source (an edit to its `sources.yml` entry);
- with no `schema:`, the DBML preflight derives from the head differs, for that source's
  table, from the one it derives from the base: columns, types, keys, refs or enum values.
  A staging model's casts, its `unique`/`not_null`/`accepted_values` tests and the
  columns it reads all shape the derived schema, so editing one staging model can change
  what every reader of its source gets. A base that cannot be derived at all counts as
  every source changed.

A change to `vars:` in `.dbt-preflight.yml` counts as a config change that reshapes the
fixtures, like `env:` or `dialect:`: the vars are passed to every dbt command preflight
runs (`--vars`), so they change what the models compile to.

And when `dbt_project.yml`, `packages.yml`, `dependencies.yml`, `package-lock.yml`,
`selectors.yml` or a checked-in `profiles.yml` in the project directory changed, nothing is
judged against the base: dbt's state comparison does not see vars or package versions. The
base is still built for the diff, and `note` says so. `package-lock.yml` is compared by
commit, not in the working tree, because preflight's own `dbt deps` rewrites it there.

### Changes in schema version 2

Version 2 came with 0.4.0, which judges failures against the base branch. Keys whose
meaning narrowed:

- `failing_tests` holds only what the change answers for, plus warnings; tests that fail
  the same way on the base moved to the new `preexisting_failing_tests`.
- `counts.tests.failed`, `counts.models.failed` and `counts.models.skipped` count only what
  the change answers for.

Added: `preexisting_failing_tests`, `broken_on_base_models`, `counts.tests.failed_on_base`,
`counts.models.failed_on_base`, `counts.models.skipped_by_base`, per-model
`tests_failed_on_base`, `broken_on_base` and `skipped_by_base`, and `base_failures` on each
failing test. A consumer that only reads `verdict` or `exit_code` needs no change; one that
wants every failure regardless reads `preexisting_failing_tests` and
`broken_on_base_models` alongside the narrowed keys.

Added since, without a version bump (0.5.0): `fixture_limited_models`,
`counts.models.fixture_limited` and `counts.models.skipped_by_fixture_limited`, per-model
`fixture_limited` and `skipped_by_fixture_limited`, `fixture_error` and `guessed_inputs` on
`unverified_broken_on_base_models`, and `fixtures.json_columns`.

## The comment's marker

Every comment preflight posts or writes starts with the line `<!-- dbt-preflight -->`.
That is how `--post` finds the existing comment to update instead of stacking a new one on
every push (`dbt_preflight/github.py`), and it is the reliable way for anything else reading
comments off the GitHub API to recognise preflight's own comment among a pull request's
others.

## Environment variables

Read by `dbt-preflight run --post` and by the bundled GitHub Action (`action.yml`):

| Variable | Used for |
| --- | --- |
| `GITHUB_TOKEN` | Authenticates the GitHub API calls `--post` makes to create or update the comment. Needs the `pull-requests: write` permission. |
| `GITHUB_REPOSITORY` | `owner/repo`, so `--post` knows which repository's API to call. |
| `GITHUB_EVENT_PATH` | The Actions event payload, read to find the pull request number when `--pr` is not given. |
| `PREFLIGHT_FORK` | Set by `action.yml`, not read by the CLI itself: `true` when the pull request comes from a fork, which runs with a read-only token, so the action skips `--post` and relies on the job summary instead. A caller driving `dbt-preflight run` directly has no need for it. |

## Where the conventions are defined

Preflight is the source of truth for the house conventions; a skill that describes them
should point here rather than restate them. The rules themselves live in
`dbt_preflight/conventions.py`: the `jba()` function is the JB Analytica preset (naming,
layering, a tested primary key, descriptions, column-naming), `none()` turns every rule
off, and `from_config()` reads the `conventions:` block of `.dbt-preflight.yml` to adjust
severities, swap in a project's own layer patterns, or change which folder is allowed to
read `source()`. README.md's "Conventions" section is the human-readable version of the
same rules; `dbt_preflight/checks.py` is where they are checked against the manifest and
the built tables.

## A pre-pull-request hook, worked example

A hook that runs before `gh pr create` (or before a coding agent opens a pull request),
blocking when the dbt project the agent changed would fail preflight:

```bash
#!/usr/bin/env bash
set -euo pipefail

dbt-preflight run \
  --base-ref origin/main \
  --comment-file .preflight-comment.md \
  --summary-file .preflight-summary.json \
  --no-fail-on-error   # the hook decides what to do with the result, not the process exit

verdict=$(python3 -c "import json; print(json.load(open('.preflight-summary.json'))['verdict'])")
# or, with jq: verdict=$(jq -r .verdict .preflight-summary.json)

echo "--- dbt-preflight: $verdict ---"
cat .preflight-comment.md

if [ "$verdict" = "failed" ] || [ "$verdict" = "could_not_run" ]; then
  echo "preflight did not pass; fix the issues above before opening the pull request." >&2
  exit 1
fi
```

`--no-fail-on-error` is deliberate here: the hook reads `verdict` itself rather than
trusting the process exit, so it can treat `passed_with_warnings` and `nothing_changed` as
fine while still failing on `failed` or `could_not_run`, without preflight's own
pass/fail rule (which already treats warnings as passing) getting in the way.
