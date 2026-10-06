"""Where the source schema comes from.

Three routes to the same thing, a parsed DBML model that model2data can generate data for:

1. A DBML file the repo already keeps (the reference architecture does). Best case: the
   file carries note hints that shape the data like a business.
2. Derived from the project's own `sources.yml`, when every source column declares a
   `data_type`.
3. Inferred from the staging models that read a source, for the columns `sources.yml`
   leaves untyped or undeclared: real projects (jaffle-shop, for one) declare sources with
   no columns at all, and the staging model that does `select id as customer_id, ... from
   {{ source(...) }}` already names every column it needs. A column no model reads, and no
   YAML types, is reported rather than guessed, because a fixture with the wrong type is
   worse than no fixture.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from model2data.parse.dbml import TableDef, parse_dbml
from model2data.utils import normalize_identifier
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from dbt_preflight.compiled import (
    TARGET_PLACEHOLDER,
    CompiledSql,
    CompiledView,
    is_empty_stand_in,
    null_casts,
)
from dbt_preflight.manifest import Manifest, ModelNode, SourceColumn, SourceTable, TestNode

# dbt / warehouse type names -> the DBML types model2data understands.
_TYPE_ALIASES = {
    "string": "varchar",
    "str": "varchar",
    "text": "varchar",
    "char": "varchar",
    "character varying": "varchar",
    "int64": "int",
    "int32": "int",
    "integer": "int",
    "bigint": "int",
    "smallint": "int",
    "tinyint": "int",
    "number": "int",
    "float64": "float",
    "double": "float",
    "double precision": "float",
    "real": "float",
    "numeric": "decimal",
    "bignumeric": "decimal",
    "bool": "boolean",
    "datetime": "timestamp",
    "timestamp_ntz": "timestamp",
    "timestamp_tz": "timestamp",
    "timestamptz": "timestamp",
    "timestamp with time zone": "timestamp",
    # sqlglot's own names for DuckDB's timestamps, as compiled SQL renders them.
    "timestampntz": "timestamp",
    "timestampltz": "timestamp",
}


class SchemaError(ValueError):
    """The schema cannot be resolved well enough to generate data from."""


@dataclass
class InferredSource:
    """One source table whose columns came from the models that read it, not sources.yml."""

    source_name: str
    table: str
    identifier: str
    models: list[str]  # staging models the columns were read from
    total_columns: int
    guessed_columns: list[str]  # columns typed by name heuristic, not an explicit cast
    # Columns only the compiled SQL accounted for, or only it typed (see `compiled.py`).
    compiled_columns: list[str] = field(default_factory=list)
    # "`col`: int in the raw SQL, varchar compiled": the raw SQL's type was kept.
    type_conflicts: list[str] = field(default_factory=list)
    # Typed varchar because a model reading the source could not be followed in full, so
    # nothing could say what it reads (a subset of guessed_columns).
    unknown_columns: list[str] = field(default_factory=list)
    # Guessed column -> unique ids of what reads it (for an unknown column, every reader
    # that could not be followed). A failure there may be preflight's guess, not the
    # project's (`cli._guess_bound`).
    guessed_readers: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ResolvedSchema:
    tables: dict[str, TableDef]
    refs: list[dict]
    dbml_path: Path
    derived: bool
    inferred: list[InferredSource] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # sources nothing reads: no fixture


def _dbml_type(data_type: str) -> str:
    base = re.sub(r"\(.*\)$", "", data_type.strip().lower()).strip()
    return _TYPE_ALIASES.get(base, base)


def _enum_name(table: str, column: str) -> str:
    """A DBML enum name for one column's accepted values, unique within the derived file.

    Qualified by table, because two tables can each have a `status` with different values,
    and DBML resolves a column's type by name across the whole document."""
    return re.sub(r"[^a-z0-9_]", "_", f"{table}__{column}".lower())


_DBML_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def source_table_names(sources: Iterable[SourceTable]) -> dict[str, str]:
    """{source unique_id: the table name its DBML table is written under}.

    The identifier alone when it is unique among the sources; `<source>__<identifier>` when
    two sources declare the same table name (`ga_traffic_org.report` and
    `ga_traffic_com.report`), because DBML tables share one namespace and model2data would
    otherwise refuse the file. `fixtures.build_fixtures` uses the same mapping to load each
    generated table under the schema and identifier dbt expects for that source."""
    srcs = list(sources)
    counts: dict[str, int] = {}
    for src in srcs:
        key = normalize_identifier(src.identifier)
        counts[key] = counts.get(key, 0) + 1
    # model2data compares names through `normalize_identifier`, so uniqueness is checked there.
    taken = {key for key, n in counts.items() if n == 1}
    names: dict[str, str] = {}
    for src in srcs:
        plain = counts[normalize_identifier(src.identifier)] == 1
        if plain and _DBML_NAME_RE.fullmatch(src.identifier):
            names[src.unique_id] = src.identifier
            continue
        # A wildcard table (GA4's `events_*`) or another name DBML cannot spell gets one it
        # can; the fixture still loads under the identifier dbt expects.
        base = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            src.identifier if plain else f"{src.source_name}__{src.identifier}",
        )
        if not re.match(r"[A-Za-z_]", base):
            base = f"t_{base}"
        if plain:
            taken.discard(normalize_identifier(src.identifier))
        name, n = base, 2
        while normalize_identifier(name) in taken:
            name, n = f"{base}_{n}", n + 1
        taken.add(normalize_identifier(name))
        names[src.unique_id] = name
    return names


def _ref_target(
    test: TestNode, sources: dict[str, SourceTable], names: dict[str, str]
) -> tuple[str, str] | None:
    """For a relationships test on a source column, the (table, column) it points at.

    Only source-to-source relationships become DBML refs. A relationship to a `ref()`
    points at a model, which does not exist in the source system.
    """
    to = str(test.kwargs.get("to", ""))
    field_ = test.kwargs.get("field")
    m = re.match(r"""source\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""", to)
    if not m or not field_:
        return None
    for src in sources.values():
        if src.source_name == m.group(1) and src.name == m.group(2):
            return names[src.unique_id], str(field_)
    return None


# Jinja calls a staging model's SQL is expected to contain. Replaced with plain
# identifiers before parsing, since sqlglot does not know Jinja.
_SOURCE_CALL_RE = re.compile(
    r"""\{\{\s*source\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)\s*\}\}"""
)
_REF_CALL_RE = re.compile(
    r"""\{\{\s*ref\(\s*['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?\s*\)\s*\}\}"""
)
_CONFIG_CALL_RE = re.compile(r"\{\{\s*config\(.*?\)\s*\}\}", re.DOTALL)
_BLOCK_OR_COMMENT_RE = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_QUOTED_IDENTIFIER_RE = re.compile(r"'([a-zA-Z_][a-zA-Z0-9_]*)'")
# date_trunc('day', ...), datediff(..., 'hour', ...): the date/time part, not a column.
_DATE_PART_WORDS = {
    "day",
    "days",
    "hour",
    "hours",
    "minute",
    "minutes",
    "second",
    "seconds",
    "week",
    "weeks",
    "month",
    "months",
    "quarter",
    "quarters",
    "year",
    "years",
}

_TARGET_PLACEHOLDER = TARGET_PLACEHOLDER

