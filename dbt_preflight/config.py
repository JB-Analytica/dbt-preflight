"""Repository-level configuration, read from `.dbt-preflight.yml`.

Everything has a default so a repo with a dbt project at its root and typed sources needs
no config file at all. The file exists for the things preflight cannot infer: where the
dbt project lives, which DBML describes the sources, and which environment variables the
project's `profiles.yml` and `sources.yml` expect to find.
"""

from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dbt_preflight.conventions import ConventionError, ConventionSet, from_config

CONFIG_FILENAME = ".dbt-preflight.yml"
WORKDIR_NAME = ".preflight"

# Columns a loader adds to every table it lands. Keyed by the `loader:` value a source
# declares in its YAML, so a project that says `loader: dlt` gets dlt's lineage columns in
# its fixtures without configuring anything.
DEFAULT_LOADER_COLUMNS: dict[str, dict[str, str]] = {
    "dlt": {"_dlt_load_id": "varchar", "_dlt_id": "varchar"},
}


class ConfigError(ValueError):
    """The config file exists but cannot be used as written."""


@dataclass
class PreflightConfig:
    repo_root: Path
    project_dir: Path
    schema: Path | None = None
    path: Path | None = None  # the config file itself, when one exists
    rows: int = 200
    rows_for: dict[str, int] = field(default_factory=dict)
    seed: int = 42
    locale: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # dbt project vars, passed to every dbt invocation as `--vars`.
    vars: dict[str, Any] = field(default_factory=dict)
    loader_columns: dict[str, dict[str, str]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_LOADER_COLUMNS.items()}
    )
    # "warn": a model that fails only because DuckDB lacks a warehouse function is reported
    # as not verified and does not fail the check. "error": it does.
    dialect_failures: str = "warn"
    # Check conventions on every model, not just the changed ones.
    check_all: bool = False
    # sqlglot dialect the project's SQL is written in, transpiled to DuckDB before it runs.
    # None: detect from the project's own profiles.yml. "duckdb" or "none": run as written.
    dialect: str | None = None
    # Metrics preflight evaluates on the base branch and the pull request, on top of the
    # ones it reads from the dbt semantic layer and Lightdash meta. Each is an aggregate
    # SQL expression over one model.
    metrics: list[dict[str, str]] = field(default_factory=list)
    conventions: ConventionSet = field(default_factory=lambda: from_config(None))

    @property
    def workdir(self) -> Path:
        return self.repo_root / WORKDIR_NAME

    @property
    def project_relpath(self) -> Path:
        return self.project_dir.relative_to(self.repo_root)

    def describe_schema_source(self) -> str:
        if self.schema is None:
            return "sources.yml (derived)"
        try:
            return str(self.schema.relative_to(self.repo_root))
        except ValueError:
            return str(self.schema)


def _as_path(repo_root: Path, value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"`{key}` must be a path string, got {value!r}.")
    return (repo_root / value).resolve()


