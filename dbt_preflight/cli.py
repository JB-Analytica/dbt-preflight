"""`dbt-preflight run`: the whole check, start to finish."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer
import yaml

from dbt_preflight import __version__
from dbt_preflight.baseline import (
    FAILING,
    fixture_shaped_error,
    is_broken_on_base,
    is_preexisting,
    same_error,
)
from dbt_preflight.checks import check_columns, check_manifest, row_counts
from dbt_preflight.compiled import CompiledSql, compile_selection, json_compile_selection
from dbt_preflight.config import CONFIG_FILENAME, ConfigError, PreflightConfig, load_config
from dbt_preflight.dbt_runner import (
    BASE_TARGET_NAME,
    DbtError,
    DbtRunner,
    NodeResult,
    RunOutcome,
    compile_models,
    read_project,
    write_profiles,
)
from dbt_preflight.diff import compute_diffs
from dbt_preflight.fixtures import build_fixtures, widen_json
from dbt_preflight.git import GitError, base_worktree, file_at, git_root, head_sha, paths_changed
from dbt_preflight.github import GitHubError, post_or_update_comment, pull_request_number
from dbt_preflight.manifest import Manifest
from dbt_preflight.metrics import collect_metrics
from dbt_preflight.report import (
    BUILT,
    FAILED,
    NO_RESULT,
    NOT_VERIFIED,
    SKIPPED,
    FailedTest,
    ModelReport,
    PreflightReport,
    render,
)
from dbt_preflight.schema import (
    SchemaError,
    derive_dbml,
    json_reads,
    resolve_schema,
    source_table_names,
)
from dbt_preflight.schema_file import annotate, count_notes, default_output
from dbt_preflight.summary import build_summary
from dbt_preflight.transpile import TranspileHook, detect_dialect

app = typer.Typer(
    help="Warehouse-free CI for dbt pull requests.",
    add_completion=False,
    no_args_is_help=True,
)


def _say(msg: str) -> None:
    typer.echo(msg, err=True)


class _StepTimer:
    """Appends seconds-since-the-previous-mark to the existing stderr step lines.

    Purely a profiling aid (see scripts/big_project.py and the scale measurements it
    backs): it does not touch the review comment, only the progress lines CI already
    shows, so it is safe to leave on unconditionally.
    """

    def __init__(self) -> None:
        self.last = time.monotonic()

    def mark(self, msg: str) -> None:
        now = time.monotonic()
        _say(f"{msg} [{now - self.last:.1f}s]")
        self.last = now


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dbt-preflight {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(  # noqa: B008
        False, "--version", callback=_version_callback, is_eager=True, help="Print the version."
    ),
) -> None:
    """Warehouse-free CI for dbt pull requests."""


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _database_name(manifest: Manifest, fallback: str) -> str:
    """The catalog name the project's sources resolve to, so the DuckDB file can match it."""
    names = {s.database for s in manifest.sources.values() if s.database}
    if not names:
        return fallback
    if len(names) > 1:
        _say(
            "⚠️  Sources resolve to more than one database "
            f"({', '.join(sorted(names))}); using the first. Set `env` in .dbt-preflight.yml "
            "so they all resolve to one name."
        )
    return sorted(names)[0]


def _assemble(
    report: PreflightReport,
    manifest: Manifest,
    outcome: RunOutcome,
    changed_ids: set[str],
    selected_ids: list[str],
    project_relpath: str,
    strict_syntax: bool = False,
    base_tests: dict[str, NodeResult] | None = None,
) -> None:
    """Fold dbt's run results into the report's per-model and per-test rows.

    With `base_tests` (the same tests' results on the base branch), a failure that was
    already there is counted and listed apart, and does not fail the check.
    """
    by_model: dict[str, ModelReport] = {}
    for uid in selected_ids:
        node = manifest.models[uid]
        path = f"{project_relpath}/{node.original_file_path}".lstrip("./")
        by_model[uid] = ModelReport(
            unique_id=uid, name=node.name, path=path, status=NO_RESULT, changed=uid in changed_ids
        )

    for r in outcome.results:
        if r.resource_type == "model":
            m = by_model.get(r.unique_id)
            if m is None:
                continue
            if r.status == "success":
                m.status = BUILT
            elif r.status == "skipped":
                m.status = SKIPPED
                m.message = r.message
            elif r.status == "error":
                fn = r.dialect_function
                # With a dialect, the SQL was transpiled for DuckDB (or could not be parsed
                # by sqlglot either): a parser error is the pull request's error, not a
                # DuckDB gap. Only a missing function still counts as "not verified".
                if fn and not (strict_syntax and r.is_parser_error):
                    m.status = NOT_VERIFIED
                    m.dialect_function = fn
                else:
                    m.status = FAILED
                m.message = r.message
        elif r.resource_type in {"test", "unit_test"}:
            test = manifest.tests.get(r.unique_id)
            model_uid = test.attached_node if test else None
            if model_uid is None and test is not None:
                model_uid = next((d for d in test.depends_on if d in by_model), None)
            if model_uid is None and r.tested_node in by_model:
                # A unit test names the model it exercises; its inputs are dependencies too.
                model_uid = r.tested_node
            if model_uid is None and r.model_name:
                model_uid = next(
                    (uid for uid, mm in by_model.items() if mm.name == r.model_name), None
                )
            if model_uid is None:
                model_uid = next((d for d in r.depends_on if d in by_model), None)
            m = by_model.get(model_uid or "")
            base = (base_tests or {}).get(r.unique_id)
            preexisting = is_preexisting(r, base)
            if m is not None:
                if r.status == "pass":
                    m.tests_passed += 1
                elif r.status == "warn":
                    m.tests_warned += 1
                elif preexisting:
                    m.tests_failed_on_base += 1
                elif r.status in FAILING:
                    m.tests_failed += 1
            if r.status in {"fail", "error", "warn"}:
                if m is not None:
                    owner = m.name
                elif test is not None and (
                    test.attached_node in manifest.models
                    or test.attached_node in manifest.seeds
                    or test.attached_node in manifest.snapshots
                ):
                    # A seed's or snapshot's own test, or one on an ephemeral model: none
                    # of them has a row of its own.
                    owner = manifest.node_name(test.attached_node)
                else:
                    owner = "(unknown)"
                report.tests.append(
                    FailedTest(
                        name=r.name,
                        model=owner,
                        status=r.status,
                        failures=r.failures,
                        message=r.message,
                        compiled_code=r.compiled_code if r.resource_type == "test" else None,
                        kind=r.resource_type,
                        test_name=test.test_name if test else None,
                        column_name=test.column_name if test else None,
                        kwargs=test.kwargs if test else {},
                        unique_id=r.unique_id,
                        preexisting=preexisting,
                        base_failures=base.failures
                        if base is not None and base.status == "fail" and r.status == "fail"
                        else None,
                    )
                )

    # A model dbt skipped because its parent failed to build was not skipped by choice.
    for m in by_model.values():
        if m.status == SKIPPED:
            m.message = m.message or "upstream model failed"
    report.models = list(by_model.values())
    # Failing tests come off the same unordered results; keep them stable for the same reason.
    report.tests.sort(key=lambda t: (t.model, t.name))