# Name -> guessed DBML type, checked in order; the first match wins.
_TIMESTAMP_SUFFIXES = ("_at", "_timestamp", "_datetime")
_DATE_SUFFIXES = ("_date", "_on")
_BOOLEAN_PREFIXES = ("is_", "has_")
_BOOLEAN_SUFFIXES = ("_flag", "_enabled", "_active")
_BOOLEAN_NAMES = {"enabled", "active"}
# Checked as a whole underscore-separated token, not a bare substring, so "package"
# doesn't become an integer just because it ends in "age".
_INT_WHOLE_WORDS = {
    "count",
    "number",
    "num",
    "qty",
    "quantity",
    "units",
    "age",
    "year",
    "month",
    "day",
}
_INT_SUFFIXES = ("_id", "_cents")
# Checked as a plain substring: real columns pair these with a noun ("tax_paid",
# "unit_price"), so a bare match is specific enough without a word-boundary rule.
_DECIMAL_WORDS = (
    "paid",
    "cost",
    "tax",
    "fee",
    "discount",
    "revenue",
    "subtotal",
    "balance",
    "margin",
    "weight",
    "score",
    "amount",
    "price",
    "total",
    "rate",
)
# Common attribute names that would otherwise trip a decimal/int word above (or a
# foreign-key name match) by coincidence; kept as varchar outright.
_VARCHAR_NAMES = {
    "email",
    "phone",
    "name",
    "city",
    "country",
    "address",
    "sku",
    "description",
    "status",
    "type",
    "category",
}
_SOURCE_TABLE_PREFIXES = ("raw_", "stg_", "src_", "source_")


def _int_name_signal(name: str) -> bool:
    tokens = set(name.split("_"))
    return bool(tokens & _INT_WHOLE_WORDS) or name.endswith(_INT_SUFFIXES)


def _decimal_name_signal(name: str) -> bool:
    return any(word in name for word in _DECIMAL_WORDS)


def _guess_type_by_name(name: str) -> str:
    """A DBML type guessed from a column's name alone, house-convention style."""
    n = name.lower()
    if n in _VARCHAR_NAMES:
        return "varchar"
    if n in ("timestamp", "datetime") or n.endswith(_TIMESTAMP_SUFFIXES):
        return "timestamp"
    if n == "date" or n.endswith(_DATE_SUFFIXES):
        return "date"
    if n == "id" or n.endswith("_id"):
        return "int"
    if n.startswith(_BOOLEAN_PREFIXES) or n.endswith(_BOOLEAN_SUFFIXES) or n in _BOOLEAN_NAMES:
        return "boolean"
    if _int_name_signal(n):
        return "int"
    if _decimal_name_signal(n):
        return "decimal"
    return "varchar"


def _table_basename(table_name: str) -> str:
    """A source table's name with a conventional loader prefix stripped.

    `raw_customers` reads as `customers` when matching a column name against it -
    real projects almost always prefix their raw tables this way.
    """
    n = table_name.lower()
    for prefix in _SOURCE_TABLE_PREFIXES:
        if n.startswith(prefix) and len(n) > len(prefix):
            return n[len(prefix) :]
    return n


def _pluralize(word: str) -> str:
    if not word:
        return word
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


_TEMPORAL_NAMES = {
    "date",
    "time",
    "timestamp",
    "datetime",
    "dt",
    "ts",
    "day",
    "week",
    "month",
    "quarter",
    "year",
    "hour",
    "minute",
    "second",
}


def _is_temporal_name(name: str) -> bool:
    """A date/time-looking column name, which is never a key into another table."""
    n = name.lower()
    return (
        n in _TEMPORAL_NAMES
        or n.endswith(_TIMESTAMP_SUFFIXES)
        or n.endswith(_DATE_SUFFIXES)
        or n.endswith(("_time", "_ts", "_dt"))
    )


def _fk_ref_target(
    name: str,
    own: SourceTable,
    all_sources: list[SourceTable],
    id_tables: set[str],
) -> SourceTable | None:
    """The other source table this column's name points at, as a foreign key -
    `customer_id`, or a bare `customer` when a `raw_customers` source exists - so the
    fixtures can keep referential integrity between them.

    An `_id` suffix alone is enough to type a column as an integer (handled in
    `_guess_type_by_name`); this only adds the `ref:` when a matching table can
    actually be found, so an unmatched `_id` column stays an ordinary integer. A target
    must have an `id` column (`id_tables`, by unique_id) because the ref points at it, and
    a date/time-looking name (`date` next to a `dates` source) is never a key. A table in
    the same dbt source as the column wins over a same-named one in another source.
    """
    n = name.lower()
    if n == "id" or _is_temporal_name(n):
        return None
    base = n[: -len("_id")] if n.endswith("_id") else n
    if not base:
        return None
    plural = _pluralize(base)
    candidates = [
        s for s in all_sources if s.unique_id != own.unique_id and s.unique_id in id_tables
    ]
    candidates.sort(key=lambda s: s.source_name != own.source_name)
    for src in candidates:
        basename = _table_basename(src.name)
        if base in (basename, src.name.lower()) or plural in (basename, src.name.lower()):
            return src
    return None


_ARITH_OPS = (exp.Div, exp.Mul, exp.Add, exp.Sub)
_COMPARISON_OPS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_NUMERIC_FUNCS = (exp.Sum, exp.Avg, exp.Round)
_STRING_FUNCS = (exp.Lower, exp.Upper, exp.Trim, exp.Concat)


def _column_hint(col: exp.Column) -> str | None:
    """ "numeric", "varchar" or "boolean" if how this column occurrence is used in the SQL signals
    a type, independent of its name; the strongest signal short of an explicit cast.

    An operand of `/`, `*`, `+`, `-` against a numeric literal, or wrapped in `sum(`,
    `avg(`, `round(`, reads as numeric. Compared to a string literal, or passed to
    `lower(`/`upper(`/`trim(`/`concat(`, reads as varchar.
    """
    parent = col.parent
    # `where opportunity.iswon`, `and not x`: a bare predicate is a boolean.
    if isinstance(parent, (exp.Where, exp.Not, exp.And, exp.Or)):
        return "boolean"
    if isinstance(parent, _ARITH_OPS):
        sibling = parent.expression if parent.this is col else parent.this
        if isinstance(sibling, exp.Literal) and sibling.is_number:
            return "numeric"
    if isinstance(parent, _COMPARISON_OPS):
        sibling = parent.expression if parent.this is col else parent.this
        if isinstance(sibling, exp.Literal) and sibling.is_string:
            return "varchar"
    node = parent
    while node is not None and not isinstance(node, exp.Select):
        if isinstance(node, _NUMERIC_FUNCS):
            return "numeric"
        if isinstance(node, _STRING_FUNCS):
            return "varchar"
        node = node.parent
    return None


def _resolve_type_and_ref(
    name: str,
    cast_type: str | None,
    hint: str | None,
    src: SourceTable,
    all_sources: list[SourceTable],
    id_tables: set[str],
) -> tuple[str, SourceTable | None]:
    """The DBML type for an inferred source column, and a foreign-key ref if its name
    points at another source table.

    Priority, strongest first: an explicit cast; how the column is used in the SQL
    (`_column_hint`); a foreign-key-shaped name (always an integer); the rest of the
    name heuristics in `_guess_type_by_name`.
    """
    if cast_type:
        return _dbml_type(cast_type), None

    fk_target = _fk_ref_target(name, src, all_sources, id_tables)
    is_id_suffix = name.lower() != "id" and name.lower().endswith("_id")

    if hint == "numeric":
        return ("int" if _int_name_signal(name.lower()) else "decimal"), fk_target
    if hint == "varchar":
        return "varchar", None
    if hint == "boolean":
        return "boolean", None

    if is_id_suffix or fk_target is not None:
        return "int", fk_target

    return _guess_type_by_name(name), None


