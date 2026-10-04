"""`dbt-preflight run`: the whole check, start to finish."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer

from dbt_preflight import __version__
from dbt_preflight.baseline import FAILING, is_preexisting
from dbt_preflight.checks import check_columns, check_manifest, row_counts
from dbt_preflight.config import ConfigError, PreflightConfig, load_config
from dbt_preflight.dbt_runner import (
    BASE_TARGET_NAME,
    DbtError,
    DbtRunner,
    NodeResult,
    RunOutcome,
    read_project,
    write_profiles,
)
from dbt_preflight.diff import compute_diffs
from dbt_preflight.fixtures import build_fixtures
from dbt_preflight.git import GitError, base_worktree, git_root, head_sha, paths_changed
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
from dbt_preflight.schema import SchemaError, resolve_schema
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
    head_runner = DbtRunner(project, profiles_dir, workdir / "target", workdir / "logs", config.env)
    head_runner.deps()
    manifest = Manifest.load(head_runner.parse())
    timer.mark(f"   parsed {len(manifest.models)} models, {len(manifest.sources)} sources")

    # DuckDB names its catalog after the file. Rename the file so `database` in the
    # sources resolves to a catalog that exists, then re-point the profile at it.
    catalog = _database_name(manifest, "preflight")
    if catalog != db_path.stem:
        db_path = workdir / f"{catalog}.duckdb"
        write_profiles(profiles_dir, project.profile, db_path)

    # 2. Fixtures. A project with no sources takes its input from seeds, which dbt loads
    # itself during the build; there is nothing to generate and nothing to miss.
    if manifest.sources:
        schema = resolve_schema(config.schema, manifest, workdir)
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
    dialect = (
        config.dialect
        if config.dialect is not None
        else detect_dialect(config.project_dir, project.profile)
    )
    if dialect and dialect not in {"duckdb", "none"}:
        report.dialect = dialect
        _say(f"   transpiling model SQL from {dialect} to DuckDB")
    changed_ids: set[str] = set()
    base: _BaseBuild | None = None
    with contextlib.ExitStack() as stack:
        if base_ref:
            base_root = stack.enter_context(
                base_worktree(config.repo_root, base_ref, workdir / "base")
            )
            base_project = read_project(base_root / config.project_relpath)
            base_runner = DbtRunner(
                base_project, profiles_dir, workdir / "base_target", workdir / "logs", config.env
            )
            base_runner.deps()
            base_runner.parse()
            timer.mark("   base parsed")
            state_dir = workdir / "base_target"
            modified = set(head_runner.modified_nodes(state_dir))
            # dbt cannot see the files that shape the fixtures. If the schema or the preflight
            # config changed, every source is effectively different and everything runs.
            watched = [p for p in (config.schema, config.path) if p is not None]
            touched = paths_changed(config.repo_root, base_ref, watched)
            if touched:
                names = ", ".join(f"`{_relative(p, config.repo_root)}`" for p in touched)
                report.note = (
                    f"{names} changed, so every source counts as modified and all models ran."
                )
                modified |= set(manifest.sources)
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
            base = _build_base(
                config,
                report,
                base_project,
                profiles_dir,
                workdir,
                db_path,
                select,
                modified,
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


@dataclass
class _BaseBuild:
    """The base branch, built on the same fixtures before the head."""

    manifest: Manifest
    # Test and unit-test results on the base branch, by unique id. Empty when its tests
    # could not run, which leaves every head failure counted, exactly as before.
    tests: dict[str, NodeResult] = field(default_factory=dict)

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
    modified: set[str],
    timer: _StepTimer,
) -> _BaseBuild | None:
    """Build the base branch's side of the selection, then run its tests.

    Tables first, tests after, in two dbt invocations rather than one `dbt build`: a test
    that fails on the base must not skip what depends on it there either, or the tests
    downstream of it would have no base result to be compared with.

    A test the pull request modified keeps no base result: its unique id survives an
    edit to a singular test's SQL, a unit test's rows or a generic test's config, so the
    base result would describe a different test. Snapshots with a fixed `target_schema`
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
    results = {
        r.unique_id: r
        for r in tests.results
        if r.resource_type in {"test", "unit_test"} and r.unique_id not in modified
    }
    failing = sum(1 for r in results.values() if r.status in FAILING)
    timer.mark(
        f"   base branch: {built} nodes built, {len(results)} tests, {failing} failing there"
    )
    return _BaseBuild(
        manifest=Manifest.load(workdir / "base_build" / "manifest.json"), tests=results
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


if __name__ == "__main__":
    app()
