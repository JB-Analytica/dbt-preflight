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
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from model2data.parse.dbml import TableDef, parse_dbml
from sqlglot import exp

from dbt_preflight.manifest import Manifest, SourceColumn, SourceTable, TestNode

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


@dataclass
class ResolvedSchema:
    tables: dict[str, TableDef]
    refs: list[dict]
    dbml_path: Path
    derived: bool
    inferred: list[InferredSource] = field(default_factory=list)


def _dbml_type(data_type: str) -> str:
    base = re.sub(r"\(.*\)$", "", data_type.strip().lower()).strip()
    return _TYPE_ALIASES.get(base, base)


def _enum_name(table: str, column: str) -> str:
    """A DBML enum name for one column's accepted values, unique within the derived file.

    Qualified by table, because two tables can each have a `status` with different values,
    and DBML resolves a column's type by name across the whole document."""
    return re.sub(r"[^a-z0-9_]", "_", f"{table}__{column}".lower())


def _ref_target(test: TestNode, sources: dict[str, SourceTable]) -> tuple[str, str] | None:
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
            return src.identifier, str(field_)
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

_TARGET_PLACEHOLDER = "__preflight_target__"

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
    if n.endswith(_TIMESTAMP_SUFFIXES):
        return "timestamp"
    if n.endswith(_DATE_SUFFIXES):
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


def _fk_ref_target(name: str, own: SourceTable, all_sources: list[SourceTable]) -> str | None:
    """The identifier of another source table this column's name points at, as a
    foreign key - `customer_id`, or a bare `customer` when a `raw_customers` source
    exists - so the fixtures can keep referential integrity between them.

    An `_id` suffix alone is enough to type a column as an integer (handled in
    `_guess_type_by_name`); this only adds the `ref:` when a matching table can
    actually be found, so an unmatched `_id` column stays an ordinary integer.
    """
    n = name.lower()
    if n == "id":
        return None
    base = n[: -len("_id")] if n.endswith("_id") else n
    if not base:
        return None
    plural = _pluralize(base)
    for src in all_sources:
        if src.unique_id == own.unique_id:
            continue
        basename = _table_basename(src.name)
        if base in (basename, src.name.lower()) or plural in (basename, src.name.lower()):
            return src.identifier
    return None


_ARITH_OPS = (exp.Div, exp.Mul, exp.Add, exp.Sub)
_COMPARISON_OPS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_NUMERIC_FUNCS = (exp.Sum, exp.Avg, exp.Round)
_STRING_FUNCS = (exp.Lower, exp.Upper, exp.Trim, exp.Concat)


def _column_hint(col: exp.Column) -> str | None:
    """ "numeric" or "varchar" if how this column occurrence is used in the SQL signals
    a type, independent of its name; the strongest signal short of an explicit cast.

    An operand of `/`, `*`, `+`, `-` against a numeric literal, or wrapped in `sum(`,
    `avg(`, `round(`, reads as numeric. Compared to a string literal, or passed to
    `lower(`/`upper(`/`trim(`/`concat(`, reads as varchar.
    """
    parent = col.parent
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
) -> tuple[str, str | None]:
    """The DBML type for an inferred source column, and a foreign-key ref if its name
    points at another source table.

    Priority, strongest first: an explicit cast; how the column is used in the SQL
    (`_column_hint`); a foreign-key-shaped name (always an integer); the rest of the
    name heuristics in `_guess_type_by_name`.
    """
    if cast_type:
        return _dbml_type(cast_type), None

    fk_target = _fk_ref_target(name, src, all_sources)
    is_id_suffix = name.lower() != "id" and name.lower().endswith("_id")

    if hint == "numeric":
        return ("int" if _int_name_signal(name.lower()) else "decimal"), fk_target
    if hint == "varchar":
        return "varchar", None

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
    then every `exp.Column` in the query is collected: a staging model's `renamed` CTE reads
    straight off the `source` CTE without qualifying columns, so this is a query-wide walk,
    not a single-clause one. Columns qualified with another table (a join, or a second
    source) are excluded. Returns None when the model does not reference this source at
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

    foreign_aliases = {
        t.alias_or_name
        for t in tree.find_all(exp.Table)
        if t.name.startswith("__preflight_") and t.name != _TARGET_PLACEHOLDER
    }

    columns: dict[str, tuple[str | None, str | None]] = {}
    for col in tree.find_all(exp.Column):
        name = col.name.lower()
        if (
            not name
            or name.startswith("__preflight_")
            or (col.table and col.table in foreign_aliases)
        ):
            continue
        cast_type, hint = columns.get(name, (None, None))
        parent = col.parent
        if isinstance(parent, exp.Cast) and parent.this is col and cast_type is None:
            cast_type = parent.to.sql(dialect=None)
        if hint is None:
            hint = _column_hint(col)
        columns[name] = (cast_type, hint)
    for name in macro_columns:
        columns.setdefault(name, (None, None))
    return columns


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
    if tree is None:
        return {}
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
    source: SourceTable, manifest: Manifest
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """{source_column_name: {test names}}, carried back from the `unique`/`not_null`
    tests a staging model declares on the alias it gave one of this source's columns.

    A source column named `id` is already the primary key regardless; this is what
    lets another column - `sku`, `order_id`, whatever the project actually keys its
    source rows by - carry the same settings, so the fixtures satisfy tests the
    project's own YAML already documents instead of leaving them to chance.
    """
    carried: dict[str, set[str]] = {}
    carried_values: dict[str, list[str]] = {}
    for model in manifest.models.values():
        if source.unique_id not in model.depends_on:
            continue
        alias_map = _model_alias_map(model.raw_code)
        if not alias_map:
            continue
        for test in manifest.tests_for_model(model.unique_id):
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


