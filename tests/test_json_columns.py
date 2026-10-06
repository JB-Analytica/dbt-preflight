"""Columns a project reads as JSON: detection in raw and compiled SQL, tracing back to the
source, the note in the derived DBML, the generated values, and the fallback for a model
that fails on a value preflight generated."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest
import sqlglot
from typer.testing import CliRunner

from dbt_preflight.baseline import fixture_shaped_error
from dbt_preflight.cli import app
from dbt_preflight.compiled import CompiledSql, json_compile_selection
from dbt_preflight.json_columns import (
    format_note,
    json_reads_in_tree,
    json_values,
    parse_note,
)
from dbt_preflight.manifest import Manifest
from dbt_preflight.report import render
from dbt_preflight.schema import derive_dbml, json_reads
from dbt_preflight.schema_file import NOTE_GUESSED, annotate, count_notes
from dbt_preflight.summary import build_summary

SRC = "source.p.shop.parcels"
STG = "model.p.stg_shop__parcels"
MART = "model.p.parcel_weights"


def _reads(sql: str, dialect: str | None = "duckdb") -> dict:
    return json_reads_in_tree(sqlglot.parse_one(sql, read=dialect))


# --- reading the SQL ------------------------------------------------------------------


def test_duckdb_functions_and_operators_are_read_with_their_paths() -> None:
    reads = _reads(
        "select json_extract(a, '$.address.city'), json_extract_string(b, '$.weight'), "
        "c ->> 'colour', d -> '$.items[0]' ->> 'sku', e::json, json_valid(f), "
        "json_keys(g), from_json(h, '{}'), json_array_length(i, '$.tags') from t"
    )
    assert reads == {
        "a": {"address.city": None},
        "b": {"weight": None},
        "c": {"colour": None},
        "d": {"items[].sku": None},
        "e": {"": None},
        "f": {"": None},
        "g": {"": None},
        "h": {"": None},
        "i": {"tags[]": None},
    }


def test_a_cast_after_the_extraction_types_the_leaf() -> None:
    reads = _reads(
        "select json_extract_string(p, '$.weight')::double, "
        "coalesce(cast(nullif(json_extract_string(p, '$.charges.data[0].rate'), '') "
        "as numeric(28,6)), 1), cast(p ->> 'count' as integer), "
        "cast(json_extract_string(p, '$.paid') as boolean), "
        "cast(json_extract_string(p, '$.sent_at') as timestamp), "
        "json_extract_string(p, '$.label') from t"
    )
    assert reads == {
        "p": {
            "weight": "number",
            "charges.data[].rate": "number",
            "count": "integer",
            "paid": "boolean",
            "sent_at": "timestamp",
            "label": None,
        }
    }


def test_bigquery_and_default_dialect_spellings() -> None:
    assert _reads(
        "select json_extract_scalar(p, '$.a.b'), json_value(q, '$.c'), parse_json(r) from t",
        "bigquery",
    ) == {"p": {"a.b": None}, "q": {"c": None}, "r": {"": None}}
    # The default dialect leaves DuckDB's names anonymous; the raw SQL is parsed that way.
    assert _reads("select json_extract_string(p, '$.x.y[*]') from t", None) == {
        "p": {"x.y[]": None}
    }


def test_a_column_that_is_not_the_json_argument_is_not_read() -> None:
    assert _reads("select json_extract_string('{\"a\": 1}', k), upper(p) from t") == {}


# --- tracing back to the source ---------------------------------------------------------


def _manifest(stg_sql: str, mart_sql: str, columns: dict | None = None) -> Manifest:
    def model(uid: str, sql: str, deps: list[str]) -> dict:
        name = uid.split(".")[-1]
        return {
            "resource_type": "model",
            "name": name,
            "path": f"{name}.sql",
            "original_file_path": f"models/{name}.sql",
            "database": "preflight",
            "schema": "main",
            "alias": name,
            "depends_on": {"nodes": deps},
            "config": {"materialized": "view"},
            "columns": {},
            "raw_code": sql,
            "relation_name": f'"preflight"."main"."{name}"',
        }

    return Manifest.from_dict(
        {
            "sources": {
                SRC: {
                    "source_name": "shop",
                    "name": "parcels",
                    "identifier": "parcels",
                    "database": "preflight",
                    "schema": "raw_shop",
                    "relation_name": '"preflight"."raw_shop"."parcels"',
                    "columns": columns
                    if columns is not None
                    else {
                        "id": {"name": "id", "data_type": "integer"},
                        "payload": {"name": "payload", "data_type": "varchar"},
                        "size": {"name": "size", "data_type": "integer"},
                    },
                }
            },
            "nodes": {
                STG: model(STG, stg_sql, [SRC]),
                MART: model(MART, mart_sql, [STG]),
            },
            "parent_map": {STG: [SRC], MART: [STG]},
            "child_map": {SRC: [STG], STG: [MART], MART: []},
        }
    )


STG_SQL = (
    "select id as parcel_id, cast(payload as varchar) as parcel_payload, size\n"
    "from {{ source('shop', 'parcels') }}"
)
MART_SQL = (
    "with p as (select * from {{ ref('stg_shop__parcels') }})\n"
    "select parcel_id, json_extract_string(parcel_payload, '$.address.city') as city,\n"
    "  cast(json_extract_string(parcel_payload, '$.weight') as double) as weight\n"
    "from p"
)


def test_a_staging_alias_is_traced_back_to_the_source_column() -> None:
    reads = json_reads(_manifest(STG_SQL, MART_SQL))
    assert reads[(SRC, "payload")] == {"address.city": None, "weight": "number"}
    # The alias is a candidate too; the caller drops it, as the source has no such column.
    assert set(reads) <= {(SRC, "payload"), (SRC, "parcel_payload")}


def test_a_read_hidden_by_a_macro_is_found_in_the_compiled_sql() -> None:
    mart_raw = (
        "select cast({{ fivetran_utils.json_parse('parcel_payload', ['weight']) }} as float) "
        "as weight from {{ ref('stg_shop__parcels') }}"
    )
    manifest = _manifest(STG_SQL, mart_raw)
    assert json_reads(manifest) == {}
    assert json_compile_selection(manifest) == [MART]
    compiled = CompiledSql(
        code={
            MART: "select cast(json_extract_string(parcel_payload, '$.weight') as float) "
            'as weight from "preflight"."main"."stg_shop__parcels"'
        },
        relations={
            SRC: ("preflight", "raw_shop", "parcels"),
            STG: ("preflight", "main", "stg_shop__parcels"),
            MART: ("preflight", "main", "parcel_weights"),
        },
    )
    assert json_reads(manifest, compiled)[(SRC, "payload")] == {"weight": "number"}


def test_a_computed_column_is_not_traced_through() -> None:
    stg = "select id, concat('{', size, '}') as payload_text from {{ source('shop', 'parcels') }}"
    mart = "select json_extract(payload_text, '$.a') from {{ ref('stg_shop__parcels') }}"
    assert json_reads(_manifest(stg, mart)) == {}


def test_the_derived_schema_notes_the_json_column_only() -> None:
    dbml, _ = derive_dbml(_manifest(STG_SQL, MART_SQL))
    assert "  payload varchar [note: 'JSON, keys read: address.city, weight (number)']" in dbml
    assert "  size int\n" in dbml or "  size int" in dbml.splitlines()


def test_a_json_read_on_a_number_column_is_left_alone() -> None:
    stg = "select json_extract(size, '$.a') as a, payload from {{ source('shop', 'parcels') }}"
    dbml, _ = derive_dbml(_manifest(stg, "select 1"))
    assert "note:" not in dbml


def test_an_untyped_json_column_is_inferred_as_text_whatever_its_name() -> None:
    columns: dict = {}
    stg = (
        "select id, json_extract_string(shipped_at, '$.when') as w "
        "from {{ source('shop', 'parcels') }}"
    )
    dbml, inferred = derive_dbml(_manifest(stg, "select 1", columns))
    assert "  shipped_at varchar [note: 'JSON, keys read: when']" in dbml
    assert "shipped_at" in inferred[0].guessed_columns


def test_schema_command_note_shares_the_json_note() -> None:
    columns: dict = {}
    stg = "select id, payload ->> 'weight' as w from {{ source('shop', 'parcels') }}"
    manifest = _manifest(stg, "select 1", columns)
    dbml, inferred = derive_dbml(manifest)
    written = annotate(dbml, manifest, inferred)
    assert f"  payload varchar [note: '{NOTE_GUESSED}; JSON, keys read: weight']" in written
    assert count_notes(written)[NOTE_GUESSED] == 2  # `id` and `payload`
    assert parse_note(f"{NOTE_GUESSED}; JSON, keys read: weight") == {"": None, "weight": None}


# --- the note and the values ------------------------------------------------------------


def test_note_round_trips() -> None:
    shape = {"address.city": None, "items[].price": "number", "": None}
    note = format_note(shape)
    assert note == "JSON, keys read: address.city, items[].price (number)"
    assert parse_note(note) == shape
    assert parse_note("JSON") == {"": None}
    assert parse_note("Free text about the column") is None
    assert parse_note(None) is None


def test_values_are_valid_json_with_every_path() -> None:
    shape = {"address.city": None, "weight": "number", "items[].sku": None, "paid": "boolean"}
    values = json_values(shape, "parcels", "payload", 42, [True] * 50)
    for text in values:
        assert text is not None
        obj = json.loads(text)
        assert isinstance(obj["address"]["city"], str)
        assert isinstance(obj["weight"], float | int)
        assert isinstance(obj["paid"], bool)
        assert 1 <= len(obj["items"]) <= 3
        assert all(isinstance(i["sku"], str) for i in obj["items"])


def test_values_are_deterministic_and_seeded() -> None:
    shape = {"a.b": "integer"}
    first = json_values(shape, "parcels", "payload", 42, [True] * 20)
    assert json_values(shape, "parcels", "payload", 42, [True] * 20) == first
    assert json_values(shape, "parcels", "payload", 43, [True] * 20) != first
    assert json_values(shape, "parcels", "other", 42, [True] * 20) != first


def test_values_keep_the_null_rate() -> None:
    present = [i % 4 != 0 for i in range(40)]
    values = json_values({"x": None}, "t", "c", 1, present)
    assert [v is not None for v in values] == present


def test_nothing_read_but_the_whole_value_is_still_distinct_per_row() -> None:
    assert json_values({"": None}, "t", "c", 1, [True, True]) == ['{"id":1}', '{"id":2}']


def test_values_are_unique_per_row() -> None:
    # A source `unique` test on the JSON column, or on an id extracted from it, holds.
    for shape in ({"name": None}, {"id": "integer"}, {"amount": "number"}):
        values = json_values(shape, "t", "c", 7, [True] * 5000)
        assert len(set(values)) == 5000


def test_new_keys_are_null() -> None:
    [text] = json_values({"amount": "number", "amout": "number"}, "t", "c", 1, [True], {"amout"})
    obj = json.loads(text)
    assert obj["amout"] is None and isinstance(obj["amount"], float)


def test_a_note_saying_not_json_opts_out() -> None:
    from dbt_preflight.json_columns import opted_out

    assert opted_out("free text, not JSON")
    assert not opted_out("JSON, keys read: a")


def test_a_qualified_column_fills_only_its_own_source() -> None:
    other = "source.p.shop.customers"
    manifest = _manifest(
        "select o.id, json_extract_string(o.payload, '$.weight') as w\n"
        "from {{ source('shop', 'parcels') }} as o\n"
        "join {{ source('shop', 'customers') }} as c on c.id = o.id",
        "select 1",
    )
    raw = manifest.models[STG]
    raw.depends_on.append(other)
    manifest.sources[other] = type(manifest.sources[SRC])(
        **{**manifest.sources[SRC].__dict__, "unique_id": other, "name": "customers",
           "identifier": "customers"}
    )  # fmt: skip
    reads = json_reads(manifest)
    assert (SRC, "payload") in reads
    assert (other, "payload") not in reads


# --- the fallback for errors about generated values -------------------------------------


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            "Invalid Input Error: Malformed JSON at byte 0 of input: unexpected character.  "
            'Input: "Weight reason."',
            "malformed JSON",
        ),
        (
            'Conversion Error: invalid timestamp field format: "Do scene important.", '
            "expected format is (YYYY-MM-DD HH:MM:SS)",
            "an invalid timestamp",
        ),
        ('Conversion Error: invalid date field format: "abc"', "an invalid date"),
        ("Conversion Error: Could not convert string 'x' to INT64", "a failed cast"),
        (
            'Invalid Input Error: Could not parse string "abc" according to format specifier '
            '"%Y-%m-%d"',
            "a date or time format it could not parse",
        ),
    ],
)
def test_fixture_shaped_errors_are_recognised(message: str, reason: str) -> None:
    assert fixture_shaped_error(f"Runtime Error in model m (models/m.sql)\n  {message}") == reason


@pytest.mark.parametrize(
    "message",
    [
        'Binder Error: Referenced column "x" not found in FROM clause!',
        "Binder Error: Cannot mix values of type VARCHAR and FLOAT in COALESCE operator",
        "Compilation Error in model m: Malformed JSON in a macro argument",
        "Catalog Error: Table with name nowhere does not exist!",
        None,
    ],
)
def test_other_errors_are_not_fixture_shaped(message: str | None) -> None:
    assert fixture_shaped_error(message) is None


# --- end to end ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def parcels(tmp_path: Path) -> Path:
    """Sources typed in sources.yml; a mart parses `payload` as JSON, another casts a text
    column to an integer, which no generated text survives."""
    repo = tmp_path / "parcels"
    _write(
        repo,
        "dbt_project.yml",
        'name: parcels\nversion: "1.0.0"\nconfig-version: 2\nprofile: parcels\n'
        'model-paths: ["models"]\nflags:\n  send_anonymous_usage_stats: false\n'
        # Tables, not views: a view is created without reading a row.
        "models:\n  parcels:\n    +materialized: table\n",
    )
    _write(
        repo,
        "models/staging/_sources.yml",
        "version: 2\nsources:\n  - name: shop\n    schema: raw\n    tables:\n"
        "      - name: parcels\n        columns:\n"
        "          - name: id\n            data_type: integer\n"
        "          - name: payload\n            data_type: string\n"
        "          - name: label\n            data_type: string\n",
    )
    _write(
        repo,
        "models/staging/stg_parcels.sql",
        "select id, payload, label from {{ source('shop', 'parcels') }}\n",
    )
    _write(
        repo,
        "models/marts/parcel_weights.sql",
        "select id,\n"
        "  json_extract_string(payload, '$.weight')::double as weight,\n"
        "  json_extract_string(payload, '$.address.city') as city\n"
        "from {{ ref('stg_parcels') }}\n",
    )
    _write(
        repo,
        "models/marts/label_numbers.sql",
        "select cast(label as integer) as n from {{ ref('stg_parcels') }}\n",
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "change")
    stg = repo / "models/staging/stg_parcels.sql"
    stg.write_text("-- touched\n" + stg.read_text())
    _git(repo, "commit", "-qam", "touch staging")
    return repo


def test_a_model_parsing_json_builds_on_both_sides(parcels: Path, tmp_path: Path) -> None:
    comment, summary_file = tmp_path / "comment.md", tmp_path / "summary.json"
    CliRunner().invoke(
        app,
        [
            "run", "--base-ref", "main", "--repo-root", str(parcels),
            "--comment-file", str(comment), "--summary-file", str(summary_file),
        ],
    )  # fmt: skip
    summary = json.loads(summary_file.read_text())
    body = comment.read_text()
    statuses = {m["name"]: m["status"] for m in summary["models"]}
    assert statuses["parcel_weights"] == "built"
    assert summary["fixtures"]["json_columns"] == ["parcels.payload"]
    assert "Filled with JSON" in body
    assert "`parcels.payload`" in body

    # The cast of generated text fails on both branches: not "broken on main".
    assert summary["broken_on_base_models"] == []
    [unverified] = summary["unverified_broken_on_base_models"]
    assert unverified["name"] == "label_numbers"
    assert unverified["fixture_error"] == "a failed cast"
    assert "on a value preflight generated (a failed cast)" in body
    assert summary["verdict"] == "failed"


# --- reached or not: who answers for a failure on preflight's data ------------------------

CUST, ORDERS, MART = (
    "model.p.stg_shop__customers",
    "model.p.stg_shop__orders",
    "model.p.dim_customers",
)
JSON_ERROR = (
    'Runtime Error\n  Invalid Input Error: Malformed JSON at byte 0. Input: "Weight reason."'
)
BINDER_ERROR = 'Runtime Error\n  Binder Error: Referenced column "x" not found in FROM clause!'


def _judge(manifest: Manifest, failing: dict[str, str], modified: set[str], **kw):
    """Judge head failures that fail the same way on the base, given what was modified."""
    from dbt_preflight.cli import _BaseBuild, _judge_builds, _tests_changed_on, _Trust
    from dbt_preflight.dbt_runner import NodeResult, RunOutcome
    from dbt_preflight.report import ModelReport, PreflightReport

    def result(uid: str, status: str, message: str = "") -> NodeResult:
        return NodeResult(uid, uid.split(".")[-1], "model", status, message, None, 0.0)

    statuses = kw.get("statuses", {})
    results = [
        result(uid, "error", failing[uid])
        if uid in failing
        else result(uid, statuses.get(uid, "success"))
        for uid in (CUST, ORDERS, MART)
    ]
    report = PreflightReport(
        models=[
            ModelReport(
                r.unique_id,
                r.name,
                "",
                {"error": "failed", "success": "built"}.get(r.status, r.status),
                r.unique_id in modified,
                message=r.message,
            )
            for r in results
        ],
        base_ref="main",
    )
    descendants = modified | manifest.descendants(modified)
    trust = _Trust(
        modified=modified,
        fixture_bound=set(),
        untrusted=set(modified),
        changed_upstream=descendants,
        guess_bound=kw.get("guess_bound", {}),
        tested_by_change=_tests_changed_on(manifest, modified),
    )
    base = _BaseBuild(manifest=manifest, tables={r.unique_id: r for r in results}, trust=trust)
    _judge_builds(report, manifest, RunOutcome(True, results), base)
    return report, {m.unique_id: m for m in report.models}


def test_unreached_failure_on_generated_data_is_a_warning(manifest: Manifest) -> None:
    report, m = _judge(manifest, {CUST: JSON_ERROR}, modified={ORDERS})
    assert m[CUST].fixture_limited and not m[CUST].unverified_broken_on_base
    assert not m[CUST].broken_on_base
    assert m[CUST].fixture_error == "malformed JSON"
    assert report.failed_models == []
    assert report.passed and report.has_warnings
    body = render(report)
    assert "Preflight's generated data cannot build this model" in body
    assert "`stg_shop__customers` — malformed JSON:" in body


def test_reached_failure_on_generated_data_could_not_be_checked(manifest: Manifest) -> None:
    report, m = _judge(manifest, {MART: JSON_ERROR}, modified={ORDERS})
    assert m[MART].unverified_broken_on_base and not m[MART].fixture_limited
    assert m[MART].reached_from == ["stg_shop__orders"]
    assert not report.passed
    body = render(report)
    assert (
        "fails on a value preflight generated (malformed JSON), and this change reaches it" in body
    )
    assert "this change reaches them from upstream" not in body  # the intro is generic


def test_unreached_other_failure_is_broken_on_main(manifest: Manifest) -> None:
    report, m = _judge(manifest, {CUST: BINDER_ERROR}, modified={ORDERS})
    assert m[CUST].broken_on_base and not m[CUST].fixture_limited
    assert report.passed


def test_unreached_failure_over_a_guessed_column_is_a_warning(manifest: Manifest) -> None:
    report, m = _judge(
        manifest, {CUST: BINDER_ERROR}, modified={ORDERS}, guess_bound={CUST: ["customers.email"]}
    )
    assert m[CUST].fixture_limited and m[CUST].fixture_error is None
    assert report.passed
    assert "reads `customers.email`, whose type preflight guessed" in render(report)
    summary = build_summary(report, 0, None)
    [entry] = summary["fixture_limited_models"]
    assert entry["reason"] == "reads `customers.email`, whose type preflight guessed"
    assert summary["counts"]["models"]["fixture_limited"] == 1


def test_what_an_unbuildable_model_skips_is_not_counted(manifest: Manifest) -> None:
    report, m = _judge(manifest, {CUST: JSON_ERROR}, modified=set(), statuses={MART: "skipped"})
    assert m[CUST].fixture_limited
    assert m[MART].skipped_by_fixture_limited
    assert report.passed
    assert "Skipped because of it: `dim_customers`." in render(report)


def test_reached_failure_over_a_guessed_column_could_not_be_checked(manifest: Manifest) -> None:
    report, m = _judge(
        manifest, {MART: BINDER_ERROR}, modified={ORDERS}, guess_bound={MART: ["orders.x"]}
    )
    assert m[MART].unverified_broken_on_base and not m[MART].fixture_limited
    assert not report.passed
    assert "reads `orders.x`, whose type preflight guessed" in render(report)


@pytest.mark.parametrize("error", [JSON_ERROR, BINDER_ERROR])
def test_a_dependant_the_change_reaches_is_not_excused_by_an_unreached_parent(
    manifest: Manifest, error: str
) -> None:
    # The diamond: the change modifies stg_shop__orders, which builds; stg_shop__customers
    # is unreached and fails on both sides; dim_customers reads both and is skipped on both.
    # It is reached through orders, so it counts, whichever category its other parent is in.
    report, m = _judge(manifest, {CUST: error}, modified={ORDERS}, statuses={MART: "skipped"})
    assert m[CUST].fixture_limited or m[CUST].broken_on_base
    assert not m[MART].skipped_by_fixture_limited and not m[MART].skipped_by_base
    assert m[MART].skipped_unchecked
    assert not report.passed
    body = render(report)
    assert "counted against this pull request because the change reaches it" in body
    assert "Unchanged models this change breaks" not in body


def test_a_model_whose_test_the_change_edits_is_reached(manifest: Manifest) -> None:
    # The change edits the `unique` test on stg_shop__customers and nothing else: that test
    # needs the model built, so its failure on generated data counts.
    report, m = _judge(manifest, {CUST: JSON_ERROR}, modified={"test.p.u1"})
    assert m[CUST].unverified_broken_on_base and not m[CUST].fixture_limited
    assert not report.passed


def test_a_renamed_json_key_reads_null_on_the_head(parcels: Path, tmp_path: Path) -> None:
    # The base reads `$.weight`; the pull request reads `$.wieght`. The fixture holds both
    # keys, so the base still gets values, and the typo reads NULL as on real data.
    mart = parcels / "models/marts/parcel_weights.sql"
    mart.write_text(mart.read_text().replace("$.weight", "$.wieght"))
    _git(parcels, "commit", "-qam", "typo")
    comment, summary_file = tmp_path / "comment.md", tmp_path / "summary.json"
    CliRunner().invoke(
        app,
        [
            "run", "--base-ref", "main", "--repo-root", str(parcels), "--keep-workdir",
            "--comment-file", str(comment), "--summary-file", str(summary_file),
        ],
    )  # fmt: skip
    summary = json.loads(summary_file.read_text())
    assert summary["fixtures"]["json_new_keys"] == ["parcels.payload: wieght"]
    assert "`parcels.payload: wieght`" in comment.read_text()
    (db_file,) = (parcels / ".preflight").glob("*.duckdb")
    copy = tmp_path / "inspect.duckdb"
    shutil.copy(db_file, copy)
    wal = db_file.with_name(db_file.name + ".wal")
    if wal.exists():  # dbt-duckdb still holds the file open: take what it has not merged
        shutil.copy(wal, copy.with_name(copy.name + ".wal"))
    con = duckdb.connect(str(copy), read_only=True)
    rows = con.execute("select payload from raw.parcels where payload is not null").fetchall()
    payloads = [json.loads(r[0]) for r in rows]
    assert all(p["weight"] is not None and p["wieght"] is None for p in payloads)
    tables = con.execute(
        "select table_schema from information_schema.tables where table_name = 'parcel_weights'"
    ).fetchall()
    by_schema = {
        s: con.execute(f'select count(weight) from "{s}".parcel_weights').fetchone()[0]
        for (s,) in tables
    }
    non_null = con.execute("select count(payload) from raw.parcels").fetchone()[0]
    assert sorted(by_schema.values()) == [0, non_null]  # head all NULL, base all values


def test_json_paths_alone_do_not_reshape_a_source() -> None:
    # Both branches build on one fixture holding both sides' paths (`widen_json`).
    from dbt_preflight.cli import _reshaped_sources

    head = _manifest(STG_SQL, MART_SQL.replace("$.weight", "$.wieght"))
    base = _manifest(STG_SQL, MART_SQL)
    assert derive_dbml(head)[0] != derive_dbml(base)[0]
    assert _reshaped_sources(head, base) == set()


def test_a_dbml_column_noted_not_json_is_left_alone(manifest: Manifest) -> None:
    import pandas as pd
    from model2data.parse.dbml import ColumnDef, TableDef

    from dbt_preflight.fixtures import _fill_json
    from dbt_preflight.schema import ResolvedSchema

    src = manifest.sources["source.p.shop.customers"]
    table = TableDef(
        "customers",
        [
            ColumnDef("payload", "varchar", description="free text, not JSON"),
            ColumnDef("attrs", "varchar"),
        ],
    )
    schema = ResolvedSchema(
        tables={"customers": table},
        refs=[],
        dbml_path=Path("x.dbml"),
        derived=False,
        json_reads={(src.unique_id, "payload"): {"a": None}, (src.unique_id, "attrs"): {}},
    )
    df = pd.DataFrame({"payload": ["Some text."], "attrs": ["Other text."]})
    filled = _fill_json(df, schema, src, "customers", 42)
    assert [name for name, _ in filled] == ["attrs"]
    assert df["payload"].tolist() == ["Some text."]


def test_a_model_with_an_edited_test_is_never_broken_on_main(manifest: Manifest) -> None:
    # The change adds a test on stg_shop__customers, which fails on main with an ordinary
    # error: excusing it would skip the new test unseen, and excuse what it skips.
    report, m = _judge(
        manifest, {CUST: BINDER_ERROR}, modified={"test.p.u1"}, statuses={MART: "skipped"}
    )
    assert not m[CUST].broken_on_base and m[CUST].unverified_broken_on_base
    assert m[CUST].edited_tests == ["unique_customers_customer_id"]
    assert not m[MART].skipped_by_base
    assert not report.passed
    assert "this change adds or edits a test on it: `unique_customers_customer_id`" in render(
        report
    )


def test_a_singular_test_reaches_the_models_it_reads(manifest: Manifest) -> None:
    from dbt_preflight.cli import _tests_changed_on
    from dbt_preflight.manifest import TestNode

    manifest.tests["test.p.assert_x"] = TestNode(
        "test.p.assert_x", "assert_x", None, None, None, [CUST, ORDERS], {}, "tests/x.sql"
    )
    assert _tests_changed_on(manifest, {"test.p.assert_x"}) == {
        CUST: ["assert_x"],
        ORDERS: ["assert_x"],
    }
    report, m = _judge(manifest, {CUST: BINDER_ERROR}, modified={"test.p.assert_x"})
    assert m[CUST].unverified_broken_on_base and not m[CUST].broken_on_base


def _widened(tmp_path: Path, compare) -> tuple[dict, object]:
    from dbt_preflight.fixtures import FixtureSummary, JsonColumn, widen_json

    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw; create table raw.parcels (payload varchar)")
    con.execute("insert into raw.parcels values ('{}')")
    con.close()
    key = (SRC, "payload")
    # `m` came from a macro the head compiled; `b` and `a` are in raw SQL.
    head = {"a": None, "b": None, "m": None}
    summary = FixtureSummary(
        json_shapes={key: JsonColumn("raw", "parcels", "parcels", "payload", head)}
    )
    widen_json(db, summary, {key: {"a": None, "z": None}}, 42, compare)
    con = duckdb.connect(str(db))
    [(text,)] = con.execute("select payload from raw.parcels").fetchall()
    return json.loads(text), summary


def test_new_json_keys_when_both_sides_compiled(tmp_path: Path) -> None:
    obj, summary = _widened(tmp_path, None)
    assert obj["a"] and obj["z"] and obj["b"] is None and obj["m"] is None
    assert not summary.json_keys_partly_compared


def test_new_json_keys_from_raw_sql_when_only_one_side_compiled(tmp_path: Path) -> None:
    # The base did not compile, so it cannot see `m`: only raw-SQL keys are compared.
    key = (SRC, "payload")
    obj, summary = _widened(tmp_path, ({key: {"a": None, "b": None}}, {key: {"a": None}}))
    assert obj["b"] is None  # renamed in raw SQL: still caught
    assert obj["m"] is not None  # macro-hidden: real values, and the summary says so
    assert summary.json_keys_partly_compared
    from dbt_preflight.report import PreflightReport

    report = PreflightReport(fixtures=summary, base_ref="main")  # type: ignore[arg-type]
    summary.json_columns = ["parcels.payload"]
    assert "compared with the base in raw SQL alone" in render(report)
    assert build_summary(report, 0, None)["fixtures"]["json_keys_partly_compared"]