def _json_value(value: Any, key: str) -> Any:
    """A `vars:` value as JSON can carry it to `--vars`: a YAML date or timestamp becomes
    its ISO string (`start_date: 2024-01-01` is a date to YAML, and a string to dbt
    either way); anything else that is not plain JSON is refused."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [_json_value(v, f"{key}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {str(k): _json_value(v, f"{key}.{k}") for k, v in value.items()}
    raise ConfigError(f"`{key}` must be a string, number, boolean, list or mapping.")


# `env_var('NAME')` with no default. A second argument is a default, so the project runs
# without the variable set and it is not missing.
_ENV_VAR_CALL = re.compile(r"""env_var\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\)""")
_ENV_VAR_SCANNED = ("*.yml", "*.yaml", "*.sql")


def missing_env_vars(project_dir: Path, provided: dict[str, str]) -> list[str]:
    """Every `env_var()` the project reads with no default that nothing supplies.

    dbt fails on the first one it happens to render, so finding them is one run per
    variable. Reading them all off the project at once turns that into a single list.
    Only calls without a default count, because a call with one is satisfied already.
    """
    known = set(provided) | set(os.environ)
    found: set[str] = set()
    for pattern in _ENV_VAR_SCANNED:
        for path in project_dir.rglob(pattern):
            # dbt's own output, and installed packages, are not the project's to fix.
            if any(part in {"target", "dbt_packages", "logs", ".preflight"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            found.update(_ENV_VAR_CALL.findall(text))
    return sorted(found - known)


def env_var_help(names: list[str]) -> str:
    """The `env:` block to paste into `.dbt-preflight.yml`, for a run that stopped on one."""
    lines = "\n".join(f"  {name}: <value>" for name in names)
    plural = "variable" if len(names) == 1 else "variables"
    return (
        f"The project reads {len(names)} environment {plural} nothing sets. "
        "Preflight writes its own profiles.yml, so a value here only has to be "
        "something the project can parse, not a real credential:\n\n"
        f"env:\n{lines}"
    )


def load_config(repo_root: Path, config_path: Path | None = None) -> PreflightConfig:
    """Build the config from `.dbt-preflight.yml` in `repo_root`, or from `config_path`.

    Relative paths inside the file resolve against the file's own directory, so a config
    kept next to a dbt project in a subfolder (a monorepo, an examples folder) reads the
    same as one at the repository root. `repo_root` stays the git root: that is where the
    base branch gets checked out and what report paths are relative to.

    A missing file is not an error: every field has a default. A present file with a
    field of the wrong shape is, because silently ignoring `rows: "lots"` would produce a
    run whose numbers nobody can explain.
    """
    repo_root = repo_root.resolve()
    path = (config_path or (repo_root / CONFIG_FILENAME)).resolve()
    base = path.parent
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a mapping at the top level.")
        raw = loaded

    known = {
        "project_dir",
        "schema",
        "rows",
        "rows_for",
        "seed",
        "locale",
        "env",
        "vars",
        "loader_columns",
        "dialect_failures",
        "check_all",
        "dialect",
        "metrics",
        "conventions",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"Unknown keys in {path.name}: {', '.join(unknown)}.")

    project_dir = _as_path(base, raw.get("project_dir", "."), "project_dir")
    if not (project_dir / "dbt_project.yml").exists():
        raise ConfigError(
            f"No dbt_project.yml in {project_dir}. Set `project_dir` in {path.name} to the "
            "folder that holds the dbt project."
        )
    if repo_root not in project_dir.parents and project_dir != repo_root:
        raise ConfigError(
            f"The dbt project at {project_dir} is outside the repository {repo_root}."
        )

    schema = None
    if raw.get("schema") is not None:
        schema = _as_path(base, raw["schema"], "schema")
        if not schema.exists():
            raise ConfigError(f"`schema` points at {schema}, which does not exist.")

    rows = raw.get("rows", 200)
    if not isinstance(rows, int) or rows < 10:
        raise ConfigError("`rows` must be a whole number of at least 10.")

    rows_for = raw.get("rows_for") or {}
    if not isinstance(rows_for, dict) or not all(
        isinstance(v, int) and v >= 1 for v in rows_for.values()
    ):
        raise ConfigError("`rows_for` must map table names to whole numbers of at least 1.")

    seed = raw.get("seed", 42)
    if not isinstance(seed, int):
        raise ConfigError("`seed` must be a whole number.")

    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError("`env` must map variable names to values.")
    env = {str(k): str(v) for k, v in env.items()}
    dbt_vars = raw.get("vars") or {}
    if not isinstance(dbt_vars, dict):
        raise ConfigError("`vars` must map dbt variable names to values.")
    dbt_vars = {str(k): _json_value(v, f"vars.{k}") for k, v in dbt_vars.items()}

    loader_columns = {k: dict(v) for k, v in DEFAULT_LOADER_COLUMNS.items()}
    extra = raw.get("loader_columns") or {}
    if not isinstance(extra, dict):
        raise ConfigError("`loader_columns` must map loader names to {column: type}.")
    for loader, cols in extra.items():
        if not isinstance(cols, dict):
            raise ConfigError(f"`loader_columns.{loader}` must map column names to types.")
        loader_columns[str(loader)] = {str(c): str(t) for c, t in cols.items()}

    dialect_failures = str(raw.get("dialect_failures", "warn"))
    if dialect_failures not in {"warn", "error"}:
        raise ConfigError("`dialect_failures` must be `warn` or `error`.")

    check_all = bool(raw.get("check_all", False))

    metrics: list[dict[str, str]] = []
    for i, m in enumerate(raw.get("metrics") or []):
        if not isinstance(m, dict) or not all(
            isinstance(m.get(k), str) for k in ("name", "model", "sql")
        ):
            raise ConfigError(
                f"`metrics[{i}]` needs `name`, `model` and `sql` (an aggregate expression)."
            )
        metrics.append({k: str(v) for k, v in m.items()})

    try:
        conventions = from_config(raw.get("conventions"))
    except ConventionError as exc:
        raise ConfigError(str(exc)) from None

    dialect = raw.get("dialect")
    if dialect is not None and not isinstance(dialect, str):
        raise ConfigError("`dialect` must be a string such as `bigquery` or `snowflake`.")

    return PreflightConfig(
        repo_root=repo_root,
        project_dir=project_dir,
        schema=schema,
        path=path if path.exists() else None,
        rows=rows,
        rows_for={str(k): int(v) for k, v in rows_for.items()},
        seed=seed,
        locale=raw.get("locale"),
        env=env,
        vars=dbt_vars,
        loader_columns=loader_columns,
        dialect_failures=dialect_failures,
        check_all=check_all,
        dialect=dialect.lower() if dialect else None,
        metrics=metrics,
        conventions=conventions,
    )
