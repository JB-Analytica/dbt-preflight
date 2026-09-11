"""Where the source schema comes from.

Two routes to the same thing, a parsed DBML model that model2data can generate data for:

1. A DBML file the repo already keeps (the reference architecture does). Best case: the
   file carries note hints that shape the data like a business.
2. Derived from the project's own `sources.yml`. This only works when every source column
   declares a `data_type`; a column without one is reported, not guessed, because a fixture
   with the wrong type is worse than no fixture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from model2data.parse.dbml import TableDef, parse_dbml

from dbt_preflight.manifest import Manifest, SourceTable, TestNode

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
class ResolvedSchema:
    tables: dict[str, TableDef]
    refs: list[dict]
    dbml_path: Path
    derived: bool


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


def derive_dbml(manifest: Manifest) -> str:
    """Write the sources of a manifest as DBML.

    Column settings come from the generic tests declared on the source: `unique` and
    `not_null` map to their DBML settings, a column called `id` (or one carrying both) is
    the primary key, and a `relationships` test to another source becomes a `ref`.
    """
    sources = manifest.sources
    if not sources:
        raise SchemaError("The dbt project declares no sources, so there is nothing to generate.")

    patch = _missing_types_patch(list(sources.values()))
    if patch:
        raise SchemaError(
            "Cannot derive a schema from sources.yml: every source column needs a `data_type` "
            "so the fixture has the right shape. Either point `schema:` in .dbt-preflight.yml "
            "at a DBML file that describes the source system, or add the missing types. "
            "This is what is missing, as YAML to paste into the sources file:\n\n"
            f"{patch}"
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
        for col in src.columns:
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
    return "\n".join(lines)


def resolve_schema(schema_file: Path | None, manifest: Manifest, workdir: Path) -> ResolvedSchema:
    if schema_file is not None:
        tables, refs = parse_dbml(schema_file)
        if not tables:
            raise SchemaError(f"{schema_file} contains no tables.")
        return ResolvedSchema(tables=tables, refs=refs, dbml_path=schema_file, derived=False)

    text = derive_dbml(manifest)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "derived.dbml"
    path.write_text(text, encoding="utf-8")
    tables, refs = parse_dbml(path)
    return ResolvedSchema(tables=tables, refs=refs, dbml_path=path, derived=True)