@app.command()
def run(
    base_ref: Optional[str] = typer.Option(
        None,
        "--base-ref",
        help="Git ref of the base branch, e.g. origin/main. Without it every model counts as changed.",
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", help="Path to .dbt-preflight.yml (default: repo root)."
    ),
    repo_root: Optional[Path] = typer.Option(
        None,
        "--repo-root",
        help="Repository root (default: the git root of the current directory).",
    ),
    comment_file: Optional[Path] = typer.Option(
        None, "--comment-file", help="Write the review comment (Markdown) here."
    ),
    summary_file: Optional[Path] = typer.Option(
        None,
        "--summary-file",
        help="Write a JSON summary of the run here, for a hook or agent to read "
        "instead of parsing the comment.",
    ),
    post: bool = typer.Option(
        False,
        "--post",
        help="Post or update the comment on the pull request. Needs GITHUB_TOKEN, "
        "GITHUB_REPOSITORY and a pull_request event (or --pr).",
    ),
    pr: Optional[int] = typer.Option(None, "--pr", help="Pull request number, for --post."),
    fail_on_error: bool = typer.Option(
        True,
        "--fail-on-error/--no-fail-on-error",
        help="Exit 1 when the check fails.",
    ),
    keep_workdir: bool = typer.Option(
        False, "--keep-workdir", help="Keep .preflight/ after the run for inspection."
    ),
) -> None:
    """Generate fixtures, build and test the changed models, check conventions, report."""
    started = time.monotonic()
    repo_root = (repo_root or git_root(Path.cwd())).resolve()
    report = PreflightReport(base_ref=base_ref, head=head_sha(repo_root))

    try:
        config = load_config(repo_root, config_path)
    except ConfigError as exc:
        report.fatal = f"Configuration error: {exc}"
        _finish(report, comment_file, summary_file, post, pr, fail_on_error)
        return

    report.seed = config.seed
    report.schema_source = config.describe_schema_source()
    report.dialect_failures_are_errors = config.dialect_failures == "error"

    workdir = config.workdir
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    try:
        _run(config, report, base_ref, workdir)
    except (SchemaError, DbtError, GitError) as exc:
        report.fatal = str(exc)
    except Exception as exc:  # noqa: BLE001 - the comment must still be written
        traceback.print_exc(file=sys.stderr)
        report.fatal = f"Unexpected {type(exc).__name__}: {exc}"
    finally:
        report.elapsed = time.monotonic() - started
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    _finish(report, comment_file, summary_file, post, pr, fail_on_error)


