"""What a model compiles to, for the source columns its raw SQL hides behind a macro.

Inference from `raw_code` (see `schema.py`) sees what a staging model spells out. A model
that reads its source through a macro shows nothing there: Fivetran's packages read every
source through a `stg_<table>_tmp` model built by `union_connections`, then fill the staging
model's columns with `fill_staging_columns(...)`; a project's own `source_or_empty(...)`
hides the `source()` call. `dbt compile` renders all of that, so the compiled SQL is read
too, as a second opinion that only fills gaps the raw SQL leaves.

Preflight compiles against an empty DuckDB file, before any fixture exists, on the head and
the base alike, so both sides see the same thing: a macro that introspects a missing
relation falls back to the column list it was written with (`cast(null as numeric(28,6)) as
id`), which is exactly the package's statement of the columns it expects.

In compiled SQL a source is no longer a `{{ source() }}` call but a relation name, so a
relation is matched to a source, or to a model, by its manifest `relation_name`, quoting and
case normalised (DuckDB compares identifiers case-insensitively).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from dbt_preflight.manifest import Manifest, ModelNode

TARGET_PLACEHOLDER = "__preflight_target__"

# Columns dbt or a package adds itself, never ones the source system has.
_INTERNAL_PREFIXES = ("_dbt_",)

_RAW_CALL_RE = re.compile(
    r"""\{\{\s*(?:source|ref)\(\s*['"][^'"]+['"](?:\s*,\s*['"][^'"]+['"])?\s*\)\s*\}\}"""
)
_RAW_CONFIG_RE = re.compile(r"\{\{\s*config\(.*?\)\s*\}\}", re.DOTALL)
_RAW_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)

RelationKey = tuple[str, ...]


def relation_key(relation_name: str | None) -> RelationKey | None:
    """`"Spike"."main"."Customer"` -> ("spike", "main", "customer"); None if unreadable."""
    if not relation_name:
        return None
    try:
        table = exp.to_table(relation_name, dialect="duckdb")
    except Exception:  # noqa: BLE001 - a relation name sqlglot cannot read just never matches
        return None
    return table_key(table)


def table_key(table: exp.Table) -> RelationKey | None:
    """The normalised (catalog, schema, name) of a table reference in parsed SQL."""
    parts = tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)
    return parts or None


# Dialects where a double-quoted token is a string, not an identifier (as in transpile.py).
_BACKTICK_DIALECTS = {"bigquery", "spark", "databricks", "hive"}
_QUOTED_PART = re.compile(r'"([^"\n]+)"(?=\.)|(?<=\.)"([^"\n]+)"')


_COLUMN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse(sql: str, read: str | None) -> exp.Expr | None:
    try:
        return sqlglot.parse_one(sql, read=read)
    except Exception:  # noqa: BLE001 - any parser failure just means "cannot infer"
        return None


def _odd_columns(tree: exp.Expr) -> bool:
    """Whether a parse found a column no warehouse would name - ` `, `1` - which is what a
    BigQuery string literal (`replace(x, " ", "_")`) becomes in DuckDB's grammar."""
    return any(not _COLUMN_NAME.fullmatch(c.name) for c in tree.find_all(exp.Column) if c.name)


def parse_compiled(sql: str, dialect: str | None = None) -> exp.Expr | None:
    """Parse compiled SQL: DuckDB first, since dbt rendered it for a DuckDB target and quotes
    relations with double quotes; then the default dialect. The project's own dialect only
    as a fallback - when neither parses it, or DuckDB's reading has columns no warehouse
    would name - since the model's body is still written in it. For a dialect where double
    quotes make a string (BigQuery), dbt's `"db"."schema"."table"` is re-quoted with
    backticks for that attempt. None when nothing parses it."""
    first = _parse(sql, "duckdb") or _parse(sql, None)
    if first is not None and not _odd_columns(first):
        return first
    if dialect and dialect not in {"duckdb", "none"}:
        own = sql
        if dialect in _BACKTICK_DIALECTS:
            own = _QUOTED_PART.sub(lambda m: f"`{m.group(1) or m.group(2)}`", sql)
        fallback = _parse(own, dialect)
        if fallback is not None:
            return fallback
    return first


