# Real-world regression suite

Four public dbt projects, each run through three kinds of pull request, so a release is
measured against code we did not write and not only against `examples/webshop`.

| Project | Why it is here |
| --- | --- |
| `jaffle_shop_classic` | dbt-labs/jaffle_shop: 5 models, seeds only, no config |
| `jaffle_shop_current` | dbt-labs/jaffle-shop main: dbt 2.0 project (its `require-dbt-version` is relaxed in the base commit) |
| `mattermost` | Snowflake project in a subdirectory, needs `.dbt-preflight.yml` and `env:` |
| `shopify` | fivetran/dbt_shopify via `integration_tests`, with a hand-written DBML as `schema:` |

Changes: `harmless` (a SQL comment in one staging model), `rename` (one column renamed in a
staging model), `full` (comments in 3 or 4 staging models).

## Run it

```bash
uv run poe realworld                                  # everything, prints before -> after
uv run poe realworld --compare                        # exit 1 if anything got worse
uv run poe realworld --project shopify --change rename
```

It needs network and takes about four minutes. Projects are fetched shallow, by pinned SHA,
into `$REALWORLD_CACHE` (default: `<system temp>/preflight-realworld`); summaries, comments
and logs per run land in `<cache>/out` (`--out-dir` to change). Each run resets the cached
clone to the pinned commit, commits the setup as base and the change as head, then runs
`dbt-preflight run --base-ref <base> --no-fail-on-error`. `--preflight "<cmd>"` runs a
different build of the tool instead of this environment's.

Not part of `poe check` or the default pytest run. The pure parts (manifest, result
extraction, comparison) are unit-tested in `tests/test_realworld.py`.

## What is compared

`baseline.json` holds one row per project and change. `--compare` fails when, against it,
models built go down, failed, not verified or no-result go up, a rename that used to be
detected is not, or the verdict is worse (passed < passed_with_warnings < failed <
nothing_changed < could_not_run). Improvements and wall time never fail. Rows with no
baseline entry are reported but do not fail.

"Rename detected" means preflight's diff lists the column as renamed, or a failing test says
the old column no longer exists.

## Targets

`manifest.yml` says per project what 0.4.0 should achieve (`harmless: passes`,
`rename: caught`). The table's `target` column shows whether each is met. It is informational:
only the baseline decides the exit code.

## Updating the baseline

Deliberately, after reading the table, in the same commit as the change that moved it:

```bash
uv run poe realworld --write-baseline                      # all rows
uv run poe realworld --project shopify --write-baseline    # only those rows are replaced
```

Never to silence a regression you do not understand.

## Changing the manifest

- Edits are `prepend` (text at the top of a file), `replace` (literal, `old` must match
  exactly once) or `copy` (a file from `files/`). No sed, so it behaves the same everywhere.
- Moving a pinned SHA changes every number for that project: update the baseline with it.
- An edit with `workaround:` papers over a preflight bug. It stays until `remove_when` is
  true; then delete it and re-baseline.
