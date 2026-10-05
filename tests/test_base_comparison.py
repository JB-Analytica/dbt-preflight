"""Pull-request runs against a base branch: seeds, and tests judged against the base.

Each test commits a base to a throwaway git repository, makes a change on a branch, and
runs the whole check with `--base-ref main`. dbt really runs, twice per test (base, then
head), so these take a while.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbt_preflight.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = Path(__file__).parent.parent / "examples" / "webshop"
STG_ORDERS = "dbt/models/staging/webshop/stg_webshop__orders.sql"
STG_YML = "dbt/models/staging/webshop/_webshop__models.yml"
ALL_STATUSES = "values: [pending, paid, shipped, delivered, cancelled]"
NO_CANCELLED = "values: [pending, paid, shipped, delivered]"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_base_then_branch(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "change")


def _edit(repo: Path, rel: str, old: str, new: str) -> None:
    path = repo / rel
    text = path.read_text()
    assert old in text, f"{old!r} not in {rel}"
    path.write_text(text.replace(old, new, 1))


def _run(repo: Path, tmp_path: Path, config: bool = True) -> tuple[int, str, dict]:
    comment, summary = tmp_path / "comment.md", tmp_path / "summary.json"
    args = ["run", "--base-ref", "main", "--repo-root", str(repo)]
    if config:
        args += ["--config", str(repo / ".dbt-preflight.yml")]
    args += ["--comment-file", str(comment), "--summary-file", str(summary)]
    result = CliRunner().invoke(app, args)
    assert summary.exists(), result.output
    return result.exit_code, comment.read_text(), json.loads(summary.read_text())


def _statuses(summary: dict) -> dict[str, str]:
    return {m["name"]: m["status"] for m in summary["models"]}


@pytest.fixture
def webshop(tmp_path: Path) -> Path:
    dest = tmp_path / "webshop"
    shutil.copytree(EXAMPLE, dest)
    return dest


def test_seed_only_project_builds_its_seeds_on_a_pull_request(tmp_path: Path) -> None:
    # Classic jaffle_shop's shape: no sources, every model reads a seed. Before, a harmless
    # comment built 0 of its models: "Table raw_customers does not exist".
    repo = tmp_path / "seedshop"
    shutil.copytree(FIXTURES / "seed_only", repo)
    _commit_base_then_branch(repo)
    _edit(repo, "models/customers.sql", "select id", "-- a harmless comment\nselect id")

    code, body, summary = _run(repo, tmp_path, config=False)
    assert code == 0, body
    assert summary["verdict"] == "passed"
    assert "| `customers` | ✅ built | 3 | 2 passed |" in body
    # The base side loaded the seed too, or there would be nothing to compare against.
    assert "Identical output to the base branch" in body
    assert "`customers`" in body.split("Identical output to the base branch")[1]


def test_seeds_beside_sources_are_loaded_on_both_sides(webshop: Path, tmp_path: Path) -> None:
    # A Tuva-style lookup seed a staging model joins, in a project that also has sources.
    (webshop / "dbt/seeds").mkdir()
    (webshop / "dbt/seeds/order_status_labels.csv").write_text(
        "status_code,status_label\npending,Pending\npaid,Paid\nshipped,Shipped\n"
        "delivered,Delivered\ncancelled,Cancelled\n"
    )
    _edit(
        webshop,
        STG_ORDERS,
        "        _dlt_load_id as dlt_load_id\n    from source\n",
        "        _dlt_load_id as dlt_load_id,\n        labels.status_label as order_status_label\n"
        "    from source\n"
        "    left join {{ ref('order_status_labels') }} as labels\n"
        "        on source.status = labels.status_code\n",
    )
    _commit_base_then_branch(webshop)
    _edit(webshop, STG_ORDERS, "with source as", "-- a harmless comment\nwith source as")

    code, body, summary = _run(webshop, tmp_path)
    assert code == 0, body
    assert summary["counts"]["models"]["failed"] == 0
    assert summary["counts"]["models"]["skipped"] == 0
    assert _statuses(summary)["stg_webshop__orders"] == "built"
    diff = next(d for d in summary["diffs"] if d["name"] == "stg_webshop__orders")
    assert diff["base_exists"] and diff["rows_differing"] == 0


def test_a_test_failing_on_base_too_does_not_fail_or_skip(webshop: Path, tmp_path: Path) -> None:
    # The base already rejects cancelled orders, which the fixtures contain. A harmless
    # change to the model must pass, with the failure reported, and everything downstream
    # of it must still build (inside one `dbt build` the failure skipped all of it).
    _edit(webshop, STG_YML, ALL_STATUSES, NO_CANCELLED)
    _commit_base_then_branch(webshop)
    _edit(webshop, STG_ORDERS, "with source as", "-- a harmless comment\nwith source as")

    code, body, summary = _run(webshop, tmp_path)
    assert code == 0, body
    assert summary["verdict"] == "passed_with_warnings"
    assert summary["failing_tests"] == []
    [old] = summary["preexisting_failing_tests"]
    assert old["readable_name"] == "`accepted_values` on `stg_webshop__orders.order_status`"
    assert old["failures"] == old["base_failures"] and old["failures"] > 0
    assert summary["counts"]["tests"]["failed_on_base"] == 1
    statuses = _statuses(summary)
    for downstream in ("int_orders__items_aggregated", "fct_orders", "dim_customers"):
        assert statuses[downstream] == "built", statuses
    assert "<details><summary>Already failing on the base branch (1)</summary>" in body
    assert "Unchanged models this change breaks" not in body


def test_a_test_new_on_head_that_fails_blocks(webshop: Path, tmp_path: Path) -> None:
    # The pull request only tightens a test. That alone has to run it, and its model.
    _commit_base_then_branch(webshop)
    _edit(webshop, STG_YML, ALL_STATUSES, NO_CANCELLED)

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1
    assert summary["verdict"] == "failed"
    [new] = summary["failing_tests"]
    assert new["readable_name"] == "`accepted_values` on `stg_webshop__orders.order_status`"
    assert new["base_failures"] is None
    assert summary["preexisting_failing_tests"] == []
    assert "| `stg_webshop__orders` |" in body.split("### Changed models")[1].split("###")[0]


def test_a_test_failing_on_more_rows_than_on_base_blocks(webshop: Path, tmp_path: Path) -> None:
    # Fails on base for cancelled orders; the change also turns delivered ones into a
    # status the test rejects, so the same test fails on more rows.
    _edit(webshop, STG_YML, ALL_STATUSES, NO_CANCELLED)
    _commit_base_then_branch(webshop)
    _edit(
        webshop,
        STG_ORDERS,
        "        status as order_status,",
        "        case when status = 'delivered' then 'lost' else status end as order_status,",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1
    assert summary["verdict"] == "failed"
    [worse] = summary["failing_tests"]
    assert worse["readable_name"] == "`accepted_values` on `stg_webshop__orders.order_status`"
    # Worse than on the base, so it is the change's failure: what reads the model is
    # skipped, as one `dbt build` would have done, and blamed on the change.
    statuses = _statuses(summary)
    for downstream in ("fct_orders", "fct_order_items", "dim_customers"):
        assert statuses[downstream] == "skipped", statuses
    assert "Unchanged models this change breaks" in body
    assert worse["failures"] > worse["base_failures"] > 0
    assert f"({worse['base_failures']} on the base branch)" in body


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


CANCELLED_CHECK = "dbt/tests/assert_no_cancelled_orders.sql"


def test_a_rewritten_singular_test_is_not_judged_by_its_old_self(
    webshop: Path, tmp_path: Path
) -> None:
    # A singular test keeps its unique id when its SQL changes. Failing on fewer rows than
    # the old version did must not read as "already failing": it is a different test.
    _write(
        webshop,
        CANCELLED_CHECK,
        "select * from {{ ref('stg_webshop__orders') }} where order_status = 'cancelled'\n",
    )
    _commit_base_then_branch(webshop)
    _edit(webshop, CANCELLED_CHECK, "\n", " and order_id % 2 = 0\n")

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    assert summary["verdict"] == "failed"
    [rewritten] = summary["failing_tests"]
    assert rewritten["name"] == "assert_no_cancelled_orders"
    assert rewritten["base_failures"] is None
    assert summary["preexisting_failing_tests"] == []


def test_a_test_named_like_a_model_does_not_take_the_model_with_it(
    webshop: Path, tmp_path: Path
) -> None:
    # Excluding the failing singular test `dim_customers` by bare name would exclude the
    # model `dim_customers` too, and the model would never build.
    _write(
        webshop,
        "dbt/tests/dim_customers.sql",
        "select customer_id from {{ ref('dim_customers') }} where customer_id <= 3\n",
    )
    _commit_base_then_branch(webshop)
    _edit(
        webshop,
        "dbt/models/marts/dim_customers.sql",
        "with customers as",
        "-- a harmless comment\nwith customers as",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 0, body
    assert summary["verdict"] == "passed_with_warnings"
    assert _statuses(summary)["dim_customers"] == "built"
    assert [t["name"] for t in summary["preexisting_failing_tests"]] == ["dim_customers"]
    assert summary["failing_tests"] == []


def test_a_worse_failure_on_an_ephemeral_model_is_not_lost(webshop: Path, tmp_path: Path) -> None:
    # An ephemeral model never has a run result of its own. A test on it that already
    # fails on the base, and fails worse on the pull request, must still be reported.
    _write(
        webshop,
        "dbt/models/intermediate/int_orders__statuses.sql",
        "{{ config(materialized='ephemeral') }}\n"
        "select case when order_status = 'cancelled' then null else order_id end as order_id\n"
        "from {{ ref('stg_webshop__orders') }}\n",
    )
    _write(
        webshop,
        "dbt/models/intermediate/_int_orders__statuses.yml",
        "version: 2\nmodels:\n  - name: int_orders__statuses\n    columns:\n"
        "      - name: order_id\n        data_tests:\n          - not_null\n",
    )
    _commit_base_then_branch(webshop)
    # Delivered orders now read as cancelled: fine by staging's own tests, but more rows
    # for the ephemeral model's not_null test to fail on.
    _edit(
        webshop,
        STG_ORDERS,
        "        status as order_status,",
        "        case when status = 'delivered' then 'cancelled' else status end as order_status,",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    [worse] = summary["failing_tests"]
    assert worse["readable_name"] == "`not_null` on `int_orders__statuses.order_id`"
    assert worse["failures"] > worse["base_failures"] > 0


def test_a_test_added_on_a_seed_alone_still_runs(tmp_path: Path) -> None:
    # No model changed, only a seed's tests: that is still something to build and run.
    repo = tmp_path / "seedshop"
    shutil.copytree(FIXTURES / "seed_only", repo)
    _commit_base_then_branch(repo)
    _write(
        repo,
        "seeds/_seeds.yml",
        "version: 2\nseeds:\n  - name: raw_customers\n    columns:\n      - name: country\n"
        "        data_tests:\n          - accepted_values:\n              arguments:\n"
        "                values: [BE]\n",
    )

    code, body, summary = _run(repo, tmp_path, config=False)
    assert summary["verdict"] == "failed", body
    [new] = summary["failing_tests"]
    assert new["model"] == "raw_customers"
    assert new["failures"] == 1


def test_a_snapshot_with_a_fixed_schema_is_built_for_the_head_only(
    webshop: Path, tmp_path: Path
) -> None:
    # A legacy `target_schema` is the same on both targets: built on the base first, the
    # head's snapshot would merge onto the base's rows instead of starting fresh.
    _write(
        webshop,
        "dbt/snapshots/orders_snapshot.sql",
        "{% snapshot orders_snapshot %}\n"
        "{{ config(target_schema='snapshots', unique_key='order_id', strategy='check',"
        " check_cols='all') }}\n"
        "select * from {{ ref('stg_webshop__orders') }}\n"
        "{% endsnapshot %}\n",
    )
    _commit_base_then_branch(webshop)
    _edit(webshop, STG_ORDERS, "with source as", "-- a harmless comment\nwith source as")

    code, body, summary = _run(webshop, tmp_path)
    assert code == 0, body
    assert "`orders_snapshot` writes to a fixed `target_schema`" in body
    # The base still built, with the snapshot excluded by an exact selector: the staging
    # model it reads has a base to compare with.
    diff = next(d for d in summary["diffs"] if d["name"] == "stg_webshop__orders")
    assert diff["base_exists"] and diff["rows_differing"] == 0


INT_ORDERS = "dbt/models/intermediate/int_orders__items_aggregated.sql"


def _break_int_orders_on_base(repo: Path) -> None:
    """The intermediate model reads a column that does not exist, on the base branch."""
    _edit(
        repo,
        INT_ORDERS,
        "sum(discount_cents) as discount_cents,",
        "sum(discount) as discount_cents,",
    )


def test_a_model_broken_on_base_too_is_flagged_not_failed(webshop: Path, tmp_path: Path) -> None:
    # The pull request touches staging customers; the intermediate model is broken on main
    # already, and the marts reading it are skipped on both branches. None of that is the
    # change's doing: flagged at the top, not failed, not blamed.
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _edit(
        webshop,
        "dbt/models/staging/webshop/stg_webshop__customers.sql",
        "with source as",
        "-- a harmless comment\nwith source as",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 0, body
    assert summary["verdict"] == "passed_with_warnings"
    [broken] = summary["broken_on_base_models"]
    assert broken["name"] == "int_orders__items_aggregated"
    assert '"discount" not found' in broken["error"]
    assert summary["counts"]["models"]["failed"] == 0
    assert summary["counts"]["models"]["failed_on_base"] == 1
    statuses = {m["name"]: m for m in summary["models"]}
    assert statuses["dim_customers"]["status"] == "skipped"
    assert statuses["dim_customers"]["skipped_by_base"]
    assert summary["counts"]["models"]["skipped"] == 0

    top = body.split("### ⚠️ Broken on main too (1)")[1].split("### Changed models")[0]
    assert "These models also fail on `main`, without this change:" in top
    assert "- `int_orders__items_aggregated` — " in top
    assert "Skipped because of it: " in top and "`dim_customers`" in top
    assert "Unchanged models this change breaks" not in body
    assert "### Build errors" not in body


def test_a_modified_model_broken_on_base_too_still_fails(webshop: Path, tmp_path: Path) -> None:
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _edit(webshop, INT_ORDERS, "with order_items as", "-- still broken\nwith order_items as")

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    assert summary["broken_on_base_models"] == []
    # Its unit test errors on both branches too, but the model it reads was modified, so
    # that error is not pre-existing either: it stays in the build and skips the model.
    assert _statuses(summary)["int_orders__items_aggregated"] in {"failed", "skipped"}
    assert not any(m["skipped_by_base"] for m in summary["models"])


def test_a_model_broken_differently_on_head_fails(webshop: Path, tmp_path: Path) -> None:
    # Broken on main for one column; the change renames another it reads earlier, so on the
    # pull request it fails on that one first. A different error is a new failure.
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _edit(
        webshop,
        "dbt/models/staging/webshop/stg_webshop__order_items.sql",
        "        unit_price_cents,\n",
        "        unit_price_cents as unit_price,\n",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    assert summary["broken_on_base_models"] == []
    assert _statuses(summary)["int_orders__items_aggregated"] in {"failed", "skipped"}
    assert not any(m["skipped_by_base"] for m in summary["models"])


def test_a_failure_caused_by_a_source_change_is_not_broken_on_base(
    webshop: Path, tmp_path: Path
) -> None:
    # The base is built on the head's fixtures. Rename a column in the source schema and
    # staging fails on both branches with the same error, but because of this change: a
    # model downstream of a modified source is never judged by the base.
    _commit_base_then_branch(webshop)
    _edit(
        webshop,
        "webshop.dbml",
        "  email varchar [not null, unique]",
        "  email_address varchar [not null, unique]",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    assert summary["broken_on_base_models"] == []
    assert _statuses(summary)["stg_webshop__customers"] == "failed"
    assert "Broken on main too" not in body


# --- What is skipped because of a model broken on main ------------------------------


def test_a_modified_model_that_now_reads_a_broken_model_counts(
    webshop: Path, tmp_path: Path
) -> None:
    # dim_products built on main. The pull request makes it read the broken intermediate
    # model, so it is skipped on head only: the change's doing, not the base's.
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _edit(
        webshop,
        "dbt/models/marts/dim_products.sql",
        "select * from final",
        "select * from final\n"
        "where product_id not in (select order_id from {{ ref('int_orders__items_aggregated') }})",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    product = next(m for m in summary["models"] if m["name"] == "dim_products")
    assert product["status"] == "skipped" and not product["skipped_by_base"]


def test_a_broken_edit_downstream_of_a_broken_model_counts(webshop: Path, tmp_path: Path) -> None:
    # fct_orders is skipped on both branches, but the pull request edited it (with a typo
    # it never gets to compile): what it changed was never checked, so it counts.
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _edit(webshop, "dbt/models/marts/fct_orders.sql", "select * from final", "selectt * from final")

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    fct = next(m for m in summary["models"] if m["name"] == "fct_orders")
    assert fct["status"] == "skipped" and not fct["skipped_by_base"]


def test_a_new_model_reading_a_broken_model_counts(webshop: Path, tmp_path: Path) -> None:
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _write(
        webshop,
        "dbt/models/marts/fct_order_money.sql",
        "select order_id, net_amount_cents from {{ ref('int_orders__items_aggregated') }}\n",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    new = next(m for m in summary["models"] if m["name"] == "fct_order_money")
    assert new["status"] == "skipped" and not new["skipped_by_base"]
    assert summary["counts"]["models"]["skipped"] >= 1


# --- Errors that can hide behind an error already on main --------------------------


def test_a_new_error_behind_an_old_one_in_the_same_model_counts(
    webshop: Path, tmp_path: Path
) -> None:
    # DuckDB stops at the first error. The intermediate model already fails on `discount`
    # on main; the change renames `discount_cents`, which it reads after that, so the
    # error message is unchanged. Something upstream changed: never broken on main.
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _edit(
        webshop,
        "dbt/models/staging/webshop/stg_webshop__order_items.sql",
        "        discount_cents,\n",
        "        discount_cents as line_discount_cents,\n",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    assert summary["broken_on_base_models"] == []


def test_a_vars_change_judges_nothing_against_the_base(webshop: Path, tmp_path: Path) -> None:
    # dbt's state comparison does not see vars. With dbt_project.yml changed, a model that
    # fails on both branches counts, as it would with no base at all.
    _break_int_orders_on_base(webshop)
    _commit_base_then_branch(webshop)
    _edit(webshop, "dbt/dbt_project.yml", "flags:\n", "vars:\n  discount_rate: 0.1\n\nflags:\n")
    _edit(
        webshop,
        "dbt/models/staging/webshop/stg_webshop__customers.sql",
        "with source as",
        "-- a harmless comment\nwith source as",
    )

    code, body, summary = _run(webshop, tmp_path)
    assert code == 1, body
    assert summary["broken_on_base_models"] == []
    assert "dbt_project.yml` changed, so nothing was judged against the base branch" in body


# --- Fixtures derived from the project: a staging edit reshapes a source -----------


def _derived_shop(repo: Path, cast_a: str, a_values: str) -> None:
    """Two staging models over one source, no DBML: the fixtures are derived from them."""
    _write(
        repo,
        "dbt_project.yml",
        'name: shopx\nversion: "1.0.0"\nconfig-version: 2\nprofile: shopx\n'
        'model-paths: ["models"]\ntest-paths: ["tests"]\n'
        "flags:\n  send_anonymous_usage_stats: false\n",
    )
    _write(
        repo,
        "models/staging/_sources.yml",
        "version: 2\nsources:\n  - name: shop\n    schema: raw\n    tables:\n"
        "      - name: orders\n      - name: customers\n",
    )
    _write(
        repo,
        "models/staging/stg_shop__orders_a.sql",
        f"select id as order_id, cast(amount as {cast_a}) as amount, status\n"
        "from {{ source('shop', 'orders') }}\n",
    )
    _write(
        repo,
        "models/staging/_a.yml",
        "version: 2\nmodels:\n  - name: stg_shop__orders_a\n    columns:\n"
        "      - name: status\n        data_tests:\n          - accepted_values:\n"
        f"              arguments:\n                values: {a_values}\n",
    )
    # Reads a relation no branch builds before anything else, so it fails on both sides
    # with the same first error, whatever the fixtures say about `amount`.
    _write(
        repo,
        "models/staging/stg_shop__orders_b.sql",
        "select o.id as order_id, o.status\nfrom finance.nowhere as n\n"
        "join {{ source('shop', 'orders') }} as o on n.id = o.id\n"
        "join {{ source('shop', 'customers') }} as c on c.id = o.amount\n",
    )
    _write(
        repo,
        "models/staging/stg_shop__orders_c.sql",
        "select id as order_id, status from {{ source('shop', 'orders') }}\n",
    )
    _write(
        repo,
        "tests/assert_no_paid_orders.sql",
        "select * from {{ ref('stg_shop__orders_c') }} where status = 'paid'\n",
    )
    _write(
        repo,
        "models/marts/fct_orders.sql",
        "select a.order_id from {{ ref('stg_shop__orders_a') }} as a\n"
        "join {{ ref('stg_shop__orders_b') }} as b on a.order_id = b.order_id\n"
        "join {{ ref('stg_shop__orders_c') }} as c on a.order_id = c.order_id\n",
    )


def test_a_cast_change_that_reshapes_a_sibling_source_blocks(tmp_path: Path) -> None:
    # The change only edits staging model a's cast, but with no DBML that cast types the
    # `amount` fixture every reader of `orders` gets. Model b fails on both branches with
    # the same first error; the base ran it on other data, so that says nothing.
    repo = tmp_path / "shopx"
    _derived_shop(repo, "integer", "[paid, shipped]")
    _commit_base_then_branch(repo)
    _edit(repo, "models/staging/stg_shop__orders_a.sql", "as integer)", "as varchar)")

    code, body, summary = _run(repo, tmp_path, config=False)
    assert summary["verdict"] == "failed", body
    assert summary["broken_on_base_models"] == []
    assert "stg_shop__orders_b" in [m["name"] for m in summary["models"]]


def test_an_accepted_values_change_that_reshapes_a_source_blocks(tmp_path: Path) -> None:
    # Model a's accepted_values become the `status` enum. Widening it means fewer `paid`
    # rows, so a test on model c fails on fewer rows than on main, which once read as
    # "already failing". Its fixtures changed: it counts.
    repo = tmp_path / "shopx"
    _derived_shop(repo, "integer", "[paid, shipped]")
    _commit_base_then_branch(repo)
    _edit(repo, "models/staging/_a.yml", "[paid, shipped]", "[paid, shipped, delivered, lost]")

    code, body, summary = _run(repo, tmp_path, config=False)
    assert summary["verdict"] == "failed", body
    assert "assert_no_paid_orders" in [t["name"] for t in summary["failing_tests"]]
    assert summary["preexisting_failing_tests"] == []


def test_a_lock_file_rewritten_by_dbt_deps_is_not_a_change(tmp_path: Path) -> None:
    from dbt_preflight.git import paths_changed

    repo = tmp_path / "r"
    _write(repo, "package-lock.yml", "packages: []\n")
    _commit_base_then_branch(repo)
    lock = repo / "package-lock.yml"
    lock.write_text("packages: [rewritten]\n")  # what `dbt deps` does in the working tree
    assert paths_changed(repo, "main", [lock]) == [lock]
    assert paths_changed(repo, "main", [lock], committed_only=True) == []
    _git(repo, "commit", "-qam", "bump the lock")
    assert paths_changed(repo, "main", [lock], committed_only=True) == [lock]
