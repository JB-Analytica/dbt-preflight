from __future__ import annotations

from pathlib import Path

import duckdb

from dbt_preflight.diff import compute_diffs
from dbt_preflight.manifest import Manifest
from dbt_preflight.metrics import config_metrics


def _two_builds(tmp_path: Path) -> Path:
    db = tmp_path / "preflight.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute("create schema preflight_base_main")
    con.execute(
        "create table preflight_base_main.dim_customers as select * from (values "
        "(1, 'a', 10.0, date '2026-01-01'), (2, 'b', 20.0, date '2026-01-02'), (3, 'c', 30.0, date '2026-01-03')"
        ") t(customer_id, name, revenue, since_date)"
    )
    # head: one row filtered out, a column dropped, a column added, a column retyped
    con.execute(
        "create table preflight_main.dim_customers as select customer_id, revenue, "
        "cast(since_date as varchar) as since_date, true as is_active "
        "from preflight_base_main.dim_customers where customer_id < 3"
    )
    con.execute(
        "create table preflight_main.stg_shop__orders as select 1 as order_id, 1 as customer_id"
    )
    con.close()
    return db


def test_schema_rows_and_metrics_diff(raw_manifest: dict, tmp_path: Path) -> None:
    head = Manifest.from_dict(raw_manifest)
    base_raw = {**raw_manifest, "nodes": {k: dict(v) for k, v in raw_manifest["nodes"].items()}}
    for node in base_raw["nodes"].values():
        if node["resource_type"] == "model":
            node["schema"] = "preflight_base_main"
    base = Manifest.from_dict(base_raw)
    db = _two_builds(tmp_path)
    metrics = config_metrics(
        [
            {
                "name": "revenue",
                "label": "Revenue",
                "model": "dim_customers",
                "sql": "sum(revenue)",
            },
            {"name": "customers", "model": "dim_customers", "sql": "count(*)"},
            {"name": "max_id", "model": "dim_customers", "sql": "max(customer_id)"},
        ],
        head,
    )

    diffs = compute_diffs(
        db, head, base, ["model.p.dim_customers", "model.p.stg_shop__orders"], metrics, None
    )
    by_name = {d.name: d for d in diffs}

    d = by_name["dim_customers"]
    assert d.base_exists and (d.rows_base, d.rows_head) == (3, 2)
    assert d.columns_added == [("is_active", "BOOLEAN")]
    assert d.columns_removed == [("name", "VARCHAR")]
    assert d.columns_retyped == [("since_date", "DATE", "VARCHAR")]
    assert d.breaking and not d.identical
    assert d.rows_differing is None  # columns differ, so row values are not compared
    moved = {m.name: (m.base, m.head) for m in d.moved_metrics}
    assert moved == {"revenue": (60.0, 30.0), "customers": (3, 2), "max_id": (3, 2)}

    # Built on head only: reported as new, never as a diff against nothing.
    o = by_name["stg_shop__orders"]
    assert not o.base_exists and o.rows_head == 1 and len(o.columns_added) == 2


def test_identical_output_is_identical(raw_manifest: dict, tmp_path: Path) -> None:
    head = Manifest.from_dict(raw_manifest)
    db = tmp_path / "preflight.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute(
        "create table preflight_main.dim_customers as select 1 as customer_id, 5.0 as revenue"
    )
    con.close()
    metrics = config_metrics(
        [{"name": "revenue", "model": "dim_customers", "sql": "sum(revenue)"}], head
    )
    # base manifest points at the same relation: nothing can differ
    diffs = compute_diffs(db, head, head, ["model.p.dim_customers"], metrics, None)
    assert diffs[0].identical and diffs[0].metrics[0].base == diffs[0].metrics[0].head == 5.0


