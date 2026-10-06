"""`dbt-preflight schema`: the derived schema written as a file to keep, and the promise that
a run reading that file builds the same fixtures a run deriving it does."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest
from model2data.parse.dbml import parse_dbml
from typer.testing import CliRunner

from dbt_preflight.cli import app
from dbt_preflight.fixtures import FixtureSummary, LoadedTable
from dbt_preflight.report import PreflightReport, render
from dbt_preflight.schema import InferredSource
from dbt_preflight.schema_file import NOTE_CAST, NOTE_COMPILED, NOTE_GUESSED, annotate
from dbt_preflight.summary import SCHEMA_VERSION, build_summary

FIXTURES = Path(__file__).parent / "fixtures"
POINTER = "Columns were guessed for"


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    """A project whose sources declare no columns: types come from staging casts and names."""
    repo = tmp_path / "shop"
    shutil.copytree(FIXTURES / "shop_schemaless", repo)
    return repo


@pytest.fixture
def billing(tmp_path: Path) -> Path:
    """The Fivetran-style project: a macro hides the source from the raw SQL."""
    repo = tmp_path / "billing"
    shutil.copytree(FIXTURES / "source_or_empty", repo)
    return repo


def _schema(repo: Path, *args: str):
    return CliRunner().invoke(app, ["schema", "--repo-root", str(repo), *args])


def _run(repo: Path, tmp_path: Path, *args: str) -> tuple[str, dict]:
    comment, summary = tmp_path / "comment.md", tmp_path / "summary.json"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo-root",
            str(repo),
            "--comment-file",
            str(comment),
            "--summary-file",
            str(summary),
            "--keep-workdir",
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    return comment.read_text(), json.loads(summary.read_text())


def _source_tables(repo: Path) -> dict[str, tuple[list[tuple[str, str]], list[tuple]]]:
    """Every generated source table of the last kept run: its column types and its rows."""
    (db_file,) = (repo / ".preflight").glob("*.duckdb")
    # dbt-duckdb still holds the file open in this process: read a copy.
    copy = repo.parent / "inspect.duckdb"
    shutil.copy(db_file, copy)
    con = duckdb.connect(str(copy), read_only=True)
    try:
        tables = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema like 'raw_%' order by 1, 2"
        ).fetchall()
        out = {}
        for schema, table in tables:
            cols = con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = ? and table_name = ? order by ordinal_position",
                [schema, table],
            ).fetchall()
            rows = con.execute(f'select * from "{schema}"."{table}" order by all').fetchall()
            out[f"{schema}.{table}"] = (cols, rows)
        return out
    finally:
        con.close()


# --- the command ---------------------------------------------------------------------


def test_writes_the_schema_with_a_note_per_untyped_column(shop: Path) -> None:
    result = _schema(shop)
    assert result.exit_code == 0, result.output
    written = shop / "source_system" / "shop.dbml"
    text = written.read_text()

    assert "Do not edit" not in text
    # Typed by a staging cast, then by name: each says which.
    assert f"signed_up date [note: '{NOTE_CAST}']" in text
    assert f"email varchar [note: '{NOTE_GUESSED}']" in text
    assert f"id int [pk, note: '{NOTE_GUESSED}']" in text
    assert f"customer_id int [ref: > customers.id, note: '{NOTE_GUESSED}']" in text
    # model2data reads these as descriptions, never as generation hints.
    tables, _ = parse_dbml(written)
    column = next(c for c in tables["customers"].columns if c.name == "email")
    assert column.note is None
    assert column.description == NOTE_GUESSED

    assert "source_system/shop.dbml" in result.output
    assert "schema: source_system/shop.dbml" in result.output
    assert ".dbt-preflight.yml" in result.output
    assert "Commit the file" in result.output
    assert "model2data studio" in result.output
    assert not (shop / ".preflight").exists()


def test_compiled_columns_say_so(billing: Path) -> None:
    assert _schema(billing).exit_code == 0
    text = (billing / "source_system" / "billing.dbml").read_text()
    assert f"number varchar [pk, note: '{NOTE_COMPILED}']" in text
    # Typed by sources.yml: no note.
    assert "  creditor_number varchar [unique, not null]\n" in text


def test_output_is_deterministic(shop: Path, tmp_path: Path) -> None:
    assert _schema(shop, "--output", str(tmp_path / "a.dbml")).exit_code == 0
    assert _schema(shop, "--output", str(tmp_path / "b.dbml")).exit_code == 0
    assert (tmp_path / "a.dbml").read_bytes() == (tmp_path / "b.dbml").read_bytes()


def test_refuses_to_overwrite_without_force(shop: Path) -> None:
    target = shop / "source_system" / "shop.dbml"
    target.parent.mkdir()
    target.write_text("// mine\n")
    result = _schema(shop)
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert target.read_text() == "// mine\n"

    assert _schema(shop, "--force").exit_code == 0
    assert "Table customers" in target.read_text()


def test_output_flag_chooses_the_path(shop: Path, tmp_path: Path) -> None:
    out = tmp_path / "elsewhere" / "src.dbml"
    result = _schema(shop, "--output", str(out))
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert not (shop / "source_system").exists()


def test_an_existing_schema_in_the_config_is_respected(shop: Path) -> None:
    mine = shop / "mine.dbml"
    mine.write_text("Table customers {\n  id int [pk]\n}\n")
    (shop / ".dbt-preflight.yml").write_text("schema: mine.dbml\n")

    result = _schema(shop)
    assert result.exit_code == 0
    assert "already points at mine.dbml" in result.output
    assert not (shop / "source_system").exists()
    assert mine.read_text() == "Table customers {\n  id int [pk]\n}\n"

    forced = _schema(shop, "--force")
    assert forced.exit_code == 0, forced.output
    assert (shop / "source_system" / "shop.dbml").exists()
    assert "unchanged" in forced.output
    assert mine.read_text() == "Table customers {\n  id int [pk]\n}\n"


def test_a_project_without_sources_is_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "seeds"
    shutil.copytree(FIXTURES / "seed_only", repo)
    result = _schema(repo)
    assert result.exit_code == 1
    assert "no sources" in result.output
    assert not (repo / "source_system").exists()


def test_annotate_leaves_a_fully_typed_source_alone() -> None:
    from dbt_preflight.manifest import Manifest

    raw = {
        "sources": {
            "source.p.s.t": {
                "source_name": "s",
                "name": "t",
                "identifier": "t",
                "database": "d",
                "schema": "raw",
                "columns": {"id": {"name": "id", "data_type": "int64"}},
            }
        },
        "nodes": {},
    }
    dbml = "// Derived by dbt-preflight from the project's sources.yml. Do not edit.\n\nTable t {\n  id int [pk]\n}\n"
    out = annotate(dbml, Manifest.from_dict(raw), [])
    assert "note:" not in out
    assert "  id int [pk]\n" in out


# --- the round trip ------------------------------------------------------------------


def _round_trip(repo: Path, tmp_path: Path, config_text: str = "") -> None:
    if config_text:
        (repo / ".dbt-preflight.yml").write_text(config_text)
    derived_comment, derived = _run(repo, tmp_path)
    derived_tables = _source_tables(repo)
    assert derived_tables

    assert _schema(repo).exit_code == 0
    (name,) = (repo / "source_system").glob("*.dbml")
    with (repo / ".dbt-preflight.yml").open("a") as f:
        f.write(f"schema: source_system/{name.name}\n")

    kept_comment, kept = _run(repo, tmp_path)
    assert kept["fixtures"]["tables"] == derived["fixtures"]["tables"]
    assert _source_tables(repo) == derived_tables
    assert kept["verdict"] == derived["verdict"]
    assert kept["counts"]["models"] == derived["counts"]["models"]
    assert "Columns inferred" not in kept_comment
    assert POINTER not in kept_comment
    assert kept["fixtures"]["guessed_sources"] == 0


def test_round_trip_small_project(shop: Path, tmp_path: Path) -> None:
    _round_trip(shop, tmp_path)


def test_round_trip_fivetran_style_project(billing: Path, tmp_path: Path) -> None:
    _round_trip(billing, tmp_path)


# --- the comment line and the summary field -------------------------------------------


def test_comment_points_at_the_command_only_when_something_was_guessed(
    shop: Path, billing: Path, tmp_path: Path
) -> None:
    body, summary = _run(shop, tmp_path)
    assert (
        "Columns were guessed for 2 sources. Keep and refine the schema with "
        "`dbt-preflight schema`, then edit it in "
        "[model2data studio](https://studio.jbanalytica.com/?ref=dbt-preflight)." in body
    )
    # Outside the folded fixtures block, where a reviewer sees it.
    assert body.index(POINTER) < body.index("<summary>Fixtures</summary>")
    assert summary["fixtures"]["guessed_sources"] == 2
    assert summary["schema_version"] == SCHEMA_VERSION == 2

    # Types read from compiled SQL are not guesses: a clean run says nothing.
    body, summary = _run(billing, tmp_path)
    assert POINTER not in body
    assert summary["fixtures"]["guessed_sources"] == 0


def _report(*inferred: InferredSource) -> PreflightReport:
    return PreflightReport(
        fixtures=FixtureSummary(
            tables=[LoadedTable("s", "t", "t", "raw", 10)], inferred_sources=list(inferred)
        ),
        schema_source="sources.yml (derived)",
    )


def _inferred(table: str, guessed: list[str]) -> InferredSource:
    return InferredSource("s", table, table, ["stg"], 3, guessed)


def test_pointer_counts_sources_with_guesses_and_nothing_else() -> None:
    assert POINTER not in render(_report())
    assert POINTER not in render(_report(_inferred("a", [])))
    one = render(_report(_inferred("a", ["x"]), _inferred("b", [])))
    assert "Columns were guessed for 1 source. " in one
    two = render(_report(_inferred("a", ["x"]), _inferred("b", ["y", "z"])))
    assert "Columns were guessed for 2 sources. " in two


def test_pointer_url_carries_no_schema_content() -> None:
    body = render(_report(_inferred("secret_table", ["secret_column"])))
    (line,) = [ln for ln in body.splitlines() if ln.startswith(POINTER)]
    assert "https://studio.jbanalytica.com/?ref=dbt-preflight)" in line
    assert "secret" not in line


def test_summary_carries_guessed_sources_additively() -> None:
    summary = build_summary(_report(_inferred("a", ["x"]), _inferred("b", [])), 0, None)
    assert summary["fixtures"]["guessed_sources"] == 1
    assert summary["schema_version"] == SCHEMA_VERSION
    assert json.loads(json.dumps(summary)) == summary
