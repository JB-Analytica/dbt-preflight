"""Turn the resolved schema into source tables in a DuckDB file.

model2data does the generation; this module maps its output onto the sources the dbt
project actually declares (database, schema, identifier), adds the columns a loader would
have stamped on, casts everything to the DBML type, and loads it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from model2data.generate.core import generate_data_from_dbml
from model2data.utils import normalize_identifier

from dbt_preflight.config import PreflightConfig
from dbt_preflight.manifest import SourceTable
from dbt_preflight.schema import ResolvedSchema

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
    summary = FixtureSummary()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        for src in sources:
            key = normalize_identifier(src.identifier)
            match = by_key.get(key) or by_key.get(normalize_identifier(src.name))
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
    return summary


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