def _run(
    config: PreflightConfig, report: PreflightReport, base_ref: str | None, workdir: Path
) -> None:
    project = read_project(config.project_dir)
    project_relpath = str(config.project_relpath)
    profiles_dir = workdir / "profiles"
    db_path = workdir / "preflight.duckdb"
    write_profiles(profiles_dir, project.profile, db_path)

    _say(f"🛫 dbt preflight {__version__} · project `{project.name}` at {project_relpath}")
    timer = _StepTimer()

    # 1. Parse the head so we know the sources and where they think they live.
    head_runner = DbtRunner(
        project, profiles_dir, workdir / "target", workdir / "logs", config.env,
        dbt_vars=config.vars,
    )  # fmt: skip
    head_runner.deps()
    manifest = Manifest.load(head_runner.parse())
    report.relations = manifest.relations()
    timer.mark(f"   parsed {len(manifest.models)} models, {len(manifest.sources)} sources")

    # DuckDB names its catalog after the file. Rename the file so `database` in the
    # sources resolves to a catalog that exists, then re-point the profile at it.
    catalog = _database_name(manifest, "preflight")
    if catalog != db_path.stem:
        db_path = workdir / f"{catalog}.duckdb"
        write_profiles(profiles_dir, project.profile, db_path)

    dialect = _project_dialect(config, project)

    # 2. Fixtures. A project with no sources takes its input from seeds, which dbt loads
    # itself during the build; there is nothing to generate and nothing to miss.
    compiled: CompiledSql | None = None
    head_dbml: str | None = None
    if manifest.sources and config.schema is None:
        compiled = _compile_for_inference(
            project, manifest, catalog, workdir, "compiled", config.env, dialect, config.vars
        )
        timer.mark(f"   {_compiled_line(compiled)}")
    elif manifest.sources and json_compile_selection(manifest):
        # A DBML file gives the types, but what a model reads as JSON through a macro
        # still only shows compiled (`schema.json_reads`).
        compiled = _compile_for_inference(
            project, manifest, catalog, workdir, "compiled", config.env, dialect, config.vars,
            json_only=True,
        )  # fmt: skip
        n = len(compiled.code) if compiled is not None else 0
        timer.mark(f"   compiled {n} models that read JSON through a macro")
    if manifest.sources:
        schema = resolve_schema(config.schema, manifest, workdir, compiled)
        if schema.derived:
            head_dbml = schema.dbml_path.read_text(encoding="utf-8")
        fixtures = build_fixtures(config, schema, list(manifest.sources.values()), db_path)
        report.fixtures = fixtures
        timer.mark(
            f"   fixtures: {len(fixtures.tables)} tables, {fixtures.total_rows:,} rows "
            f"from {report.schema_source} (seed {config.seed})"
        )
        for missing in fixtures.unmatched_sources:
            _say(f"   ⚠️  no schema table for source {missing}")
    else:
        report.schema_source = "seeds"
        report.note = (
            "The project declares no sources, so its seeds were the only input; "
            "no synthetic data was generated."
        )
        timer.mark("   no sources declared: the project's seeds are the only input")

    # 3. Base manifest, for state:modified. The worktree stays checked out until the end of
    # the run: before the head build, the base is built too, into its own schemas, on the
    # same fixtures, so its test results and its tables can be compared with the head's.
    if dialect:
        report.dialect = dialect
        _say(f"   transpiling model SQL from {dialect} to DuckDB")
    changed_ids: set[str] = set()
    base: _BaseBuild | None = None
    base_compiled: CompiledSql | None = None
    with contextlib.ExitStack() as stack:
        if base_ref:
            base_root = stack.enter_context(
                base_worktree(config.repo_root, base_ref, workdir / "base")
            )
            base_project = read_project(base_root / config.project_relpath)
            base_runner = DbtRunner(
                base_project, profiles_dir, workdir / "base_target", workdir / "logs", config.env,
                dbt_vars=config.vars,
            )  # fmt: skip
            base_runner.deps()
            base_runner.parse()
            timer.mark("   base parsed")
            state_dir = workdir / "base_target"
            modified = set(head_runner.modified_nodes(state_dir))
            # dbt cannot see the files that shape the fixtures. If the schema or the preflight
            # config changed, every source is effectively different and everything runs.
            watched = [p for p in (config.schema, config.path) if p is not None]
            touched = paths_changed(config.repo_root, base_ref, watched)
            if config.path in touched and not _config_reshapes_fixtures(config, base_ref):
                touched.remove(config.path)
            if touched:
                names = ", ".join(f"`{_relative(p, config.repo_root)}`" for p in touched)
                report.note = (
                    f"{names} changed, so every source counts as modified and all models ran."
                )
                modified |= set(manifest.sources)
            # With no DBML file the fixtures are derived from the project itself, and a
            # staging model's casts or tests shape them: a source whose derived table
            # differs from the base's is as changed as an edited sources.yml.
            if config.schema is None and manifest.sources:
                base_manifest = Manifest.load(state_dir / "manifest.json")
                base_compiled = _compile_for_inference(
                    base_project,
                    base_manifest,
                    catalog,
                    workdir,
                    "base_compiled",
                    config.env,
                    dialect,
                    config.vars,
                )
                timer.mark(f"   base {_compiled_line(base_compiled)}")
                reshaped = _reshaped_sources(
                    manifest, base_manifest, compiled, base_compiled, head_dbml
                )
                if reshaped - modified:
                    names = ", ".join(
                        f"`{manifest.sources[u].identifier}`" for u in sorted(reshaped - modified)
                    )
                    _say(f"   derived fixtures differ from the base for {names}")
                    modified |= reshaped
            # The JSON columns get the base's paths too: both branches build on one
            # fixture, so a key the pull request renamed must not validate itself.
            if report.fixtures is not None and report.fixtures.json_shapes:
                base_manifest = Manifest.load(state_dir / "manifest.json")
                if config.schema is not None:
                    base_compiled = _compile_for_inference(
                        base_project, base_manifest, catalog, workdir, "base_compiled",
                        config.env, dialect, config.vars, json_only=True,
                    )  # fmt: skip
                widen_json(
                    db_path,
                    report.fixtures,
                    json_reads(base_manifest, base_compiled),
                    config.seed,
                    # A side read without its compiled SQL misses macro-hidden paths: then
                    # new keys come from both sides' raw SQL, which they read alike.
                    compare=None
                    if (compiled is None) == (base_compiled is None)
                    else (json_reads(manifest), json_reads(base_manifest)),
                )
            # dbt's state comparison does not see vars or package versions. When a file
            # that can change what every model does changed, nothing is judged by the base.
            project_files = [config.project_dir / n for n in _PROJECT_FILES]
            moved = paths_changed(config.repo_root, base_ref, project_files)
            # `dbt deps` has just rewritten the lock file in the working tree on both
            # sides; only a committed change to it is the pull request's.
            moved += paths_changed(
                config.repo_root,
                base_ref,
                [config.project_dir / "package-lock.yml"],
                committed_only=True,
            )
            judge = not moved
            if moved:
                names = ", ".join(f"`{_relative(p, config.repo_root)}`" for p in moved)
                extra = (
                    f"{names} changed, so nothing was judged against the base branch: every "
                    "failing test and model counts."
                )
                report.note = f"{report.note} {extra}" if report.note else extra
            # Models, plus the seeds and snapshots they read: a pull-request build that
            # selected models alone never loaded a seed, so a project whose staging layer
            # reads `ref('raw_customers')` failed on every model.
            affected_nodes = manifest.affected_nodes(modified)
            affected = [uid for uid in affected_nodes if uid in manifest.models]
            inputs = {**manifest.sources, **manifest.seeds, **manifest.snapshots}
            # "Changed" rows in the comment: modified models, models that read a modified
            # source, seed or snapshot directly (the staging layer of a schema change), and
            # models whose own tests were added or edited.
            changed_ids = {
                uid
                for uid in affected
                if uid in modified
                or any(p in modified and p in inputs for p in manifest.parent_map.get(uid, []))
            } | manifest.tested_models(modified)
            n_models = len([m for m in modified if m in manifest.models])
            n_inputs = len([m for m in modified if m in inputs])
            n_tests = len([m for m in modified if m.split(".", 1)[0] in {"test", "unit_test"}])
            _say(
                f"   {n_models} models, {n_inputs} sources/seeds/snapshots and {n_tests} tests "
                f"changed against {base_ref}"
            )
            # Not just models: a test added on a seed, or an edited snapshot nothing reads,
            # still has something to build and run.
            if not affected_nodes:
                report.nothing_changed = True
                return
            select: list[str] | None = [manifest.node_name(uid) for uid in affected_nodes]
            loads = len(affected_nodes) - len(affected)
            _say(
                f"   building {len(affected)} models the change can reach"
                + (f", and the {loads} seeds/snapshots they read" if loads else "")
            )
            report.shared_snapshots = sorted(
                manifest.node_name(u)
                for u in affected_nodes
                if u in manifest.fixed_schema_snapshots
            )
            untrusted = modified | _fixture_bound(manifest, modified)
            base = _build_base(
                config,
                report,
                base_project,
                profiles_dir,
                workdir,
                db_path,
                select,
                _Trust(
                    modified=modified,
                    fixture_bound=_fixture_bound(manifest, modified),
                    untrusted=untrusted,
                    changed_upstream=untrusted | manifest.descendants(untrusted),
                    judge=judge,
                    guess_bound=_guess_bound(manifest, report),
                    tested_by_change=_tests_changed_on(manifest, modified),
                ),
                timer,
            )
        else:
            changed_ids = set(manifest.models)
            select = None
            _say("   no base ref given: building every model")

        _build_and_check(
            config,
            report,
            manifest,
            head_runner,
            select,
            changed_ids,
            db_path,
            project_relpath,
            timer,
            base,
        )

        # 6. The diff, against the base built before the head.
        if base is not None:
            _diff_against_base(config, report, manifest, base, db_path, changed_ids, timer)


# Project-level files whose change can alter every model without dbt's state comparison
# noticing: vars, package versions, selectors, a checked-in profile. `package-lock.yml`
# too, compared by commit (see `_run`).
_PROJECT_FILES = (
    "dbt_project.yml",
    "packages.yml",
    "dependencies.yml",
    "selectors.yml",
    "profiles.yml",
)

_DBML_BLOCK = re.compile(r"^(Table|Enum) (\S+) \{\n(.*?)^\}", re.MULTILINE | re.DOTALL)


def _dbml_tables(text: str) -> dict[str, str]:
    """Each table of a derived DBML file, with the enums its columns use, as text."""
    tables: dict[str, str] = {}
    enums: dict[str, str] = {}
    for kind, name, body in _DBML_BLOCK.findall(text):
        (tables if kind == "Table" else enums)[name] = body
    out: dict[str, str] = {}
    for name, body in tables.items():
        used = [enums[t] for t in re.findall(r"^\s+\S+ (\S+)", body, re.MULTILINE) if t in enums]
        out[name] = body + "".join(used)
    return out


# The config keys that shape the fixtures or what the models compile to. A change to any
# other key (conventions, metrics, how dialect gaps are judged) leaves every source as it was.
_FIXTURE_KEYS = (
    "project_dir",
    "schema",
    "rows",
    "rows_for",
    "seed",
    "locale",
    "env",
    "vars",
    "loader_columns",
    "dialect",
)


