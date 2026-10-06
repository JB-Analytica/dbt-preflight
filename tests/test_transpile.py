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


# The shape `dbt_utils.star` renders for the DuckDB target, as in Fivetran's
# shopify__customers: double-quoted identifiers, one per line.
STAR_SQL = """with customers as (

    select
        "customer_id",
  "email",
  "first_name"
    from "memory"."s"."customer_metafields"

)
select customers.customer_id, initcap(customers.first_name) as first_name
from customers
where customers.email != "" and customers."customer_id" > 0
"""


def _star_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("create schema s")
    con.execute(
        "create table s.customer_metafields as "
        "select 1 as customer_id, 'a@b.c' as email, 'ada' as first_name"
    )
    return con


def test_dbt_rendered_identifiers_stay_identifiers_from_bigquery() -> None:
    out = transpile_sql(STAR_SQL, "bigquery")
    assert "'customer_id'" not in out and "'email'" not in out
    assert "email <> ''" in out  # BigQuery's own "" string is still a string
    assert _star_db().execute(out).fetchall() == [(1, "Ada")]


def test_bigquery_double_quoted_strings_stay_strings() -> None:
    out = transpile_sql(
        'select concat(first_name, " ", last_name) as n, if(paid, "yes", "no") as p, '
        '\'single\' as s from `p`.`d`.`t` where status in ("paid", "shipped")',
        "bigquery",
    )
    assert "' '" in out and "'yes'" in out and "'no'" in out
    assert "'paid'" in out and "'shipped'" in out and "'single'" in out


def test_duckdb_decides_when_a_token_could_be_either() -> None:
    # A constant string column written BigQuery's way reads as a column under the dbt
    # rule; DuckDB cannot plan that, so the plain reading wins.
    con = _star_db()

    def accepts(sql: str) -> bool:
        try:
            con.execute(f"explain {sql}")
            return True
        except duckdb.Error:
            return False

    sql = 'select "paid", customer_id from "memory"."s"."customer_metafields"'
    assert "`paid`" not in transpile_sql(sql, "bigquery", accepts)
    assert con.execute(transpile_sql(sql, "bigquery", accepts)).fetchall() == [("paid", 1)]
    assert con.execute(transpile_sql(STAR_SQL, "bigquery", accepts)).fetchall() == [(1, "Ada")]
