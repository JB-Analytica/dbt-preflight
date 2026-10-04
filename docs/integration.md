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

Every flag on `run`:

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

`schema_version` is `1`. Top-level keys:

| Key | Type | What it holds |
| --- | --- | --- |
| `schema_version` | integer | Bumped when a key's meaning or shape changes, not when a key is only added. |
| `verdict` | string | One of `passed`, `passed_with_warnings`, `failed`, `could_not_run`, `nothing_changed`. A run whose only failing tests also fail on the base branch is `passed_with_warnings` (see below). |
| `exit_code` | integer | What the process actually exited with (0 or 1; see above). |
| `base_ref` | string or null | The `--base-ref` this run was given. |
| `head` | string or null | The head commit's SHA. |
| `elapsed_seconds` | number | Total run time. |
| `fatal` | string or null | Set only on `could_not_run`: why preflight could not run at all. |
| `note` | string or null | One line of context also shown under the comment's summary line, e.g. why every source counted as modified. |
| `counts` | object | `models` (built/failed/skipped/not_verified/no_result), `tests` (passed/failed/warned/failed_on_base), `violations` (error/warn), `metrics` (defined/moved). `tests.failed` counts only failures the change answers for; `tests.failed_on_base` the ones that also fail on the base branch. |
| `models` | array | Every model in the run's selection: name, path, status, changed, rows, tests_passed/failed/warned, tests_failed_on_base, dialect_function. |
| `failing_tests` | array | Every failing or warning test the change answers for: name (dbt's own), readable_name (a generic test's own name and target, when it has one), model, status, failures, base_failures (the base branch's failing-row count whenever the same, unedited test also failed there with rows; null when it passed, errored or is new or edited. In this list a non-null value is always smaller than `failures`: the test got worse), reading (a one-line plain-English reading of the DuckDB error, when there is one). A test that fails the same way on the base branch is not here but in `preexisting_failing_tests`. |
| `preexisting_failing_tests` | array | Failing tests that fail on the base branch the same way, on at least as many rows: same keys as `failing_tests`. They do not fail the run. Empty without `--base-ref`. |
| `violations` | array | Convention violations: rule, severity, model, path, message. |
| `diffs` | array | Base-versus-head comparison for changed models and everything downstream: rows, added/removed/retyped/renamed columns, moved metrics with base and head values (`spans` names the models a metric reads when it reads more than one, e.g. a ratio of orders to customers; empty otherwise), and where a removed or renamed column was referenced on the base branch. |
| `fixtures` | object or null | The synthetic data generated: tables and rows, sources whose columns were inferred rather than declared, and model2data's own warnings. |
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
| errors | errors with the same error | no, in `preexisting_failing_tests` |
| fails | errors, or the reverse | yes, in `failing_tests` |

Pre-existing failures make the verdict `passed_with_warnings` rather than `passed`: they
are a real finding about the project, only not this change's. Tests already failing on the
base are run after the build rather than inside it, so a pre-existing failure never skips
the models downstream of it. One that turns out worse on the pull request is the change's
failure, and is reported the way a single `dbt build` would have: the models downstream of
what it tests show as skipped.

Rows are what dbt reports as `failures`, so for a test that counts groups rather than rows
(`accepted_values` counts distinct rejected values) "more rows" means more of those. This is additive to schema version 1: a consumer that reads
`verdict`, `counts.tests.failed` or `failing_tests` keeps getting what the run blames on
the change. A consumer that wants every failing test regardless reads both lists.

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
