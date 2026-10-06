"""Schema inference from compiled SQL, where the raw SQL hides a source behind a macro."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from dbt_preflight.cli import app
from dbt_preflight.compiled import CompiledSql, CompiledView, compile_selection
from dbt_preflight.dbt_runner import compile_models
from dbt_preflight.manifest import Manifest
from dbt_preflight.schema import SchemaError, derive_dbml

DB = "preflight"


def _source(source_name: str, name: str, columns: dict | None = None) -> dict:
    return {
        "source_name": source_name,
        "name": name,
        "identifier": name,
        "database": DB,
        "schema": f"raw_{source_name}",
        "loader": "",
        "columns": columns or {},
        "relation_name": f'"{DB}"."raw_{source_name}"."{name}"',
    }


def _model(name: str, depends_on: list[str], raw_code: str, compiled: str | None = None) -> dict:
    node = {
        "resource_type": "model",
        "name": name,
        "path": f"staging/{name}.sql",
        "original_file_path": f"models/staging/{name}.sql",
        "database": DB,
        "schema": "main",
        "alias": name,
        "description": "",
        "depends_on": {"nodes": depends_on},
        "config": {"materialized": "view"},
        "columns": {},
        "raw_code": raw_code,
        "relation_name": f'"{DB}"."main"."{name}"',
        "fqn": ["p", "staging", name],
    }
    if compiled is not None:
        node["compiled_code"] = compiled
    return node


def _test(name: str, test_name: str, column: str, model_uid: str) -> dict:
    return {
        "resource_type": "test",
        "name": name,
        "column_name": column,
        "attached_node": model_uid,
        "depends_on": {"nodes": [model_uid]},
        "test_metadata": {"name": test_name, "kwargs": {"column_name": column}},
        "original_file_path": "models/staging/_models.yml",
    }


def _derive(raw: dict, with_compiled: bool = True) -> tuple[str, list]:
    manifest = Manifest.from_dict(raw)
    compiled = CompiledSql.from_dict(raw) if with_compiled else None
    return derive_dbml(manifest, compiled)


def _table(dbml: str, name: str) -> str:
    m = re.search(rf"^Table {name} \{{\n(.*?)^\}}", dbml, re.MULTILINE | re.DOTALL)
    assert m, f"no table {name} in\n{dbml}"
    return m.group(1)


# --- Fivetran: a `_tmp` model per source, then fill_staging_columns -------------------

SRC = "source.p.shopify.customer"
TMP = "model.p.stg_shopify__customer_tmp"
STG = "model.p.stg_shopify__customer"

_TMP_RAW = """{{ fivetran_utils.union_connections(connection_dictionary='shopify_sources',
    single_source_name='shopify', single_table_name='customer') }}"""
# What `union_connections` renders when the relation does not exist (preflight compiles
# against an empty database).
_TMP_STAND_IN = """
-- ** Values passed to adapter.get_relation:
        select
            cast(null as TEXT) as _dbt_source_relation
        limit 0
