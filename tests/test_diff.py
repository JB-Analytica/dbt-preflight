from __future__ import annotations

from pathlib import Path

import duckdb

from dbt_preflight.diff import compute_diffs
from dbt_preflight.manifest import Manifest
from dbt_preflight.metrics import config_metrics, dbt_metrics


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
    # columns differ, so row values are compared on customer_id and revenue only: both head
    # rows (1, 10.0) and (2, 20.0) have an identical match on base, so nothing differs there.
    assert d.rows_differing == 0
    assert d.rows_differing_common_columns == 2
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


def test_row_values_compared_on_shared_columns_when_schema_changed(
    raw_manifest: dict, tmp_path: Path
) -> None:
    """An added column must not hide a value change on the columns both sides still have."""
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
        "create table preflight_base_main.dim_customers as select * from (values "
        "(1, 10.0), (2, 20.0), (3, 30.0)) t(customer_id, revenue)"
    )
    # head adds a column and changes customer 2's revenue: the schema no longer matches, but
    # customer_id and revenue are still shared, with matching types.
    con.execute(
        "create table preflight_main.dim_customers as select * from (values "
        "(1, 10.0, true), (2, 25.0, true), (3, 30.0, false)) t(customer_id, revenue, is_active)"
    )
    con.close()
    diffs = compute_diffs(db, head, base, ["model.p.dim_customers"], [], None)
    d = diffs[0]
    assert d.columns_added == [("is_active", "BOOLEAN")]
    assert d.rows_differing == 1  # only customer 2's row has no match on (customer_id, revenue)
    assert d.rows_differing_common_columns == 2


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


def _two_schema_builds(db: Path, base_rows: str, head_rows: str) -> None:
    """Two `dim_customers(customer_id, revenue, segment)` tables for a breakdown test."""
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute("create schema preflight_base_main")
    con.execute(
        f"create table preflight_base_main.dim_customers as select * from (values {base_rows}) "
        "t(customer_id, revenue, segment)"
    )
    con.execute(
        f"create table preflight_main.dim_customers as select * from (values {head_rows}) "
        "t(customer_id, revenue, segment)"
    )
    con.close()


def _head_and_base(raw_manifest: dict) -> tuple[Manifest, Manifest]:
    """`raw_manifest` as head, and a copy on `preflight_base_main` as base."""
    head = Manifest.from_dict(raw_manifest)
    base_raw = {**raw_manifest, "nodes": {k: dict(v) for k, v in raw_manifest["nodes"].items()}}
    for node in base_raw["nodes"].values():
        if node["resource_type"] == "model":
            node["schema"] = "preflight_base_main"
    return head, Manifest.from_dict(base_raw)


def test_moved_metric_broken_down_by_lightdash_dimension(
    raw_manifest: dict, tmp_path: Path
) -> None:
    """A moved metric is broken down by a Lightdash `dimension.type: string` column, largest
    contributors first, when the model has no semantic model of its own."""
    raw_manifest["nodes"]["model.p.dim_customers"]["columns"] = {
        "segment": {"config": {"meta": {"dimension": {"type": "string"}}}}
    }
    head, base = _head_and_base(raw_manifest)
    db = tmp_path / "preflight.duckdb"
    _two_schema_builds(
        db,
        "(1, 10.0, 'consumer'), (2, 20.0, 'consumer'), (3, 30.0, 'business')",
        "(1, 15.0, 'consumer'), (2, 20.0, 'consumer'), (3, 50.0, 'business')",
    )
    metrics = config_metrics(
        [{"name": "revenue", "label": "Revenue", "model": "dim_customers", "sql": "sum(revenue)"}],
        head,
    )
    d = compute_diffs(db, head, base, ["model.p.dim_customers"], metrics, None)[0]
    (moved,) = d.moved_metrics
    assert moved.base == 60.0 and moved.head == 85.0
    # business moved by 20 (30 -> 50), consumer by only 5 (30 -> 35): business sorts first.
    assert moved.breakdown == {"segment": [("business", 30.0, 50.0), ("consumer", 30.0, 35.0)]}


