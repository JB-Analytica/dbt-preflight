from __future__ import annotations

import duckdb

from dbt_preflight.checks import check_columns, check_manifest, row_counts
from dbt_preflight.manifest import Manifest


def _rules(violations) -> list[tuple[str, str]]:
    return sorted((v.rule, v.model) for v in violations)


def test_clean_project_has_no_violations(manifest: Manifest) -> None:
    ids = ["model.p.stg_shop__customers"]
    assert check_manifest(manifest, ids, "dbt") == []


def test_orders_staging_lacks_a_primary_key_test(manifest: Manifest) -> None:
    violations = check_manifest(manifest, ["model.p.stg_shop__orders"], "dbt")
    assert _rules(violations) == [("primary_key", "stg_shop__orders")]
    assert violations[0].path == "dbt/models/staging/shop/stg_shop__orders.sql"


def test_naming_and_layering_rules(raw_manifest: dict) -> None:
    # A mart with the wrong prefix that also reads a source directly, and a staging
    # model that joins another model.
    raw_manifest["nodes"]["model.p.customers"] = {
        **raw_manifest["nodes"]["model.p.dim_customers"],
        "name": "customers",
        "path": "marts/customers.sql",
        "original_file_path": "models/marts/customers.sql",
        "description": "",
        "depends_on": {"nodes": ["source.p.shop.customers"]},
    }
    raw_manifest["nodes"]["model.p.stg_shop__orders"]["depends_on"]["nodes"].append(
        "model.p.stg_shop__customers"
    )
    manifest = Manifest.from_dict(raw_manifest)

    violations = check_manifest(manifest, ["model.p.customers", "model.p.stg_shop__orders"], ".")
    assert _rules(violations) == [
        ("description", "customers"),
        ("layering", "customers"),
        ("layering", "stg_shop__orders"),
        ("naming", "customers"),
        ("primary_key", "customers"),
        ("primary_key", "stg_shop__orders"),
    ]
    layering = [v for v in violations if v.rule == "layering" and v.model == "customers"][0]
    assert "only `staging/` reads `source()`" in layering.message
    assert layering.path == "models/marts/customers.sql"


def test_models_outside_the_three_layers_are_not_named_checked(raw_manifest: dict) -> None:
    raw_manifest["nodes"]["model.p.dim_customers"]["path"] = "reports/dim_customers.sql"
    raw_manifest["nodes"]["model.p.dim_customers"]["name"] = "whatever_report"
    manifest = Manifest.from_dict(raw_manifest)
    violations = check_manifest(manifest, ["model.p.dim_customers"], ".")
    assert _rules(violations) == [("primary_key", "whatever_report")]


def test_column_rules_read_built_types(manifest: Manifest, tmp_path) -> None:
    db = tmp_path / "preflight.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema preflight_main")
    con.execute(
        """
        create table preflight_main.dim_customers as
        select 1 as customer_id, timestamp '2026-01-01' as created,
               date '2026-01-01' as first_order_date, true as active,
               true as is_business, 'x' as "BadName"
        """
    )
    con.close()

    violations = check_columns(manifest, ["model.p.dim_customers"], "dbt", db)
    messages = sorted(v.message for v in violations)
    assert messages == [
        "boolean column `active` should read as a claim: `is_` or `has_`",
        "column `BadName` is not snake_case",
        "timestamp column `created` should end in `_at`",
    ]
    assert row_counts(manifest, ["model.p.dim_customers", "model.p.stg_shop__orders"], db) == {
        "model.p.dim_customers": 1
    }
