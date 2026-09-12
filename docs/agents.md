# Using preflight from a coding agent

Preflight is the feedback loop that lets an agent work on a dbt project without a warehouse.
An agent that runs it before opening a pull request, reads the comment, and fixes what it
reports produces changes a reviewer can merge without re-running anything.

## Instructions to give the agent

Add this to the repository's `CLAUDE.md`, `AGENTS.md` or equivalent:

```markdown
## Verifying dbt changes

Before you open or update a pull request that touches `models/`, `macros/` or the source
schema, run:

    dbt-preflight run --base-ref origin/main --comment-file .preflight-comment.md

Read `.preflight-comment.md`. It lists the models your change reaches, which built, which
tests failed and on how many rows, and which house conventions the change breaks, each
with a file path. Fix every ❌ before opening the pull request. A ⚠️ "not verified" means
the model uses a function DuckDB lacks; leave it unless the task is portability.

Do not add warehouse credentials to get a model verified. Preflight is warehouse-free by
design; if it cannot run something, say so in the pull request description.
```

## What the comment gives an agent

- **A file path on every finding.** Convention violations point at the SQL file or, when
  the fix belongs in YAML, at the YAML file that describes the model.
- **The compiled SQL of every failing test**, inside a `<details>` block, so the agent can
  see the exact query that failed rather than guessing from the test name.
- **The reach of the change.** "Unchanged models this change breaks" tells the agent which
  other files it has to touch. A renamed column is a three-file change, not one.
- **Row counts.** A changed model that builds with zero rows almost always has a filter or
  a join that no longer matches anything; the count makes that visible without a query.
- **The diff against the base branch.** Row counts, rows whose values changed, columns
  that appeared or disappeared, and the metrics that moved, with base and PR values. An
  agent asked for "a refactor with no behaviour change" can read "Identical output to the
  base branch" as proof, and a moved metric as the thing to explain in the pull request.
- **What a removed or renamed column was wired to.** For each one, the comment lists its
  references on the base branch: the YAML column entry, Lightdash meta, semantic-layer
  expressions, tests and downstream models. Every item on that list needs the new name.
  "No reference on the base branch" means only consumers outside the repository can break.
- **What a new column holds.** Type, null count and, for low-cardinality columns, the value
  distribution: `is_business (BOOLEAN, 12 true, 138 false)`. A column of all false would
  show as such, without a second run and a DuckDB query.

## What an agent still needs from a human

- Whether a failing test is the change being wrong or the test being stale. Preflight
  reports the fact; the decision is the reviewer's.
- Whether a "not verified" model should be made portable. That is a project decision, not a
  pull-request decision.
- Production numbers. Synthetic data proves the SQL runs and the tests pass; it cannot say
  whether revenue moved.

## Files preflight leaves behind

Add these two lines to the repository's `.gitignore` so an agent never has to decide whether
to commit them:

```
.preflight/
.preflight-comment.md
```

The first is the work directory (fixtures, the DuckDB file, dbt artefacts), kept only with
`--keep-workdir`. The second is the comment written by `--comment-file`. Neither belongs in
a pull request.

## Hooking it in

With [Claude Code](https://claude.com/claude-code), a `PreToolUse` hook on `gh pr create`
that runs preflight and blocks when it fails turns the instruction above into a guarantee.
The JB Analytica harness will ship that hook; until then, the instruction block is enough
for an agent that reads its `CLAUDE.md`.

A hook, a bot or a plugin that has to act on the result programmatically, rather than have
an agent read the comment, should read `--summary-file`'s JSON instead of parsing the
Markdown: it carries the verdict, every count and every finding as structured data, derived
from the same report the comment renders from so the two never disagree.
[docs/integration.md](integration.md) is the contract: every flag, the exit code, the JSON
schema, and a worked pre-pull-request hook that reads `verdict` and exits non-zero on
`failed` or `could_not_run`.