def _infer_source_columns(
    source: SourceTable, manifest: Manifest
) -> tuple[dict[str, tuple[str | None, str | None]], list[str]]:
    """Columns (and any explicit cast type / usage hint) inferred from every model that
    reads a source.

    A staging model typically names every column it selects off its source, so this is
    read as the closest thing to a schema a project without one has. Models that do not
    parse, or do not read this source at all, are silently skipped.
    """
    columns: dict[str, tuple[str | None, str | None]] = {}
    used_models: list[str] = []
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
    return columns, sorted(used_models)


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
    sources: dict[str, SourceTable], manifest: Manifest
) -> tuple[
    dict[str, list[SourceColumn]],
    list[InferredSource],
    list[SourceTable],
    dict[tuple[str, str], set[str]],
    dict[tuple[str, str], str],
    dict[tuple[str, str], list[str]],
]:
    """Declared columns, filled in from the staging models where sources.yml falls short.

    A source with every column already typed passes through untouched. Otherwise, every
    column the reading models can account for - the union of what is declared and what is
    inferred - is used; a source left with an untyped, unread column goes to `still_missing`
    for the caller to turn into a SchemaError.

    Also returns, for every source (typed or not): `unique`/`not_null` tests carried back
    from a staging model's alias for one of its columns, and the foreign-key ref a
    column's name points at, when one was found - both keyed by (source identifier,
    column name), for the caller to fold into the DBML it writes.
    """
    effective: dict[str, list[SourceColumn]] = {}
    inferred: list[InferredSource] = []
    still_missing: list[SourceTable] = []
    carried_tests: dict[tuple[str, str], set[str]] = {}
    carried_values: dict[tuple[str, str], list[str]] = {}
    fk_refs: dict[tuple[str, str], str] = {}
    all_sources = list(sources.values())

    for src in sources.values():
        src_tests, src_values = _carried_tests_for_source(src, manifest)
        for col_name, tests in src_tests.items():
            carried_tests.setdefault((src.identifier, col_name), set()).update(tests)
        for col_name, values in src_values.items():
            carried_values.setdefault((src.identifier, col_name), values)

        if src.columns and all(c.data_type for c in src.columns):
            effective[src.unique_id] = src.columns
            continue

        found, used_models = _infer_source_columns(src, manifest)
        declared_names = {c.name for c in src.columns}
        merged: list[SourceColumn] = []
        guessed: list[str] = []
        unresolved = False

        for col in src.columns:
            if col.data_type:
                merged.append(col)
            elif col.name in found:
                cast_type, hint = found[col.name]
                dtype, ref = _resolve_type_and_ref(col.name, cast_type, hint, src, all_sources)
                if not cast_type:
                    guessed.append(col.name)
                if ref is not None:
                    fk_refs[(src.identifier, col.name)] = ref
                merged.append(SourceColumn(col.name, dtype, col.description))
            else:
                unresolved = True
        for name, (cast_type, hint) in found.items():
            if name in declared_names:
                continue
            dtype, ref = _resolve_type_and_ref(name, cast_type, hint, src, all_sources)
            if not cast_type:
                guessed.append(name)
            if ref is not None:
                fk_refs[(src.identifier, name)] = ref
            merged.append(SourceColumn(name, dtype))

        if unresolved or not merged:
            still_missing.append(src)
            continue

        effective[src.unique_id] = merged
        inferred.append(
            InferredSource(
                source_name=src.source_name,
                table=src.name,
                identifier=src.identifier,
                models=used_models,
                total_columns=len(merged),
                guessed_columns=sorted(guessed),
            )
        )

    return effective, inferred, still_missing, carried_tests, fk_refs, carried_values