@dataclass
class CompiledSql:
    """The compiled SQL of the models preflight compiled, and every node's relation."""

    code: dict[str, str] = field(default_factory=dict)  # model unique_id -> compiled SQL
    relations: dict[str, RelationKey] = field(default_factory=dict)  # source/model uid -> key
    failed: list[str] = field(default_factory=list)  # unique ids dbt could not compile
    dialect: str | None = None  # the project's own SQL dialect, a parsing fallback

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], failed: list[str] | None = None, dialect: str | None = None
    ) -> CompiledSql:
        out = cls(failed=sorted(failed or []), dialect=dialect)
        for uid, src in (raw.get("sources") or {}).items():
            key = relation_key(src.get("relation_name"))
            if key:
                out.relations[uid] = key
        for uid, node in (raw.get("nodes") or {}).items():
            if node.get("resource_type") != "model":
                continue
            key = relation_key(node.get("relation_name"))
            if key:
                out.relations[uid] = key
            code = node.get("compiled_code")
            if isinstance(code, str) and code.strip():
                out.code[uid] = code
        return out

    @classmethod
    def load(
        cls, manifest_path: Path, failed: list[str] | None = None, dialect: str | None = None
    ) -> CompiledSql:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls.from_dict(raw, failed, dialect)


def compile_selection(manifest: Manifest) -> list[str]:
    """The models worth compiling: every model that reads a source directly, and every
    model reading one of those that reads a single source and nothing else (a candidate
    pass-through such as Fivetran's `stg_<table>_tmp`). Downstream marts never shape a
    source's columns, and compiling them costs time and the odd macro that cannot run
    against an empty database. Sorted unique ids."""
    direct = {
        uid
        for uid, model in manifest.models.items()
        if any(dep in manifest.sources for dep in model.depends_on)
    }
    candidates = {
        uid
        for uid in direct
        if len(manifest.models[uid].depends_on) == 1
        and manifest.models[uid].depends_on[0] in manifest.sources
    }
    second = {
        uid
        for uid, model in manifest.models.items()
        if any(dep in candidates for dep in model.depends_on)
    }
    return sorted(direct | second)


# A Jinja expression mentioning JSON: `{{ fivetran_utils.json_parse("receipt", [...]) }}`.
_JINJA_JSON_RE = re.compile(r"\{\{(?:(?!\}\}).)*json", re.IGNORECASE | re.DOTALL)


def json_compile_selection(
    manifest: Manifest, already: set[str] | frozenset[str] = frozenset()
) -> list[str]:
    """Models whose raw SQL calls a macro with `json` in its name or arguments, minus
    `already`. What such a model reads as JSON shows only in its compiled SQL, and a mart
    is not otherwise compiled (`compile_selection`). A JSON function written out in the
    raw SQL needs no compiling: the raw SQL is read too. Sorted unique ids."""
    return sorted(
        uid
        for uid, model in manifest.models.items()
        if uid not in already and _JINJA_JSON_RE.search(model.raw_code or "")
    )


def _is_star_select(tree: exp.Expr, upstream: RelationKey | None) -> bool:
    """`select * from <upstream>`, optionally with a `where`, and nothing else: no `limit`
    or `offset`, and a bare `*`, not one with `except`/`replace`/`rename`."""
    if not isinstance(tree, exp.Select) or upstream is None:
        return False
    for arg in (
        "with_", "joins", "group", "having", "qualify", "order", "distinct", "laterals",
        "limit", "offset", "windows", "pivots", "sample",
    ):  # fmt: skip
        if tree.args.get(arg):
            return False
    if len(tree.expressions) != 1 or not isinstance(tree.expressions[0], exp.Star):
        return False
    if any(tree.expressions[0].args.get(a) for a in ("except_", "replace", "rename", "ilike")):
        return False
    from_ = tree.args.get("from_")
    table = from_.this if from_ is not None else None
    return isinstance(table, exp.Table) and table_key(table) == upstream


