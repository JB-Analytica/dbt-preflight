"""Run dbt-preflight against four public dbt projects and compare with a baseline.

    uv run python scripts/realworld/run.py                       # run everything, show the table
    uv run python scripts/realworld/run.py --compare             # exit 1 if anything got worse
    uv run python scripts/realworld/run.py --project shopify --change rename
    uv run python scripts/realworld/run.py --write-baseline      # accept today's numbers

Needs network (it fetches the pinned commits and each project's dbt packages) and a few
minutes. See scripts/realworld/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realworld_core as core  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "realworld",
    "GIT_AUTHOR_EMAIL": "realworld@example.invalid",
    "GIT_COMMITTER_NAME": "realworld",
    "GIT_COMMITTER_EMAIL": "realworld@example.invalid",
}


class SuiteError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise SuiteError(f"git {' '.join(args)} failed in {repo}: {res.stderr.strip()}")
    return res.stdout.strip()


def fetch_pinned(project: core.Project, cache: Path) -> Path:
    """A clone at `cache/<name>` that has the pinned commit, fetched shallow by SHA."""
    repo = cache / project.name
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
        git(repo, "remote", "add", "origin", project.repo)
    have = subprocess.run(
        ["git", "cat-file", "-e", f"{project.sha}^{{commit}}"],
        cwd=repo,
        env=GIT_ENV,
        stderr=subprocess.DEVNULL,
    )
    if have.returncode != 0:
        print(f"  fetching {project.repo} @ {project.sha[:10]}", flush=True)
        git(repo, "fetch", "-q", "--depth", "1", "origin", project.sha)
    return repo


def apply_edit(repo: Path, edit: core.Edit) -> None:
    if edit.copy:
        src = core.FILES_DIR / edit.copy
        dest = repo / (edit.to or "")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return
    path = repo / (edit.file or "")
    if not path.is_file():
        raise SuiteError(f"{edit.file}: not in the checked-out project")
    text = path.read_text()
    if edit.prepend is not None:
        path.write_text(edit.prepend + text)
        return
    old, new = edit.replace_old or "", edit.replace_new or ""
    count = text.count(old)
    if count != 1:
        raise SuiteError(f"{edit.file}: expected exactly one match for {old!r}, found {count}")
    path.write_text(text.replace(old, new))


def build_commits(project: core.Project, change: str, repo: Path) -> tuple[str, str]:
    """Reset to the pinned commit, commit the setup as base and the change as head."""
    git(repo, "checkout", "-q", "-f", "--detach", project.sha)
    git(repo, "clean", "-fdxq")
    for edit in project.setup:
        apply_edit(repo, edit)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", f"realworld base: {project.name}")
    base = git(repo, "rev-parse", "HEAD")
    for edit in project.changes[change]:
        apply_edit(repo, edit)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"realworld change: {change}")
    return base, git(repo, "rev-parse", "HEAD")


def run_one(
    project: core.Project,
    change: str,
    repo: Path,
    out_dir: Path,
    preflight: list[str],
    timeout: int,
) -> core.Row:
    stem = f"{project.name}-{change}"
    summary_file = out_dir / f"{stem}.json"
    summary_file.unlink(missing_ok=True)
    try:
        base, _head = build_commits(project, change, repo)
    except SuiteError as exc:
        print(f"  setup failed: {exc}", flush=True)
        return core.failed_row()
    cmd = [
        *preflight,
        "run",
        "--base-ref",
        base,
        "--repo-root",
        str(repo),
        "--summary-file",
        str(summary_file),
        "--comment-file",
        str(out_dir / f"{stem}.md"),
        "--no-fail-on-error",
    ]
    started = time.monotonic()
    with (out_dir / f"{stem}.log").open("w") as log:
        try:
            subprocess.run(
                cmd, cwd=repo, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            print(f"  timed out after {timeout}s", flush=True)
    wall = time.monotonic() - started
    if not summary_file.exists():
        print(f"  no summary written; see {out_dir / f'{stem}.log'}", flush=True)
        return core.failed_row(wall)
    summary = json.loads(summary_file.read_text())
    if summary.get("fatal"):
        print(f"  fatal: {str(summary['fatal'])[:200]}", flush=True)
    return core.extract_row(
        summary,
        change=change,
        rename_old=project.rename_old,
        rename_new=project.rename_new,
        wall_seconds=wall,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--project", action="append", help="only this project (repeatable)")
    ap.add_argument("--change", action="append", help="only this change (repeatable)")
    ap.add_argument("--compare", action="store_true", help="exit 1 if anything got worse")
    ap.add_argument(
        "--write-baseline", action="store_true", help="store this run's numbers as the baseline"
    )
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(
            os.environ.get("REALWORLD_CACHE", Path(tempfile.gettempdir()) / "preflight-realworld")
        ),
        help="where the projects are fetched (default: $REALWORLD_CACHE or the system temp dir)",
    )
    ap.add_argument("--out-dir", type=Path, help="summaries, comments, logs (default: <cache>/out)")
    ap.add_argument("--manifest", type=Path, default=core.MANIFEST_PATH)
    ap.add_argument("--baseline", type=Path, default=core.BASELINE_PATH)
    ap.add_argument(
        "--preflight",
        default=None,
        help="command to run instead of this environment's dbt-preflight",
    )
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per run")
    args = ap.parse_args(argv)

    projects = core.load_manifest(args.manifest)
    for name in args.project or []:
        if name not in projects:
            ap.error(f"unknown project {name!r}; known: {', '.join(projects)}")
    for change in args.change or []:
        if change not in core.CHANGE_NAMES:
            ap.error(f"unknown change {change!r}; known: {', '.join(core.CHANGE_NAMES)}")

    cache: Path = args.cache_dir.resolve()
    out_dir: Path = (args.out_dir or cache / "out").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    preflight = (
        shlex.split(args.preflight)
        if args.preflight
        else [sys.executable, "-m", "dbt_preflight.cli"]
    )
    version = metadata.version("dbt-preflight")

    rows: dict[str, core.Row] = {}
    started = time.monotonic()
    for name, project in projects.items():
        if args.project and name not in args.project:
            continue
        print(f"{name}", flush=True)
        try:
            repo = fetch_pinned(project, cache)
        except SuiteError as exc:
            print(f"  fetch failed: {exc}", flush=True)
            repo = None
        for change in core.CHANGE_NAMES:
            if args.change and change not in args.change:
                continue
            print(f"  {change}", flush=True)
            if repo is None:
                rows[core.key(name, change)] = core.failed_row()
                continue
            rows[core.key(name, change)] = run_one(
                project, change, repo, out_dir, preflight, args.timeout
            )

    baseline = core.load_baseline(args.baseline)
    print()
    print(f"dbt-preflight {version}; baseline: {baseline.get('preflight_version') or 'none'}")
    print(core.render_table(rows, baseline, projects))
    print(f"\ntotal {time.monotonic() - started:.0f}s; artefacts in {out_dir}")

    (out_dir / "results.json").write_text(
        json.dumps(
            {"preflight_version": version, "results": {k: r.as_dict() for k, r in rows.items()}},
            indent=2,
        )
        + "\n"
    )

    if args.write_baseline:
        shas = {n: p.sha for n, p in projects.items()}
        args.baseline.write_text(
            json.dumps(core.merge_baseline(baseline, rows, version, shas), indent=2) + "\n"
        )
        print(f"baseline written to {args.baseline}")
        return 0

    found = core.compare(baseline, rows)
    if found:
        print("\nWorse than baseline:")
        for k, problems in found.items():
            print(f"  {k}: {'; '.join(problems)}")
    if args.compare:
        if not baseline.get("results"):
            print("no baseline to compare against; run with --write-baseline first")
            return 1
        return 1 if found else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
