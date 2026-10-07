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
from collections.abc import Callable
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


def profiles_candidates(project_dir: Path, repo_root: Path | None = None) -> list[Path]:
    """Where a checked-in `profiles.yml` may sit, nearest the project first.

    dbt itself only looks beside `dbt_project.yml` (and in `~/.dbt`), but a repository that
    keeps the dbt project in a subdirectory very often keeps the profile at the repository
    root instead, next to the CI workflow that uses it. Looking only beside the project
    meant no dialect was detected for those, and every warehouse function went untranspiled.
    """
    seen: list[Path] = []
    for directory in (project_dir, project_dir.parent, repo_root):
        if directory is None:
            continue
        candidate = (directory / "profiles.yml").resolve()
        if candidate not in seen:
            seen.append(candidate)
    return seen


def detect_dialect(project_dir: Path, profile: str, repo_root: Path | None = None) -> str | None:
    """The sqlglot dialect of the project's own target, read from a checked-in profiles.yml.

    Only the repository is consulted, never `~/.dbt`: a profile in the repository documents
    what the project is written for; one on a developer's machine is theirs.
    """
    for path in profiles_candidates(project_dir, repo_root):
        dialect = _dialect_from(path, profile)
        if dialect is not None:
            return dialect
    return None


def _dialect_from(path: Path, profile: str) -> str | None:
    if not path.exists():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
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


_LEX = re.compile(
    r"""(?P<comment>--[^\n]*|/\*.*?\*/)
    |(?P<string>'{3}.*?'{3}|'(?:\\.|''|[^'\\])*')
    |(?P<backtick>`[^`]*`)
    |(?P<dq>"[^"`\n]*")
    |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    |(?P<space>\s+)
    |(?P<punct>.)""",
    re.VERBOSE | re.DOTALL,
)
_SELECT_ITEM_START = {",", "select", "distinct"}
_SELECT_ITEM_END = {",", "from", "as"}


def _protect_identifiers(sql: str, dialect: str) -> str:
    return _protect(sql, dialect)[0]


def _protect(sql: str, dialect: str) -> tuple[str, set[str]]:
    """dbt-rendered double-quoted identifiers as backticked ones, for a dialect where a
    double-quoted token is a string.

    dbt renders for the DuckDB target, so a macro quotes identifiers with double quotes:
    `dbt_utils.star` writes `"customer_id",\n  "email"`, which BigQuery's grammar reads as
    string literals (`SELECT 'customer_id'`). A double-quoted token is taken for an
    identifier where only an identifier can stand: next to a `.` (`"db"."schema"."t"`,
    `t."col"`), after `as`, or alone as an item of a select list, outside any function
    call. Everywhere else (`status = "paid"`, `concat(a, "-")`) it stays BigQuery's string,
    and single-quoted strings, backticks and comments are never touched."""
    if dialect not in _BACKTICK_DIALECTS:
        return sql, set()
    rewritten: set[str] = set()
    tokens = [(m.lastgroup, m.group()) for m in _LEX.finditer(sql)]
    significant = [i for i, (kind, _) in enumerate(tokens) if kind not in {"space", "comment"}]

    def key(i: int) -> str | None:
        kind, text = tokens[i]
        return text.lower() if kind in {"word", "punct"} else kind

    frames = [{"call": False, "select": False}]  # one per parenthesis level
    out = [text for _, text in tokens]
    for n, i in enumerate(significant):
        kind, text = tokens[i]
        prev = key(significant[n - 1]) if n > 0 else None
        nxt = key(significant[n + 1]) if n + 1 < len(significant) else None
        if kind == "punct" and text == "(":
            frames.append({"call": nxt not in {"select", "with"}, "select": False})
        elif kind == "punct" and text == ")" and len(frames) > 1:
            frames.pop()
        elif kind == "word" and text.lower() == "select":
            frames[-1]["select"] = True
        elif kind == "word" and text.lower() == "from":
            frames[-1]["select"] = False
        elif kind == "dq" and len(text) > 2:
            frame = frames[-1]
            select_item = (
                frame["select"]
                and not frame["call"]
                and prev in _SELECT_ITEM_START
                and (nxt in _SELECT_ITEM_END or nxt is None)
            )
            if prev == "." or nxt == "." or prev == "as" or select_item:
                out[i] = f"`{text[1:-1]}`"
                rewritten.add(text[1:-1])
    return "".join(out), rewritten


def _transpile(sql: str, dialect: str) -> str:
    statements = sqlglot.transpile(sql, read=dialect, write="duckdb", pretty=True)
    return ";\n\n".join(s for s in statements if s.strip())


def _about(error: str, names: set[str]) -> bool:
    """Whether DuckDB's error is a binder error naming one of `names`: a column the
    identifier reading looked for and did not find."""
    lowered = error.lower()
    return "binder error" in lowered and any(f'"{n.lower()}"' in lowered for n in names)


def transpile_sql(
    sql: str, dialect: str, explain: Callable[[str], str | None] | None = None
) -> str:
    """`sql` in `dialect`, rewritten for DuckDB. Raises SqlglotError when it cannot parse.

    Read with dbt's double-quoted identifiers kept as identifiers (`_protect_identifiers`).
    `explain`, when given, asks DuckDB to plan a result: None when it can, else its error.
    Only when the identifier reading fails for a reason other than one of those identifiers
    being missing does the plain reading (relation names only, every other double-quoted
    token a string) get a chance, and only if DuckDB can plan it. A missing column stays a
    missing column: read as a string constant it would quietly pass a broken model."""
    protected, names = _protect(sql, dialect)
    plain = _protect_relations(sql, dialect)
    try:
        out = _transpile(protected, dialect)
    except SqlglotError:
        if plain == protected:
            raise
        return _transpile(plain, dialect)
    if explain is None or plain == protected:
        return out
    error = explain(out)
    if error is None or _about(error, names):
        return out
    try:
        alternative = _transpile(plain, dialect)
    except SqlglotError:
        return out
    return alternative if explain(alternative) is None else out


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
        return self.db_path is not None and self._duckdb_error(sql) is None

    def _duckdb_error(self, sql: str) -> str | None:
        """DuckDB's error planning `sql`, or None when it plans (`_duckdb_accepts`)."""
        if self.db_path is None:
            return "no database"
        try:
            if self._con is None:
                self._con = duckdb.connect(str(self.db_path))
            self._con.execute(f"explain {sql}")
            return None
        except duckdb.Error as exc:
            return str(exc)

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
            explain = self._duckdb_error if self.db_path is not None else None
            node.compiled_code = transpile_sql(code, self.dialect, explain)
            self.rewritten.append(node.name)
        except SqlglotError as exc:
            self.unparsed[node.name] = str(exc).splitlines()[0][:200]
