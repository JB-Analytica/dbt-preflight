from __future__ import annotations

from decimal import Decimal

import duckdb

from dbt_preflight.manifest import Manifest
from dbt_preflight.metrics import (
    SOURCE_CONFIG,
    SOURCE_DBT,
    SOURCE_LIGHTDASH,
    collect_metrics,
    combine,
    config_metrics,
    dbt_metrics,
    evaluate,
    lightdash_metrics,
)


def _with_semantic_layer(raw: dict) -> dict:
    mart = "model.p.dim_customers"
    orders = "model.p.stg_shop__orders"
    raw["semantic_models"] = {
        "semantic_model.p.orders": {
            "name": "orders",
            "depends_on": {"nodes": [orders]},
            "measures": [{"name": "order_count", "agg": "count", "expr": "order_id"}],
            "dimensions": [],
            "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
        },
        "semantic_model.p.customers": {
            "name": "customers",
            "depends_on": {"nodes": [mart]},
            "measures": [
                {"name": "customer_count", "agg": "count", "expr": "customer_id"},
                {"name": "revenue", "agg": "sum", "expr": "lifetime_net_revenue_eur"},
                {"name": "buyers", "agg": "sum_boolean", "expr": "has_ordered"},
                {"name": "p90", "agg": "percentile", "expr": "lifetime_net_revenue_eur"},
            ],
            "dimensions": [
                {"name": "customer_segment", "type": "categorical"},
                {"name": "customer_since_at", "type": "time", "expr": "customer_since_at"},
            ],
            "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        },
    }
    raw["metrics"] = {
        "metric.p.customers": {
            "name": "customers",
            "label": "Customers",
            "type": "simple",
            "type_params": {"measure": {"name": "customer_count", "filter": None}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.business_customers": {
            "name": "business_customers",
            "label": "Business customers",
            "type": "simple",
            "type_params": {"measure": {"name": "customer_count", "filter": None}},
            "filter": {
                "where_filters": [
                    {
                        "where_sql_template": "{{ Dimension('customer__customer_segment') }} = 'business'"
                    }
                ]
            },
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.revenue": {
            "name": "revenue",
            "label": "Revenue",
            "type": "simple",
            "type_params": {"measure": {"name": "revenue", "filter": None}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.revenue_per_customer": {
            "name": "revenue_per_customer",
            "label": "Revenue per customer",
            "type": "ratio",
            "type_params": {"numerator": {"name": "revenue"}, "denominator": {"name": "customers"}},
            "filter": None,
            "depends_on": {"nodes": ["metric.p.revenue", "metric.p.customers"]},
        },
        "metric.p.business_share": {
            "name": "business_share",
            "label": "Business share",
            "type": "derived",
            "type_params": {
                "expr": "business_customers / customers",
                "metrics": [{"name": "business_customers"}, {"name": "customers"}],
            },
            "filter": None,
            "depends_on": {"nodes": ["metric.p.business_customers", "metric.p.customers"]},
        },
        "metric.p.buyers": {
            "name": "buyers",
            "label": "Buyers",
            "type": "simple",
            "type_params": {"measure": {"name": "buyers", "filter": None}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.p90": {
            "name": "p90",
            "label": "P90 revenue",
            "type": "simple",
            "type_params": {"measure": {"name": "p90", "filter": None}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.cumulative_revenue": {
            "name": "cumulative_revenue",
            "label": "Cumulative revenue",
            "type": "cumulative",
            "type_params": {"measure": {"name": "revenue", "filter": None}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.revenue_7d": {
            "name": "revenue_7d",
            "label": "Revenue, trailing 7 days",
            "type": "cumulative",
            "type_params": {
                "measure": {"name": "revenue", "filter": None},
                "cumulative_type_params": {"window": {"count": 7, "granularity": "day"}},
            },
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.revenue_mtd": {
            "name": "revenue_mtd",
            "label": "Revenue month to date",
            "type": "cumulative",
            "type_params": {
                "measure": {"name": "revenue", "filter": None},
                "grain_to_date": "month",
            },
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.signup_conversion": {
            "name": "signup_conversion",
            "label": "Signup conversion",
            "type": "conversion",
            "type_params": {"conversion_type_params": {}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.customers"]},
        },
        "metric.p.orders": {
            "name": "orders",
            "label": "Orders",
            "type": "simple",
            "type_params": {"measure": {"name": "order_count", "filter": None}},
            "filter": None,
            "depends_on": {"nodes": ["semantic_model.p.orders"]},
        },
        "metric.p.orders_per_customer": {
            "name": "orders_per_customer",
            "label": "Orders per customer",
            "type": "ratio",
            "type_params": {"numerator": {"name": "orders"}, "denominator": {"name": "customers"}},
            "filter": None,
            "depends_on": {"nodes": ["metric.p.orders", "metric.p.customers"]},
        },
        "metric.p.revenue_per_order": {
            "name": "revenue_per_order",
            "label": "Revenue per order",
            "type": "derived",
            "type_params": {
                "expr": "revenue / orders",
                "metrics": [{"name": "revenue"}, {"name": "orders"}],
            },
            "filter": None,
            "depends_on": {"nodes": ["metric.p.revenue", "metric.p.orders"]},
        },
    }
    return raw


def _with_lightdash(raw: dict) -> dict:
    node = raw["nodes"]["model.p.dim_customers"]
    node["config"]["meta"] = {
        "metrics": {
            "revenue_per_buyer": {
                "type": "number",
                "label": "Revenue per buyer",
                "sql": "${revenue} / nullif(${buyer_count}, 0)",
            },
            "weird": {"type": "string", "sql": "x"},
        }
    }
    node["columns"] = {
        "customer_id": {
            "name": "customer_id",
            "meta": {
                "metrics": {
                    "customer_count": {"type": "count_distinct", "label": "Customers"},
                    "buyer_count": {
                        "type": "count_distinct",
                        "label": "Buyers",
                        "filters": [{"has_ordered": True}],
                    },
                    "consumer_count": {
                        "type": "count_distinct",
                        "filters": [{"customer_segment": "consumer"}, {"country": "!null"}],
                    },
                }
            },
        },
        "lifetime_net_revenue_eur": {
            "name": "lifetime_net_revenue_eur",
            "config": {"meta": {"metrics": {"revenue": {"type": "sum", "label": "Revenue"}}}},
        },
    }
    return raw


def _customers_table() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("create schema s")
    con.execute(
        """
        create table s.dim_customers as
        select * from (values
            (1, 'consumer', 'BE', true, 100.0),
            (2, 'business', 'BE', true, 300.0),
            (3, 'consumer', null, false, 0.0),
            (4, 'consumer', 'NL', true, 50.0)
        ) t(customer_id, customer_segment, country, has_ordered, lifetime_net_revenue_eur)
        """
    )
    return con


def test_dbt_metrics_become_aggregates(raw_manifest: dict) -> None:
    manifest = Manifest.from_dict(_with_semantic_layer(raw_manifest))
    defs = {m.name: m for m in dbt_metrics(manifest)}
    assert defs["customers"].sql == "count(customer_id)"
    assert defs["business_customers"].sql == (
        "count(customer_id) filter (where (customer_segment = 'business'))"
    )
    assert defs["revenue"].sql == "sum(lifetime_net_revenue_eur)"
    assert defs["revenue_per_customer"].sql == (
        "cast((sum(lifetime_net_revenue_eur)) as double) / nullif((count(customer_id)), 0)"
    )
    assert "cast((count(customer_id) filter" in defs["business_share"].sql
    assert defs["buyers"].sql == "sum(case when has_ordered then 1 else 0 end)"
    assert defs["p90"].unsupported == "aggregation `percentile` is not evaluated"
    # All-time cumulative: the running total's final value is the plain aggregate.
    assert defs["cumulative_revenue"].sql == "sum(lifetime_net_revenue_eur)"
    assert defs["revenue_7d"].unsupported == (
        "cumulative over a 7 day window needs a time spine and is not evaluated"
    )
    assert defs["revenue_mtd"].unsupported == (
        "cumulative to the month needs a time spine and is not evaluated"
    )
    assert "conversion metrics" in (defs["signup_conversion"].unsupported or "")
    single = {n: m for n, m in defs.items() if not m.spans_models and n != "orders"}
    assert all(m.model_uid == "model.p.dim_customers" for m in single.values())
    assert all(m.source == SOURCE_DBT for m in defs.values())


def test_dbt_metrics_across_models_keep_one_input_per_model(raw_manifest: dict) -> None:
    manifest = Manifest.from_dict(_with_semantic_layer(raw_manifest))
    defs = {m.name: m for m in dbt_metrics(manifest)}
    ratio = defs["orders_per_customer"]
    assert ratio.spans_models and ratio.sql == ""
    assert ratio.model_uids == ["model.p.stg_shop__orders", "model.p.dim_customers"]
    assert ratio.model_uid == "model.p.stg_shop__orders"
    inputs = {m.name: m for m in ratio.inputs.values()}
    assert inputs["orders"].sql == "count(order_id)"
    assert inputs["customers"].sql == "count(customer_id)"
    assert not inputs["orders"].spans_models
    assert ratio.expr == (
        "cast((__preflight_orders__) as double) / nullif((__preflight_customers__), 0)"
    )
    derived = defs["revenue_per_order"]
    assert derived.spans_models
    assert derived.model_uids == ["model.p.dim_customers", "model.p.stg_shop__orders"]
    assert derived.expr == (
        "(cast((__preflight_revenue__) as double) / cast((__preflight_orders__) as double))"
    )


def test_combine_puts_input_values_together(raw_manifest: dict) -> None:
    manifest = Manifest.from_dict(_with_semantic_layer(raw_manifest))
    defs = {m.name: m for m in dbt_metrics(manifest)}
    ratio = defs["orders_per_customer"]
    tokens = {m.name: token for token, m in ratio.inputs.items()}
    con = duckdb.connect()
    assert combine(con, ratio, {tokens["orders"]: 10, tokens["customers"]: 4}) == 2.5
    assert combine(con, ratio, {tokens["orders"]: 10, tokens["customers"]: 0}) is None
    assert combine(con, ratio, {tokens["orders"]: None, tokens["customers"]: 4}) is None
    # A missing input is not silently treated as zero.
    assert combine(con, ratio, {tokens["orders"]: 10}) is None
    # A sum over a DECIMAL column comes back from DuckDB as a Decimal; it must inline as digits.
    assert (
        combine(con, ratio, {tokens["orders"]: Decimal("10.50"), tokens["customers"]: 4}) == 2.625
    )


def test_dbt_metrics_evaluate_on_duckdb(raw_manifest: dict) -> None:
    manifest = Manifest.from_dict(_with_semantic_layer(raw_manifest))
    defs = dbt_metrics(manifest)
    con = _customers_table()
    values = evaluate(con, '"s"."dim_customers"', defs, None)
    assert values["customers"] == 4
    assert values["business_customers"] == 1
    assert values["revenue"] == 450.0
    assert values["revenue_per_customer"] == 112.5
    assert values["business_share"] == 0.25
    assert values["buyers"] == 3
    assert values["cumulative_revenue"] == 450.0
    assert "p90" not in values and "revenue_7d" not in values
    # A metric spanning models has no single relation to run on; the diff combines it.
    assert "orders_per_customer" not in values and "revenue_per_order" not in values


def test_lightdash_metrics_resolve_references_and_filters(raw_manifest: dict) -> None:
    manifest = Manifest.from_dict(_with_lightdash(raw_manifest))
    defs = {m.name: m for m in lightdash_metrics(manifest.models["model.p.dim_customers"])}
    assert defs["customer_count"].sql == "count(distinct customer_id)"
    assert (
        defs["buyer_count"].sql == "count(distinct customer_id) filter (where has_ordered = true)"
    )
    assert defs["consumer_count"].sql == (
        "count(distinct customer_id) filter (where customer_segment = 'consumer' and country is not null)"
    )
    assert defs["revenue"].sql == "sum(lifetime_net_revenue_eur)"
    assert defs["revenue_per_buyer"].sql == (
        "(sum(lifetime_net_revenue_eur)) / nullif((count(distinct customer_id) filter (where has_ordered = true)), 0)"
    )
    assert defs["weird"].unsupported == "metric type `string` is not evaluated"
    assert defs["revenue"].label == "Revenue" and defs["revenue"].source == SOURCE_LIGHTDASH

    con = _customers_table()
    values = evaluate(con, '"s"."dim_customers"', list(defs.values()), None)
    assert values["consumer_count"] == 2
    assert values["revenue_per_buyer"] == 150.0


def test_config_metrics_need_an_existing_model(manifest: Manifest) -> None:
    defs = config_metrics(
        [
            {"name": "rows", "model": "dim_customers", "sql": "count(*)"},
            {"name": "nope", "model": "missing", "sql": "count(*)"},
        ],
        manifest,
    )
    assert defs[0].model_uid == "model.p.dim_customers" and defs[0].source == SOURCE_CONFIG
    assert defs[1].unsupported == "no model named `missing`"


def test_collect_dedupes_by_model_and_name(raw_manifest: dict) -> None:
    manifest = Manifest.from_dict(_with_lightdash(_with_semantic_layer(raw_manifest)))
    defs = collect_metrics(
        manifest, [{"name": "revenue", "model": "dim_customers", "sql": "sum(1)"}]
    )
    names = [m.name for m in defs]
    assert names.count("revenue") == 1
    assert next(m for m in defs if m.name == "revenue").source == SOURCE_DBT


def test_one_broken_metric_does_not_hide_the_others(manifest: Manifest) -> None:
    con = _customers_table()
    defs = config_metrics(
        [
            {"name": "ok", "model": "dim_customers", "sql": "count(*)"},
            {"name": "broken", "model": "dim_customers", "sql": "sum(no_such_column)"},
        ],
        manifest,
    )
    values = evaluate(con, '"s"."dim_customers"', defs, None)
    assert values["ok"] == 4 and values["broken"] is None
    assert "no_such_column" in (defs[1].unsupported or "")
