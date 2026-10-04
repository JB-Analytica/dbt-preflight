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
    # The mart's own accepted_values test rejects the new status too: new, so it counts.
    by_name = {t["readable_name"]: t for t in summary["failing_tests"]}
    worse = by_name["`accepted_values` on `stg_webshop__orders.order_status`"]
    assert by_name["`accepted_values` on `fct_orders.order_status`"]["base_failures"] is None
    assert worse["failures"] > worse["base_failures"] > 0
    assert f"({worse['base_failures']} on the base branch)" in body
