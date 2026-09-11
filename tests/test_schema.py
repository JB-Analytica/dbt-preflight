from __future__ import annotations

import pytest

from dbt_preflight.manifest import Manifest, SourceTable
from dbt_preflight.schema import SchemaError, derive_dbml, resolve_schema


def test_derived_dbml_carries_types_keys_and_refs(manifest: Manifest) -> None:
    dbml, inferred = derive_dbml(manifest)
    assert "Table customers {" in dbml
    assert "  id int [pk]" in dbml
    assert "  email varchar" in dbml
    assert "  created_at timestamp" in dbml
    assert "  customer_id int [ref: > customers.id]" in dbml
    assert "Note: 'One row per customer'" in dbml
    assert inferred == []  # every column already typed, nothing to infer


def test_derived_dbml_parses_with_model2data(manifest: Manifest, tmp_path) -> None:
    resolved = resolve_schema(None, manifest, tmp_path)
    assert resolved.derived
    assert set(resolved.tables) == {"customers", "orders"}
    assert len(resolved.refs) == 1
    assert (tmp_path / "derived.dbml").exists()


def test_untyped_source_column_is_an_error(raw_manifest: dict) -> None:
    raw_manifest["sources"]["source.p.shop.orders"]["columns"]["ordered_at"] = {
        "name": "ordered_at"
    }
    manifest = Manifest.from_dict(raw_manifest)
    with pytest.raises(SchemaError) as exc:
        derive_dbml(manifest)
    assert "- name: ordered_at" in str(exc.value)
    assert "data_type: <type>" in str(exc.value)


def test_source_without_columns_is_an_error(raw_manifest: dict) -> None:
    raw_manifest["sources"]["source.p.shop.orders"]["columns"] = {}
    manifest = Manifest.from_dict(raw_manifest)
    with pytest.raises(SchemaError, match="every column the staging model reads"):
        derive_dbml(manifest)


def test_explicit_dbml_file_wins(manifest: Manifest, tmp_path) -> None:
    dbml = tmp_path / "shop.dbml"
    dbml.write_text("Table customers {\n  id int [pk]\n  email varchar\n}\n")
    resolved = resolve_schema(dbml, manifest, tmp_path / "work")
    assert not resolved.derived
    assert list(resolved.tables) == ["customers"]


def _source(source_name: str, name: str, schema: str = "raw_jaffle_shop") -> dict:
    return {
        "source_name": source_name,
        "name": name,
        "identifier": name,
        "database": "preflight",
        "schema": schema,
        "loader": "",
        "columns": {},
    }


def _staging_model(name: str, path: str, depends_on: list[str], raw_code: str) -> dict:
    return {
        "resource_type": "model",
        "name": name,
        "path": path,
        "original_file_path": f"models/{path}",
        "database": "preflight",
        "schema": "preflight_main",
        "alias": name,
        "description": "",
        "depends_on": {"nodes": depends_on},
        "config": {"materialized": "view"},
        "columns": {},
        "raw_code": raw_code,
    }


_STG_CUSTOMERS_SQL = """
with source as (
    select * from {{ source('jaffle_shop', 'customers') }}
),
renamed as (
    select
        id as customer_id,
        name,
        lower(email) as email,
        is_active,
        cast(signup_date as date) as signup_date,
        created_at
    from source
)
select * from renamed
"""

_STG_ORDERS_SQL = """
with source as (
    select * from {{ source('jaffle_shop', 'orders') }}
),
renamed as (
    select
        id as order_id,
        customer_id,
        order_total_cents,
        ordered_at
    from source
)
select * from renamed
"""


@pytest.fixture
def jaffle_manifest() -> Manifest:
    """Sources with no declared columns at all, read by jaffle-shop-style staging models."""
    src_customers = "source.jaffle_shop.jaffle_shop.customers"
    src_orders = "source.jaffle_shop.jaffle_shop.orders"
    stg_customers = "model.jaffle_shop.stg_customers"
    stg_orders = "model.jaffle_shop.stg_orders"
    raw = {
        "sources": {
            src_customers: _source("jaffle_shop", "customers"),
            src_orders: _source("jaffle_shop", "orders"),
        },
        "nodes": {
            stg_customers: _staging_model(
                "stg_customers", "staging/stg_customers.sql", [src_customers], _STG_CUSTOMERS_SQL
            ),
            stg_orders: _staging_model(
                "stg_orders", "staging/stg_orders.sql", [src_orders], _STG_ORDERS_SQL
            ),
        },
        "parent_map": {stg_customers: [src_customers], stg_orders: [src_orders]},
        "child_map": {src_customers: [stg_customers], src_orders: [stg_orders]},
    }
    return Manifest.from_dict(raw)


