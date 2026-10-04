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