def derive_dbml(manifest: Manifest) -> tuple[str, list[InferredSource]]:
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

    Returns the DBML text and a record of every source whose columns were inferred.
    """
    sources = manifest.sources
    if not sources:
        raise SchemaError("The dbt project declares no sources, so there is nothing to generate.")

    effective, inferred, still_missing, carried_tests, fk_refs, carried_values = _resolve_columns(
        sources, manifest
    )
    if still_missing:
        patch = _missing_types_patch(still_missing)
        if patch:
            raise SchemaError(
                "Cannot derive a schema from sources.yml: every source column needs a "
                "`data_type`, or has to be read by a staging model preflight can infer it "
                "from, so the fixture has the right shape. Either point `schema:` in "
                ".dbt-preflight.yml at a DBML file that describes the source system, or add "
                "the missing types. This is what is missing, as YAML to paste into the "
                f"sources file:\n\n{patch}"
            )

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
        key = (src.identifier, test.column_name)
        if test.test_name in {"unique", "not_null"}:
            col_settings.setdefault(key, set()).add(test.test_name)
        elif test.test_name == "relationships":
            target = _ref_target(test, sources)
            if target is not None:
                col_refs[key] = target  # an explicit test's ref wins over a guessed one
        elif test.test_name == "accepted_values":
            values = _accepted_values(test)
            if values:
                col_values[key] = values  # declared on the source itself: it wins

    enum_blocks: list[str] = []
    table_lines: list[str] = []
    for src in sources.values():
        table_lines.append(f"Table {src.identifier} {{")
        for col in effective.get(src.unique_id, src.columns):
            settings: list[str] = []
            tests = col_settings.get((src.identifier, col.name), set())
            if col.name == "id" or {"unique", "not_null"} <= tests:
                settings.append("pk")
            else:
                if "unique" in tests:
                    settings.append("unique")
                if "not_null" in tests:
                    settings.append("not null")
            ref = col_refs.get((src.identifier, col.name))
            if ref is not None:
                settings.append(f"ref: > {ref[0]}.{ref[1]}")
            suffix = f" [{', '.join(settings)}]" if settings else ""
            col_type = _dbml_type(col.data_type or "varchar")
            # An `accepted_values` test names the only values the column may hold, so the
            # column is written as an enum of exactly those: model2data draws from an enum's
            # own values, where a varchar would get placeholder text the test then rejects.
            # Only a string column qualifies - an enum's values are strings, so turning a
            # numeric column into one would change its type to satisfy a test.
            values = col_values.get((src.identifier, col.name))
            if values and col_type == "varchar":
                enum_name = _enum_name(src.identifier, col.name)
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
    return "\n".join(lines), inferred


def resolve_schema(schema_file: Path | None, manifest: Manifest, workdir: Path) -> ResolvedSchema:
    if schema_file is not None:
        tables, refs = parse_dbml(schema_file)
        if not tables:
            raise SchemaError(f"{schema_file} contains no tables.")
        return ResolvedSchema(tables=tables, refs=refs, dbml_path=schema_file, derived=False)

    text, inferred = derive_dbml(manifest)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "derived.dbml"
    path.write_text(text, encoding="utf-8")
    tables, refs = parse_dbml(path)
    return ResolvedSchema(tables=tables, refs=refs, dbml_path=path, derived=True, inferred=inferred)
