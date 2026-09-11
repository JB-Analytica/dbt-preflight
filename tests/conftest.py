from __future__ import annotations

from pathlib import Path

import pytest

from dbt_preflight.manifest import Manifest

ROOT = Path(__file__).parent


def _model(
    name: str,
    path: str,
    depends_on: list[str],
    description: str = "described",
    raw_code: str = "",
) -> dict:
    return {
        "resource_type": "model",
        "name": name,
        "path": path,
        "original_file_path": f"models/{path}",
        "database": "preflight",
        "schema": "preflight_main",
        "alias": name,
        "description": description,
        "depends_on": {"nodes": depends_on},
        "config": {"materialized": "view"},
        "columns": {},
        "raw_code": raw_code,
    }


def _generic_test(
    name: str, test_name: str, column: str, attached: str, depends_on: list[str], **kwargs
) -> dict:
    return {
        "resource_type": "test",
        "name": name,
        "column_name": column,
        "attached_node": attached,
        "depends_on": {"nodes": depends_on},
        "test_metadata": {"name": test_name, "kwargs": {"column_name": column, **kwargs}},
        "original_file_path": "models/_models.yml",
    }


@pytest.fixture
def raw_manifest() -> dict:
    """A tiny, well-formed project: two sources, two staging models, one mart."""
    cust = "model.p.stg_shop__customers"
    orders = "model.p.stg_shop__orders"
    mart = "model.p.dim_customers"
    src_c = "source.p.shop.customers"
    src_o = "source.p.shop.orders"
    return {
        "sources": {
            src_c: {
                "source_name": "shop",
                "name": "customers",
                "identifier": "customers",
                "database": "preflight",
                "schema": "raw_shop",
                "loader": "dlt",
                "description": "One row per customer",
                "columns": {
                    "id": {"name": "id", "data_type": "int64"},
                    "email": {"name": "email", "data_type": "string"},
                    "created_at": {"name": "created_at", "data_type": "timestamp"},
                },
            },
            src_o: {
                "source_name": "shop",
                "name": "orders",
                "identifier": "orders",
                "database": "preflight",
                "schema": "raw_shop",
                "loader": "",
                "columns": {
                    "id": {"name": "id", "data_type": "int64"},
                    "customer_id": {"name": "customer_id", "data_type": "int64"},
                    "ordered_at": {"name": "ordered_at", "data_type": "timestamp"},
                },
            },
        },
        "nodes": {
            cust: _model("stg_shop__customers", "staging/shop/stg_shop__customers.sql", [src_c]),
            orders: _model("stg_shop__orders", "staging/shop/stg_shop__orders.sql", [src_o]),
            mart: _model("dim_customers", "marts/dim_customers.sql", [cust, orders]),
            "test.p.u1": _generic_test(
                "unique_customers_customer_id", "unique", "customer_id", cust, [cust]
            ),
            "test.p.n1": _generic_test(
                "not_null_customers_customer_id", "not_null", "customer_id", cust, [cust]
            ),
            "test.p.r1": _generic_test(
                "relationships_orders_customer_id",
                "relationships",
                "customer_id",
                orders,
                [orders, cust],
                to="ref('stg_shop__customers')",
                field="customer_id",
            ),
            "test.p.su": _generic_test(
                "source_unique_customers_id", "unique", "id", src_c, [src_c]
            ),
            "test.p.sn": _generic_test(
                "source_not_null_customers_id", "not_null", "id", src_c, [src_c]
            ),
            "test.p.sr": _generic_test(
                "source_relationships_orders_customer_id",
                "relationships",
                "customer_id",
                src_o,
                [src_o, src_c],
                to="source('shop', 'customers')",
                field="id",
            ),
        },
        "parent_map": {
            cust: [src_c],
            orders: [src_o],
            mart: [cust, orders],
        },
        "child_map": {
            src_c: [cust],
            src_o: [orders],
            cust: [mart, "test.p.u1", "test.p.n1", "test.p.r1"],
            orders: [mart, "test.p.r1"],
            mart: [],
        },
    }


@pytest.fixture
def manifest(raw_manifest: dict) -> Manifest:
    return Manifest.from_dict(raw_manifest)