def test_columns_inferred_from_staging_models(jaffle_manifest: Manifest) -> None:
    dbml, inferred = derive_dbml(jaffle_manifest)

    assert "Table customers {" in dbml
    assert "  id int [pk]" in dbml
    assert "  name varchar" in dbml
    assert "  email varchar" in dbml
    assert "  is_active boolean" in dbml
    assert "  created_at timestamp" in dbml
    assert "Table orders {" in dbml
    assert "  customer_id int" in dbml
    assert "  order_total_cents int" in dbml
    assert "  ordered_at timestamp" in dbml

    by_table = {i.table: i for i in inferred}
    assert set(by_table) == {"customers", "orders"}
    assert by_table["customers"].source_name == "jaffle_shop"
    assert by_table["customers"].models == ["stg_customers"]
    assert by_table["customers"].total_columns == 6
    # signup_date was cast explicitly, so it is not a guess; the unqualified names are.
    assert "signup_date" not in by_table["customers"].guessed_columns
    assert {"id", "name", "email", "is_active", "created_at"} <= set(
        by_table["customers"].guessed_columns
    )


def test_cast_type_wins_over_name_guess(jaffle_manifest: Manifest) -> None:
    dbml, _inferred = derive_dbml(jaffle_manifest)
    assert "  signup_date date" in dbml


def test_declared_data_type_is_kept_over_inference() -> None:
    """A column sources.yml already types keeps that type, even one inference would guess
    differently; a column left untyped in the same source still triggers inference for it
    and for the columns sources.yml never declared at all."""
    src_customers = "source.jaffle_shop.jaffle_shop.customers"
    stg_customers = "model.jaffle_shop.stg_customers"
    src = _source("jaffle_shop", "customers")
    # A name that would normally guess "timestamp", declared as varchar on purpose, plus
    # a column with no data_type at all so this source still needs inference.
    src["columns"] = {
        "created_at": {"name": "created_at", "data_type": "string"},
        "is_active": {"name": "is_active"},
    }
    raw = {
        "sources": {src_customers: src},
        "nodes": {
            stg_customers: _staging_model(
                "stg_customers", "staging/stg_customers.sql", [src_customers], _STG_CUSTOMERS_SQL
            ),
        },
        "parent_map": {stg_customers: [src_customers]},
        "child_map": {src_customers: [stg_customers]},
    }
    manifest = Manifest.from_dict(raw)

    dbml, inferred = derive_dbml(manifest)
    assert "  created_at varchar" in dbml  # declared, kept as-is, not the "timestamp" guess
    assert "  is_active boolean" in dbml  # declared but untyped, guessed from the name
    assert "  id int [pk]" in dbml  # never declared at all, inferred wholesale
    assert "created_at" not in inferred[0].guessed_columns
    assert "is_active" in inferred[0].guessed_columns
    assert "id" in inferred[0].guessed_columns


def test_source_read_by_no_model_still_errors(jaffle_manifest: Manifest) -> None:
    """A source inference cannot help either (no columns, no reading model) still errors."""
    src_products = "source.jaffle_shop.jaffle_shop.products"
    manifest = jaffle_manifest
    manifest.sources[src_products] = SourceTable(
        unique_id=src_products,
        source_name="jaffle_shop",
        name="products",
        identifier="products",
        database="preflight",
        schema="raw_jaffle_shop",
        loader="",
    )
    with pytest.raises(SchemaError) as exc:
        derive_dbml(manifest)
    text = str(exc.value)
    assert "every column the staging model reads" in text
    assert "- name: products" in text


def test_untyped_sources_get_a_yaml_patch(raw_manifest: dict) -> None:
    raw_manifest["sources"]["source.p.shop.orders"]["columns"]["ordered_at"] = {
        "name": "ordered_at"
    }
    raw_manifest["sources"]["source.p.shop.customers"]["columns"] = {}
    manifest = Manifest.from_dict(raw_manifest)
    with pytest.raises(SchemaError) as exc:
        derive_dbml(manifest)
    text = str(exc.value)
    assert "as YAML to paste into the sources file" in text
    assert "  - name: shop\n    tables:\n" in text
    assert (
        "      - name: customers\n        columns:  # every column the staging model reads" in text
    )
    assert (
        "      - name: orders\n        columns:\n          - name: ordered_at\n            data_type: <type>"
        in text
    )