"""
_STG_RAW = """with base as (select * from {{ ref('stg_shopify__customer_tmp') }}),
fields as (
    select {{ fivetran_utils.fill_staging_columns(
        source_columns=adapter.get_columns_in_relation(ref('stg_shopify__customer_tmp')),
        staging_columns=get_customer_columns()) }}
    from base
)
select id as customer_id, lower(email) as email, accepts_marketing from fields"""
# The spike's compile of the real stg_shopify__customer, cut down.
_STG_COMPILED = f"""
with base as (
    select * from "{DB}"."main"."stg_shopify__customer_tmp"
),
fields as (
    select
            cast(null as timestamp) as created_at,
            cast(null as numeric(28,6)) as id,
            cast(null as numeric(28,6)) as default_address_id,
            cast(null as TEXT) as email,
            cast(null as boolean) as accepts_marketing,
            cast(null as float) as total_spent,
            cast(null as integer) as orders_count
    , cast('{DB}.shopify' as TEXT) as source_relation
    from base
),
final as (
    select id as customer_id, lower(email) as email, accepts_marketing, total_spent
    from fields
)
select * from final
"""


def _fivetran(tmp_compiled: str = _TMP_STAND_IN, declared: dict | None = None) -> dict:
    return {
        "sources": {SRC: _source("shopify", "customer", declared)},
        "nodes": {
            TMP: _model("stg_shopify__customer_tmp", [SRC], _TMP_RAW, tmp_compiled),
            STG: _model("stg_shopify__customer", [TMP], _STG_RAW, _STG_COMPILED),
            "test.p.u": _test("unique_customer_id", "unique", "customer_id", STG),
        },
    }


def test_fivetran_tmp_and_fill_staging_columns_derive_columns_and_types() -> None:
    dbml, inferred = _derive(_fivetran())
    table = _table(dbml, "customer")
    assert "  id int [pk]" in table  # a numeric key is an integer
    assert "  created_at timestamp\n" in table
    assert "  email varchar\n" in table
    assert "  accepts_marketing boolean\n" in table
    assert "  total_spent float\n" in table
    assert "  orders_count int\n" in table
    assert "  default_address_id int\n" in table
    # dbt's own bookkeeping column and a literal are not source columns.
    assert "_dbt_source_relation" not in table
    assert "source_relation" not in table
    [src] = inferred
    assert src.compiled_columns == sorted(src.compiled_columns)
    assert len(src.compiled_columns) == 7
    assert src.models == ["stg_shopify__customer"]


def test_without_compiled_sql_fivetran_is_still_an_error() -> None:
    with pytest.raises(SchemaError):
        _derive(_fivetran(), with_compiled=False)


def test_pure_select_star_tmp_is_a_pass_through_too() -> None:
    star = f'select * from "{DB}"."raw_shopify"."customer"'
    raw = _fivetran(tmp_compiled=star)
    view = CompiledView(Manifest.from_dict(raw), CompiledSql.from_dict(raw))
    assert view.pass_through == {TMP: SRC}
    dbml, _ = _derive(raw)
    assert "  email varchar\n" in _table(dbml, "customer")


def test_a_model_with_columns_is_not_a_pass_through() -> None:
    raw = _fivetran(tmp_compiled=f'select id from "{DB}"."raw_shopify"."customer"')
    view = CompiledView(Manifest.from_dict(raw), CompiledSql.from_dict(raw))
    assert view.pass_through == {}


def test_raw_pass_through_counts_when_it_did_not_compile() -> None:
    raw = _fivetran()
    del raw["nodes"][TMP]["compiled_code"]
    raw["nodes"][TMP]["raw_code"] = "select * from {{ source('shopify', 'customer') }}"
    view = CompiledView(Manifest.from_dict(raw), CompiledSql.from_dict(raw))
    assert view.pass_through == {TMP: SRC}


def test_declared_columns_no_model_reads_are_typed_by_name() -> None:
    # Fivetran's sources.yml documents columns the staging macro does not select.
    declared = {"_fivetran_deleted": {"name": "_fivetran_deleted"}, "email": {"name": "email"}}
    dbml, inferred = _derive(_fivetran(declared=declared))
    table = _table(dbml, "customer")
    assert "  _fivetran_deleted varchar\n" in table
    assert "_fivetran_deleted" in inferred[0].guessed_columns


def test_unread_columns_are_a_flagged_varchar_when_a_reader_did_not_compile() -> None:
    declared = {"_fivetran_deleted": {"name": "_fivetran_deleted"}}
    raw = _fivetran(declared=declared)
    compiled = CompiledSql.from_dict(raw)
    del compiled.code[STG]
    dbml, inferred = derive_dbml(Manifest.from_dict(raw), compiled)
    assert "  _fivetran_deleted varchar\n" in _table(dbml, "customer")
    assert inferred[0].unknown_columns == ["_fivetran_deleted"]


def test_typed_null_keys_keep_their_foreign_key_ref() -> None:
    raw = _fivetran()
    orders_src = "source.p.shopify.orders"
    orders = "model.p.stg_shopify__orders"
    raw["sources"][orders_src] = _source("shopify", "orders")
    raw["nodes"][orders] = _model(
        "stg_shopify__orders",
        [orders_src],
        "{{ fill() }}",
        "select cast(null as numeric(28,6)) as id, cast(null as numeric(28,6)) as customer_id",
    )
    dbml, _ = _derive(raw)
    assert "  customer_id int [ref: > customer.id]" in _table(dbml, "orders")


# --- A project macro around source() --------------------------------------------------

B_SRC = "source.p.billing.creditors"
B_STG = "model.p.stg_billing__creditors"

# The raw SQL builds its select list in a loop, which inference from raw SQL cannot follow.
_LOOP_RAW = """with source as ({{ source_or_empty('billing', 'creditors') }})
select {% for c in ['number', 'name'] %}cast({{ c }} as {{ dbt.type_string() }})
as creditor_{{ c }}{% if not loop.last %}, {% endif %}{% endfor %}
from source"""
_LOOP_COMPILED = f"""with source as (select * from "{DB}"."raw_billing"."creditors")
select cast(number as TEXT) as creditor_number, cast(name as TEXT) as creditor_name
from source"""


def _billing() -> dict:
    return {
        "sources": {B_SRC: _source("billing", "creditors")},
        "nodes": {
            B_STG: _model("stg_billing__creditors", [B_SRC], _LOOP_RAW, _LOOP_COMPILED),
            "test.p.u": _test("u", "unique", "creditor_number", B_STG),
            "test.p.n": _test("n", "not_null", "creditor_number", B_STG),
        },
    }


def test_compiled_alias_map_carries_unique_back_through_a_macro() -> None:
    dbml, inferred = _derive(_billing())
    table = _table(dbml, "creditors")
    assert "  number varchar [pk]" in table
    assert "  name varchar\n" in table
    assert inferred[0].compiled_columns == ["name", "number"]


def test_only_one_primary_key_per_table() -> None:
    # `id` is the key by name; a second column carrying unique + not_null must not become
    # half of a composite key, or neither is unique in the fixtures.
    raw = _billing()
    raw["sources"][B_SRC]["columns"] = {
        "id": {"name": "id", "data_type": "int"},
        "number": {"name": "number", "data_type": "string"},
        "name": {"name": "name", "data_type": "string"},
    }
    dbml, _ = _derive(raw)
    table = _table(dbml, "creditors")
    assert "  id int [pk]" in table
    assert "  number varchar [unique, not null]" in table


def test_raw_type_wins_over_compiled_and_the_conflict_is_noted() -> None:
    raw = {
        "sources": {B_SRC: _source("billing", "creditors")},
        "nodes": {
            B_STG: _model(
                "stg_billing__creditors",
                [B_SRC],
                "select cast(number as int) as n from {{ source('billing', 'creditors') }}",
                f'select cast(number as varchar) as n from "{DB}"."raw_billing"."creditors"',
            ),
        },
    }
    dbml, inferred = _derive(raw)
    assert "  number int\n" in _table(dbml, "creditors")
    assert inferred[0].type_conflicts == ["`number`: int in the raw SQL, varchar compiled"]
    assert inferred[0].compiled_columns == []


# --- Ambiguity, failures, and projects that already worked ----------------------------


def test_null_cast_in_a_multi_source_model_is_not_attributed() -> None:
    a, b = "source.p.shop.a", "source.p.shop.b"
    both = "model.p.both"
    raw = {
        "sources": {a: _source("shop", "a"), b: _source("shop", "b")},
        "nodes": {
            both: _model(
                "both",
                [a, b],
                "{{ macro() }}",
                f'select x.id, cast(null as integer) as mystery from "{DB}"."raw_shop"."a" x '
                f'join "{DB}"."raw_shop"."b" y on x.id = y.id',
            ),
        },
    }
    dbml, _ = _derive(raw)
    assert "mystery" not in dbml
    assert "  id int [pk]" in _table(dbml, "a")


def test_a_model_that_failed_to_compile_falls_back_to_raw_sql() -> None:
    raw = _billing()
    raw["nodes"][B_STG]["raw_code"] = (
        "select number as creditor_number from {{ source('billing', 'creditors') }}"
    )
    del raw["nodes"][B_STG]["compiled_code"]
    compiled = CompiledSql.from_dict(raw, failed=["stg_billing__creditors"])
    with_failure, _ = derive_dbml(Manifest.from_dict(raw), compiled)
    raw_only, _ = derive_dbml(Manifest.from_dict(raw))
    assert with_failure == raw_only
    assert "  number int [pk]" in with_failure  # raw SQL alone: typed by its name


def _compile_like_dbt(raw: dict) -> dict:
    """Give every model the compiled code dbt would: source() and ref() calls rendered."""
    out = copy.deepcopy(raw)
    sources = {(s["source_name"], s["name"]): s for s in out["sources"].values()}
    for src in out["sources"].values():
        src.setdefault("relation_name", f'"{src["database"]}"."{src["schema"]}"."{src["name"]}"')
        sources[(src["source_name"], src["name"])] = src
    models = {n["name"]: n for n in out["nodes"].values() if n["resource_type"] == "model"}
    for node in models.values():
        node.setdefault("relation_name", f'"{DB}"."main"."{node["name"]}"')
    for node in models.values():
        sql = re.sub(
            r"\{\{\s*source\('([^']+)',\s*'([^']+)'\)\s*\}\}",
            lambda m: sources[(m.group(1), m.group(2))]["relation_name"],
            node.get("raw_code", ""),
        )
        sql = re.sub(
            r"\{\{\s*ref\('([^']+)'\)\s*\}\}", lambda m: models[m.group(1)]["relation_name"], sql
        )
        node["compiled_code"] = sql
    return out


_JAFFLE_CUSTOMERS = """with source as (select * from {{ source('jaffle', 'customers') }}),
renamed as (
    select id as customer_id, name, lower(email) as email, is_active,
        cast(signup_date as date) as signup_date, created_at
    from source
)
select * from renamed"""
_JAFFLE_ORDERS = """with source as (select * from {{ source('jaffle', 'orders') }}),
renamed as (select id as order_id, customer_id, order_total_cents, ordered_at from source)
select * from renamed"""


def test_projects_that_already_work_derive_byte_identical_dbml(raw_manifest: dict) -> None:
    c, o = "source.j.jaffle.customers", "source.j.jaffle.orders"
    stg_c, stg_o = "model.j.stg_customers", "model.j.stg_orders"
    jaffle = {
        "sources": {c: _source("jaffle", "customers"), o: _source("jaffle", "orders")},
        "nodes": {
            stg_c: _model("stg_customers", [c], _JAFFLE_CUSTOMERS),
            stg_o: _model("stg_orders", [o], _JAFFLE_ORDERS),
            "test.j.u": _test("u", "unique", "customer_id", stg_c),
            "test.j.n": _test("n", "not_null", "order_id", stg_o),
        },
    }
    for project in (raw_manifest, jaffle):
        compiled = _compile_like_dbt(project)
        before = derive_dbml(Manifest.from_dict(project))
        after = derive_dbml(Manifest.from_dict(compiled), CompiledSql.from_dict(compiled))
        assert after[0] == before[0]
        assert [s.compiled_columns for s in after[1]] == [[] for _ in before[1]]


def test_derivation_is_deterministic() -> None:
    raw = _fivetran()
    assert _derive(raw)[0] == _derive(copy.deepcopy(raw))[0]


def test_compile_selection_is_source_readers_and_pass_through_readers() -> None:
    raw = _fivetran()
    raw["nodes"]["model.p.mart"] = _model("mart", [STG], "select * from {{ ref('x') }}")
    assert compile_selection(Manifest.from_dict(raw)) == [STG, TMP]


class _FakeRunner:
    """Stands in for DbtRunner: fails on the given models (unique id -> file path) until
    their exact selectors are excluded."""

    def __init__(self, tmp_path, failing: dict[str, str], selectors: dict[str, str]) -> None:
        self.target_path = tmp_path
        self.failing = failing
        self.selectors = selectors
        self.calls: list[list[str]] = []

    def _args(self, command: str, *extra: str) -> list[str]:
        return [command, *extra]

    def _invoke(self, args: list[str], quiet: bool = False):
        self.calls.append(args)
        excluded = args[args.index("--exclude") + 1 :] if "--exclude" in args else []
        left = [u for u in sorted(self.failing) if self.selectors[u] not in excluded]
        if left:
            name = left[0].rsplit(".", 1)[-1]
            msg = f"Runtime Error\n  Compilation Error in model {name} ({self.failing[left[0]]})"
            return SimpleNamespace(success=False, exception=msg, result=None)
        (self.target_path / "manifest.json").write_text("{}")
        return SimpleNamespace(success=True, exception=None, result=None)


_SELECTORS = {
    "model.p.metafields": "resource_type:model,fqn:p.metafields",
    "model.p.dim_customer.v1": "resource_type:model,fqn:p.dim_customer.v1",
    "model.p.dim_customer.v2": "resource_type:model,fqn:p.dim_customer.v2",
}
_PATHS = {
    "models/metafields.sql": "model.p.metafields",
    "models/dim_customer_v1.sql": "model.p.dim_customer.v1",
    "models/dim_customer_v2.sql": "model.p.dim_customer.v2",
}


def test_compile_leaves_out_failing_models_by_exact_selector(tmp_path) -> None:
    # Version 1 of a versioned model fails; version 2 must still compile, so the exclusion
    # is by selector, never by the bare name `dim_customer`.
    failing = {"model.p.metafields": "models/metafields.sql",
               "model.p.dim_customer.v1": "models/dim_customer_v1.sql"}  # fmt: skip
    runner = _FakeRunner(tmp_path, failing, _SELECTORS)
    outcome = compile_models(runner, _SELECTORS, _PATHS)  # type: ignore[arg-type]
    assert outcome.manifest == tmp_path / "manifest.json"
    assert outcome.failed == ["model.p.dim_customer.v1", "model.p.metafields"]
    assert len(runner.calls) == 3
    last = runner.calls[-1]
    excluded = last[last.index("--exclude") + 1 :]
    assert excluded == [_SELECTORS["model.p.dim_customer.v1"], _SELECTORS["model.p.metafields"]]
    assert _SELECTORS["model.p.dim_customer.v2"] in last[: last.index("--exclude")]
    assert "dim_customer" not in last


def test_compile_gives_up_on_an_error_it_cannot_attribute(tmp_path) -> None:
    runner = _FakeRunner(tmp_path, {}, _SELECTORS)
    runner._invoke = lambda args, quiet=False: SimpleNamespace(  # type: ignore[method-assign]
        success=False, exception="Database Error: disk full", result=None
    )
    outcome = compile_models(runner, _SELECTORS, _PATHS)  # type: ignore[arg-type]
    assert outcome.manifest is None


def test_a_side_that_did_not_compile_counts_every_source_as_reshaped() -> None:
    from dbt_preflight.cli import _reshaped_sources

    raw = _fivetran()
    manifest = Manifest.from_dict(raw)
    compiled = CompiledSql.from_dict(raw)
    # Only the head compiled: the two derivations are not like for like.
    assert _reshaped_sources(manifest, manifest, compiled, None) == {SRC}
    # Both compiled the same way: nothing differs.
    assert _reshaped_sources(manifest, manifest, compiled, compiled) == set()


# --- Review findings: carry-back from the wrong model, readers that are not accounted for


def _cols(d: dict) -> dict:
    return {k: {"name": k, "data_type": v} for k, v in d.items()}


def _fan_out(typed: bool) -> dict:
    """A `customer_orders` mart joining a `select *` staging model of orders with
    customers, testing its own grain (`customer_id` unique and not null)."""
    orders = {"id": "integer", "customer_id": "integer" if typed else None,
              "amount": "numeric" if typed else None}  # fmt: skip
    customers = {"id": "integer", "name": "varchar" if typed else None}
    o, c = "source.p.s.orders", "source.p.s.customers"
    raw = {
        "sources": {o: _source("s", "orders", _cols(orders)),
                    c: _source("s", "customers", _cols(customers))},  # fmt: skip
        "nodes": {
            "model.p.stg_orders": _model(
                "stg_orders", [o], "select * from {{ source('s','orders') }}",
                f'select * from "{DB}"."raw_s"."orders"',
            ),
            "model.p.stg_customers": _model(
                "stg_customers", [c], "select id, name from {{ source('s','customers') }}",
                f'select id, name from "{DB}"."raw_s"."customers"',
            ),
            "model.p.customer_orders": _model(
                "customer_orders", ["model.p.stg_orders", "model.p.stg_customers"], "x",
                f'select o.customer_id, c.name, sum(o.amount) as total '
                f'from "{DB}"."main"."stg_orders" o join "{DB}"."main"."stg_customers" c '
                f"on c.id = o.customer_id group by 1, 2",
            ),
            "test.p.u": _test("u", "unique", "customer_id", "model.p.customer_orders"),
            "test.p.n": _test("n", "not_null", "customer_id", "model.p.customer_orders"),
        },
    }  # fmt: skip
    return raw


def test_a_marts_grain_test_is_not_carried_back_through_a_pass_through() -> None:
    dbml, _ = _derive(_fan_out(typed=False))
    table = _table(dbml, "orders")
    assert "  customer_id int [ref: > customers.id]" in table  # a key, not a unique one
    assert "unique" not in table and "not null" not in table


def test_fully_typed_project_with_a_mart_over_select_star_is_byte_identical() -> None:
    raw = _fan_out(typed=True)
    assert _derive(raw)[0] == _derive(raw, with_compiled=False)[0]


def test_a_single_source_model_that_groups_carries_nothing_back() -> None:
    raw = _fan_out(typed=False)
    raw["nodes"]["model.p.customer_orders"].update(
        depends_on={"nodes": ["model.p.stg_orders"]},
        compiled_code=f'select customer_id, count(*) as n from "{DB}"."main"."stg_orders" '
        "group by 1",
    )
    dbml, _ = _derive(raw)
    assert "unique" not in _table(dbml, "orders")


def _events(compiled_sql: str) -> dict:
    src = "source.p.s.events"
    return {
        "sources": {src: _source("s", "events", _cols({"id": "integer", "occurred": None}))},
        "nodes": {
            "model.p.stg_events": _model(
                "stg_events", [src], "select {{ cols() }} from {{ my_rel('s', 'events') }}",
                compiled_sql,
            ),
        },
    }  # fmt: skip


@pytest.mark.parametrize(
    "compiled_sql",
    [
        # sqlglot cannot parse it in any dialect
        f'select id, amount ->>> 2 from "{DB}"."raw_s"."events"',
        # the relation is not one preflight knows
        'select id from "elsewhere"."raw_s"."events_v2"',
        # a star that is not a pass-through hands unknown columns on
        f'select *, now() as loaded_at from "{DB}"."raw_s"."events"',
        f'select * exclude (id) from "{DB}"."raw_s"."events"',
        f'with s as (select * from "{DB}"."raw_s"."events") select s.*, 1 as one from s',
        # an unqualified column next to a join could be the source's
        f'select e.id, occurred from "{DB}"."raw_s"."events" e join "{DB}"."main"."x" x '
        "on x.id = e.id",
    ],
)
def test_an_unaccounted_reader_makes_the_unread_column_a_flagged_varchar(
    compiled_sql: str,
) -> None:
    # Not trusted as unread, not guessed by name: text, and the comment says why.
    dbml, inferred = _derive(_events(compiled_sql))
    assert "  occurred varchar\n" in _table(dbml, "events")
    assert inferred[0].unknown_columns == ["occurred"]


def test_two_part_relation_names_match_an_unambiguous_source() -> None:
    dbml, inferred = _derive(_events("select id, cast(occurred as date) as d from raw_s.events"))
    assert "  occurred date\n" in _table(dbml, "events")
    assert inferred[0].guessed_columns == []


def test_the_projects_dialect_parses_what_duckdb_cannot() -> None:
    raw = _events(f'select id, occurred\nfrom "{DB}"."raw_s"."events" # a BigQuery comment')
    # DuckDB's and the default grammar reject `#`: the reader cannot be followed.
    _, inferred = derive_dbml(Manifest.from_dict(raw), CompiledSql.from_dict(raw))
    assert inferred[0].unknown_columns == ["occurred"]
    dbml, inferred = derive_dbml(
        Manifest.from_dict(raw), CompiledSql.from_dict(raw, dialect="bigquery")
    )
    assert "  occurred " in _table(dbml, "events")
    assert inferred[0].unknown_columns == []


def test_typed_null_refs_only_for_id_names_to_integer_keys() -> None:
    raw = _fivetran()
    orders_src, accounts_src = "source.p.shopify.orders", "source.p.shopify.accounts"
    raw["sources"][orders_src] = _source("shopify", "orders")
    raw["sources"][accounts_src] = _source(
        "shopify", "accounts", _cols({"id": "varchar", "name": "varchar"})
    )
    raw["nodes"]["model.p.stg_shopify__orders"] = _model(
        "stg_shopify__orders", [orders_src], "{{ fill() }}",
        "select cast(null as numeric(28,6)) as id, cast(null as integer) as customer, "
        "cast(null as integer) as account_id, cast(null as numeric(28,6)) as customer_id",
    )  # fmt: skip
    table = _table(_derive(raw)[0], "orders")
    assert "  customer int\n" in table  # a count as often as a key: no ref
    assert "  account_id int\n" in table  # accounts.id is a varchar: no ref
    assert "  customer_id int [ref: > customer.id]" in table


@pytest.mark.parametrize(
    "tmp_sql",
    [
        f'select * from "{DB}"."raw_shopify"."customer" limit 10',
        f'select * from "{DB}"."raw_shopify"."customer" offset 1',
        f'select * replace (1 as id) from "{DB}"."raw_shopify"."customer"',
        f'select * exclude (id) from "{DB}"."raw_shopify"."customer"',
    ],
)
def test_star_with_limit_or_modifiers_is_not_a_pass_through(tmp_sql: str) -> None:
    raw = _fivetran(tmp_compiled=tmp_sql)
    view = CompiledView(Manifest.from_dict(raw), CompiledSql.from_dict(raw))
    assert view.pass_through == {}


# --- End to end: dbt really compiles and builds ----------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "source_or_empty"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_source_or_empty_project_carries_unique_back_and_passes(tmp_path: Path) -> None:
    repo = tmp_path / "billing"
    shutil.copytree(FIXTURE, repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "harmless")
    model = repo / "models/staging/stg_billing__creditors.sql"
    model.write_text("-- a comment\n" + model.read_text())
    _git(repo, "commit", "-q", "-am", "harmless")

    comment, summary = tmp_path / "comment.md", tmp_path / "summary.json"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo-root",
            str(repo),
            "--base-ref",
            "main",
            "--comment-file",
            str(comment),
            "--summary-file",
            str(summary),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(summary.read_text())
    assert data["verdict"] in {"passed", "passed_with_warnings"}, comment.read_text()
    assert data["counts"]["tests"]["failed"] == 0
    # Both sides were derived from compiled SQL the same way: no source counts as reshaped.
    assert "derived fixtures differ" not in result.output
    debtors = next(s for s in data["fixtures"]["inferred_sources"] if s["table"] == "debtors")
    assert debtors["compiled_columns"] == ["name", "number"]
    assert "2 columns for 1 sources read from compiled SQL." in comment.read_text()


def test_source_or_empty_project_whole_build(tmp_path: Path) -> None:
    repo = tmp_path / "billing"
    shutil.copytree(FIXTURE, repo)
    comment = tmp_path / "comment.md"
    result = CliRunner().invoke(
        app, ["run", "--repo-root", str(repo), "--comment-file", str(comment)]
    )
    assert result.exit_code == 0, result.output
    body = comment.read_text()
    assert "Built 2 of 2 models" in body
    # `id` is the key, and creditor_number unique on its own: both unique tests pass.
    assert "| `stg_billing__creditors` | ✅ built | 200 | 4 passed |" in body
    assert "| `stg_billing__debtors` | ✅ built | 200 | 2 passed |" in body
    assert (
        "Read by no model, snapshot or test, so no fixture: `raw_billing.archived_invoices`" in body
    )
    assert "Sources with no matching table" not in body


def test_a_test_carries_back_through_a_typed_null_placeholder() -> None:
    # Fivetran's `final` reads `accepts_marketing` from `fields`, where it is the typed null
    # standing for the source column: a not_null test on it is a claim about the source.
    raw = _fivetran()
    raw["nodes"]["test.p.nn"] = _test("nn", "not_null", "accepts_marketing", STG)
    table = _table(_derive(raw)[0], "customer")
    assert "  accepts_marketing boolean [not null]" in table


# --- Second review: the remaining gaps -------------------------------------------------

O_SRC, C_SRC = "source.p.s.orders", "source.p.s.customers"
O_REL, C_REL = f'"{DB}"."raw_s"."orders"', f'"{DB}"."raw_s"."customers"'


def _orders(models: dict, tests: dict | None = None, amount_type=None) -> dict:
    orders = {"id": "integer", "customer_id": "integer", "amount": amount_type,
              "status": amount_type}  # fmt: skip
    return {
        "sources": {
            O_SRC: _source("s", "orders", _cols(orders)),
            C_SRC: _source("s", "customers", _cols({"id": "integer", "region": "varchar"})),
        },
        "nodes": {**models, **(tests or {})},
    }


def _accounted(raw: dict, model_uid: str) -> bool:
    from dbt_preflight.schema import _reader_accounted_for

    manifest = Manifest.from_dict(raw)
    view = CompiledView(manifest, CompiledSql.from_dict(raw))
    return _reader_accounted_for(view, manifest.models[model_uid], manifest.sources[O_SRC])


@pytest.mark.parametrize(
    "compiled_sql",
    [
        # a star inside a CTE that joins the source with something else
        f"with j as (select * from {O_REL} o join {C_REL} c on c.id = o.customer_id) "
        "select j.status::integer as status from j",
        # whole-row references and pattern selection
        f"select id, to_json(o) as payload from {O_REL} o",
        f"select id, struct_pack(o.*) as payload from {O_REL} o",
        f"select id, columns('am.*') from {O_REL} o",
    ],
)
def test_readers_the_walk_cannot_follow_are_not_accounted_for(compiled_sql: str) -> None:
    deps = [O_SRC, C_SRC] if "join" in compiled_sql else [O_SRC]
    raw = _orders({"model.p.m": _model("m", deps, "x", compiled_sql)})
    assert not _accounted(raw, "model.p.m")
    _, inferred = _derive(raw)
    assert "status" in next(i for i in inferred if i.table == "orders").unknown_columns


def test_a_whole_row_reference_is_not_a_column() -> None:
    raw = _orders({"model.p.m": _model("m", [O_SRC], "x", f"select id, to_json(o) from {O_REL} o")})
    assert "  o " not in _table(_derive(raw)[0], "orders")


def test_a_single_source_star_cte_read_by_name_is_accounted_for() -> None:
    sql = f"with j as (select * from {O_REL} o) select j.status::integer as status, j.amount from j"
    raw = _orders({"model.p.m": _model("m", [O_SRC], "x", sql)})
    assert _accounted(raw, "model.p.m")
    assert "  status int\n" in _table(_derive(raw)[0], "orders")


def test_a_filter_in_a_pass_through_stops_carry_back() -> None:
    raw = _orders(
        {
            "model.p.base_orders": _model(
                "base_orders", [O_SRC],
                "select * from {{ source('s','orders') }} where status <> 'deleted'",
                f"select * from {O_REL} where status <> 'deleted'",
            ),
            "model.p.stg_orders": _model(
                "stg_orders", ["model.p.base_orders"], "x",
                f'select id as order_id, customer_id from "{DB}"."main"."base_orders"',
            ),
        },
        {"test.p.u": _test("u", "unique", "customer_id", "model.p.stg_orders")},
    )  # fmt: skip
    view = CompiledView(Manifest.from_dict(raw), CompiledSql.from_dict(raw))
    assert view.pass_through == {"model.p.base_orders": O_SRC}  # still its columns
    assert view.filtered == {"model.p.base_orders"}  # but not its rows
    assert "unique" not in _table(_derive(raw)[0], "orders")


@pytest.mark.parametrize(
    "tail",
    [
        "select order_id, unnest(string_split(tags, ',')) as tag from {r}",
        "select order_id, generate_series(1, 3) as n from {r}",
        "select order_id from {r} using sample 10%",
        "select order_id from {r} order by id fetch first 5 rows only",
        "select order_id from {r} offset 5",
        "select order_id from (select * from {r}) unpivot (v for k in (id, tags))",
        "select order_id, explode(split(tags, ',')) as tag from {r}",
        # a CTE of typed nulls over nothing does not stand for the source
        "with s as (select * from {r}), d as (select cast(null as integer) as order_id) "
        "select order_id from d",
    ],
)
def test_grain_changes_and_unrelated_typed_nulls_carry_nothing_back(tail: str) -> None:
    rel = f'"{DB}"."raw_s"."lines"'
    src = "source.p.s.lines"
    cols = _cols({"id": "integer", "order_id": "integer", "tags": "varchar"})
    raw = {
        "sources": {src: _source("s", "lines", cols)},
        "nodes": {
            "model.p.stg_lines": _model("stg_lines", [src], "x", tail.format(r=rel)),
            "test.p.u": _test("u", "unique", "order_id", "model.p.stg_lines"),
        },
    }
    assert "  order_id int\n" in _table(_derive(raw)[0], "lines")


def _direct_mart(raw_code: str, compiled_sql: str) -> dict:
    return _orders(
        {"model.p.mart": _model("mart", [O_SRC], raw_code, compiled_sql)},
        {"test.p.u": _test("u", "unique", "customer_id", "model.p.mart")},
        amount_type="varchar",
    )


@pytest.mark.parametrize("with_compiled", [False, True])
def test_a_mart_reading_the_source_directly_carries_nothing_back(with_compiled: bool) -> None:
    raw = _direct_mart(
        "select customer_id, sum(amount) as total from {{ source('s','orders') }} group by 1",
        f"select customer_id, sum(amount) as total from {O_REL} group by 1",
    )
    assert "  customer_id int\n" in _table(_derive(raw, with_compiled)[0], "orders")


def test_a_staging_model_with_a_where_carries_nothing_back_from_raw_sql() -> None:
    raw = _direct_mart(
        "select customer_id from {{ source('s','orders') }} where status <> 'deleted'",
        f"select customer_id from {O_REL} where status <> 'deleted'",
    )
    assert "  customer_id int\n" in _table(_derive(raw, with_compiled=False)[0], "orders")


def test_a_plain_staging_model_still_carries_its_tests_back_from_raw_sql() -> None:
    raw = _direct_mart(
        "select customer_id, status from {{ source('s','orders') }}",
        f"select customer_id, status from {O_REL}",
    )
    for with_compiled in (False, True):
        assert "  customer_id int [unique]" in _table(_derive(raw, with_compiled)[0], "orders")


# --- Step 2: nothing untyped stops the run ---------------------------------------------


def test_skipped_sources_and_unknown_columns_are_said_plainly() -> None:
    from dbt_preflight.fixtures import FixtureSummary
    from dbt_preflight.report import PreflightReport, _fixtures_block
    from dbt_preflight.schema import InferredSource

    report = PreflightReport()
    report.fixtures = FixtureSummary(
        skipped_sources=["shopify_graphql.tax_line"],
        inferred_sources=[
            InferredSource(
                "s",
                "events",
                "events",
                ["stg_events"],
                3,
                guessed_columns=["amount", "occurred"],
                unknown_columns=["occurred"],
            )  # fmt: skip
        ],
    )
    block = _fixtures_block(report)
    assert "Read by no model, snapshot or test, so no fixture: `shopify_graphql.tax_line`" in block
    assert "types guessed for `amount`" in block
    assert "typed varchar because a model reading it could not be followed" in block
    assert "`occurred`" in block


def test_a_wildcard_identifier_gets_a_dbml_name_and_keeps_its_relation(tmp_path) -> None:
    from dbt_preflight.schema import resolve_schema

    src = "source.p.ga4.events"
    raw = {
        "sources": {
            src: {
                **_source("ga4", "events", _cols({"event_name": "varchar"})),
                "identifier": "events_*",
            }
        },  # fmt: skip
        "nodes": {"model.p.stg": _model("stg", [src], "select event_name from x")},
    }
    resolved = resolve_schema(None, Manifest.from_dict(raw), tmp_path)
    assert list(resolved.tables) == ["events__"]
