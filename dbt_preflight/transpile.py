"""Run warehouse-dialect SQL on DuckDB by transpiling it with sqlglot.

dbt compiles each model for the DuckDB target, so `ref()` and `source()` already resolve to
DuckDB relations. What is left in the SQL is the project's own dialect: `timestamp_diff`,
`initcap`, `safe_divide`, BigQuery's argument order for `date_trunc`. sqlglot rewrites that
into DuckDB's spelling. The hook sits on dbt's compiler, after Jinja rendering and before
the materialisation wraps the body in `create view ... as`, so dbt's own bookkeeping SQL is
never touched.

DuckDB gets the first word. A model it accepts as written is left alone: a project's own
`adapter.dispatch` macros already rendered their DuckDB branch, and transpiling that from
BigQuery would break what was portable. Only a model DuckDB rejects is rewritten, and
anything sqlglot cannot parse runs as written and is reported, so a transpile failure never
hides a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
import yaml
from sqlglot.errors import SqlglotError

# dbt adapter `type` -> sqlglot dialect. Types absent here run untranslated.
ADAPTER_DIALECTS = {
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "postgres": "postgres",
    "redshift": "redshift",
    "databricks": "databricks",
    "spark": "spark",
    "trino": "trino",
    "athena": "trino",
    "clickhouse": "clickhouse",
    "mysql": "mysql",
    "sqlserver": "tsql",
    "synapse": "tsql",
    "fabric": "tsql",
    "oracle": "oracle",
}

# Dialects where a double-quoted token is a string, not an identifier. dbt renders DuckDB
# relations as "db"."schema"."table"; those must read as identifiers to the parser.
_BACKTICK_DIALECTS = {"bigquery", "spark", "databricks", "hive"}
_QUOTED_RELATION = re.compile(r'"([^"\n]+)"\."([^"\n]+)"(?:\."([^"\n]+)")?')


def detect_dialect(project_dir: Path, profile: str) -> str | None:
    """The sqlglot dialect of the project's own target, read from a checked-in profiles.yml.

    Only the project directory is consulted, never `~/.dbt`: a profile in the repository
    documents what the project is written for; one on a developer's machine is theirs.
    """
    path = project_dir / "profiles.yml"
    if not path.exists():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    entry = doc.get(profile) if isinstance(doc, dict) else None
    if not isinstance(entry, dict):
        return None
    outputs = entry.get("outputs") or {}
    if not isinstance(outputs, dict) or not outputs:
        return None
    target = entry.get("target")
    chosen = outputs.get(target) if isinstance(target, str) else None
    if not isinstance(chosen, dict):
        chosen = next((o for o in outputs.values() if isinstance(o, dict)), None)
    if not chosen:
        return None
    adapter = str(chosen.get("type", "")).lower()
    return ADAPTER_DIALECTS.get(adapter)


def _protect_relations(sql: str, dialect: str) -> str:
    if dialect not in _BACKTICK_DIALECTS:
        return sql

    def repl(m: re.Match[str]) -> str:
        parts = [p for p in m.groups() if p is not None]
        return ".".join(f"`{p}`" for p in parts)

    return _QUOTED_RELATION.sub(repl, sql)


def transpile_sql(sql: str, dialect: str) -> str:
    """`sql` in `dialect`, rewritten for DuckDB. Raises SqlglotError when it cannot parse."""
    statements = sqlglot.transpile(
        _protect_relations(sql, dialect), read=dialect, write="duckdb", pretty=True
    )
    return ";\n\n".join(s for s in statements if s.strip())


@dataclass
class TranspileHook:
    """Patches dbt's compiler so model bodies are transpiled after rendering.

    Keeps a record of what it did, for the comment: which nodes were rewritten and which
    could not be parsed and ran as written.
    """

    dialect: str
    db_path: Path | None = None  # the fixture database, to ask DuckDB before rewriting
    rewritten: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)  # DuckDB ran them as written
    unparsed: dict[str, str] = field(default_factory=dict)
    _original: Any = None
    _con: Any = None

    def install(self) -> None:
        from dbt.compilation import Compiler

        if self._original is not None:
            return
        original = Compiler._compile_code
        hook = self

        def _compile_code(compiler, node, manifest, extra_context=None):
            node = original(compiler, node, manifest, extra_context)
            hook._rewrite(node)
            return node

        Compiler._compile_code = _compile_code  # ty: ignore[invalid-assignment]
        self._original = original

    def uninstall(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
        if self._original is None:
            return
        from dbt.compilation import Compiler

        Compiler._compile_code = self._original
        self._original = None

    def _duckdb_accepts(self, sql: str) -> bool:
        """Whether DuckDB can plan `sql` as written, against the relations built so far.

        dbt compiles a node right before it runs it, so its upstream relations exist by
        then. EXPLAIN plans without executing.
        """
        if self.db_path is None:
            return False
        try:
            if self._con is None:
                self._con = duckdb.connect(str(self.db_path))
            self._con.execute(f"explain {sql}")
            return True
        except duckdb.Error:
            return False

    def _rewrite(self, node: Any) -> None:
        rtype = str(getattr(node, "resource_type", "")).split(".")[-1].lower()
        # Models and hand-written (singular) tests carry the project's dialect. Generic
        # tests are rendered from dbt macros for DuckDB already.
        is_singular_test = rtype == "test" and getattr(node, "test_metadata", None) is None
        if rtype not in {"model", "snapshot"} and not is_singular_test:
            return
        code = getattr(node, "compiled_code", None)
        if not code or not code.strip():
            return
        if self._duckdb_accepts(code):
            self.accepted.append(node.name)
            return
        try:
            node.compiled_code = transpile_sql(code, self.dialect)
            self.rewritten.append(node.name)
        except SqlglotError as exc:
            self.unparsed[node.name] = str(exc).splitlines()[0][:200]
