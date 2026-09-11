"""Running dbt against the fixture database.

dbt is invoked in-process through `dbtRunner`, the supported programmatic API. Every call
gets its own profiles directory and target path inside the preflight work directory, so
nothing preflight does touches the project's real `target/` or the developer's profiles.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dbt_preflight.transpile import TranspileHook

TARGET_NAME = "preflight"
BASE_TARGET_NAME = "preflight_base"  # the base branch builds here, on the same fixtures

# DuckDB's way of saying "that function is not one of mine". A model failing with one of
# these used a warehouse dialect DuckDB does not speak; that is a fact about the check,
# not about the pull request.
_DIALECT_PATTERNS = [
    re.compile(r"(?:Scalar|Aggregate|Macro|Table) Function with name (\w+) does not exist"),
    re.compile(r"Parser Error"),
    re.compile(r"Binder Error: No function matches the given name and argument types '(\w+)"),
]


class DbtError(RuntimeError):
    pass


@dataclass
class DbtProject:
    dir: Path
    name: str
    profile: str
    model_paths: list[str]
    has_packages: bool


def read_project(project_dir: Path) -> DbtProject:
    raw = yaml.safe_load((project_dir / "dbt_project.yml").read_text(encoding="utf-8")) or {}
    profile = raw.get("profile") or raw.get("name")
    if not profile:
        raise DbtError("dbt_project.yml has neither `profile` nor `name`.")
    return DbtProject(
        dir=project_dir,
        name=str(raw.get("name", profile)),
        profile=str(profile),
        model_paths=[str(p) for p in raw.get("model-paths", ["models"])],
        has_packages=(project_dir / "packages.yml").exists()
        or (project_dir / "dependencies.yml").exists(),
    )


def write_profiles(profiles_dir: Path, profile: str, db_path: Path) -> Path:
    """A profiles.yml with a single DuckDB target pointing at the fixture file.

    The DuckDB catalog is named after the file, so the caller picks `db_path`'s stem to
    match whatever `database` the project's sources resolve to.
    """
    profiles_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        profile: {
            "target": TARGET_NAME,
            "outputs": {
                TARGET_NAME: {
                    "type": "duckdb",
                    "path": str(db_path),
                    "schema": "preflight",
                    "threads": 4,
                },
                BASE_TARGET_NAME: {
                    "type": "duckdb",
                    "path": str(db_path),
                    "schema": "preflight_base",
                    "threads": 4,
                },
            },
        }
    }
    path = profiles_dir / "profiles.yml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


@dataclass
class NodeResult:
    unique_id: str
    name: str
    resource_type: str
    status: str  # success | error | skipped | pass | fail | warn
    message: str
    failures: int | None
    execution_time: float
    compiled_code: str | None = None
    model_name: str | None = None  # unit tests: the model they exercise, by name
    tested_node: str | None = None  # unit tests: the model they exercise, by unique id
    depends_on: list[str] = field(default_factory=list)

    @property
    def dialect_function(self) -> str | None:
        """The DuckDB-missing function this node failed on, if that is why it failed."""
        if self.status != "error":
            return None
        for pattern in _DIALECT_PATTERNS:
            m = pattern.search(self.message or "")
            if m:
                return m.group(1) if m.groups() else "syntax"
        return None

    @property
    def is_parser_error(self) -> bool:
        """DuckDB could not parse the statement at all.

        Without a known dialect that may be foreign syntax; with one, the SQL was already
        rewritten for DuckDB (or sqlglot could not parse it either), so it is a real error.
        """
        return self.status == "error" and "Parser Error" in (self.message or "")


@dataclass
class RunOutcome:
    success: bool
    results: list[NodeResult] = field(default_factory=list)
    elapsed: float = 0.0
    error: str | None = None


class DbtRunner:
    def __init__(
        self,
        project: DbtProject,
        profiles_dir: Path,
        target_path: Path,
        log_path: Path,
        env: dict[str, str],
        target: str = TARGET_NAME,
    ) -> None:
        self.project = project
        self.profiles_dir = profiles_dir
        self.target_path = target_path
        self.log_path = log_path
        self.env = env
        self.target = target

    def _args(self, command: str, *extra: str) -> list[str]:
        return [
            command,
            "--project-dir",
            str(self.project.dir),
            "--profiles-dir",
            str(self.profiles_dir),
            "--target",
            self.target,
            "--target-path",
            str(self.target_path),
            "--log-path",
            str(self.log_path),
            "--log-level",
            "warn",
            "--log-level-file",
            "info",
            "--no-use-colors",
            *extra,
        ]

    def _invoke(self, args: list[str], quiet: bool = False) -> Any:
        from dbt.cli.main import dbtRunner

        previous = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
        try:
            # dbt logs to stdout. Stdout is where the comment goes when no file is given,
            # so dbt's own output moves to stderr, where CI still shows it. Commands whose
            # result is read programmatically (`ls`) print nothing at all.
            sink = io.StringIO() if quiet else sys.stderr
            with contextlib.redirect_stdout(sink):
                return dbtRunner().invoke(args)
        finally:
            for k, v in previous.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def deps(self) -> None:
        if not self.project.has_packages:
            return
        # `dbt deps` does not take --target or --target-path; give it only what it knows.
        res = self._invoke(
            [
                "deps",
                "--project-dir",
                str(self.project.dir),
                "--profiles-dir",
                str(self.profiles_dir),
                "--log-path",
                str(self.log_path),
                "--log-level",
                "warn",
                "--no-use-colors",
            ]
        )
        if not res.success:
            raise DbtError(f"dbt deps failed: {res.exception}")

    def parse(self) -> Path:
        """Parse the project and return the path of the manifest it wrote."""
        res = self._invoke(self._args("parse"))
        if not res.success:
            raise DbtError(f"dbt parse failed: {res.exception}")
        manifest = self.target_path / "manifest.json"
        if not manifest.exists():
            raise DbtError(f"dbt parse succeeded but wrote no manifest at {manifest}.")
        return manifest

    def modified_nodes(self, state_dir: Path) -> list[str]:
        """Unique ids of the models and sources `state:modified` selects against `state_dir`."""
        res = self._invoke(
            self._args(
                "ls",
                "--select",
                "state:modified",
                "--state",
                str(state_dir),
                "--resource-type",
                "model",
                "--resource-type",
                "source",
                "--output",
                "json",
                "--output-keys",
                "unique_id",
                "--quiet",
            ),
            quiet=True,
        )
        if not res.success:
            raise DbtError(f"dbt ls failed: {res.exception}")
        ids: list[str] = []
        for line in res.result or []:
            try:
                ids.append(json.loads(line)["unique_id"])
            except (ValueError, KeyError, TypeError):
                continue
        return ids

    def build(
        self,
        select: list[str] | None,
        transpile: TranspileHook | None = None,
        command: str = "build",
    ) -> RunOutcome:
        """Build (and test) the given models, or everything when `select` is None.

        Tests run under `cautious` indirect selection: only when every model they read is
        in the selection. The caller closes the selection over those models, so a test
        that is skipped here is one that nothing in the change can affect.
        """
        extra: list[str] = []
        if select is not None:
            if not select:
                return RunOutcome(success=True)
            extra += ["--select", *select]
            if command == "build":
                extra += ["--indirect-selection", "cautious"]
        if transpile is not None:
            transpile.install()
        try:
            res = self._invoke(self._args(command, *extra))
        finally:
            if transpile is not None:
                transpile.uninstall()

        outcome = RunOutcome(success=bool(res.success))
        result = res.result
        if result is None:
            outcome.error = str(res.exception) if res.exception else "dbt build produced no result."
            outcome.success = False
            return outcome

        outcome.elapsed = float(getattr(result, "elapsed_time", 0.0) or 0.0)
        for r in getattr(result, "results", []) or []:
            node = r.node
            outcome.results.append(
                NodeResult(
                    unique_id=node.unique_id,
                    name=node.name,
                    resource_type=str(node.resource_type).split(".")[-1].lower(),
                    status=str(r.status).split(".")[-1].lower(),
                    message=str(r.message or ""),
                    failures=r.failures,
                    execution_time=float(r.execution_time or 0.0),
                    compiled_code=getattr(node, "compiled_code", None),
                    model_name=getattr(node, "model", None),
                    tested_node=getattr(node, "tested_node_unique_id", None),
                    depends_on=list(getattr(getattr(node, "depends_on", None), "nodes", []) or []),
                )
            )
        return outcome
