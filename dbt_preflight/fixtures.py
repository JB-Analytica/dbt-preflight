"""Turn the resolved schema into source tables in a DuckDB file.

model2data does the generation; this module maps its output onto the sources the dbt
project actually declares (database, schema, identifier), adds the columns a loader would
have stamped on, casts everything to the DBML type, and loads it. model2data's own
warnings about the data it generated (columns it had to fill with generic text, tables
stuck in an unresolved foreign-key cycle, ...) are collected onto the summary too, so the
comment can tell a weak fixture from a strong one.

A text column some model reads with a JSON function gets JSON instead of model2data's text,
with every path the SQL reads present (`json_columns.py`): from the column's note in the
DBML (`JSON, keys read: ...`) and from what the project's SQL reads, traced back to the
source (`schema.json_reads`). The values come from the seed and the table and column names
alone, and keep the column's nulls.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from model2data.generate.core import (
    generate_data_from_dbml,
    get_cyclic_tables,
    get_unresolved_composite_keys,
)
from model2data.generate.faker import get_unmapped_columns, reset_stats
from model2data.parse.dbml import get_parse_warnings
from model2data.utils import normalize_identifier

from dbt_preflight.config import PreflightConfig
from dbt_preflight.json_columns import JSON_CAPABLE_TYPES, json_values, merge_shape, parse_note
from dbt_preflight.manifest import SourceTable
from dbt_preflight.schema import InferredSource, ResolvedSchema, source_table_names

# DBML / model2data types -> DuckDB types for the cast after load.
_DUCK_TYPES = {
    "int": "BIGINT",
    "integer": "BIGINT",
    "bigint": "BIGINT",
    "smallint": "BIGINT",
    "tinyint": "BIGINT",
    "serial": "BIGINT",
    "float": "DOUBLE",
    "double": "DOUBLE",
    "real": "DOUBLE",
    "decimal": "DOUBLE",
    "numeric": "DOUBLE",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "datetime": "TIMESTAMP",
    "date": "DATE",
}


@dataclass
class LoadedTable:
    source_name: str
    name: str
    identifier: str
    schema: str
    rows: int


@dataclass
class FixtureSummary:
    tables: list[LoadedTable] = field(default_factory=list)
    unmatched_sources: list[str] = field(default_factory=list)
    unused_dbml_tables: list[str] = field(default_factory=list)
    inferred_sources: list[InferredSource] = field(default_factory=list)
    # "<source>.<table>" of the sources nothing reads, given no fixture (derived schema).
    skipped_sources: list[str] = field(default_factory=list)
    # model2data's own warnings about the data it generated, surfaced so a reviewer can
    # tell a weak fixture (placeholder text, an unresolved cycle) from a strong one.
    unmapped_columns: list[tuple[str, str]] = field(default_factory=list)
    cyclic_tables: list[str] = field(default_factory=list)
    unresolved_composite_keys: list[str] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)
    # "<identifier>.<column>" of the text columns filled with JSON, because a model reads
    # them with a JSON function (or the schema's note says one does).
    json_columns: list[str] = field(default_factory=list)

    @property
    def guessed_sources(self) -> int:
        """Sources with at least one column whose type preflight guessed."""
        return sum(1 for s in self.inferred_sources if s.guessed_columns)

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)


def _duck_type(dbml_type: str) -> str:
    base = dbml_type.strip().lower().split("(")[0]
    return _DUCK_TYPES.get(base, "VARCHAR")


def _loader_values(column: str, table: str, n: int, seed: int) -> list[str]:
    """Deterministic stand-ins for the columns a loader adds.

    `_dlt_load_id` is an epoch-second string like dlt writes, `_dlt_id` a short opaque id.
    Anything else gets a stable hash so a project that declares its own loader columns
    still receives non-null, unique-per-row values.
    """
    if column == "_dlt_load_id":
        return [f"{1_757_000_000 + seed}.{i % 7:06d}" for i in range(n)]
    return [
        hashlib.blake2b(f"{table}:{column}:{i}:{seed}".encode(), digest_size=10).hexdigest()[:14]
        for i in range(n)
    ]


def build_fixtures(
    config: PreflightConfig,
    schema: ResolvedSchema,
    sources: list[SourceTable],
    db_path: Path,
) -> FixtureSummary:
    parse_warnings = get_parse_warnings()

    reset_stats()  # clear the record of columns generated with generic fallback text
    generated = generate_data_from_dbml(
        tables=schema.tables,
        refs=schema.refs,
        base_rows=config.rows,
        seed=config.seed,
        row_overrides=config.rows_for,
        locale=config.locale,
    )
    by_key = {normalize_identifier(name): (name, df) for name, df in generated.items()}
    used: set[str] = set()
    summary = FixtureSummary(
        inferred_sources=schema.inferred,
        skipped_sources=list(schema.skipped),
        unmapped_columns=get_unmapped_columns(),
        cyclic_tables=get_cyclic_tables(),
        unresolved_composite_keys=get_unresolved_composite_keys(),
        parse_warnings=parse_warnings,
    )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        table_names = source_table_names(sources)
        skipped = set(schema.skipped)
        for src in sources:
            if f"{src.source_name}.{src.name}" in skipped:
                continue
            # A derived schema writes two same-named source tables under distinct names.
            match = (
                by_key.get(normalize_identifier(table_names[src.unique_id]))
                or by_key.get(normalize_identifier(src.identifier))
                or by_key.get(normalize_identifier(src.name))
            )
            if match is None:
                summary.unmatched_sources.append(f"{src.source_name}.{src.name}")
                continue
            table_name, df = match
            used.add(table_name)
            df = df.copy()

            loader_cols = config.loader_columns.get(src.loader.lower(), {}) if src.loader else {}
            for col, _type in loader_cols.items():
                if col not in df.columns:
                    df[col] = _loader_values(col, table_name, len(df), config.seed)

            for col in _fill_json(df, schema, src, table_name, config.seed):
                summary.json_columns.append(f"{src.identifier}.{col}")
            _load(con, src.schema, src.identifier, df, schema.tables[table_name], loader_cols)
            summary.tables.append(
                LoadedTable(
                    source_name=src.source_name,
                    name=src.name,
                    identifier=src.identifier,
                    schema=src.schema,
                    rows=len(df),
                )
            )
    finally:
        con.close()

    summary.unused_dbml_tables = sorted(set(generated) - used)
    # model2data counted these as placeholder text; they hold JSON now.
    filled = {c.split(".", 1)[1] for c in summary.json_columns}
    summary.unmapped_columns = [(c, t) for c, t in summary.unmapped_columns if c not in filled]
    return summary


def _fill_json(
    df: pd.DataFrame, schema: ResolvedSchema, src: SourceTable, table_name: str, seed: int
) -> list[str]:
    """Replace, in place, the values of the columns read as JSON; return their names.

    Only a text column (or one typed `json`) qualifies: a column the schema types as a
    number or a date stays what the schema says, whatever a model does with it."""
    filled: list[str] = []
    for col in schema.tables[table_name].columns:
        base = col.data_type.strip().lower().split("(")[0]
        if base not in JSON_CAPABLE_TYPES or col.name not in df.columns:
            continue
        noted = parse_note(col.description)
        read = schema.json_reads.get((src.unique_id, col.name.lower()))
        if noted is None and read is None:
            continue
        shape = merge_shape(dict(noted or {}), read or {})
        present = df[col.name].notna().tolist()
        df[col.name] = json_values(shape, table_name, col.name, seed, present)
        filled.append(col.name)
    return filled


def _load(
    con: duckdb.DuckDBPyConnection,
    schema: str,
    identifier: str,
    df: pd.DataFrame,
    table_def,
    loader_cols: dict[str, str],
) -> None:
    types = {c.name: _duck_type(c.data_type) for c in table_def.columns}
    types.update({c: _duck_type(t) for c, t in loader_cols.items()})

    # Everything goes in as text first, then is cast once, so a generator that emits ISO
    # strings for timestamps and one that emits datetimes land identically.
    text_df = df.astype(object).where(pd.notna(df), None)
    for col in text_df.columns:
        text_df[col] = text_df[col].map(lambda v: None if v is None else str(v))

    con.execute(f'create schema if not exists "{schema}"')
    con.register("_preflight_src", text_df)
    select = ", ".join(
        f'try_cast("{col}" as {types.get(col, "VARCHAR")}) as "{col}"' for col in text_df.columns
    )
    con.execute(
        f'create or replace table "{schema}"."{identifier}" as select {select} from _preflight_src'
    )
    con.unregister("_preflight_src")