def _config_reshapes_fixtures(config: PreflightConfig, base_ref: str) -> bool:
    """Whether the config's change between the base and the head can change the fixtures.

    A config file that is new, unreadable on either side, or differs in any key in
    `_FIXTURE_KEYS` counts as reshaping them; when in doubt it does.
    """
    if config.path is None:
        return False
    before = file_at(config.repo_root, base_ref, config.path)
    if before is None:
        return True
    try:
        base_raw = yaml.safe_load(before) or {}
        head_raw = yaml.safe_load(config.path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return True
    if not isinstance(base_raw, dict) or not isinstance(head_raw, dict):
        return True
    return any(base_raw.get(k) != head_raw.get(k) for k in _FIXTURE_KEYS)


_JSON_NOTE = re.compile(r"(?:, )?note: 'JSON[^']*'")


def _without_json_notes(dbml: str) -> str:
    return _JSON_NOTE.sub("", dbml).replace(" [] ", " ").replace(" []\n", "\n")


def _reshaped_sources(
    head: Manifest,
    base: Manifest,
    head_compiled: CompiledSql | None = None,
    base_compiled: CompiledSql | None = None,
    head_text: str | None = None,
) -> set[str]:
    """Head sources whose derived fixture table differs from the one the base derives.

    Columns, types, keys, refs and enum values all come from the project's own YAML and
    staging SQL when there is no DBML file, so a pull request that edits a cast or a test
    in one staging model changes the data every reader of that source gets. When the base
    cannot be derived at all, every source counts. Each side is derived from its own
    compiled SQL, compiled the same way, so a difference is the change's. When only the
    head compiled, the two cannot be compared like for like, and every source counts too;
    when only the base did, it is derived without it, as the head was.

    `head_text` is the DBML the run already derived for the head, when there is one.
    """
    if head_compiled is not None and base_compiled is None:
        return set(head.sources)
    if head_compiled is None:
        base_compiled = None
    if head_text is None:
        try:
            head_text, _ = derive_dbml(head, head_compiled)
        except SchemaError:
            return set()  # the head run reports this itself
    try:
        base_text, _ = derive_dbml(base, base_compiled)
    except SchemaError:
        return set(head.sources)
    # The JSON notes say which paths each side reads; the fixture holds both sides' paths
    # (`fixtures.widen_json`), so a difference there reshapes nothing.
    head_text, base_text = _without_json_notes(head_text), _without_json_notes(base_text)
    head_tables, base_tables = _dbml_tables(head_text), _dbml_tables(base_text)
    # By the name each side writes the source's table under, not its identifier: that name
    # is sanitised (`events_*` -> `events__`) or prefixed for a duplicate identifier
    # (`<source>__<identifier>`), and a lookup that finds nothing on either side would
    # compare equal and hide the change.
    head_names = source_table_names(head.sources.values())
    base_names = source_table_names(base.sources.values())
    return {
        uid
        for uid in head.sources
        if head_tables.get(head_names[uid])
        != (base_tables.get(base_names[uid]) if uid in base_names else None)
    }


def _compile_for_inference(
    project,
    manifest: Manifest,
    catalog: str,
    workdir: Path,
    name: str,
    env: dict[str, str],
    dialect: str | None = None,
    dbt_vars: dict | None = None,
    json_only: bool = False,
) -> CompiledSql | None:
    """Compile the models that read sources, for the schema inference (`compiled.py`).

    Then, in a second compile so a failure there cannot cost the inference anything, the
    models that read JSON through a macro (`json_compile_selection`), for the columns that
    get JSON fixtures. `json_only` compiles only those, for a run with a DBML file.

    Against a DuckDB file of its own, empty, under the catalog name the sources resolve
    to: the head and the base then see exactly the same database - no relation at all -
    whenever each is compiled, so a macro that introspects one renders its fallback on both
    sides. None when nothing compiled; the inference then reads raw SQL alone.
    """
    compile_profiles = workdir / f"{name}_profiles"
    (workdir / name).mkdir(parents=True, exist_ok=True)
    write_profiles(compile_profiles, project.profile, workdir / name / f"{catalog}.duckdb")
    runner = DbtRunner(
        project, compile_profiles, workdir / f"{name}_target", workdir / "logs", env,
        dbt_vars=dbt_vars,
    )  # fmt: skip
    selection = [] if json_only else compile_selection(manifest)
    compiled: CompiledSql | None = None
    if selection:
        outcome = compile_models(
            runner,
            {u: manifest.selector(u) for u in selection},
            {manifest.models[u].original_file_path: u for u in selection},
        )
        if outcome.manifest is None:
            return None
        # Read now: the second compile rewrites the same manifest.json.
        compiled = CompiledSql.load(outcome.manifest, outcome.failed, dialect)
    extra = json_compile_selection(manifest, set(selection))
    if not extra or (compiled is None and not json_only):
        return compiled
    outcome = compile_models(
        runner,
        {u: manifest.selector(u) for u in extra},
        {manifest.models[u].original_file_path: u for u in extra},
    )
    if outcome.manifest is None:
        return compiled
    more = CompiledSql.load(outcome.manifest, dialect=dialect)
    if compiled is None:
        compiled = CompiledSql(relations=more.relations, dialect=dialect)
    for uid in extra:
        if uid in more.code:
            compiled.code.setdefault(uid, more.code[uid])
    return compiled


def _project_dialect(config: PreflightConfig, project) -> str | None:
    """The SQL dialect to transpile from: configured, else read from the profile; None for DuckDB."""
    dialect = (
        config.dialect
        if config.dialect is not None
        else detect_dialect(config.project_dir, project.profile)
    )
    return None if dialect in {"duckdb", "none"} else dialect


def _compiled_line(compiled: CompiledSql | None) -> str:
    if compiled is None:
        return "could not compile the models that read sources; inferring from raw SQL"
    line = f"compiled {len(compiled.code)} models that read sources"
    if compiled.failed:
        line += f" ({len(compiled.failed)} failed to compile, read as raw SQL)"
    return line


def _fixture_bound(manifest: Manifest, modified: set[str]) -> set[str]:
    """The modified sources and everything downstream of them.

    The base is built on the head's fixtures, so once a source changed (a renamed column
    in the DBML, an edited `sources.yml`) the base reads data its own code was not written
    for, and fails for the change's reasons, not its own. Nothing here is judged by its
    base result: not its builds, and not the tests that read it.
    """
    sources = {uid for uid in modified if uid in manifest.sources}
    return sources | manifest.descendants(sources)


@dataclass
class _Trust:
    """What the base branch's results may be used for, given what the change touched."""

    modified: set[str]
    # Modified sources and everything downstream: the base reads the head's fixtures there.
    fixture_bound: set[str]
    # `modified` plus `fixture_bound`: never skipped "because of the base".
    untrusted: set[str]
    # `untrusted` and everything downstream of it: never broken on the base, and no test
    # reading it is pre-existing on an error, since one error can hide another.
    changed_upstream: set[str]
    # Nodes reading a source column whose type preflight guessed, directly or upstream:
    # uid -> "<table>.<column>". Never "broken on main", never a pre-existing failure.
    guess_bound: dict[str, list[str]] = field(default_factory=dict)
    # Model -> the names of the data tests or unit tests the change added or edited on it
    # (declared on it, or a singular test reading it): `_tests_changed_on`.
    tested_by_change: dict[str, list[str]] = field(default_factory=dict)
    # False when a project-level file changed: nothing is judged by the base at all.
    judge: bool = True


@dataclass
class _BaseBuild:
    """The base branch, built on the same fixtures before the head."""

    manifest: Manifest
    # Test and unit-test results on the base branch, by unique id. Empty when its tests
    # could not run, which leaves every head failure counted, exactly as before.
    tests: dict[str, NodeResult] = field(default_factory=dict)
    # Model, seed and snapshot results on the base branch, by unique id.
    tables: dict[str, NodeResult] = field(default_factory=dict)
    trust: _Trust | None = None  # None: nothing is judged by the base

    def failing_test_selectors(self, head: Manifest) -> list[str]:
        """Exact selectors for the head's copies of the tests that fail on the base."""
        return sorted(
            head.selector(uid)
            for uid, r in self.tests.items()
            if r.status in FAILING and uid in head.fqns
        )


def _build_base(
    config: PreflightConfig,
    report: PreflightReport,
    base_project,
    profiles_dir: Path,
    workdir: Path,
    db_path: Path,
    select: list[str],
    trust: _Trust,
    timer: _StepTimer,
) -> _BaseBuild | None:
    """Build the base branch's side of the selection, then run its tests.

    Tables first, tests after, in two dbt invocations rather than one `dbt build`: a test
    that fails on the base must not skip what depends on it there either, or the tests
    downstream of it would have no base result to be compared with.

    A test the pull request modified keeps no base result: its unique id survives an
    edit to a singular test's SQL, a unit test's rows or a generic test's config, so the
    base result would describe a different test. Neither does a test that reads anything
    the base ran on foreign fixtures for, a test that errored on the base and reads
    anything the change reached (`_Trust`), nor, when `judge` is false, any test at all. Snapshots with a fixed `target_schema`
    are left out, with everything downstream of them: building them here would leave the
    pull request merging its snapshot onto the base's rows in the same table.
    """
    base_state = Manifest.load(workdir / "base_target" / "manifest.json")
    shared = {u for u in base_state.fixed_schema_snapshots if base_state.snapshots[u] in select}
    excluded = shared | base_state.descendants(shared)
    exclude = [base_state.selector(u) for u in sorted(excluded) if u in base_state.fqns]
    on_base = {base_state.node_name(u) for u in base_state.models}
    on_base |= set(base_state.seeds.values()) | set(base_state.snapshots.values())
    names = [n for n in select if n in on_base]
    runner = DbtRunner(
        base_project,
        profiles_dir,
        workdir / "base_build",
        workdir / "logs",
        config.env,
        target=BASE_TARGET_NAME,
        dbt_vars=config.vars,
    )
    if not names:
        runner.parse()
        return _BaseBuild(manifest=Manifest.load(workdir / "base_build" / "manifest.json"))

    hook = TranspileHook(report.dialect, db_path) if report.dialect else None
    _say(f"   building {len(names)} models, seeds and snapshots on the base branch")
    # `+`: the base may read an ancestor the head no longer does, which the head's
    # selection, closed over the head's graph, would not include.
    tables = runner.build(
        [f"+{n}" for n in names],
        hook,
        exclude=exclude,
        exclude_resource_types=["test", "unit_test"],
    )
    if tables.error:
        _say(f"   ⚠️  base build failed, no diff and no base test results: {tables.error}")
        return None
    tests = runner.build(names, hook, command="test", exclude=exclude)
    if tests.error:
        _say(f"   ⚠️  base tests could not run, every head failure counts: {tests.error}")
    built = sum(1 for r in tables.results if r.status == "success")
    results = {r.unique_id: r for r in tests.results if _base_test_usable(r, trust)}
    failing = sum(1 for r in results.values() if r.status in FAILING)
    timer.mark(
        f"   base branch: {built} nodes built, {len(results)} tests, {failing} failing there"
    )
    return _BaseBuild(
        manifest=Manifest.load(workdir / "base_build" / "manifest.json"),
        tests=results,
        tables={r.unique_id: r for r in tables.results if r.resource_type in _TABLE_KINDS},
        trust=trust if trust.judge else None,
    )


def _build_and_check(
    config: PreflightConfig,
    report: PreflightReport,
    manifest: Manifest,
    head_runner: DbtRunner,
    select: list[str] | None,
    changed_ids: set[str],
    db_path: Path,
    project_relpath: str,
    timer: _StepTimer,
    base: _BaseBuild | None = None,
) -> None:
    # 4. Build, transpiling the project's dialect to DuckDB on the way.
    hook = TranspileHook(report.dialect, db_path) if report.dialect else None
    # A test already failing on the base branch is left out of the build and run after
    # it, on its own: inside `dbt build` its failure would skip every model downstream,
    # so the rest of the pull request would go unchecked for something it did not do.
    known_failing = (
        base.failing_test_selectors(manifest) if base is not None and select is not None else []
    )
    outcome = head_runner.build(select, hook, exclude=known_failing)
    if outcome.error:
        raise DbtError(outcome.error)
    if base is not None and known_failing:
        later = head_runner.build(known_failing, hook, command="test")
        if later.error:
            raise DbtError(later.error)
        _fold_in_later_tests(manifest, outcome, later.results, base)
    if hook is not None:
        report.untranspiled = dict(hook.unparsed)
        for name, why in hook.unparsed.items():
            _say(f"   ⚠️  {name}: could not transpile, ran as written ({why})")
    selected_ids = [
        r.unique_id
        for r in outcome.results
        if r.resource_type == "model" and r.unique_id in manifest.models
    ]
    # Models in the selection that never got a result (rare) still deserve a row.
    for uid in changed_ids:
        if uid not in selected_ids and uid in manifest.models:
            selected_ids.append(uid)
    # dbt returns results in thread-completion order, which differs from run to run. The
    # comment is updated in place on every push, so that order would reshuffle its model
    # lists with no change behind it, and the summary JSON with them. Order by model name
    # once, here, and every list downstream (rows, "Also rebuilt", violations) is stable.
    selected_ids.sort(key=lambda uid: manifest.models[uid].name)
    _assemble(
        report,
        manifest,
        outcome,
        changed_ids,
        selected_ids,
        project_relpath,
        hook is not None,
        base.tests if base is not None else None,
    )
    if base is not None:
        _judge_builds(report, manifest, outcome, base)
        _mark_guessed_tests(report, manifest, base)
    built = [m for m in report.models if m.status == BUILT]
    timer.mark(
        f"   built {len(built)}/{len(report.models)} models, "
        f"{len(report.failing_tests)} failing tests"
        + (
            f" ({len(report.preexisting_tests)} more failing on base too)"
            if report.preexisting_tests
            else ""
        )
    )

    # 5. Rows and conventions.
    built_ids = [m.unique_id for m in report.models if m.status == BUILT]
    counts = row_counts(manifest, built_ids, db_path)
    for m in report.models:
        m.rows = counts.get(m.unique_id)

    check_ids = selected_ids if config.check_all else [u for u in selected_ids if u in changed_ids]
    report.violations = check_manifest(manifest, check_ids, project_relpath, config.conventions)
    report.violations += check_columns(
        manifest,
        [u for u in check_ids if u in built_ids],
        project_relpath,
        db_path,
        config.conventions,
    )
    timer.mark(f"   {len(report.violations)} convention issues")


_TABLE_KINDS = {"model", "seed", "snapshot"}


def _fold_in_later_tests(
    manifest: Manifest, outcome: RunOutcome, later: list[NodeResult], base: _BaseBuild
) -> None:
    """Merge the tests run after the build into its results, as `dbt build` would have.

    A test whose model did not build is dropped: dbt build would have skipped it, and its
    failure says nothing the build error does not. An ephemeral model never has a result
    of its own, so one counts as built when everything it reads did.

    A test that fails worse than on the base is the change's doing, and inside one
    `dbt build` it would have skipped everything downstream of the models it reads. That
    is replayed here: those models are reported as skipped, and their tests dropped, so
    the comment says what a single build would have said.
    """
    status = {r.unique_id: r.status for r in outcome.results if r.resource_type in _TABLE_KINDS}

    def built(uid: str) -> bool:
        if uid in status:
            return status[uid] == "success"
        model = manifest.models.get(uid)
        if model is None or model.materialized != "ephemeral":
            return False  # not in this build at all
        # Inlined into whatever reads it: as good as the nodes it reads.
        return all(built(d) for d in model.depends_on if d.split(".", 1)[0] in _TABLE_KINDS)

    def parents(r: NodeResult) -> set[str]:
        return {d for d in r.depends_on if d.split(".", 1)[0] in _TABLE_KINDS}

    kept = [r for r in later if all(built(d) for d in parents(r))]
    worse = [
        r
        for r in kept
        if r.status in FAILING and not is_preexisting(r, base.tests.get(r.unique_id))
    ]
    skipped: set[str] = set()
    for r in worse:
        skipped |= manifest.descendants(parents(r))
    outcome.results += kept
    if not skipped:
        return
    results: list[NodeResult] = []
    for r in outcome.results:
        if r.resource_type in _TABLE_KINDS and r.unique_id in skipped:
            r.status = "skipped"
            r.message = "an upstream test failed"
        elif r.resource_type in {"test", "unit_test"} and parents(r) & skipped:
            continue
        results.append(r)
    outcome.results = results


def _judge_builds(
    report: PreflightReport, manifest: Manifest, outcome: RunOutcome, base: _BaseBuild
) -> None:
    """Mark the models that fail to build on the base branch too, and what that skips.

    A model broken on the base the same way, with nothing the change touched upstream of
    it, is flagged rather than failed (dbt_preflight/baseline.py). A model skipped on
    head is put down to it only when it was skipped on the base too, the change did not
    modify or add it or reshape its fixtures, and nothing else the change did is upstream
    of it: a model that fails only on head, a seed or snapshot that failed, or a test the
    change made fail. Anything else skipped still counts, exactly as before.
    """
    trust = base.trust
    if trust is None:
        return
    head = {r.unique_id: r for r in outcome.results if r.resource_type in _TABLE_KINDS}
    for m in report.models:
        r = head.get(m.unique_id)
        on_base = base.tables.get(m.unique_id)
        if m.status != FAILED or r is None:
            continue
        guessed = trust.guess_bound.get(m.unique_id)
        same_on_base = (
            on_base is not None
            and on_base.status == "error"
            and same_error(r.message, on_base.message)
            # A compilation error happens before any data is read, so no fixture can have
            # caused it: that one is judged against the base as usual.
            and "Compilation Error" not in (r.message or "")
        )
        # Malformed JSON, a timestamp that does not parse, a failed cast: an error about a
        # value, and the values are preflight's generated data on both branches.
        shaped = fixture_shaped_error(r.message) if _reads_a_source(manifest, m.unique_id) else None
        if same_on_base and (shaped or guessed):
            # Failing the same way on main proves nothing about the project when the data
            # is preflight's: not "broken on main". It counts only when the change reaches it.
            m.fixture_error = shaped
            m.guessed_inputs = guessed or []
            if _reached(m.unique_id, trust):
                m.unverified_broken_on_base = True
                m.reached_from = _changed_ancestors(manifest, m.unique_id, trust.modified)
                m.edited_tests = trust.tested_by_change.get(m.unique_id, [])
            else:
                m.fixture_limited = True
            continue
        # A model the change added or edited a test on is never excused: the test needs it.
        m.broken_on_base = is_broken_on_base(
            r, on_base, trust.changed_upstream | set(trust.tested_by_change)
        )
        if (
            not m.broken_on_base
            # Not what the change modified, nor what reads fixtures it changed: there the
            # base ran on the change's data, so its error is the change's too.
            and m.unique_id not in trust.untrusted
            and on_base is not None
            and on_base.status == "error"
            and same_error(r.message, on_base.message)
        ):
            # The same error on main, but the change reaches the model from upstream and
            # DuckDB reports only the first error: it counts, as "could not be checked",
            # not as something this change is known to have broken.
            m.unverified_broken_on_base = True
            m.reached_from = _changed_ancestors(manifest, m.unique_id, trust.modified)
            m.edited_tests = trust.tested_by_change.get(m.unique_id, [])
    broken = {m.unique_id for m in report.models if m.broken_on_base}
    unverified = {m.unique_id for m in report.models if m.unverified_broken_on_base}
    limited = {m.unique_id for m in report.models if m.fixture_limited}
    if not broken and not unverified and not limited:
        return

    roots = {
        m.unique_id
        for m in report.models
        if m.status in {FAILED, NOT_VERIFIED} and not m.broken_on_base and not m.fixture_limited
    }
    roots |= {
        uid
        for uid, r in head.items()
        if r.resource_type != "model" and r.status not in {"success", "skipped"}
    }
    for t in report.failing_tests:
        test = manifest.tests.get(t.unique_id) or manifest.unit_tests.get(t.unique_id)
        if test is not None:
            roots |= {d for d in test.depends_on if d.split(".", 1)[0] in _TABLE_KINDS}
    from_change = manifest.descendants(roots)
    from_base = manifest.descendants(broken)
    from_limited = manifest.descendants(limited)
    # What only an unverified model is upstream of is skipped "because of it": still
    # counted, but not said to be broken by the change.
    from_unverified = manifest.descendants(unverified)
    from_known = manifest.descendants(roots - unverified)
    for m in report.models:
        m.skipped_by_unverified = (
            m.status == SKIPPED and m.unique_id in from_unverified and m.unique_id not in from_known
        )
        on_base = base.tables.get(m.unique_id)
        m.skipped_by_base = (
            m.status == SKIPPED
            and m.unique_id in from_base
            and m.unique_id not in from_change
            # Something the change reaches answers for itself, even downstream of a broken
            # model (a model reading both); and "skipped on both branches" has to be true.
            and not _reached(m.unique_id, trust)
            and on_base is not None
            and on_base.status == "skipped"
        )
        # The same, behind a model preflight's data cannot build.
        m.skipped_by_fixture_limited = (
            m.status == SKIPPED
            and not m.skipped_by_base
            and m.unique_id in from_limited
            and m.unique_id not in from_change
            and not _reached(m.unique_id, trust)
            and on_base is not None
            and on_base.status == "skipped"
        )
        # Skipped on both branches behind such a model, but the change reaches it through
        # another parent: it counts, as unchecked, not as something the change broke.
        m.skipped_unchecked = (
            m.status == SKIPPED
            and not m.skipped_by_base
            and not m.skipped_by_fixture_limited
            and m.unique_id in (from_base | from_limited)
            and m.unique_id not in from_change
            and on_base is not None
            and on_base.status == "skipped"
        )


def _tests_changed_on(manifest: Manifest, modified: set[str]) -> dict[str, list[str]]:
    """{model: names of the tests the change added or edited on it}: generic tests by the
    node they are declared on, unit tests by the model they exercise, and singular tests
    (no attached node) by every model they read."""
    out: dict[str, set[str]] = {}
    for uid, test in manifest.tests.items():
        if uid not in modified:
            continue
        targets = [test.attached_node] if test.attached_node else test.depends_on
        for target in targets:
            if target in manifest.models:
                out.setdefault(target, set()).add(test.name)
    for uid, unit in manifest.unit_tests.items():
        if uid in modified and unit.model_uid in manifest.models:
            out.setdefault(unit.model_uid, set()).add(uid.split(".")[-1])
    return {uid: sorted(names) for uid, names in sorted(out.items())}


def _reached(uid: str, trust: _Trust) -> bool:
    """Whether the change can affect a node: it modified it, the node reads fixtures the
    change reshaped, or either is upstream of it (`_Trust.changed_upstream`); or the change
    added or edited a data test or unit test on it, whose result needs the model built
    (`_Trust.tested_by_change`). Anything else
    builds from identical SQL on identical data on both branches, so by determinism the
    change cannot have affected it."""
    return uid in trust.changed_upstream or uid in trust.tested_by_change


def _reads_a_source(manifest: Manifest, uid: str) -> bool:
    """Whether a source is upstream of a node: only then is generated data in what it reads."""
    seen: set[str] = set()
    frontier = list(manifest.parent_map.get(uid, []))
    while frontier:
        parent = frontier.pop()
        if parent in seen:
            continue
        if parent in manifest.sources:
            return True
        seen.add(parent)
        frontier += manifest.parent_map.get(parent, [])
    return False


def _base_test_usable(r: NodeResult, trust: _Trust) -> bool:
    """Whether a test's base result may judge the head's: see `_build_base`."""
    guessed = r.unique_id in trust.guess_bound or bool(trust.guess_bound.keys() & set(r.depends_on))
    return (
        trust.judge
        and r.resource_type in {"test", "unit_test"}
        and r.unique_id not in trust.modified
        and not trust.fixture_bound.intersection(r.depends_on)
        and not (r.status == "error" and bool(trust.changed_upstream.intersection(r.depends_on)))
        # A test over what reads a guessed column that errors on the base: a type mismatch
        # or failed cast on guessed data is the guess's as likely as the project's, so it
        # is not pre-existing. Failing rows on both branches still are (annotated, see
        # `_mark_guessed_tests`): an invariant random data breaks is not a typing question.
        and not (r.status == "error" and guessed)
    )


def _mark_guessed_tests(report: PreflightReport, manifest: Manifest, base: _BaseBuild) -> None:
    """Name the guessed columns a failing test reads, directly or upstream, so the comment
    says the failure may be preflight's guess rather than the project: an error there was
    not judged against the base (`_build_base`), and failing rows on both branches stay
    pre-existing but say what they read."""
    if base.trust is None or not base.trust.guess_bound:
        return
    bound = base.trust.guess_bound
    for t in report.tests:
        test = manifest.tests.get(t.unique_id) or manifest.unit_tests.get(t.unique_id)
        cols = set(bound.get(t.unique_id, []))
        for dep in test.depends_on if test is not None else []:
            cols |= set(bound.get(dep, []))
        t.guessed_inputs = sorted(cols)


def _guess_bound(manifest: Manifest, report: PreflightReport) -> dict[str, list[str]]:
    """{node unique id: the guessed source columns it reads, as "<table>.<column>"}.

    Column by column, as far as inference can tell: the models whose SQL reads a guessed
    column (for a column typed varchar because a reader could not be followed, those
    readers), a test on the source over a guessed column, and everything downstream of
    them. A guessed column nothing reads binds nothing."""
    fx = report.fixtures
    if fx is None:
        return {}
    direct: dict[str, set[str]] = {}
    guessed_by_source: dict[tuple[str, str], set[str]] = {}
    for src in fx.inferred_sources:
        guessed_by_source[(src.source_name, src.table)] = set(src.guessed_columns)
        for col, readers in src.guessed_readers.items():
            for uid in readers:
                direct.setdefault(uid, set()).add(f"{src.identifier}.{col}")
    for test in manifest.source_tests():
        src = manifest.sources.get(test.attached_node or "")
        if src is None or not test.column_name:
            continue
        if test.column_name in guessed_by_source.get((src.source_name, src.name), set()):
            direct.setdefault(test.unique_id, set()).add(f"{src.identifier}.{test.column_name}")
    out = {uid: set(cols) for uid, cols in direct.items()}
    for uid, cols in direct.items():
        for child in manifest.descendants({uid}):
            out.setdefault(child, set()).update(cols)
    return {uid: sorted(cols) for uid, cols in sorted(out.items())}


def _changed_ancestors(manifest: Manifest, uid: str, modified: set[str]) -> list[str]:
    """The names of what the change modified upstream of `uid`, nearest first."""
    seen: set[str] = set()
    out: list[str] = []
    frontier = list(manifest.parent_map.get(uid, []))
    while frontier:
        parent = frontier.pop(0)
        if parent in seen:
            continue
        seen.add(parent)
        if parent in modified:
            source = manifest.sources.get(parent)
            out.append(
                f"{source.source_name}.{source.name}" if source else manifest.node_name(parent)
            )
        frontier += manifest.parent_map.get(parent, [])
    return out


def _diff_against_base(
    config: PreflightConfig,
    report: PreflightReport,
    manifest: Manifest,
    base: _BaseBuild,
    db_path: Path,
    changed_ids: set[str],
    timer: _StepTimer,
) -> None:
    """Compare columns, rows and metrics of the changed models with the base branch's."""
    # The change and everything downstream of it: a metric on a mart moves when a staging
    # model upstream changes, so the mart is what has to be compared.
    built = {m.unique_id for m in report.models if m.status == BUILT}
    compare: set[str] = set()
    frontier = [uid for uid in changed_ids if uid in manifest.models]
    while frontier:
        uid = frontier.pop()
        if uid in compare:
            continue
        compare.add(uid)
        frontier += [c for c in manifest.child_map.get(uid, []) if c in manifest.models]
    # Downstream of a snapshot with a fixed schema there is no base build to compare with.
    shared = {
        u
        for u in manifest.fixed_schema_snapshots
        if manifest.snapshots[u] in report.shared_snapshots
    }
    unshared = manifest.descendants(shared)
    compare_ids = sorted(uid for uid in compare if uid in built and uid not in unshared)
    if not compare_ids:
        return

    metric_defs = collect_metrics(manifest, config.metrics)
    report.metrics_defined = len(metric_defs)
    report.diffs = compute_diffs(
        db_path, manifest, base.manifest, compare_ids, metric_defs, report.dialect
    )
    moved = sum(len(d.moved_metrics) for d in report.diffs)
    timer.mark(
        f"   diff: {len(report.diffs)} models compared against {report.base_ref}; "
        f"{len(metric_defs)} metrics defined across the project, {moved} moved"
    )


def _finish(
    report: PreflightReport,
    comment_file: Path | None,
    summary_file: Path | None,
    post: bool,
    pr: int | None,
    fail_on_error: bool,
) -> None:
    body = render(report)
    if comment_file is not None:
        comment_file.parent.mkdir(parents=True, exist_ok=True)
        comment_file.write_text(body, encoding="utf-8")
        _say(f"   comment written to {comment_file}")
    else:
        typer.echo(body)

    if post:
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")
        number = pr or pull_request_number()
        if not token or not repo or not number:
            _say(
                "⚠️  --post needs GITHUB_TOKEN, GITHUB_REPOSITORY and a pull request number "
                "(from the event payload or --pr); comment not posted."
            )
        else:
            try:
                url = post_or_update_comment(repo, number, body, token)
                _say(f"   comment posted: {url}")
            except GitHubError as exc:
                _say(f"⚠️  {exc}")

    # Computed before the summary is written, and again below, rather than shared: the
    # second use is the process's actual exit, which must stay the last thing this
    # function does so a crash while writing files still lets the caller see it happen.
    exit_code = 1 if (fail_on_error and not report.passed) else 0
    if summary_file is not None:
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary = build_summary(report, exit_code, comment_file)
        summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        _say(f"   summary written to {summary_file}")

    if report.fatal:
        _say(f"❌ {report.fatal}")
    elif report.passed:
        _say("✅ preflight passed" + (" with warnings" if report.has_warnings else ""))
    else:
        _say("❌ preflight failed")

    if fail_on_error and not report.passed:
        sys.exit(1)


@app.command()
def schema(
    config_path: Optional[Path] = typer.Option(
        None, "--config", help="Path to .dbt-preflight.yml (default: repo root)."
    ),
    repo_root: Optional[Path] = typer.Option(
        None,
        "--repo-root",
        help="Repository root (default: the git root of the current directory).",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        help="Where to write the DBML (default: source_system/<project name>.dbml, next to "
        ".dbt-preflight.yml).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing file, and derive a schema even when the config already "
        "has `schema:`.",
    ),
) -> None:
    """Write the schema a run derives from the project as a DBML file to keep and refine.

    Needs no warehouse, credentials or base ref: the same derivation a run does on the
    head (sources.yml, the staging models' SQL, compiled SQL), written to a file. Columns
    preflight typed itself carry a note saying where the type came from.
    """
    repo_root = (repo_root or git_root(Path.cwd())).resolve()
    try:
        config = load_config(repo_root, config_path)
    except ConfigError as exc:
        _say(f"❌ Configuration error: {exc}")
        raise typer.Exit(1) from None

    if config.schema is not None and not force:
        typer.echo(
            f"`schema:` in {_relative(config.path or repo_root, repo_root)} already points at "
            f"{config.describe_schema_source()}; nothing written. Edit that file, or pass "
            "--force to derive a fresh schema anyway."
        )
        return

    config_file = config.path or (config_path or repo_root / CONFIG_FILENAME).resolve()
    try:
        project = read_project(config.project_dir)
    except DbtError as exc:
        _say(f"❌ {exc}")
        raise typer.Exit(1) from None
    target = (output or default_output(config_file.parent, project.name)).resolve()
    if target.exists() and not force:
        _say(f"❌ {target} already exists. Pass --force to overwrite it.")
        raise typer.Exit(1)

    workdir = config.workdir
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        dbml = _derive_schema_file(config, project, workdir)
    except (SchemaError, DbtError) as exc:
        _say(f"❌ {exc}")
        raise typer.Exit(1) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dbml, encoding="utf-8")

    notes = count_notes(dbml)
    tables = len(re.findall(r"^Table ", dbml, re.MULTILINE))
    typer.echo(f"Wrote {_relative(target, repo_root)}: {tables} tables, {len(dbml):,} bytes.")
    if any(notes.values()):
        typer.echo(
            f"{sum(notes.values())} columns carry a note saying where their type came from: "
            + ", ".join(f"{n} {k}" for k, n in notes.items() if n)
            + "."
        )
    shown = _relative(target, config_file.parent)
    typer.echo("")
    typer.echo("Next:")
    typer.echo(f"  1. Add `schema: {shown}` to {_relative(config_file, repo_root)}.")
    typer.echo("  2. Commit the file.")
    typer.echo(
        "  3. Refine it in model2data studio (https://studio.jbanalytica.com/?ref=dbt-preflight): "
        "paste it into the editor,\n     or open the repository as a repository project."
    )
    if config.schema is not None:
        typer.echo(
            f"\n`schema:` already points at {config.describe_schema_source()}; "
            "it is unchanged and the new file is not used until you edit it."
        )


def _derive_schema_file(config: PreflightConfig, project, workdir: Path) -> str:
    """The DBML a run on the head would derive, as a file to keep. No warehouse, no base."""
    profiles_dir = workdir / "profiles"
    write_profiles(profiles_dir, project.profile, workdir / "preflight.duckdb")
    _say(f"🛫 dbt preflight {__version__} · schema for project `{project.name}`")
    runner = DbtRunner(project, profiles_dir, workdir / "target", workdir / "logs", config.env)
    runner.deps()
    manifest = Manifest.load(runner.parse())
    catalog = _database_name(manifest, "preflight")
    dialect = _project_dialect(config, project)
    compiled = _compile_for_inference(
        project, manifest, catalog, workdir, "compiled", config.env, dialect
    )
    _say(f"   {_compiled_line(compiled)}")
    dbml, inferred = derive_dbml(manifest, compiled)
    return annotate(dbml, manifest, inferred)


if __name__ == "__main__":
    app()