def test_value_changes_count_as_differing_rows(raw_manifest: dict, tmp_path: Path) -> None:
    head = Manifest.from_dict(raw_manifest)
    base_raw = {**raw_manifest, "nodes": {k: dict(v) for k, v in raw_manifest["nodes"].items()}}
    for node in base_raw["nodes"].values():
        if node["resource_type"] == "model":
            node["schema"] = "preflight_base_main"
    base = Manifest.from_dict(base_raw)
    db = tmp_path / "preflight.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute("create schema preflight_base_main")
    con.execute(
        "create table preflight_base_main.dim_customers as "
        "select * from (values (1, 10.0), (2, 20.0), (3, 30.0)) t(customer_id, revenue)"
    )
    con.execute(
        "create table preflight_main.dim_customers as "
        "select * from (values (1, 10.0), (2, 25.0), (3, 35.0)) t(customer_id, revenue)"
    )
    con.close()
    diffs = compute_diffs(db, head, base, ["model.p.dim_customers"], [], None)
    d = diffs[0]
    assert not d.schema_changed and not d.rows_changed
    assert d.rows_differing == 2 and not d.identical


def test_rename_profile_and_references(raw_manifest: dict, tmp_path: Path) -> None:
    # Base has customer_segment; head renames it to segment (same values) and adds a boolean.
    head_raw = {**raw_manifest, "nodes": {k: dict(v) for k, v in raw_manifest["nodes"].items()}}
    base_raw = {**raw_manifest, "nodes": {k: dict(v) for k, v in raw_manifest["nodes"].items()}}
    for node in base_raw["nodes"].values():
        if node["resource_type"] == "model":
            node["schema"] = "preflight_base_main"
    mart = base_raw["nodes"]["model.p.dim_customers"]
    mart["columns"] = {"customer_segment": {"name": "customer_segment"}}
    mart["config"] = {
        **mart["config"],
        "meta": {
            "metrics": {
                "biz": {
                    "type": "count",
                    "sql": "${TABLE}.customer_id",
                    "filters": [{"customer_segment": "business"}],
                }
            }
        },
    }
    base_raw["nodes"]["model.p.report"] = {
        **raw_manifest["nodes"]["model.p.stg_shop__orders"],
        "name": "rpt_segments",
        "path": "marts/rpt_segments.sql",
        "schema": "preflight_base_main",
        "raw_code": "select customer_segment, count(*) from {{ ref('dim_customers') }} group by 1",
        "depends_on": {"nodes": ["model.p.dim_customers"]},
    }
    base_raw["child_map"] = {**base_raw["child_map"], "model.p.dim_customers": ["model.p.report"]}
    base_raw["semantic_models"] = {
        "semantic_model.p.customers": {
            "name": "customers",
            "depends_on": {"nodes": ["model.p.dim_customers"]},
            "measures": [],
            "dimensions": [{"name": "customer_segment", "type": "categorical"}],
            "entities": [],
        }
    }
    head = Manifest.from_dict(head_raw)
    base = Manifest.from_dict(base_raw)

    db = tmp_path / "preflight.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute("create schema preflight_base_main")
    con.execute(
        "create table preflight_base_main.dim_customers as select * from (values "
        "(1, 'consumer'), (2, 'business'), (3, 'consumer')) t(customer_id, customer_segment)"
    )
    con.execute(
        "create table preflight_main.dim_customers as select customer_id, "
        "customer_segment as segment, customer_segment = 'business' as is_business "
        "from preflight_base_main.dim_customers"
    )
    con.close()

    d = compute_diffs(db, head, base, ["model.p.dim_customers"], [], None)[0]
    assert d.columns_renamed == [("customer_segment", "segment")]
    assert d.columns_removed == [] and [c for c, _ in d.columns_added] == ["is_business"]
    assert d.breaking and not d.identical
    assert d.profiles["is_business"].describe() == "1 true, 2 false"
    refs = d.references["customer_segment"]
    assert "its YAML column entry" in refs[0]
    assert "Lightdash meta on `dim_customers`" in refs
    assert "semantic model `customers`" in refs
    assert "downstream model `rpt_segments`" in refs


def test_added_column_profile_for_low_and_high_cardinality(
    raw_manifest: dict, tmp_path: Path
) -> None:
    from dbt_preflight.diff import ColumnProfile

    assert (
        ColumnProfile("x", "VARCHAR", 5, 1, 2, [("a", 3), ("b", 1)]).describe()
        == "2 distinct: a 3, b 1; 1 null"
    )
    assert ColumnProfile("x", "BIGINT", 150, 0, 150).describe() == "150 distinct"
    assert (
        ColumnProfile("x", "BOOLEAN", 3, 0, 2, [("false", 2), ("true", 1)]).describe()
        == "1 true, 2 false"
    )
