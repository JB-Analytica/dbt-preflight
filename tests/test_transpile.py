from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from sqlglot.errors import SqlglotError

from dbt_preflight.transpile import detect_dialect, transpile_sql


def test_bigquery_functions_become_duckdb() -> None:
    sql = """
        select
            timestamp_diff(shipped_at, ordered_at, hour) / 24.0 as days_to_ship,
            initcap(origin) as origin_name,
            date_trunc(ordered_at, month) as order_month,
            safe_divide(net, gross) as ratio,
            cast(weight_grams as string) || 'g' as weight
        from "preflight"."preflight_staging"."stg_webshop__orders"
        where status = "paid"
    """
    out = transpile_sql(sql, "bigquery")
    assert "DATE_DIFF('HOUR', ordered_at, shipped_at)" in out
    assert "DATE_TRUNC('MONTH', ordered_at)" in out
    assert "CASE WHEN gross <> 0 THEN net / gross ELSE NULL END" in out
    assert "CAST(weight_grams AS TEXT)" in out
    assert '"preflight"."preflight_staging"."stg_webshop__orders"' in out
    assert "status = 'paid'" in out
    assert "initcap" not in out.lower() or "INITCAP(" not in out  # rewritten as an expression


def test_transpiled_bigquery_runs_on_duckdb() -> None:
    con = duckdb.connect()
    con.execute("create schema s")
    con.execute(
        "create table s.t as select timestamp '2026-01-02 12:00:00' as shipped_at, "
        "timestamp '2026-01-01 00:00:00' as ordered_at, 'ethiopia' as origin, 1450 as w"
    )
    sql = transpile_sql(
        "select timestamp_diff(shipped_at, ordered_at, hour) / 24.0 as d, initcap(origin) as o, "
        'cast(w as string) || "g" as g from "memory"."s"."t"',
        "bigquery",
    )
    assert con.execute(sql).fetchone() == (1.5, "Ethiopia", "1450g")


def test_snowflake_keeps_double_quoted_identifiers() -> None:
    out = transpile_sql('select "Col" from "db"."sch"."t" where x = \'a\'', "snowflake")
    assert '"db"."sch"."t"' in out and '"Col"' in out


def test_unparseable_sql_raises() -> None:
    with pytest.raises(SqlglotError):
        transpile_sql("select from where (((", "bigquery")


def test_detect_dialect_from_project_profiles(tmp_path: Path) -> None:
    (tmp_path / "profiles.yml").write_text(
        "refarch:\n  target: \"{{ env_var('DBT_TARGET', 'dev') }}\"\n"
        "  outputs:\n    dev:\n      type: bigquery\n    prod:\n      type: bigquery\n"
    )
    assert detect_dialect(tmp_path, "refarch") == "bigquery"
    assert detect_dialect(tmp_path, "other") is None


def test_detect_dialect_honours_default_target(tmp_path: Path) -> None:
    (tmp_path / "profiles.yml").write_text(
        "p:\n  target: local\n  outputs:\n    local:\n      type: duckdb\n    prod:\n      type: snowflake\n"
    )
    assert detect_dialect(tmp_path, "p") is None  # duckdb needs no transpiling


def test_detect_dialect_without_profiles(tmp_path: Path) -> None:
    assert detect_dialect(tmp_path, "p") is None
