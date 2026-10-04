from conftest import _generic_test

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


def _with_seed_and_snapshot(raw: dict) -> Manifest:
    """The fixture project, plus a seed staging reads and a snapshot over the mart."""
    seed = "seed.p.country_codes"
    snap = "snapshot.p.dim_customers_snapshot"
    cust = "model.p.stg_shop__customers"
    mart = "model.p.dim_customers"
    raw["nodes"][seed] = {"resource_type": "seed", "name": "country_codes"}
    raw["nodes"][snap] = {"resource_type": "snapshot", "name": "dim_customers_snapshot"}
    raw["parent_map"][cust] = [*raw["parent_map"][cust], seed]
    raw["parent_map"][snap] = [mart]
    raw["child_map"][seed] = [cust]
    raw["child_map"][mart] = [snap]
    return Manifest.from_dict(raw)


def test_affected_nodes_include_the_seeds_the_models_read(raw_manifest: dict) -> None:
    # A pull-request build that selected models alone never loaded the seed, and every
    # model reading it failed with "table does not exist".
    manifest = _with_seed_and_snapshot(raw_manifest)
    nodes = manifest.affected_nodes({"model.p.dim_customers"})
    assert "seed.p.country_codes" in nodes
    assert "snapshot.p.dim_customers_snapshot" in nodes  # downstream of the change
    assert manifest.node_name("seed.p.country_codes") == "country_codes"
    # The report's rows stay models only.
    assert manifest.affected_models({"model.p.dim_customers"}) == [
        "model.p.dim_customers",
        "model.p.stg_shop__customers",
        "model.p.stg_shop__orders",
    ]


def test_a_modified_seed_affects_the_models_that_read_it(raw_manifest: dict) -> None:
    manifest = _with_seed_and_snapshot(raw_manifest)
    assert manifest.affected_models({"seed.p.country_codes"}) == [
        "model.p.dim_customers",
        "model.p.stg_shop__customers",
        "model.p.stg_shop__orders",
    ]


def test_an_edited_test_affects_the_model_it_is_declared_on(manifest: Manifest) -> None:
    # A pull request that only tightens a test must still build the model and run it, and
    # with it whatever else has a test reading that model (the relationships test).
    assert manifest.affected_models({"test.p.u1"}) == [
        "model.p.stg_shop__customers",
        "model.p.stg_shop__orders",
    ]
    assert manifest.tested_models({"test.p.u1"}) == {"model.p.stg_shop__customers"}


def test_unit_tests_are_read_with_the_model_they_exercise(raw_manifest: dict) -> None:
    raw_manifest["unit_tests"] = {
        "unit_test.p.dim_customers.sums": {
            "model": "dim_customers",
            "depends_on": {"nodes": ["model.p.dim_customers"]},
        }
    }
    manifest = Manifest.from_dict(raw_manifest)
    assert manifest.tested_models({"unit_test.p.dim_customers.sums"}) == {"model.p.dim_customers"}
    assert "model.p.dim_customers" in manifest.affected_models({"unit_test.p.dim_customers.sums"})


def test_selector_pins_one_node_even_when_a_test_shares_a_models_name(raw_manifest: dict) -> None:
    # A singular test named `dim_customers`: a bare-name selector would also match the model.
    raw_manifest["nodes"]["model.p.dim_customers"]["fqn"] = ["p", "marts", "dim_customers"]
    raw_manifest["nodes"]["test.p.dim_customers"] = {
        "resource_type": "test",
        "name": "dim_customers",
        "fqn": ["p", "dim_customers"],
        "depends_on": {"nodes": ["model.p.dim_customers"]},
    }
    manifest = Manifest.from_dict(raw_manifest)
    assert manifest.selector("test.p.dim_customers") == "resource_type:test,fqn:p.dim_customers"
    assert manifest.selector("model.p.dim_customers") == (
        "resource_type:model,fqn:p.marts.dim_customers"
    )


def test_a_test_added_on_a_seed_selects_the_seed(raw_manifest: dict) -> None:
    _with_seed_and_snapshot(raw_manifest)
    raw_manifest["nodes"]["test.p.seed_check"] = _generic_test(
        "accepted_values_country_codes_code",
        "accepted_values",
        "code",
        "seed.p.country_codes",
        ["seed.p.country_codes"],
    )
    manifest = Manifest.from_dict(raw_manifest)
    assert manifest.affected_nodes({"test.p.seed_check"}) == ["seed.p.country_codes"]
    assert manifest.affected_models({"test.p.seed_check"}) == []


def test_snapshots_with_a_fixed_target_schema_are_marked(raw_manifest: dict) -> None:
    raw_manifest["nodes"]["snapshot.p.legacy"] = {
        "resource_type": "snapshot",
        "name": "legacy",
        "config": {"target_schema": "snapshots"},
    }
    raw_manifest["nodes"]["snapshot.p.modern"] = {
        "resource_type": "snapshot",
        "name": "modern",
        "config": {"target_schema": None, "schema": "snapshots"},
    }
    manifest = Manifest.from_dict(raw_manifest)
    assert manifest.fixed_schema_snapshots == {"snapshot.p.legacy"}
