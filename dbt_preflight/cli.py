"""`dbt-preflight run`: the whole check, start to finish."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

import typer

from dbt_preflight import __version__
from dbt_preflight.checks import check_columns, check_manifest, row_counts
from dbt_preflight.config import ConfigError, PreflightConfig, load_config
from dbt_preflight.dbt_runner import (
    BASE_TARGET_NAME,
    DbtError,
    DbtRunner,
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
from dbt_preflight.transpile import TranspileHook, detect_dialect

app = typer.Typer(
    help="Warehouse-free CI for dbt pull requests.",
    add_completion=False,
    no_args_is_help=True,
)


def _say(msg: str) -> None:
    typer.echo(msg, err=True)


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
) -> None:
    """Fold dbt's run results into the report's per-model and per-test rows."""
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
            if m is not None:
                if r.status == "pass":
                    m.tests_passed += 1
                elif r.status == "warn":
                    m.tests_warned += 1
                elif r.status in {"fail", "error"}:
                    m.tests_failed += 1
            if r.status in {"fail", "error", "warn"}:
                report.tests.append(
                    FailedTest(
                        name=r.name,
                        model=m.name if m else "(unknown)",
                        status=r.status,
                        failures=r.failures,
                        message=r.message,
                        compiled_code=r.compiled_code if r.resource_type == "test" else None,
                        kind=r.resource_type,
                    )
                )

    # A model dbt skipped because its parent failed to build was not skipped by choice.
    for m in by_model.values():
        if m.status == SKIPPED:
            m.message = m.message or "upstream model failed"
    report.models = list(by_model.values())


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
        _finish(report, comment_file, post, pr, fail_on_error)
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

    _finish(report, comment_file, post, pr, fail_on_error)


def _run(
    config: PreflightConfig, report: PreflightReport, base_ref: str | None, workdir: Path
) -> None:
    project = read_project(config.project_dir)
    project_relpath = str(config.project_relpath)
    profiles_dir = workdir / "profiles"
    db_path = workdir / "preflight.duckdb"
    write_profiles(profiles_dir, project.profile, db_path)

    _say(f"🛫 dbt preflight {__version__} · project `{project.name}` at {project_relpath}")

    # 1. Parse the head so we know the sources and where they think they live.
    head_runner = DbtRunner(project, profiles_dir, workdir / "target", workdir / "logs", config.env)
    head_runner.deps()
    manifest = Manifest.load(head_runner.parse())
    _say(f"   parsed {len(manifest.models)} models, {len(manifest.sources)} sources")

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
        _say(
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
        _say("   no sources declared: the project's seeds are the only input")

    # 3. Base manifest, for state:modified. The worktree stays checked out until the end of
    # the run: after the head build, the base is built too, into its own schemas, for the diff.
    state_dir: Path | None = None
    changed_ids: set[str] = set()
    base_project = None
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
            affected = manifest.affected_models(modified)
            # "Changed" rows in the comment: modified models, plus models that read a
            # modified source directly (the staging layer of a schema change).
            changed_ids = {
                uid
                for uid in affected
                if uid in modified
                or any(
                    p in modified and p in manifest.sources
                    for p in manifest.parent_map.get(uid, [])
                )
            }
            _say(
                f"   {len([m for m in modified if m in manifest.models])} models and "
                f"{len([m for m in modified if m in manifest.sources])} sources changed against {base_ref}"
            )
            if not affected:
                report.nothing_changed = True
                return
            select: list[str] | None = [manifest.models[uid].name for uid in affected]
            _say(f"   building {len(select)} models the change can reach")
        else:
            changed_ids = set(manifest.models)
            select = None
            _say("   no base ref given: building every model")

        _build_and_check(
            config,
            report,
            manifest,
            head_runner,
            project,
            select,
            changed_ids,
            db_path,
            project_relpath,
        )

        # 6. The base, built on the same fixtures, and the diff.
        if base_ref and base_project is not None and state_dir is not None:
            _diff_against_base(
                config,
                report,
                manifest,
                base_project,
                profiles_dir,
                workdir,
                state_dir,
                db_path,
                changed_ids,
            )


def _build_and_check(
    config: PreflightConfig,
    report: PreflightReport,
    manifest: Manifest,
    head_runner: DbtRunner,
    project,
    select: list[str] | None,
    changed_ids: set[str],
    db_path: Path,
    project_relpath: str,
) -> None:
    # 4. Build, transpiling the project's dialect to DuckDB on the way.
    dialect = (
        config.dialect
        if config.dialect is not None
        else detect_dialect(config.project_dir, project.profile)
    )
    hook: TranspileHook | None = None
    if dialect and dialect not in {"duckdb", "none"}:
        hook = TranspileHook(dialect, db_path)
        report.dialect = dialect
        _say(f"   transpiling model SQL from {dialect} to DuckDB")
    outcome = head_runner.build(select, hook)
    if hook is not None:
        report.untranspiled = dict(hook.unparsed)
        for name, why in hook.unparsed.items():
            _say(f"   ⚠️  {name}: could not transpile, ran as written ({why})")
    if outcome.error:
        raise DbtError(outcome.error)
    selected_ids = [
        r.unique_id
        for r in outcome.results
        if r.resource_type == "model" and r.unique_id in manifest.models
    ]
    # Models in the selection that never got a result (rare) still deserve a row.
    for uid in changed_ids:
        if uid not in selected_ids and uid in manifest.models:
            selected_ids.append(uid)
    _assemble(
        report, manifest, outcome, changed_ids, selected_ids, project_relpath, hook is not None
    )
    built = [m for m in report.models if m.status == BUILT]
    _say(
        f"   built {len(built)}/{len(report.models)} models, "
        f"{len(report.failing_tests)} failing tests"
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
    _say(f"   {len(report.violations)} convention issues")


def _diff_against_base(
    config: PreflightConfig,
    report: PreflightReport,
    manifest: Manifest,
    base_project,
    profiles_dir: Path,
    workdir: Path,
    state_dir: Path,
    db_path: Path,
    changed_ids: set[str],
) -> None:
    """Build the changed models on the base branch, then compare columns, rows and metrics."""
    base_state = Manifest.load(state_dir / "manifest.json")
    base_names = {m.name for m in base_state.models.values()}
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
    compare_ids = sorted(uid for uid in compare if uid in built)
    targets = [manifest.models[uid].name for uid in compare_ids]
    to_build = [f"+{n}" for n in targets if n in base_names]
    if not targets:
        return

    base_runner = DbtRunner(
        base_project,
        profiles_dir,
        workdir / "base_build",
        workdir / "logs",
        config.env,
        target=BASE_TARGET_NAME,
    )
    hook = TranspileHook(report.dialect, db_path) if report.dialect else None
    if to_build:
        _say(f"   building {len(to_build)} models on the base branch for the diff")
        outcome = base_runner.build(to_build, hook, command="run")
        if outcome.error:
            _say(f"   ⚠️  base build failed, no diff: {outcome.error}")
            return
    else:
        base_runner.parse()
    base_manifest = Manifest.load(workdir / "base_build" / "manifest.json")

    metric_defs = collect_metrics(manifest, config.metrics)
    report.metrics_defined = len(metric_defs)
    report.diffs = compute_diffs(
        db_path, manifest, base_manifest, compare_ids, metric_defs, report.dialect
    )
    moved = sum(len(d.moved_metrics) for d in report.diffs)
    _say(
        f"   diff: {len(report.diffs)} models compared against {report.base_ref}; "
        f"{len(metric_defs)} metrics defined across the project, {moved} moved"
    )


def _finish(
    report: PreflightReport,
    comment_file: Path | None,
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