def _macro_arg_columns(jinja_expr: str) -> set[str]:
    """Column-shaped string-literal arguments inside a Jinja macro call.

    A macro wrapping a single column - `{{ dbt.date_trunc('day', 'ordered_at') }}` - is
    common enough in staging models that dropping the whole call would lose a column that
    appears nowhere else in the query. A quoted identifier that is not a date/time part is
    read as the column name the macro was given; nothing here overrides an explicit cast.
    """
    return {
        m.lower()
        for m in _QUOTED_IDENTIFIER_RE.findall(jinja_expr)
        if m.lower() not in _DATE_PART_WORDS
    }


def _model_source_columns(
    raw_code: str, source_name: str, table_name: str
) -> dict[str, tuple[str | None, str | None]] | None:
    """{column_name: (explicit cast type, usage hint)} a model reads from one source table.

    Both are `None` with no evidence either way. `{{ source(...) }}` and `{{ ref(...) }}`
    calls are swapped for plain identifiers so sqlglot can parse the compiled-looking SQL,
    then every `exp.Column` in every select scope of the query is considered: a staging
    model's `renamed` CTE reads straight off the `source` CTE without qualifying columns, so
    this is a query-wide walk, not a single-clause one. A column counts only when it is
    qualified by this source's alias, or is unqualified in a scope that reads this source and
    nothing else (`_column_is_from_target`); an unqualified column in a scope that joins
    several sources is ambiguous and is left unattributed. Returns None when the model does not reference this source at
    all, or the SQL does not parse - normal for a model this house style would flag as not
    staging.
    """
    counter = 0
    found = False

    def _sub_source(m: re.Match[str]) -> str:
        nonlocal counter, found
        if (m.group(1), m.group(2)) == (source_name, table_name):
            found = True
            return _TARGET_PLACEHOLDER
        counter += 1
        return f"__preflight_src_{counter}__"

    def _sub_ref(_m: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return f"__preflight_ref_{counter}__"

    sql = _SOURCE_CALL_RE.sub(_sub_source, raw_code)
    sql = _REF_CALL_RE.sub(_sub_ref, sql)
    if not found:
        return None
    # `{{ config(...) }}` is always its own statement, safe to drop outright. A block or
    # comment is dropped too - a best effort, since a {% for %} loop over columns cannot be
    # reconstructed without running it. Anything else - `{{ some_macro(...) }}` used as a
    # select expression, like dbt's own `dbt.date_trunc(...)` - becomes a placeholder value,
    # since dropping it would leave `, as alias,` where an expression has to be.
    sql = _CONFIG_CALL_RE.sub("", sql)
    sql = _BLOCK_OR_COMMENT_RE.sub("", sql)
    macro_columns: set[str] = set()

    def _sub_expr(m: re.Match[str]) -> str:
        macro_columns.update(_macro_arg_columns(m.group(0)))
        return "__preflight_expr__"

    sql = _JINJA_EXPR_RE.sub(_sub_expr, sql)

    try:
        tree = sqlglot.parse_one(sql, read=None)
    except Exception:  # noqa: BLE001 - any parser failure just means "cannot infer"
        return None
    if tree is None:
        return None

    columns = _columns_in_tree(tree, table_name)
    # A macro's string argument names a column but not which table it belongs to, so it is
    # only trusted when the model reads nothing but this source.
    if not any(
        t.name.startswith("__preflight_") and t.name != _TARGET_PLACEHOLDER
        for t in tree.find_all(exp.Table)
    ):
        for name in macro_columns:
            columns.setdefault(name, (None, None))
    return columns


def _columns_in_tree(tree: exp.Expr, table_name: str) -> dict[str, tuple[str | None, str | None]]:
    """{column_name: (explicit cast type, usage hint)} read off the source the placeholder
    stands for, in SQL already parsed: the walk `_model_source_columns` describes, shared
    with the compiled-SQL route (`compiled.with_target` puts the placeholder in)."""
    columns: dict[str, tuple[str | None, str | None]] = {}
    for scope in traverse_scope(tree):
        for col in scope.columns:
            name = col.name.lower()
            if not name or name.startswith("__preflight_") or not _DBML_NAME_RE.fullmatch(name):
                continue
            if not _column_is_from_target(col, scope, table_name):
                continue
            cast_type, hint = columns.get(name, (None, None))
            parent = col.parent
            if isinstance(parent, exp.Cast) and parent.this is col and cast_type is None:
                cast_type = parent.to.sql(dialect=None)
            if hint is None:
                hint = _column_hint(col)
            columns[name] = (cast_type, hint)
    return columns


def _leaf_tables(source: object, seen: frozenset[int] = frozenset()) -> set[str] | None:
    """The names of the real tables a scope source ultimately reads from: the table itself,
    or, for a CTE / derived table, every table underneath it. None when it cannot be told.

    Underneath means what its FROM and JOINs select (or each branch of a union), not every
    CTE it could see: `d as (select 1 as id)` defined after a CTE over the source does not
    read the source."""
    if isinstance(source, exp.Table):
        return {source.name}
    if not isinstance(source, Scope) or id(source) in seen:
        return None
    out: set[str] = set()
    inners = list(source.union_scopes) or [src for _n, src in source.selected_sources.values()]
    for inner in inners:
        leaves = _leaf_tables(inner, seen | {id(source)})
        if leaves is None:
            return None
        out |= leaves
    return out


def _column_is_from_target(col: exp.Column, scope: Scope, table_name: str) -> bool:
    """Whether one column reference in `scope` belongs to the source table being inferred.

    A qualified column belongs to the source its qualifier names. An unqualified one is only
    attributed when the scope reads exactly one source, since otherwise it could be any of
    them - and giving it to all of them produces ambiguous references downstream. Either
    way the source has to resolve to the target table alone (a CTE over two tables does
    not), and the name must not be one the CTE defines itself (`id as customer_id`).
    """
    sources = {k.lower(): v for k, v in scope.sources.items()}
    # `from {{ source('s', 'licenses') }}` with no alias is qualified by the table's own name,
    # which the placeholder hides.
    if _TARGET_PLACEHOLDER in sources:
        sources.setdefault(table_name.lower(), sources[_TARGET_PLACEHOLDER])
    if col.table:
        source = sources.get(col.table.lower())
    elif len(scope.sources) == 1:
        source = next(iter(scope.sources.values()))
    else:
        return False
    if source is None or _leaf_tables(source) != {_TARGET_PLACEHOLDER}:
        return False
    name = col.name.lower()
    if isinstance(source, Scope):
        body = source.expression
        selects = body.named_selects if isinstance(body, exp.Query) else []
        defined = {n.lower() for n in selects if n != "*"}
        if name in defined:
            return False
    # An alias minted in this same select and used in a clause (`... as total` then
    # `where total > 0`) is not a source column; one read inside the select list is.
    select = scope.expression
    if isinstance(select, exp.Select) and not col.table:
        projections = {id(e) for e in select.expressions}
        in_select_list = False
        node: exp.Expr | None = col
        while node is not None and node is not select:
            if id(node) in projections:
                in_select_list = True
                break
            node = node.parent
        if not in_select_list:
            for e in select.expressions:
                if isinstance(e, exp.Alias) and e.alias.lower() == name:
                    return False
    return True


def _parse_staging_sql(raw_code: str) -> exp.Expr | None:
    """Parse a staging model's SQL well enough to read its own select-list aliases.

    Jinja is swapped for plain placeholders the same way `_model_source_columns` does,
    but this version does not care which source a `source()`/`ref()` call points at -
    every one becomes an anonymous placeholder, since all that matters here is that the
    surrounding SQL parses.
    """
    counter = 0

    def _sub_call(_m: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return f"__preflight_src_{counter}__"

    sql = _SOURCE_CALL_RE.sub(_sub_call, raw_code)
    sql = _REF_CALL_RE.sub(_sub_call, sql)
    sql = _CONFIG_CALL_RE.sub("", sql)
    sql = _BLOCK_OR_COMMENT_RE.sub("", sql)
    sql = _JINJA_EXPR_RE.sub("__preflight_expr__", sql)
    try:
        tree = sqlglot.parse_one(sql, read=None)
    except Exception:  # noqa: BLE001 - any parser failure just means "cannot infer"
        return None
    return tree


# Wrappers a `unique`/`not_null` test can be carried back through, because they preserve
# both identity and nullness: `cast(x as t)` is null exactly when `x` is, and two rows
# differ after the cast only if they differed before it. That pair is the whole
# requirement, and it is why the list is this short. `lower(email)` preserves nullness but
# not identity; `coalesce(x, 0)` preserves identity but turns a nullable column non-null,
# so carrying a `not_null` back through it would assert something about the source that
# the staging test never checked. Neither belongs here. `exp.TryCast` subclasses
# `exp.Cast`, so both spellings and `x::t` are covered.
_NULL_PRESERVING_WRAPPERS = (exp.Cast,)


def _unwrap_null_preserving(e: exp.Expression) -> exp.Expression:
    """`cast(cast(x as int) as varchar)` -> the `x` column reference underneath."""
    while isinstance(e, _NULL_PRESERVING_WRAPPERS):
        e = e.this
    return e


def _model_alias_map(raw_code: str) -> dict[str, str]:
    """{a staging model's own output column name: the source column it came from}.

    `id as customer_id` -> `{"customer_id": "id"}`; a bare, unaliased column -
    `order_id` - maps to itself, since it is its own alias. A cast around the column
    counts too (`cast(site_tag as string) as site_tag`), because a staging layer over a
    schemaless loader is where types get pinned, and such a project casts every column it
    selects - without this it would carry nothing at all. Only the wrappers in
    `_NULL_PRESERVING_WRAPPERS` are seen through: `lower(email) as email` is a transform,
    not a column a not_null/unique test's name can be carried straight back through, so it
    is left out - the test would have to attach to the source column itself for that.
    """
    tree = _parse_staging_sql(raw_code)
    return _alias_map_of_tree(tree) if tree is not None else {}


def _alias_map_of_tree(tree: exp.Expr) -> dict[str, str]:
    """`_model_alias_map` on SQL already parsed, raw or compiled."""
    aliases: dict[str, str] = {}
    for select in tree.find_all(exp.Select):
        for e in select.expressions:
            if isinstance(e, exp.Alias):
                inner = _unwrap_null_preserving(e.this)
                if isinstance(inner, exp.Column):
                    aliases.setdefault(e.alias_or_name.lower(), inner.name.lower())
            elif isinstance(e, exp.Column):
                aliases.setdefault(e.name.lower(), e.name.lower())
    return aliases


def _accepted_values(test: TestNode) -> list[str]:
    """The `values:` an `accepted_values` test declares, as strings, in declared order.

    A test with no usable values - an empty list, or one carrying something that is not a
    scalar - yields nothing, and the column is generated as it would have been."""
    raw = test.kwargs.get("values")
    if not isinstance(raw, list) or not raw:
        return []
    out: list[str] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return []
        text = str(value)
        # The value has to survive being written into a DBML enum block and read back.
        if not text or any(ch in text for ch in "\"'{}\n\r"):
            return []
        out.append(text)
    return out


def _carried_tests_for_source(
    source: SourceTable, manifest: Manifest, view: CompiledView | None = None
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """{source_column_name: {test names}}, carried back from the `unique`/`not_null`
    tests a staging model declares on the alias it gave one of this source's columns.

    A source column named `id` is already the primary key regardless; this is what
    lets another column - `sku`, `order_id`, whatever the project actually keys its
    source rows by - carry the same settings, so the fixtures satisfy tests the
    project's own YAML already documents instead of leaving them to chance.

    A test is a claim about the model's grain, and carried back from the wrong model it
    hands the fixtures a key the source does not have - which makes a fan-out pass. So a
    test only carries back from a model that reads this one source and nothing else and
    keeps its rows (`_keeps_source_rows`), whether the alias comes from its raw SQL or its
    compiled SQL. The compiled SQL adds aliases to the raw SQL's, never in place of one,
    and only through a column that resolves to the source in its own scope
    (`_compiled_alias_map`).
    """
    carried: dict[str, set[str]] = {}
    carried_values: dict[str, list[str]] = {}
    for uid, model in manifest.models.items():
        if not _keeps_source_rows(model, source, view):
            continue
        direct = source.unique_id in model.depends_on
        alias_map = _model_alias_map(model.raw_code) if direct else {}
        if view is not None:
            for alias, column in _compiled_alias_map(view, model, source).items():
                alias_map.setdefault(alias, column)
        if not alias_map:
            continue
        for test in manifest.tests_for_model(uid):
            if not test.column_name:
                continue
            source_col = alias_map.get(test.column_name.lower())
            if source_col is None:
                continue
            if test.test_name in {"unique", "not_null"}:
                carried.setdefault(source_col, set()).add(test.test_name)
            elif test.test_name == "accepted_values":
                values = _accepted_values(test)
                if values:
                    carried_values.setdefault(source_col, values)
    return carried, carried_values


# What can change a query's grain or drop rows: grouping, distinct, aggregates, set
# operations, joins, filters, sampling, paging, and anything that turns one row into many
# (`unnest`, `explode`, `generate_series`, pivots). A model with any of these does not have
# its source's rows.
_GRAIN_CHANGES = (
    exp.Group, exp.Distinct, exp.AggFunc, exp.SetOperation, exp.Join, exp.Where,
    exp.Having, exp.Qualify, exp.Limit, exp.Offset, exp.Fetch, exp.TableSample,
    exp.Unnest, exp.Explode, exp.Inline, exp.GenerateSeries, exp.Lateral, exp.Pivot,
)  # fmt: skip


def _changes_grain(tree: exp.Expr) -> bool:
    """Whether anything in `_GRAIN_CHANGES` occurs - outside an empty stand-in, whose
    `where false` or `limit 0` is a source that did not exist at compile time, not a
    filter the model applies."""
    for node in tree.find_all(*_GRAIN_CHANGES):
        select = node if isinstance(node, exp.Select) else node.find_ancestor(exp.Select)
        if select is None or not is_empty_stand_in(select):
            return True
    return False


def _keeps_source_rows(model: ModelNode, source: SourceTable, view: CompiledView | None) -> bool:
    """Whether a model has exactly its source's rows, so a `unique`/`not_null` test on it
    says something about the source: it reads that one source and nothing else (directly,
    or with compiled SQL through unfiltered pass-throughs), and neither its raw SQL nor its
    compiled SQL changes the grain (`_GRAIN_CHANGES`). SQL that does not parse cannot
    carry anything back anyway."""
    if view is not None:
        if view.single_source(model) != source.unique_id:
            return False
        if any(dep in view.filtered for dep in model.depends_on):
            return False
        tree = view.tree(model.unique_id)
        if tree is not None and _changes_grain(tree):
            return False
    elif set(model.depends_on) != {source.unique_id}:
        return False
    if source.unique_id in model.depends_on:
        raw_tree = _parse_staging_sql(model.raw_code)
        if raw_tree is not None and _changes_grain(raw_tree):
            return False
    return True


def _compiled_alias_map(
    view: CompiledView, model: ModelNode, source: SourceTable
) -> dict[str, str]:
    """{output alias: source column} from a model's compiled SQL, for test carry-back.

    Only from a model that reads this source and nothing else, directly or through a
    pass-through, and has the source's rows exactly (`_GRAIN_CHANGES`): a mart over a
    `select *` staging model and another table, or one that groups, tests its own grain, not
    the source's. And only where the aliased column resolves to the source in its own scope
    (`_column_is_from_target`), not by its bare name.
    """
    if view.single_source(model) != source.unique_id:
        return {}
    found = view.target_tree(model, source.unique_id)
    if found is None or not found[1]:
        return {}
    tree = found[0]
    if _changes_grain(tree):
        return {}
    aliases: dict[str, str] = {}
    for scope in traverse_scope(tree):
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        for e in select.expressions:
            alias = e.alias_or_name.lower() if isinstance(e, (exp.Alias, exp.Column)) else ""
            inner = _unwrap_null_preserving(e.this) if isinstance(e, exp.Alias) else e
            if not alias or not isinstance(inner, exp.Column) or isinstance(inner.this, exp.Star):
                continue
            if _column_is_from_target(inner, scope, source.name) or _is_typed_null_column(
                inner, scope
            ):
                aliases.setdefault(alias, inner.name.lower())
    return aliases


def _is_typed_null_column(col: exp.Column, scope: Scope) -> bool:
    """Whether a column is read from a CTE or subquery that defines it as a typed null of
    the same name (`cast(null as T) as customer_id`). In a model that reads one source
    and nothing else, that is the package's placeholder for the source column - Fivetran's
    `fill_staging_columns` selects the real column there once the source exists - so it
    resolves to that source column, exactly as the typed null types it."""
    selected = {name: src for name, (_node, src) in scope.selected_sources.items()}
    if col.table:
        source = selected.get(col.table)
    elif len(selected) == 1:
        source = next(iter(selected.values()))
    else:
        return False
    if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
        return False
    # It has to stand for this source: a CTE of typed nulls over nothing is not one.
    if _leaf_tables(source) != {_TARGET_PLACEHOLDER}:
        return False
    name = col.name.lower()
    return any(
        isinstance(e, exp.Alias)
        and e.alias_or_name.lower() == name
        and isinstance(e.this, exp.Cast)
        and isinstance(e.this.this, exp.Null)
        for e in source.expression.expressions
    )


def _star_reaches_target(scope: Scope) -> bool:
    """Whether a `*` in this scope's output, followed down through CTEs and subqueries,
    reaches the source - so the model passes on columns no SQL names. A star whose
    qualifier cannot be resolved counts as reaching it."""
    if isinstance(scope.expression, exp.SetOperation):
        return any(_star_reaches_target(s) for s in scope.union_scopes)
    select = scope.expression
    if not isinstance(select, exp.Select):
        return True
    # What the FROM and JOINs select from, not every CTE in sight (`scope.sources`).
    selected = {name: src for name, (_node, src) in scope.selected_sources.items()}
    for e in select.expressions:
        if isinstance(e, exp.Star):
            sources = list(selected.values())
        elif isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
            sources = [selected.get(e.table)] if e.table else list(selected.values())
        else:
            continue
        for src in sources:
            if src is None:
                return True
            if isinstance(src, exp.Table) and src.name == _TARGET_PLACEHOLDER:
                return True
            if isinstance(src, Scope) and _star_reaches_target(src):
                return True
    return False


def _reader_accounted_for(view: CompiledView, model: ModelNode, source: SourceTable) -> bool:
    """Whether every column this model reads from the source is known: its compiled SQL
    parsed, names the source's relation (or a pass-through's), passes no `*` over it on to
    its output, and reads no unqualified column in a scope that joins the source with
    something else. A pass-through hands every column on, so it is its readers that count.
    Anything short of that is "cannot tell"."""
    if model.unique_id in view.pass_through:
        return True
    found = view.target_tree(model, source.unique_id)
    if found is None or not found[1]:
        return False
    scopes = traverse_scope(found[0])
    if not scopes or _star_reaches_target(scopes[-1]):
        return False
    return all(_scope_accounted_for(scope) for scope in scopes)


def _scope_accounted_for(scope: Scope) -> bool:
    """One scope of `_reader_accounted_for`: False when it reads the source in a way the
    column walk cannot follow."""
    selected = {name.lower(): src for name, (_node, src) in scope.selected_sources.items()}
    leaves = {name: _leaf_tables(src) for name, src in selected.items()}
    reads_target = {n for n, lv in leaves.items() if lv is None or _TARGET_PLACEHOLDER in lv}
    if not reads_target:
        return True
    joined = len(selected) > 1
    # A star anywhere in a scope that joins the source with something else - its columns
    # then reach the next scope under a name that is not the source's - or a star inside
    # an expression (`struct_pack(o.*)`), or an unresolvable qualifier.
    projections = (
        {id(e) for e in scope.expression.expressions}
        if isinstance(scope.expression, exp.Select)
        else set()
    )
    stars = [
        node
        for node in scope.walk()
        if isinstance(node, exp.Star)
        and not (isinstance(node.parent, exp.Column) or isinstance(node.parent, exp.Count))
    ] + list(scope.stars)
    for star in stars:
        qualifier = star.table.lower() if isinstance(star, exp.Column) and star.table else None
        over = {qualifier} if qualifier else set(selected)
        if qualifier and qualifier not in selected:
            return False
        if not over & reads_target:
            continue
        if joined or id(star) not in projections:
            return False
    # DuckDB's `columns('re')` selects source columns by pattern.
    if any(True for _ in scope.expression.find_all(exp.Columns)):
        return False
    for col in scope.columns:
        name = col.name.lower()
        # A bare alias passed to a function (`to_json(o)`, `struct_pack(o)`) may be the
        # whole row, every column at once. Elsewhere - `cast(source as varchar)`, a plain
        # select item - DuckDB binds a column of that name first, so it is just a column,
        # and the walk keeps it either way.
        if (
            not col.table
            and name in reads_target
            and isinstance(col.parent, exp.Func)
            and not isinstance(col.parent, exp.Cast)
        ):
            return False
        # An unqualified column next to a join could be the source's.
        if joined and not col.table:
            return False
    return True


@dataclass
class _Inferred:
    """What the models reading one source say about its columns."""

    columns: dict[str, tuple[str | None, str | None]]  # name -> (cast type, usage hint)
    models: list[str]
    # name -> unique ids of the models whose SQL reads that column, raw or compiled.
    readers_by_column: dict[str, set[str]] = field(default_factory=dict)
    compiled_columns: list[str] = field(default_factory=list)
    type_conflicts: list[str] = field(default_factory=list)
    # Columns typed by a compiled `cast(null as T)`: a package's own statement of the column,
    # so a key keeps its foreign-key ref (`_typed_null_type_and_ref`).
    typed_nulls: set[str] = field(default_factory=set)


def _infer_source_columns(source: SourceTable, manifest: Manifest) -> _Inferred:
    """Columns (and any explicit cast type / usage hint) inferred from every model that
    reads a source, from its raw SQL.

    A staging model typically names every column it selects off its source, so this is
    read as the closest thing to a schema a project without one has. Models that do not
    parse, or do not read this source at all, are silently skipped.
    """
    columns: dict[str, tuple[str | None, str | None]] = {}
    used_models: list[str] = []
    readers: dict[str, set[str]] = {}
    for model in manifest.models.values():
        if source.unique_id not in model.depends_on:
            continue
        found = _model_source_columns(model.raw_code, source.source_name, source.name)
        if found is None:
            continue
        used_models.append(model.name)
        for name, (cast_type, hint) in found.items():
            existing_cast, existing_hint = columns.get(name, (None, None))
            columns[name] = (existing_cast or cast_type, existing_hint or hint)
            readers.setdefault(name, set()).add(model.unique_id)
    return _Inferred(columns, sorted(used_models), readers_by_column=readers)


def _compiled_source_columns(
    source: SourceTable, view: CompiledView, readers: dict[str, set[str]] | None = None
) -> tuple[dict[str, tuple[str | None, str | None]], dict[str, str], set[str]]:
    """Columns the compiled SQL of a source's readers accounts for, and which model each
    came from first.

    Two kinds of evidence. Columns read off the source's relation, or a pass-through's,
    found by the same walk the raw SQL gets. And `cast(null as T) as col`, read as "the
    model expects column `col` of type `T`" - but only in a model whose every dependency is
    this one source, directly or through a pass-through: with two sources upstream, a
    typed null could stand for either.

    Returns the columns, the model each came from first, and the names whose type is a
    typed null's.
    """
    columns: dict[str, tuple[str | None, str | None]] = {}
    origin: dict[str, str] = {}
    typed_nulls: set[str] = set()
    for model in view.readers(source.unique_id):
        tree = view.tree(model.unique_id)
        target = view.target_tree(model, source.unique_id)
        if tree is None or target is None:
            continue
        found: dict[str, tuple[str | None, str | None]] = {}
        if target[1]:
            found = _columns_in_tree(target[0], source.name)
        nulls: set[str] = set()
        if view.single_source(model) == source.unique_id:
            for name, cast_type in null_casts(tree).items():
                existing_cast, hint = found.get(name, (None, None))
                found[name] = (existing_cast or cast_type, hint)
                if existing_cast is None:
                    nulls.add(name)
        for name, (cast_type, hint) in sorted(found.items()):
            existing_cast, existing_hint = columns.get(name, (None, None))
            columns[name] = (existing_cast or cast_type, existing_hint or hint)
            origin.setdefault(name, model.name)
            if existing_cast is None and name in nulls:
                typed_nulls.add(name)
            if readers is not None:
                readers.setdefault(name, set()).add(model.unique_id)
    return columns, origin, typed_nulls


def _usable_cast(cast_type: str | None) -> str | None:
    """A raw cast to a type the Jinja substitution hid (`cast(x as {{ dbt.type_int() }})`
    parses as a cast to the placeholder) is no type at all."""
    return None if cast_type is None or "__preflight_" in cast_type else cast_type


def _merge_compiled(
    raw: _Inferred,
    compiled: dict[str, tuple[str | None, str | None]],
    origin: dict[str, str],
    typed_nulls: set[str],
) -> _Inferred:
    """The raw SQL's inference with the compiled SQL's filling its gaps.

    The raw SQL is kept wherever it already says something: a column it found keeps its
    cast, and a compiled cast to another type is only noted. The compiled SQL adds the
    columns the raw SQL never saw, and the type of a column the raw SQL found but could
    not type. Its models join the list only when they added something.
    """
    columns = dict(raw.columns)
    added: set[str] = set()
    conflicts: list[str] = []
    for name in sorted(compiled):
        c_cast, c_hint = compiled[name]
        if name not in columns:
            columns[name] = (c_cast, c_hint)
            added.add(name)
            continue
        r_cast, r_hint = columns[name]
        if _usable_cast(r_cast) is None:
            if c_cast is not None:
                columns[name] = (c_cast, r_hint or c_hint)
                added.add(name)
            elif r_hint is None and c_hint is not None:
                columns[name] = (r_cast, c_hint)
                added.add(name)
        elif c_cast is not None and _dbml_type(c_cast) != _dbml_type(r_cast or ""):
            conflicts.append(
                f"`{name}`: {_dbml_type(r_cast or '')} in the raw SQL, "
                f"{_dbml_type(c_cast)} compiled"
            )
    models = sorted(set(raw.models) | {origin[n] for n in added if n in origin})
    typed = {n for n in added if n in typed_nulls and columns[n][0] == compiled[n][0]}
    return _Inferred(columns, models, raw.readers_by_column, sorted(added), conflicts, typed)


def _is_key_name(name: str) -> bool:
    n = name.lower()
    return n == "id" or n.endswith("_id")


def _id_is_int(src: SourceTable, inferred: _Inferred) -> bool:
    """Whether a source's `id` column ends up an integer in the derived schema."""
    for col in src.columns:
        if col.name.lower() == "id" and col.data_type:
            return _dbml_type(col.data_type) == "int"
    cast_type, hint = inferred.columns.get("id", (None, None))
    if cast_type is None:
        return hint not in {"varchar", "boolean"}
    dtype = _dbml_type(cast_type)
    return dtype == "int" or ("id" in inferred.typed_nulls and dtype in {"decimal", "numeric"})


def _typed_null_type_and_ref(
    name: str,
    cast_type: str,
    src: SourceTable,
    all_sources: list[SourceTable],
    int_id_tables: set[str],
) -> tuple[str, SourceTable | None]:
    """The DBML type for a column a compiled `cast(null as T)` typed, and its foreign-key
    ref.

    The type is the package's, with one correction: a key typed `numeric` is an integer.
    Fivetran declares every id `numeric(28,6)` so a 64-bit id fits on any warehouse, and a
    fixture of fractional ids neither joins nor takes a ref from an integer column. A key
    that ends up an integer keeps the foreign-key ref its name points at, which an explicit
    cast in a staging model does not get: this is the package describing the source, not a
    model reshaping it. Only a `*_id` name gets one, and only to a table whose `id` is an
    integer too (`int_id_tables`): `cast(null as integer) as customer` is a count as often
    as it is a key.
    """
    dtype = _dbml_type(cast_type)
    if dtype in {"decimal", "numeric"} and _is_key_name(name):
        dtype = "int"
    if dtype != "int" or not name.lower().endswith("_id"):
        return dtype, None
    return dtype, _fk_ref_target(name, src, all_sources, int_id_tables)


def _missing_types_patch(sources: list[SourceTable]) -> str:
    """A `sources:` YAML fragment naming every table and column that lacks a data_type.

    Tables with no columns at all get a placeholder column block, since preflight cannot
    know their columns; columns that exist but lack a type get the exact line to fill in.
    """
    by_source: dict[str, list[str]] = {}
    for src in sources:
        lines: list[str] = []
        if not src.columns:
            lines += [
                f"      - name: {src.name}",
                "        columns:  # every column the staging model reads",
                "          - name: <column>",
                "            data_type: <type>",
            ]
        else:
            missing = [c for c in src.columns if not c.data_type]
            if missing:
                lines += [f"      - name: {src.name}", "        columns:"]
                for col in missing:
                    lines += [f"          - name: {col.name}", "            data_type: <type>"]
        if lines:
            by_source.setdefault(src.source_name, []).extend(lines)
    if not by_source:
        return ""
    out = ["sources:"]
    for source_name, lines in by_source.items():
        out += [f"  - name: {source_name}", "    tables:", *lines]
    return "\n".join(out)


def _resolve_columns(
    sources: dict[str, SourceTable],
    manifest: Manifest,
    names: dict[str, str],
    view: CompiledView | None = None,
) -> tuple[
    dict[str, list[SourceColumn]],
    list[InferredSource],
    list[SourceTable],
    dict[tuple[str, str], set[str]],
    dict[tuple[str, str], str],
    dict[tuple[str, str], list[str]],
    list[SourceTable],
]:
    """Declared columns, filled in from the staging models where sources.yml falls short.

    A source nothing reads - no model, directly or through a pass-through, no snapshot and
    no test - and that sources.yml does not fully type is skipped: it needs no fixture, and
    nothing would type it (the last element). A fully typed one keeps its fixture as
    before: it costs nothing, and another source's foreign key may point at it.
    A source with every column already typed passes through untouched. Otherwise every
    column declared or read is used, and none stops the run: one the SQL never types is
    typed by its name when every reader is accounted for (so no model reads it), and is a
    `varchar` when a reader could not be followed (`_reader_accounted_for`); both are
    listed as guessed. Only a read source with no column known at all - nothing declared,
    nothing read - goes to `still_missing`, for the caller to turn into a SchemaError.

    Also returns, for every source (typed or not): `unique`/`not_null` tests carried back
    from a staging model's alias for one of its columns, and the foreign-key ref a
    column's name points at, when one was found - both keyed by (source unique_id,
    column name), for the caller to fold into the DBML it writes. The ref's value is the
    DBML table name it points at.

    With `view`, the compiled SQL fills what the raw SQL leaves open (`_merge_compiled`).
    """
    effective: dict[str, list[SourceColumn]] = {}
    inferred: list[InferredSource] = []
    still_missing: list[SourceTable] = []
    skipped = [
        src
        for src in sources.values()
        if not (src.columns and all(c.data_type for c in src.columns))
        and not _is_read(src, manifest, view)
    ]
    skipped_ids = {src.unique_id for src in skipped}
    sources = {uid: src for uid, src in sources.items() if uid not in skipped_ids}
    carried_tests: dict[tuple[str, str], set[str]] = {}
    carried_values: dict[tuple[str, str], list[str]] = {}
    fk_refs: dict[tuple[str, str], str] = {}
    all_sources = list(sources.values())

    # What each source's models read, up front: a foreign-key guess needs to know whether
    # its target has an `id` column, and an untyped target only learns that from inference.
    found_by_source: dict[str, _Inferred] = {}
    id_tables: set[str] = set()
    for src in all_sources:
        fully_typed = bool(src.columns) and all(c.data_type for c in src.columns)
        if fully_typed:
            found_by_source[src.unique_id] = _Inferred({}, [])
        else:
            inferred_here = _infer_source_columns(src, manifest)
            if view is not None:
                inferred_here = _merge_compiled(
                    inferred_here,
                    *_compiled_source_columns(src, view, inferred_here.readers_by_column),
                )
            found_by_source[src.unique_id] = inferred_here
        if (
            "id" in {c.name.lower() for c in src.columns}
            or "id" in found_by_source[src.unique_id].columns
        ):
            id_tables.add(src.unique_id)
    int_id_tables = {uid for uid in id_tables if _id_is_int(sources[uid], found_by_source[uid])}

    for src in all_sources:
        src_tests, src_values = _carried_tests_for_source(src, manifest, view)
        for col_name, tests in src_tests.items():
            carried_tests.setdefault((src.unique_id, col_name), set()).update(tests)
        for col_name, values in src_values.items():
            carried_values.setdefault((src.unique_id, col_name), values)

        if src.columns and all(c.data_type for c in src.columns):
            effective[src.unique_id] = src.columns
            continue

        result = found_by_source[src.unique_id]
        found = result.columns
        typed_nulls = result.typed_nulls
        declared_names = {c.name for c in src.columns}
        merged: list[SourceColumn] = []
        guessed: list[str] = []
        unknown: list[str] = []

        def _resolve(
            name: str,
            cast_type: str | None,
            hint: str | None,
            src: SourceTable = src,
            guessed: list[str] = guessed,
            typed_nulls: set[str] = typed_nulls,
        ) -> str:
            if cast_type is not None and name in typed_nulls:
                dtype, target = _typed_null_type_and_ref(
                    name, cast_type, src, all_sources, int_id_tables
                )
            else:
                dtype, target = _resolve_type_and_ref(
                    name, cast_type, hint, src, all_sources, id_tables
                )
            if not cast_type:
                guessed.append(name)
            if target is not None:
                fk_refs[(src.unique_id, name)] = names[target.unique_id]
            return dtype

        # With every reader followed (`_reader_accounted_for`), a declared column none of
        # their SQL mentions is one nothing reads (Fivetran's sources.yml documents more
        # columns than its staging macros select): it is typed by its name and listed as
        # guessed. Otherwise - no compiled SQL, a model the walk cannot follow, or a
        # snapshot or singular test reading the source - it is a flagged varchar below.
        opaque = _opaque_readers(src, manifest, view)
        unread_ok = view is not None and not opaque
        for col in src.columns:
            if col.data_type:
                merged.append(col)
            elif col.name in found:
                cast_type, hint = found[col.name]
                merged.append(
                    SourceColumn(col.name, _resolve(col.name, cast_type, hint), col.description)
                )
            elif unread_ok:
                merged.append(
                    SourceColumn(col.name, _resolve(col.name, None, None), col.description)
                )
            else:
                # A reader's SQL could not be followed, so a model may read this column in
                # a way nothing here can type: text, flagged, rather than a guess by name.
                guessed.append(col.name)
                unknown.append(col.name)
                merged.append(SourceColumn(col.name, "varchar", col.description))
        for name, (cast_type, hint) in found.items():
            if name in declared_names:
                continue
            merged.append(SourceColumn(name, _resolve(name, cast_type, hint)))

        if not merged:
            still_missing.append(src)
            continue

        effective[src.unique_id] = merged
        merged_names = {c.name for c in merged}
        inferred.append(
            InferredSource(
                source_name=src.source_name,
                table=src.name,
                identifier=src.identifier,
                models=result.models,
                total_columns=len(merged),
                guessed_columns=sorted(guessed),
                compiled_columns=[c for c in result.compiled_columns if c in merged_names],
                type_conflicts=result.type_conflicts,
                unknown_columns=sorted(unknown),
                guessed_readers={
                    name: (
                        list(opaque)
                        if name in unknown
                        else sorted(result.readers_by_column.get(name, set()))
                    )
                    for name in sorted(set(guessed))
                },
            )
        )

    return effective, inferred, still_missing, carried_tests, fk_refs, carried_values, skipped


def _opaque_readers(src: SourceTable, manifest: Manifest, view: CompiledView | None) -> list[str]:
    """Unique ids of what reads a source in a way nothing here can follow column by column:
    a model whose compiled SQL `_reader_accounted_for` rejects (every model reading it,
    without compiled SQL), a snapshot, or a test other than a generic one on a named
    column. Kept in step with `_is_read`, which counts the same readers."""
    out: set[str] = set()
    if view is None:
        out |= {uid for uid, m in manifest.models.items() if src.unique_id in m.depends_on}
    else:
        out |= {
            m.unique_id
            for m in view.readers(src.unique_id)
            if not _reader_accounted_for(view, m, src)
        }
    out |= {c for c in manifest.child_map.get(src.unique_id, []) if c in manifest.snapshots}
    for uid, test in manifest.tests.items():
        if src.unique_id not in test.depends_on:
            continue
        on_column = test.test_name is not None and (
            (test.attached_node == src.unique_id and test.column_name)
            or (test.test_name == "relationships" and test.kwargs.get("field"))
        )
        if not on_column:
            out.add(uid)
    return sorted(out)


def _is_read(src: SourceTable, manifest: Manifest, view: CompiledView | None) -> bool:
    """Whether anything enabled reads a source: a model (directly, or through a
    pass-through), a snapshot, or a test."""
    if any(src.unique_id in m.depends_on for m in manifest.models.values()):
        return True
    if view is not None and view.readers(src.unique_id):
        return True
    if any(src.unique_id in t.depends_on for t in manifest.tests.values()):
        return True
    return any(
        child in manifest.snapshots or child in manifest.tests or child in manifest.models
        for child in manifest.child_map.get(src.unique_id, [])
    )


def derive_dbml(
    manifest: Manifest, compiled: CompiledSql | None = None
) -> tuple[str, list[InferredSource]]:
    """Write the sources of a manifest as DBML.

    Column settings come from the generic tests declared on the source - `unique` and
    `not_null` map to their DBML settings, a column called `id` (or one carrying both) is
    the primary key, and a `relationships` test to another source becomes a `ref` - plus
    the same `unique`/`not_null` tests carried back from a staging model's alias for one of
    the source's columns. A column whose name looks like a foreign key (`customer_id`, or a
    bare `customer` when a `raw_customers` source exists) gets a ref to that table's `id`
    too, when an explicit `relationships` test did not already set one. A source with
    columns sources.yml leaves untyped, or does not declare at all, has them inferred from
    the staging models that read it; only a source inference cannot help either raises a
    SchemaError.

    `compiled`, when preflight could compile the models that read sources, fills what the
    raw SQL leaves open: see `compiled.py` and `_merge_compiled`.

    Returns the DBML text and a record of every source whose columns were inferred.
    """
    return (derived := derive_schema(manifest, compiled)).text, derived.inferred


@dataclass
class DerivedSchema:
    text: str
    inferred: list[InferredSource]
    skipped: list[str]  # "<source>.<table>" of the sources nothing reads, sorted


def derive_schema(manifest: Manifest, compiled: CompiledSql | None = None) -> DerivedSchema:
    """`derive_dbml`, plus the sources it skipped because nothing reads them."""
    sources = manifest.sources
    if not sources:
        raise SchemaError("The dbt project declares no sources, so there is nothing to generate.")

    names = source_table_names(sources.values())
    view = CompiledView(manifest, compiled) if compiled is not None else None
    (
        effective, inferred, still_missing, carried_tests, fk_refs, carried_values, skipped
    ) = _resolve_columns(sources, manifest, names, view)  # fmt: skip
    if still_missing:
        tables = ", ".join(f"`{s.source_name}.{s.name}`" for s in still_missing)
        patch = _missing_types_patch(still_missing)
        raise SchemaError(
            f"No columns are known for {tables}: sources.yml declares none, and no model "
            "that reads it names one preflight can see, so there is no table to generate. "
            "Point `schema:` in .dbt-preflight.yml at a DBML file describing the source "
            "system (`dbt-preflight schema` writes a starting one from this project), or "
            f"declare its columns in the sources file:\n\n{patch}"
        )
    skipped_ids = {s.unique_id for s in skipped}
    sources = {uid: src for uid, src in sources.items() if uid not in skipped_ids}

    col_settings: dict[tuple[str, str], set[str]] = {}
    for key, tests in carried_tests.items():
        col_settings.setdefault(key, set()).update(tests)
    col_refs: dict[tuple[str, str], tuple[str, str]] = {
        key: (target, "id") for key, target in fk_refs.items()
    }
    col_values: dict[tuple[str, str], list[str]] = dict(carried_values)
    for test in manifest.source_tests():
        src = sources.get(test.attached_node or "")
        if src is None or not test.column_name:
            continue
        key = (src.unique_id, test.column_name)
        if test.test_name in {"unique", "not_null"}:
            col_settings.setdefault(key, set()).add(test.test_name)
        elif test.test_name == "relationships":
            target = _ref_target(test, sources, names)
            if target is not None:
                col_refs[key] = target  # an explicit test's ref wins over a guessed one
        elif test.test_name == "accepted_values":
            values = _accepted_values(test)
            if values:
                col_values[key] = values  # declared on the source itself: it wins

    enum_blocks: list[str] = []
    table_lines: list[str] = []
    for src in sources.values():
        table_name = names[src.unique_id]
        table_lines.append(f"Table {table_name} {{")
        columns = effective.get(src.unique_id, src.columns)
        # One primary key per table: `id` when there is one, else the first column carrying
        # both `unique` and `not_null`. Two `pk` columns would be read as one composite key,
        # which leaves each of them free to repeat - and the staging model's `unique` test
        # on either one then fails on the fixtures.
        pk_column = next((c.name for c in columns if c.name == "id"), None) or next(
            (
                c.name
                for c in columns
                if {"unique", "not_null"} <= col_settings.get((src.unique_id, c.name), set())
            ),
            None,
        )
        for col in columns:
            settings: list[str] = []
            tests = col_settings.get((src.unique_id, col.name), set())
            if col.name == pk_column:
                settings.append("pk")
            else:
                if "unique" in tests:
                    settings.append("unique")
                if "not_null" in tests:
                    settings.append("not null")
            ref = col_refs.get((src.unique_id, col.name))
            if ref is not None:
                settings.append(f"ref: > {ref[0]}.{ref[1]}")
            suffix = f" [{', '.join(settings)}]" if settings else ""
            col_type = _dbml_type(col.data_type or "varchar")
            # An `accepted_values` test names the only values the column may hold, so the
            # column is written as an enum of exactly those: model2data draws from an enum's
            # own values, where a varchar would get placeholder text the test then rejects.
            # Only a string column qualifies - an enum's values are strings, so turning a
            # numeric column into one would change its type to satisfy a test.
            values = col_values.get((src.unique_id, col.name))
            if values and col_type == "varchar":
                enum_name = _enum_name(table_name, col.name)
                enum_blocks.append(
                    f"Enum {enum_name} {{\n" + "".join(f'  "{v}"\n' for v in values) + "}\n"
                )
                col_type = enum_name
            table_lines.append(f"  {col.name} {col_type}{suffix}")
        if src.description:
            note = src.description.strip().replace("'", "\\'").splitlines()[0]
            table_lines.append(f"  Note: '{note}'")
        table_lines.append("}")
        table_lines.append("")
    lines = ["// Derived by dbt-preflight from the project's sources.yml. Do not edit.", ""]
    lines += enum_blocks  # enums first, so a column's type is defined before it is used
    lines += table_lines
    return DerivedSchema(
        "\n".join(lines), inferred, sorted(f"{s.source_name}.{s.name}" for s in skipped)
    )


def resolve_schema(
    schema_file: Path | None,
    manifest: Manifest,
    workdir: Path,
    compiled: CompiledSql | None = None,
) -> ResolvedSchema:
    if schema_file is not None:
        tables, refs = parse_dbml(schema_file)
        if not tables:
            raise SchemaError(f"{schema_file} contains no tables.")
        return ResolvedSchema(tables=tables, refs=refs, dbml_path=schema_file, derived=False)

    derived = derive_schema(manifest, compiled)
    text, inferred = derived.text, derived.inferred
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "derived.dbml"
    path.write_text(text, encoding="utf-8")
    try:
        tables, refs = parse_dbml(path)
    except Exception as exc:  # noqa: BLE001 - model2data's own error type varies by release
        raise SchemaError(
            f"The schema derived from sources.yml could not be read by model2data "
            f"({type(exc).__name__}): {exc}"
        ) from exc
    return ResolvedSchema(
        tables=tables,
        refs=refs,
        dbml_path=path,
        derived=True,
        inferred=inferred,
        skipped=derived.skipped,
    )