def is_empty_stand_in(tree: exp.Expr) -> bool:
    """A select of typed nulls that reads no table and returns no rows: what a macro
    renders in place of a source that does not exist yet (`union_connections`,
    `source_or_empty`). `limit 0` or `where false` says it returns nothing."""
    if not isinstance(tree, exp.Select) or tree.args.get("with_"):
        return False
    if any(True for _ in tree.find_all(exp.Table)):
        return False
    if not tree.expressions or not all(
        isinstance(e, exp.Alias) and _is_null_cast(e.this) for e in tree.expressions
    ):
        return False
    limit = tree.args.get("limit")
    if isinstance(limit, exp.Limit):
        value = limit.expression
        if isinstance(value, exp.Literal) and not value.is_string and str(value.this) == "0":
            return True
    where = tree.args.get("where")
    return (
        isinstance(where, exp.Where)
        and isinstance(where.this, exp.Boolean)
        and where.this.this is False
    )


def _is_null_cast(e: exp.Expr) -> bool:
    return isinstance(e, exp.Cast) and isinstance(e.this, exp.Null)


def _raw_star_select_tree(raw_code: str) -> exp.Expr | None:
    """The parsed raw SQL of a pass-through - `select * from {{ source(...) }}` with at most
    a `where` and a `config()` call - or None. Anything else in Jinja and it is not one."""
    sql = _RAW_CONFIG_RE.sub("", raw_code)
    sql = _RAW_COMMENT_RE.sub("", sql)
    sql, n = _RAW_CALL_RE.subn(TARGET_PLACEHOLDER, sql)
    if n != 1 or "{{" in sql or "{%" in sql:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=None)
    except Exception:  # noqa: BLE001
        return None
    if tree is None or not _is_star_select(tree, (TARGET_PLACEHOLDER.lower(),)):
        return None
    return tree


def null_casts(tree: exp.Expr) -> dict[str, str]:
    """{column: type} for every `cast(null as T) as column` in any select of the query.

    In a model that reads one source and nothing else, this is the package stating which
    column of that source it expects, and as what type. The first occurrence wins; names
    dbt or a package adds itself (`_dbt_source_relation`) are not the source's."""
    out: dict[str, str] = {}
    for select in tree.find_all(exp.Select):
        for e in select.expressions:
            if not isinstance(e, exp.Alias) or not _is_null_cast(e.this):
                continue
            name = e.alias_or_name.lower()
            if not name or name.startswith(_INTERNAL_PREFIXES):
                continue
            cast = e.this
            assert isinstance(cast, exp.Cast)
            out.setdefault(name, cast.to.sql(dialect="duckdb"))
    return out


def with_target(tree: exp.Expr, is_target: Callable[[exp.Table], bool]) -> tuple[exp.Expr, bool]:
    """A copy of `tree` with every table reference `is_target` accepts replaced by the
    placeholder the column walk in `schema.py` looks for. A reference with no alias keeps
    its own table name as one, so `customer.id` still resolves. Also whether any was found."""
    tree = tree.copy()
    found = False
    for table in list(tree.find_all(exp.Table)):
        if not is_target(table):
            continue
        found = True
        alias = table.alias or table.name
        table.set("this", exp.to_identifier(TARGET_PLACEHOLDER))
        table.set("db", None)
        table.set("catalog", None)
        table.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    return tree, found


