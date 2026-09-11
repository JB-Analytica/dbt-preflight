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
_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)

_TARGET_PLACEHOLDER = "__preflight_target__"

# Name -> guessed DBML type, checked in order; the first match wins.
_INT_SUFFIXES = ("_id", "_cents", "_count", "quantity", "units")
_DECIMAL_SUFFIXES = ("amount", "price", "total", "rate")


def _guess_type_by_name(name: str) -> str:
    """A DBML type guessed from a column's name alone, house-convention style."""
    n = name.lower()
    if n.endswith("_at"):
        return "timestamp"
    if n.endswith("_date"):
        return "date"
    if n == "id" or n.endswith("_id"):
        return "int"
    if n.startswith("is_") or n.startswith("has_"):
        return "boolean"
    if n.endswith(_INT_SUFFIXES):
        return "int"
    if n.endswith(_DECIMAL_SUFFIXES):
        return "decimal"
    return "varchar"


def _model_source_columns(
    raw_code: str, source_name: str, table_name: str
) -> dict[str, str | None] | None:
    """{column_name: explicit cast type, or None} a model reads from one source table.

    `{{ source(...) }}` and `{{ ref(...) }}` calls are swapped for plain identifiers so
    sqlglot can parse the compiled-looking SQL, then every `exp.Column` in the query is
    collected: a staging model's `renamed` CTE reads straight off the `source` CTE without
    qualifying columns, so this is a query-wide walk, not a single-clause one. Columns
    qualified with another table (a join, or a second source) are excluded. Returns None
    when the model does not reference this source at all, or the SQL does not parse -
    normal for a model this house style would flag as not staging.
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
    sql = _JINJA_RE.sub(" ", sql)

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

    columns: dict[str, str | None] = {}
    for col in tree.find_all(exp.Column):
        name = col.name.lower()
        if not name or (col.table and col.table in foreign_aliases):
            continue
        columns.setdefault(name, None)
        parent = col.parent
        if isinstance(parent, exp.Cast) and parent.this is col and columns[name] is None:
            columns[name] = parent.to.sql(dialect=None)
    return columns


def _infer_source_columns(
    source: SourceTable, manifest: Manifest
) -> tuple[dict[str, str | None], list[str]]:
    """Columns (and any explicit cast type) inferred from every model that reads a source.

    A staging model typically names every column it selects off its source, so this is
    read as the closest thing to a schema a project without one has. Models that do not
    parse, or do not read this source at all, are silently skipped.
    """
    columns: dict[str, str | None] = {}
    used_models: list[str] = []
    for model in manifest.models.values():
        if source.unique_id not in model.depends_on:
            continue
        found = _model_source_columns(model.raw_code, source.source_name, source.name)
        if found is None:
            continue
        used_models.append(model.name)
        for name, cast_type in found.items():
            if name not in columns:
                columns[name] = cast_type
            elif columns[name] is None and cast_type:
                columns[name] = cast_type
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
) -> tuple[dict[str, list[SourceColumn]], list[InferredSource], list[SourceTable]]:
    """Declared columns, filled in from the staging models where sources.yml falls short.

    A source with every column already typed passes through untouched. Otherwise, every
    column the reading models can account for - the union of what is declared and what is
    inferred - is used; a source left with an untyped, unread column goes to `still_missing`
    for the caller to turn into a SchemaError.
    """
    effective: dict[str, list[SourceColumn]] = {}
    inferred: list[InferredSource] = []
    still_missing: list[SourceTable] = []

    for src in sources.values():
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
                cast_type = found[col.name]
                dtype = _dbml_type(cast_type) if cast_type else _guess_type_by_name(col.name)
                if not cast_type:
                    guessed.append(col.name)
                merged.append(SourceColumn(col.name, dtype, col.description))
            else:
                unresolved = True
        for name, cast_type in found.items():
            if name in declared_names:
                continue
            dtype = _dbml_type(cast_type) if cast_type else _guess_type_by_name(name)
            if not cast_type:
                guessed.append(name)
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

    return effective, inferred, still_missing


def derive_dbml(manifest: Manifest) -> tuple[str, list[InferredSource]]:
    """Write the sources of a manifest as DBML.

    Column settings come from the generic tests declared on the source: `unique` and
    `not_null` map to their DBML settings, a column called `id` (or one carrying both) is
    the primary key, and a `relationships` test to another source becomes a `ref`. A source
    with columns sources.yml leaves untyped, or does not declare at all, has them inferred
    from the staging models that read it; only a source inference cannot help either raises
    a SchemaError.

    Returns the DBML text and a record of every source whose columns were inferred.
    """
    sources = manifest.sources
    if not sources:
        raise SchemaError("The dbt project declares no sources, so there is nothing to generate.")

    effective, inferred, still_missing = _resolve_columns(sources, manifest)
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
    col_refs: dict[tuple[str, str], tuple[str, str]] = {}
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
                col_refs[key] = target

    lines = ["// Derived by dbt-preflight from the project's sources.yml. Do not edit.", ""]
    for src in sources.values():
        lines.append(f"Table {src.identifier} {{")
        for col in effective.get(src.unique_id, src.columns):
            settings: list[str] = []
            tests = col_settings.get((src.identifier, col.name), set())
            if col.name == "id" or {"unique", "not_null"} <= tests and col.name.endswith("_id"):
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
            lines.append(f"  {col.name} {_dbml_type(col.data_type or 'varchar')}{suffix}")
        if src.description:
            note = src.description.strip().replace("'", "\\'").splitlines()[0]
            lines.append(f"  Note: '{note}'")
        lines.append("}")
        lines.append("")
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
