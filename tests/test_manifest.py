from dbt_preflight.manifest import Manifest


def test_sources_and_models_are_read(manifest: Manifest) -> None:
    assert set(manifest.sources) == {"source.p.shop.customers", "source.p.shop.orders"}
    assert manifest.sources["source.p.shop.customers"].loader == "dlt"
    assert [c.name for c in manifest.sources["source.p.shop.customers"].columns] == [
        "id",
        "email",
        "created_at",
    ]
    assert manifest.models["model.p.dim_customers"].layer == "marts"
    assert manifest.models["model.p.stg_shop__orders"].layer == "staging"


def test_column_tests_group_by_column(manifest: Manifest) -> None:
    assert manifest.column_tests("model.p.stg_shop__customers") == {
        "customer_id": {"unique", "not_null"}
    }
    assert manifest.column_tests("model.p.stg_shop__orders") == {"customer_id": {"relationships"}}


def test_affected_models_follow_children_tests_and_parents(manifest: Manifest) -> None:
    # Changing customers reaches dim_customers (child) and stg_shop__orders (its
    # relationships test reads customers), and nothing needs more parents than that.
    affected = manifest.affected_models({"model.p.stg_shop__customers"})
    assert affected == [
        "model.p.dim_customers",
        "model.p.stg_shop__customers",
        "model.p.stg_shop__orders",
    ]


def test_affected_models_close_over_ancestors(manifest: Manifest) -> None:
    # Changing the mart alone still needs both staging models built first.
    affected = manifest.affected_models({"model.p.dim_customers"})
    assert affected == [
        "model.p.dim_customers",
        "model.p.stg_shop__customers",
        "model.p.stg_shop__orders",
    ]


def test_affected_models_ignores_unknown_ids(manifest: Manifest) -> None:
    assert manifest.affected_models({"model.p.does_not_exist"}) == []
