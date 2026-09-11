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


def _model_test(
    name: str, test_name: str, column: str, attached: str, depends_on: list[str]
) -> dict:
    return {
        "resource_type": "test",
        "name": name,
        "column_name": column,
        "attached_node": attached,
        "depends_on": {"nodes": depends_on},
        "test_metadata": {"name": test_name, "kwargs": {"column_name": column}},
        "original_file_path": "models/staging/_models.yml",
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


def test_macro_calls_do_not_break_parsing() -> None:
    """A model like jaffle-shop's stg_orders, that wraps columns in dbt macros.

    `{{ config(...) }}`, a comment block and `{{ dbt.date_trunc(...) }}` used as a select
    expression would all leave invalid SQL behind if simply deleted; the macro call also
    wraps the only occurrence of `ordered_at` in the query, so it has to be read out of the
    call's own arguments, not off a bare `exp.Column`.
    """
    src_orders = "source.jaffle_shop.jaffle_shop.orders"
    stg_orders = "model.jaffle_shop.stg_orders"
    sql = """
{{ config(materialized='view') }}
with
source as (
    select * from {{ source('jaffle_shop', 'orders') }}
),
renamed as (
    select
        -- ids
        id as order_id,
        customer_id,
        {{ cents_to_dollars('subtotal') }} as subtotal,
        {{ dbt.date_trunc('day', 'ordered_at') }} as ordered_at
    from source
)
select * from renamed
"""
    raw = {
        "sources": {src_orders: _source("jaffle_shop", "orders")},
        "nodes": {
            stg_orders: _staging_model("stg_orders", "staging/stg_orders.sql", [src_orders], sql),
        },
        "parent_map": {stg_orders: [src_orders]},
        "child_map": {src_orders: [stg_orders]},
    }
    manifest = Manifest.from_dict(raw)

    dbml, inferred = derive_dbml(manifest)
    assert "  id int [pk]" in dbml
    assert "  customer_id int" in dbml
    # Both only ever appear as a macro's string-literal argument, never a bare column: read
    # out of the call itself, "day" (a date part, not a column) correctly left out.
    assert "  ordered_at timestamp" in dbml
    assert "  subtotal decimal" in dbml  # "subtotal" ends in "total", one of the guessed suffixes
    assert "  day" not in dbml
    assert inferred[0].models == ["stg_orders"]


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


def _single_model_manifest(source_name: str, table: str, raw_code: str) -> Manifest:
    """A manifest with one source, read by one staging model, and nothing else."""
    src = f"source.p.{source_name}.{table}"
    stg = f"model.p.stg_{table}"
    raw = {
        "sources": {src: _source(source_name, table)},
        "nodes": {stg: _staging_model(f"stg_{table}", f"staging/stg_{table}.sql", [src], raw_code)},
        "parent_map": {stg: [src]},
        "child_map": {src: [stg]},
    }
    return Manifest.from_dict(raw)


def test_extended_decimal_name_words() -> None:
    """Every new decimal-word trigger, each paired with a noun as real columns are."""
    manifest = _single_model_manifest(
        "shop",
        "orders",
        """
        select
            id as order_id,
            supply_cost,
            shipping_fee,
            discount_pct,
            monthly_revenue,
            account_balance,
            profit_margin,
            trust_score
        from {{ source('shop', 'orders') }}
        """,
    )
    dbml, _ = derive_dbml(manifest)
    assert "  supply_cost decimal" in dbml
    assert "  shipping_fee decimal" in dbml
    assert "  discount_pct decimal" in dbml
    assert "  monthly_revenue decimal" in dbml
    assert "  account_balance decimal" in dbml
    assert "  profit_margin decimal" in dbml
    assert "  trust_score decimal" in dbml


def test_extended_int_name_words_as_whole_tokens() -> None:
    """The new integer-word triggers match a whole underscore-separated token, not a
    bare substring - "package" and "average" don't become integers just because they
    end in the letters "age"."""
    manifest = _single_model_manifest(
        "shop",
        "orders",
        """
        select
            id as order_id,
            item_count,
            num_guests,
            customer_age,
            ship_year,
            delivery_month,
            trip_day,
            package_weight,
            average_price
        from {{ source('shop', 'orders') }}
        """,
    )
    dbml, _ = derive_dbml(manifest)
    assert "  item_count int" in dbml
    assert "  num_guests int" in dbml
    assert "  customer_age int" in dbml
    assert "  ship_year int" in dbml
    assert "  delivery_month int" in dbml
    assert "  trip_day int" in dbml
    # "weight"/"price" are decimal words; neither name is mistaken for an integer just
    # because "package" ends in "age" or "average" contains it as a bare substring.
    assert "  package_weight decimal" in dbml
    assert "  average_price decimal" in dbml


def test_arithmetic_usage_signals_numeric() -> None:
    """A column with no name-based signal at all, used as an operand of `/`, inside
    `sum(`/`round(`, is read as numeric from the SQL alone - decimal by default, int
    when the name itself says integer."""
    manifest = _single_model_manifest(
        "shop",
        "widgets",
        """
        select
            id as widget_id,
            widget_alpha / 100 as alpha,
            sum(widget_beta) as beta_total,
            round(widget_gamma, 2) as gamma,
            widget_units_count / 10 as units_per_box
        from {{ source('shop', 'widgets') }}
        """,
    )
    dbml, _ = derive_dbml(manifest)
    assert "  widget_alpha decimal" in dbml
    assert "  widget_beta decimal" in dbml
    assert "  widget_gamma decimal" in dbml
    # "count" in the name pushes an otherwise-decimal numeric hint to an integer.
    assert "  widget_units_count int" in dbml


def test_string_usage_overrides_a_decimal_name_guess() -> None:
    """Being passed to `trim(`/`lower(`, or compared to a string literal, is stronger
    evidence than the column's own name - even a name that would otherwise read as a
    decimal word."""
    manifest = _single_model_manifest(
        "shop",
        "orders",
        """
        select
            id as order_id,
            trim(exchange_rate) as rate_code,
            amount_code = 'active' as is_active_amount
        from {{ source('shop', 'orders') }}
        """,
    )
    dbml, _ = derive_dbml(manifest)
    # "rate"/"amount" are decimal words; the SQL usage overrides that guess.
    assert "  exchange_rate varchar" in dbml
    assert "  amount_code varchar" in dbml


def test_fk_shaped_names_get_a_ref() -> None:
    """`_id`-suffixed and bare foreign-key-shaped names, matched against another
    source table's name with a conventional loader prefix stripped."""
    src_customers = "source.p.shop.customers"
    src_stores = "source.p.shop.raw_stores"
    src_orders = "source.p.shop.orders"
    stg_orders = "model.p.stg_orders"
    customers = _source("shop", "customers")
    customers["columns"] = {"id": {"name": "id", "data_type": "int64"}}
    stores = _source("shop", "raw_stores")
    stores["columns"] = {"id": {"name": "id", "data_type": "int64"}}
    sql = """
    select
        id as order_id,
        customer as customer_ref,
        store_id,
        widget_id
    from {{ source('shop', 'orders') }}
    """
    raw = {
        "sources": {
            src_customers: customers,
            src_stores: stores,
            src_orders: _source("shop", "orders"),
        },
        "nodes": {
            stg_orders: _staging_model("stg_orders", "staging/stg_orders.sql", [src_orders], sql)
        },
        "parent_map": {stg_orders: [src_orders]},
        "child_map": {src_orders: [stg_orders]},
    }
    manifest = Manifest.from_dict(raw)
    dbml, _ = derive_dbml(manifest)
    assert "  customer int [ref: > customers.id]" in dbml
    assert "  store_id int [ref: > raw_stores.id]" in dbml
    # "widget_id" looks like a foreign key by its `_id` suffix alone but no source
    # table named (raw_)widgets exists, so it stays a plain, ref-less integer.
    assert "  widget_id int" in dbml
    assert "widget_id int [ref:" not in dbml


def test_explicit_relationships_ref_wins_over_a_guessed_fk() -> None:
    """An explicit `relationships` test's target overrides a name-guessed one.

    `customer` is foreign-key-shaped by name alone and would default to
    `ref: > customers.id`; an explicit test naming a different field on the same
    table wins instead, proving which one the derived DBML actually used.
    """
    src_customers = "source.p.shop.customers"
    src_orders = "source.p.shop.orders"
    stg_orders = "model.p.stg_orders"
    customers = _source("shop", "customers")
    customers["columns"] = {
        "id": {"name": "id", "data_type": "int64"},
        "email": {"name": "email", "data_type": "string"},
    }
    raw = {
        "sources": {src_customers: customers, src_orders: _source("shop", "orders")},
        "nodes": {
            stg_orders: _staging_model(
                "stg_orders",
                "staging/stg_orders.sql",
                [src_orders],
                "select id as order_id, customer from {{ source('shop', 'orders') }}",
            ),
            "test.p.custom_rel": {
                "resource_type": "test",
                "name": "relationships_orders_customer_to_customers_email",
                "column_name": "customer",
                "attached_node": src_orders,
                "depends_on": {"nodes": [src_orders, src_customers]},
                "test_metadata": {
                    "name": "relationships",
                    "kwargs": {
                        "column_name": "customer",
                        "to": "source('shop', 'customers')",
                        "field": "email",
                    },
                },
                "original_file_path": "models/_sources.yml",
            },
        },
        "parent_map": {stg_orders: [src_orders]},
        "child_map": {src_orders: [stg_orders]},
    }
    manifest = Manifest.from_dict(raw)
    dbml, _ = derive_dbml(manifest)
    assert "  customer int [ref: > customers.email]" in dbml
    assert "ref: > customers.id" not in dbml


def test_unique_not_null_carried_back_from_staging_alias() -> None:
    """A staging model's own `unique`/`not_null` tests on the alias it gave a source
    column carry back to that column - `pk` when both are declared on the same alias,
    `unique`/`not null` alone otherwise - even for a column that is not literally `id`,
    and for a bare, unaliased passthrough column."""
    src_products = "source.p.shop.products"
    stg_products = "model.p.stg_products"
    sql = """
    select
        sku as product_id,
        warehouse_id,
        name as product_name
    from {{ source('shop', 'products') }}
    """
    raw = {
        "sources": {src_products: _source("shop", "products")},
        "nodes": {
            stg_products: _staging_model(
                "stg_products", "staging/stg_products.sql", [src_products], sql
            ),
            "test.p.pu": _model_test(
                "unique_stg_products_product_id",
                "unique",
                "product_id",
                stg_products,
                [stg_products],
            ),
            "test.p.pn": _model_test(
                "not_null_stg_products_product_id",
                "not_null",
                "product_id",
                stg_products,
                [stg_products],
            ),
            "test.p.wn": _model_test(
                "not_null_stg_products_warehouse_id",
                "not_null",
                "warehouse_id",
                stg_products,
                [stg_products],
            ),
        },
        "parent_map": {stg_products: [src_products]},
        "child_map": {src_products: [stg_products]},
    }
    manifest = Manifest.from_dict(raw)
    dbml, _ = derive_dbml(manifest)
    assert "  sku varchar [pk]" in dbml
    assert "  warehouse_id int [not null]" in dbml


def test_transform_alias_does_not_carry_a_test_back() -> None:
    """`lower(email) as email` is a transform, not a plain rename: a test on that alias
    cannot be carried back to a single source column, so it is left alone."""
    src_customers = "source.p.shop.customers"
    stg_customers = "model.p.stg_customers"
    sql = """
    select
        id as customer_id,
        lower(email) as email
    from {{ source('shop', 'customers') }}
    """
    raw = {
        "sources": {src_customers: _source("shop", "customers")},
        "nodes": {
            stg_customers: _staging_model(
                "stg_customers", "staging/stg_customers.sql", [src_customers], sql
            ),
            "test.p.eu": _model_test(
                "unique_stg_customers_email", "unique", "email", stg_customers, [stg_customers]
            ),
        },
        "parent_map": {stg_customers: [src_customers]},
        "child_map": {src_customers: [stg_customers]},
    }
    manifest = Manifest.from_dict(raw)
    dbml, _ = derive_dbml(manifest)
    assert "  email varchar" in dbml
    assert "  email varchar [unique]" not in dbml
