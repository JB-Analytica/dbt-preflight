"""model2data's own warnings, surfaced onto the FixtureSummary."""

from __future__ import annotations

from pathlib import Path

from dbt_preflight.config import PreflightConfig
from dbt_preflight.fixtures import build_fixtures
from dbt_preflight.manifest import Manifest, SourceTable
from dbt_preflight.schema import resolve_schema


def _config(tmp_path: Path) -> PreflightConfig:
    return PreflightConfig(repo_root=tmp_path, project_dir=tmp_path, rows=5, seed=1)


def _source(identifier: str = "widgets") -> SourceTable:
    return SourceTable(
        unique_id=f"source.p.raw.{identifier}",
        source_name="raw",
        name=identifier,
        identifier=identifier,
        database=None,
        schema="raw",
        loader="",
    )


def test_unmapped_columns_are_collected(tmp_path: Path) -> None:
    """A column model2data cannot map to a generator is reported, not silently guessed."""
    dbml = tmp_path / "shop.dbml"
    dbml.write_text("Table widgets {\n  id int [pk]\n  zorb_flonk varchar\n}\n")
    manifest = Manifest(sources={}, models={}, tests={})
    schema = resolve_schema(dbml, manifest, tmp_path / "work")

    summary = build_fixtures(_config(tmp_path), schema, [_source()], tmp_path / "db.duckdb")

    assert summary.unmapped_columns == [("zorb_flonk", "varchar")]
    assert summary.cyclic_tables == []
    assert summary.unresolved_composite_keys == []


def test_stats_do_not_leak_between_runs(tmp_path: Path) -> None:
    """reset_stats() runs before generation, so an earlier run's warnings do not linger."""
    dbml = tmp_path / "shop.dbml"
    dbml.write_text("Table widgets {\n  id int [pk]\n  zorb_flonk varchar\n}\n")
    manifest = Manifest(sources={}, models={}, tests={})
    schema = resolve_schema(dbml, manifest, tmp_path / "work")
    build_fixtures(_config(tmp_path), schema, [_source()], tmp_path / "db1.duckdb")

    clean_dbml = tmp_path / "clean.dbml"
    clean_dbml.write_text("Table clean {\n  id int [pk]\n  email varchar\n}\n")
    clean_schema = resolve_schema(clean_dbml, manifest, tmp_path / "work2")
    summary = build_fixtures(
        _config(tmp_path), clean_schema, [_source("clean")], tmp_path / "db2.duckdb"
    )

    assert summary.unmapped_columns == []


def test_inferred_sources_carried_onto_the_summary(tmp_path: Path) -> None:
    """The schema's own inference record ends up on the FixtureSummary untouched."""
    dbml = tmp_path / "shop.dbml"
    dbml.write_text("Table widgets {\n  id int [pk]\n  name varchar\n}\n")
    manifest = Manifest(sources={}, models={}, tests={})
    schema = resolve_schema(dbml, manifest, tmp_path / "work")

    summary = build_fixtures(_config(tmp_path), schema, [_source()], tmp_path / "db.duckdb")

    assert summary.inferred_sources == schema.inferred == []