def test_moved_metric_broken_down_by_semantic_layer_categorical_dimension(
    raw_manifest: dict, tmp_path: Path
) -> None:
    """The same breakdown, sourced from a dbt semantic model's categorical dimension rather
    than Lightdash meta, and evaluated through a dbt-defined simple metric."""
    raw_manifest["semantic_models"] = {
        "semantic_model.p.customers": {
            "name": "customers",
            "depends_on": {"nodes": ["model.p.dim_customers"]},
            "measures": [{"name": "revenue", "agg": "sum", "expr": "revenue"}],
            "dimensions": [
                {"name": "customer_segment", "type": "categorical"},
                {"name": "since", "type": "time"},  # not categorical: never used for a breakdown
            ],
            "entities": [],
        }
    }
    raw_manifest["metrics"] = {
        "metric.p.revenue": {
            "name": "revenue",
            "label": "Revenue",
            "type": "simple",
            "type_params": {"measure": {"name": "revenue", "filter": None}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        }
    }
    head, base = _head_and_base(raw_manifest)
    db = tmp_path / "preflight.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute("create schema preflight_base_main")
    con.execute(
        "create table preflight_base_main.dim_customers as select * from (values "
        "(1, 10.0, 'consumer'), (2, 20.0, 'consumer'), (3, 30.0, 'business')) "
        "t(customer_id, revenue, customer_segment)"
    )
    con.execute(
        "create table preflight_main.dim_customers as select * from (values "
        "(1, 15.0, 'consumer'), (2, 20.0, 'consumer'), (3, 50.0, 'business')) "
        "t(customer_id, revenue, customer_segment)"
    )
    con.close()
    metrics = dbt_metrics(head)
    d = compute_diffs(db, head, base, ["model.p.dim_customers"], metrics, None)[0]
    (moved,) = d.moved_metrics
    assert moved.breakdown == {
        "customer_segment": [("business", 30.0, 50.0), ("consumer", 30.0, 35.0)]
    }


def test_breakdown_caps_at_three_rows(raw_manifest: dict, tmp_path: Path) -> None:
    """More than three distinct dimension values: only the three largest movers are kept."""
    raw_manifest["nodes"]["model.p.dim_customers"]["columns"] = {
        "segment": {"config": {"meta": {"dimension": {"type": "string"}}}}
    }
    head, base = _head_and_base(raw_manifest)
    db = tmp_path / "preflight.duckdb"
    _two_schema_builds(
        db,
        "(1, 10.0, 'a'), (2, 10.0, 'b'), (3, 10.0, 'c'), (4, 10.0, 'd')",
        "(1, 40.0, 'a'), (2, 30.0, 'b'), (3, 20.0, 'c'), (4, 10.5, 'd')",
    )
    metrics = config_metrics(
        [{"name": "revenue", "label": "Revenue", "model": "dim_customers", "sql": "sum(revenue)"}],
        head,
    )
    d = compute_diffs(db, head, base, ["model.p.dim_customers"], metrics, None)[0]
    (moved,) = d.moved_metrics
    values = [row[0] for row in moved.breakdown["segment"]]
    assert values == ["a", "b", "c"]  # largest movers first, "d" (+0.5) dropped


def test_breakdown_skips_the_models_own_primary_key(raw_manifest: dict, tmp_path: Path) -> None:
    """The primary key is one row per value: not a meaningful breakdown, so it is skipped
    even though it is a Lightdash `dimension.type: string` column like any other."""
    raw_manifest["nodes"]["model.p.dim_customers"]["config"] = {
        **raw_manifest["nodes"]["model.p.dim_customers"]["config"],
        "meta": {"primary_key": "customer_id"},
    }
    raw_manifest["nodes"]["model.p.dim_customers"]["columns"] = {
        "customer_id": {"config": {"meta": {"dimension": {"type": "string"}}}},
        "segment": {"config": {"meta": {"dimension": {"type": "string"}}}},
    }
    head, base = _head_and_base(raw_manifest)
    db = tmp_path / "preflight.duckdb"
    _two_schema_builds(
        db,
        "(1, 10.0, 'consumer'), (2, 20.0, 'consumer'), (3, 30.0, 'business')",
        "(1, 15.0, 'consumer'), (2, 20.0, 'consumer'), (3, 50.0, 'business')",
    )
    metrics = config_metrics(
        [{"name": "revenue", "label": "Revenue", "model": "dim_customers", "sql": "sum(revenue)"}],
        head,
    )
    d = compute_diffs(db, head, base, ["model.p.dim_customers"], metrics, None)[0]
    (moved,) = d.moved_metrics
    assert list(moved.breakdown) == ["segment"]  # customer_id, the primary key, is excluded


def test_breakdown_skips_dimensions_lightdash_marks_hidden(
    raw_manifest: dict, tmp_path: Path
) -> None:
    """A Lightdash dimension marked `hidden: true` (an internal-only field) is not offered
    as a breakdown, the same way the team hides it from Lightdash's own explorer."""
    raw_manifest["nodes"]["model.p.dim_customers"]["columns"] = {
        "phone": {"config": {"meta": {"dimension": {"type": "string", "hidden": True}}}},
        "segment": {"config": {"meta": {"dimension": {"type": "string"}}}},
    }
    head, base = _head_and_base(raw_manifest)
    db = tmp_path / "preflight.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute("create schema preflight_base_main")
    con.execute(
        "create table preflight_base_main.dim_customers as select * from (values "
        "(1, 10.0, '0470', 'consumer'), (2, 20.0, '0471', 'consumer'), "
        "(3, 30.0, '0472', 'business')) t(customer_id, revenue, phone, segment)"
    )
    con.execute(
        "create table preflight_main.dim_customers as select * from (values "
        "(1, 15.0, '0470', 'consumer'), (2, 20.0, '0471', 'consumer'), "
        "(3, 50.0, '0472', 'business')) t(customer_id, revenue, phone, segment)"
    )
    con.close()
    metrics = config_metrics(
        [{"name": "revenue", "label": "Revenue", "model": "dim_customers", "sql": "sum(revenue)"}],
        head,
    )
    d = compute_diffs(db, head, base, ["model.p.dim_customers"], metrics, None)[0]
    (moved,) = d.moved_metrics
    assert list(moved.breakdown) == ["segment"]  # phone, hidden, is excluded


def test_breakdown_gates_out_high_cardinality_dimensions(
    raw_manifest: dict, tmp_path: Path
) -> None:
    """`city` is near-unique per row (13 distinct values, over the cap of 12) and tells a
    reviewer nothing; `segment` (2 distinct values) is the useful breakdown and is kept."""
    raw_manifest["nodes"]["model.p.dim_customers"]["columns"] = {
        "city": {"config": {"meta": {"dimension": {"type": "string"}}}},
        "segment": {"config": {"meta": {"dimension": {"type": "string"}}}},
    }
    head, base = _head_and_base(raw_manifest)
    db = tmp_path / "preflight.duckdb"

    # 13 customers: a distinct city each, a 7/6 split between two segments. Business
    # customers' revenue doubles on head; consumers are unchanged.
    def row(customer_id: int, revenue: float) -> str:
        segment = "consumer" if customer_id % 2 else "business"
        return f"({customer_id}, {revenue}, 'city_{customer_id}', '{segment}')"

    base_rows = ", ".join(row(i, 10.0) for i in range(1, 14))
    head_rows = ", ".join(row(i, 20.0 if i % 2 == 0 else 10.0) for i in range(1, 14))
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute("create schema preflight_base_main")
    con.execute(
        f"create table preflight_base_main.dim_customers as select * from (values {base_rows}) "
        "t(customer_id, revenue, city, segment)"
    )
    con.execute(
        f"create table preflight_main.dim_customers as select * from (values {head_rows}) "
        "t(customer_id, revenue, city, segment)"
    )
    con.close()
    metrics = config_metrics(
        [{"name": "revenue", "label": "Revenue", "model": "dim_customers", "sql": "sum(revenue)"}],
        head,
    )
    d = compute_diffs(db, head, base, ["model.p.dim_customers"], metrics, None)[0]
    (moved,) = d.moved_metrics
    assert moved.breakdown == {"segment": [("business", 60.0, 120.0), ("consumer", 70.0, 70.0)]}


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
