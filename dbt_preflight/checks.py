"""House conventions, checked against the manifest and the built tables.

The rules are the JB Analytica warehouse conventions: staging / intermediate / marts
layering with the matching name prefixes, one source per staging model, every model
described, a tested primary key, snake_case columns, and type-revealing suffixes
(`_at`, `_date`, `is_` / `has_`). Column-type rules run against the DuckDB tables the
build produced, so they see the real output types rather than a YAML claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

from dbt_preflight.conventions import ERROR, WARN, ConventionSet, jba
from dbt_preflight.manifest import Manifest, ModelNode

SEVERITY_ERROR = ERROR
SEVERITY_WARN = WARN

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class Violation:
    rule: str
    severity: str
    model: str
    path: str  # repo-relative
    message: str


def relation(model: ModelNode) -> str:
    """The fully qualified, quoted DuckDB name of a model's table.

    Always three parts: when the catalog (the DuckDB file) and the schema share a name,
    a two-part name is ambiguous to DuckDB.
    """
    parts = [model.database, model.schema, model.alias]
    return ".".join(f'"{p}"' for p in parts if p)


def _path(model: ModelNode, project_relpath: str, yaml: bool = False) -> str:
    """Repo-relative path of the model's SQL, or of its YAML when the fix belongs there."""
    rel = (model.patch_path if yaml and model.patch_path else None) or model.original_file_path
    return f"{project_relpath}/{rel}" if project_relpath not in {"", "."} else rel


def check_manifest(
    manifest: Manifest,
    model_ids: list[str],
    project_relpath: str,
    conventions: ConventionSet | None = None,
) -> list[Violation]:
    c = conventions or jba()
    out: list[Violation] = []
    for uid in model_ids:
        model = manifest.models.get(uid)
        if model is None:
            continue
        path = _path(model, project_relpath)
        layer = model.layer

        pattern = c.pattern(layer)
        if c.enabled("naming") and pattern and not pattern.match(model.name):
            out.append(
                Violation(
                    "naming",
                    c.severity("naming"),
                    model.name,
                    path,
                    f"models in `{layer}/` are named {c.hints.get(layer, 'differently')}",
                )
            )

        if c.enabled("layering"):
            sources = [d for d in model.depends_on if d.startswith("source.")]
            models = [d for d in model.depends_on if d.startswith("model.")]
            sev = c.severity("layering")
            if c.source_layer and layer == c.source_layer:
                if len(sources) != 1:
                    out.append(
                        Violation(
                            "layering",
                            sev,
                            model.name,
                            path,
                            f"a {layer} model reads exactly one source; this one reads {len(sources)}",
                        )
                    )
                if models:
                    out.append(
                        Violation(
                            "layering",
                            sev,
                            model.name,
                            path,
                            f"a {layer} model does not `ref()` other models; joins belong downstream",
                        )
                    )
            elif c.source_layer and layer and sources:
                out.append(
                    Violation(
                        "layering",
                        sev,
                        model.name,
                        path,
                        f"only `{c.source_layer}/` reads `source()`; go through a "
                        f"{c.source_layer} model instead "
                        f"({', '.join(s.split('.')[-1] for s in sources)})",
                    )
                )

        yaml_path = _path(model, project_relpath, yaml=True)
        if c.enabled("description") and not model.description.strip():
            where = "in its YAML" if model.patch_path else "in a YAML entry (none exists yet)"
            out.append(
                Violation(
                    "description",
                    c.severity("description"),
                    model.name,
                    yaml_path,
                    f"`{model.name}` has no description; add one {where}",
                )
            )

        if c.enabled("primary_key"):
            tests = manifest.column_tests(uid)
            has_pk = any({"unique", "not_null"} <= names for names in tests.values())
            if not has_pk:
                out.append(
                    Violation(
                        "primary_key",
                        c.severity("primary_key"),
                        model.name,
                        yaml_path,
                        f"`{model.name}` has no column tested `unique` + `not_null`; "
                        "test its primary key",
                    )
                )
    return out


def check_columns(
    manifest: Manifest,
    model_ids: list[str],
    project_relpath: str,
    db_path,
    conventions: ConventionSet | None = None,
) -> list[Violation]:
    """Column naming rules, read from the tables the build produced."""
    c = conventions or jba()
    if not c.enabled("column_naming"):
        return []
    sev = c.severity("column_naming")
    out: list[Violation] = []
    con = duckdb.connect(str(db_path))
    try:
        for uid in model_ids:
            model = manifest.models.get(uid)
            # Only models inside a declared layer; utilities and the like are exempt.
            if model is None or (c.layers and model.layer not in c.layers):
                continue
            path = _path(model, project_relpath)
            rows = con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_catalog = coalesce(?, table_catalog) "
                "and table_schema = ? and table_name = ?",
                [model.database, model.schema, model.alias],
            ).fetchall()
            for column, data_type in rows:
                dtype = str(data_type).upper()
                if not _SNAKE.match(column):
                    out.append(
                        Violation(
                            "column_naming",
                            sev,
                            model.name,
                            path,
                            f"column `{column}` is not snake_case",
                        )
                    )
                    continue
                if dtype.startswith("TIMESTAMP") and not column.endswith(c.timestamp_suffix):
                    out.append(
                        Violation(
                            "column_naming",
                            sev,
                            model.name,
                            path,
                            f"timestamp column `{column}` should end in `{c.timestamp_suffix}`",
                        )
                    )
                elif dtype == "DATE" and not column.endswith(c.date_suffix):
                    out.append(
                        Violation(
                            "column_naming",
                            sev,
                            model.name,
                            path,
                            f"date column `{column}` should end in `{c.date_suffix}`",
                        )
                    )
                elif dtype == "BOOLEAN" and not column.startswith(c.boolean_prefixes):
                    prefixes = " or ".join(f"`{p}`" for p in c.boolean_prefixes)
                    out.append(
                        Violation(
                            "column_naming",
                            sev,
                            model.name,
                            path,
                            f"boolean column `{column}` should read as a claim: {prefixes}",
                        )
                    )
    finally:
        con.close()
    return out


def row_counts(manifest: Manifest, model_ids: list[str], db_path) -> dict[str, int]:
    counts: dict[str, int] = {}
    con = duckdb.connect(str(db_path))
    try:
        for uid in model_ids:
            model = manifest.models.get(uid)
            if model is None:
                continue
            try:
                row = con.execute(f"select count(*) from {relation(model)}").fetchone()
            except duckdb.Error:
                continue
            if row:
                counts[uid] = int(row[0])
    finally:
        con.close()
    return counts