class CompiledView:
    """Compiled SQL read against one manifest: parsed trees, pass-through models, and which
    source each model reads.

    A *pass-through* is a model that reads one source (or another pass-through) and nothing
    else, and whose SQL is `select * from` it, optionally with a `where`, or an empty stand-in
    of typed nulls. It counts as that source: a model reading it reads the source."""

    def __init__(self, manifest: Manifest, compiled: CompiledSql) -> None:
        self.manifest = manifest
        self.compiled = compiled
        self._trees: dict[str, exp.Expr | None] = {}
        self._by_key: dict[RelationKey, str] = {}
        by_suffix: dict[RelationKey, set[str]] = {}
        for uid, key in sorted(compiled.relations.items()):
            self._by_key.setdefault(key, uid)
            by_suffix.setdefault(key[-2:], set()).add(uid)
        # `schema.table` with no database names a node only when one node has that suffix.
        self._by_suffix = {k: next(iter(v)) for k, v in by_suffix.items() if len(v) == 1}
        self.pass_through: dict[str, str] = {}
        # Pass-throughs with a `where` somewhere along the chain: still the source's
        # columns, but not its rows, so no test carries back through one.
        self.filtered: set[str] = set()
        for uid in sorted(manifest.models):
            src = self._pass_through_source(uid, frozenset())
            if src is not None:
                self.pass_through[uid] = src
        self._readers: dict[str, list[ModelNode]] = {}
        for uid in sorted(manifest.models):
            model = manifest.models[uid]
            for src in sorted(set(self.upstream_sources(model).values())):
                self._readers.setdefault(src, []).append(model)

    def tree(self, uid: str) -> exp.Expr | None:
        if uid not in self._trees:
            code = self.compiled.code.get(uid)
            self._trees[uid] = parse_compiled(code, self.compiled.dialect) if code else None
        return self._trees[uid]

    def node_for(self, table: exp.Table) -> str | None:
        """The source or model a table reference in compiled SQL names, if any: by its full
        relation name, or by `schema.table` when exactly one node ends that way."""
        key = table_key(table)
        if key is None:
            return None
        if key in self._by_key:
            return self._by_key[key]
        return self._by_suffix.get(key) if len(key) == 2 else None

    def _pass_through_source(self, uid: str, seen: frozenset[str]) -> str | None:
        model = self.manifest.models.get(uid)
        if model is None or len(model.depends_on) != 1 or uid in seen:
            return None
        upstream = model.depends_on[0]
        if upstream in self.manifest.sources:
            source = upstream
        elif upstream in self.manifest.models:
            source = self._pass_through_source(upstream, seen | {uid})
        else:
            return None
        if source is None:
            return None
        tree = self.tree(uid)
        filtered = upstream in self.filtered
        if tree is not None:
            star = _is_star_select(tree, self.compiled.relations.get(upstream))
            filtered = filtered or (star and tree.args.get("where") is not None)
            # An empty stand-in is a source that did not exist at compile time, not a
            # filter: once the source exists, the macro renders `select *` from it.
            ok = star or (upstream in self.manifest.sources and is_empty_stand_in(tree))
        else:
            raw_tree = _raw_star_select_tree(model.raw_code)
            ok = upstream in self.manifest.sources and raw_tree is not None
            filtered = filtered or (raw_tree is not None and raw_tree.args.get("where") is not None)
        if ok and filtered:
            self.filtered.add(uid)
        return source if ok else None

    def upstream_sources(self, model: ModelNode) -> dict[str, str]:
        """{node the model depends on: the source it stands for}, for every dependency
        that is a source or a pass-through."""
        out: dict[str, str] = {}
        for dep in model.depends_on:
            if dep in self.manifest.sources:
                out[dep] = dep
            elif dep in self.pass_through:
                out[dep] = self.pass_through[dep]
        return out

    def single_source(self, model: ModelNode) -> str | None:
        """The one source a model reads, directly or through a pass-through, when every
        node it depends on resolves to that same source; None otherwise."""
        if not model.depends_on:
            return None
        upstream = self.upstream_sources(model)
        if len(upstream) != len(model.depends_on):
            return None
        sources = set(upstream.values())
        return next(iter(sources)) if len(sources) == 1 else None

    def readers(self, source_uid: str) -> list[ModelNode]:
        """Models reading a source directly or through a pass-through, by unique id."""
        return self._readers.get(source_uid, [])

    def target_tree(self, model: ModelNode, source_uid: str) -> tuple[exp.Expr, bool] | None:
        """The model's compiled SQL with every relation that stands for the source (the
        source itself, or a pass-through of it) swapped for the placeholder, and whether
        there was one. None when there is no parsed compiled SQL for the model."""
        tree = self.tree(model.unique_id)
        if tree is None:
            return None
        stands_for = {d for d, s in self.upstream_sources(model).items() if s == source_uid}
        return with_target(tree, lambda t: self.node_for(t) in stands_for)
