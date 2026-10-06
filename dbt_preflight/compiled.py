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


def parse_compiled(sql: str) -> exp.Expr | None:
    """Parse compiled SQL: DuckDB first, since dbt rendered it for a DuckDB target and quotes
    relations with double quotes; the default dialect as a fallback for what DuckDB's
    grammar in sqlglot rejects. None when neither parses it."""
    for dialect in ("duckdb", None):
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: BLE001 - any parser failure just means "cannot infer"
            continue
        if tree is not None:
            return tree
    return None


@dataclass
class CompiledSql:
    """The compiled SQL of the models preflight compiled, and every node's relation."""

    code: dict[str, str] = field(default_factory=dict)  # model unique_id -> compiled SQL
    relations: dict[str, RelationKey] = field(default_factory=dict)  # source/model uid -> key
    failed: list[str] = field(default_factory=list)  # models dbt could not compile

    @classmethod
    def from_dict(cls, raw: dict[str, Any], failed: list[str] | None = None) -> CompiledSql:
        out = cls(failed=sorted(failed or []))
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
    def load(cls, manifest_path: Path, failed: list[str] | None = None) -> CompiledSql:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls.from_dict(raw, failed)


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


def _is_star_select(tree: exp.Expr, upstream: RelationKey | None) -> bool:
    """`select * from <upstream>`, optionally with a `where`, and nothing else."""
    if not isinstance(tree, exp.Select) or upstream is None:
        return False
    for arg in ("with_", "joins", "group", "having", "qualify", "order", "distinct", "laterals"):
        if tree.args.get(arg):
            return False
    if len(tree.expressions) != 1 or not isinstance(tree.expressions[0], exp.Star):
        return False
    from_ = tree.args.get("from_")
    table = from_.this if from_ is not None else None
    return isinstance(table, exp.Table) and table_key(table) == upstream


def _is_empty_stand_in(tree: exp.Expr) -> bool:
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


def _raw_is_star_select(raw_code: str) -> bool:
    """The raw-SQL spelling of a pass-through: `select * from {{ source(...) }}` with at most
    a `where` and a `config()` call. Anything else in Jinja and it is not one."""
    sql = _RAW_CONFIG_RE.sub("", raw_code)
    sql = _RAW_COMMENT_RE.sub("", sql)
    sql, n = _RAW_CALL_RE.subn(TARGET_PLACEHOLDER, sql)
    if n != 1 or "{{" in sql or "{%" in sql:
        return False
    try:
        tree = sqlglot.parse_one(sql, read=None)
    except Exception:  # noqa: BLE001
        return False
    return tree is not None and _is_star_select(tree, (TARGET_PLACEHOLDER.lower(),))


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


def with_target(tree: exp.Expr, keys: set[RelationKey]) -> tuple[exp.Expr, bool]:
    """A copy of `tree` with every reference to one of `keys` replaced by the placeholder
    the column walk in `schema.py` looks for. A reference with no alias keeps its own
    table name as one, so `customer.id` still resolves. Also whether any was found."""
    tree = tree.copy()
    found = False
    for table in list(tree.find_all(exp.Table)):
        if table_key(table) not in keys:
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
        self.pass_through: dict[str, str] = {}
        for uid in sorted(manifest.models):
            src = self._pass_through_source(uid, frozenset())
            if src is not None:
                self.pass_through[uid] = src

    def tree(self, uid: str) -> exp.Expr | None:
        if uid not in self._trees:
            code = self.compiled.code.get(uid)
            self._trees[uid] = parse_compiled(code) if code else None
        return self._trees[uid]

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
        if tree is not None:
            ok = _is_star_select(tree, self.compiled.relations.get(upstream)) or (
                upstream in self.manifest.sources and _is_empty_stand_in(tree)
            )
        else:
            ok = upstream in self.manifest.sources and _raw_is_star_select(model.raw_code)
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
        return [
            self.manifest.models[uid]
            for uid in sorted(self.manifest.models)
            if source_uid in self.upstream_sources(self.manifest.models[uid]).values()
        ]

    def relation_keys(self, model: ModelNode, source_uid: str) -> set[RelationKey]:
        """The relations in a model's compiled SQL that stand for one source."""
        return {
            key
            for dep, src in self.upstream_sources(model).items()
            if src == source_uid and (key := self.compiled.relations.get(dep)) is not None
        }

    def every_reader_compiled(self, source_uid: str) -> bool:
        """Whether the compiled SQL of every model reading a source, directly or through a
        pass-through, is in hand - vacuously true for a source nothing reads. Then a column
        neither the raw nor the compiled SQL mentions is one no model reads."""
        readers = {m.unique_id for m in self.readers(source_uid)}
        return all(uid in self.compiled.code for uid in readers)
