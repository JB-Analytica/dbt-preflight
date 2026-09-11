"""The whole check, on the bundled example, without a base branch.

This is the test that proves the pieces fit: DBML in, fixtures in DuckDB, dbt build with
tests, conventions, comment out. It takes a few seconds because dbt really runs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbt_preflight.cli import app

EXAMPLE = Path(__file__).parent.parent / "examples" / "webshop"


@pytest.fixture
def example_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "webshop"
    shutil.copytree(EXAMPLE, dest)
    return dest


def test_example_passes_clean(example_copy: Path, tmp_path: Path) -> None:
    comment = tmp_path / "comment.md"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo-root",
            str(example_copy),
            "--config",
            str(example_copy / ".dbt-preflight.yml"),
            "--comment-file",
            str(comment),
        ],
    )
    assert result.exit_code == 0, result.output
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ✅ passed" in body
    assert "Built 10 of 10 models" in body
    assert "0 convention issues" in body
    assert "| `fct_orders` | ✅ built | 800 |" in body
    assert "| `dim_products` | ✅ built | 60 |" in body
    assert "### Not verified on DuckDB" not in body
    assert not (example_copy / ".preflight").exists()


def test_broken_rename_fails(example_copy: Path, tmp_path: Path) -> None:
    model = example_copy / "dbt/models/staging/webshop/stg_webshop__customers.sql"
    model.write_text(model.read_text().replace("id as customer_id,", "id as cust_id,"))
    comment = tmp_path / "comment.md"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo-root",
            str(example_copy),
            "--config",
            str(example_copy / ".dbt-preflight.yml"),
            "--comment-file",
            str(comment),
        ],
    )
    assert result.exit_code == 1
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ❌ failed" in body
    assert "### Failing tests" in body
    assert "`not_null_stg_webshop__customers_customer_id`" in body
    assert "| `dim_customers` | ⏭️ skipped |" in body


def test_bigquery_sql_is_transpiled_when_the_profile_says_bigquery(
    example_copy: Path, tmp_path: Path
) -> None:
    # Put the warehouse-specific spellings back and let a checked-in profiles.yml say the
    # project targets BigQuery. Every model must build, with nothing "not verified".
    fct = example_copy / "dbt/models/marts/fct_orders.sql"
    fct.write_text(
        fct.read_text().replace(
            "{{ hours_between('orders.shipped_at', 'orders.ordered_at') }}",
            "timestamp_diff(orders.shipped_at, orders.ordered_at, hour)",
        )
    )
    dim = example_copy / "dbt/models/marts/dim_products.sql"
    dim.write_text(
        dim.read_text()
        .replace("{{ title_case('coffee_origin') }}", "initcap(coffee_origin)")
        .replace("{{ title_case('roast_level') }}", "initcap(roast_level)")
        .replace("{{ title_case('product_category') }}", "initcap(product_category)")
    )
    (example_copy / "dbt/profiles.yml").write_text(
        "webshop:\n  target: prod\n  outputs:\n    prod:\n      type: bigquery\n"
        "      method: service-account\n      project: x\n      dataset: y\n"
    )
    comment = tmp_path / "comment.md"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo-root",
            str(example_copy),
            "--config",
            str(example_copy / ".dbt-preflight.yml"),
            "--comment-file",
            str(comment),
        ],
    )
    assert result.exit_code == 0, result.output
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ✅ passed" in body
    assert "Built 10 of 10 models" in body
    assert "Model SQL transpiled from bigquery to DuckDB with sqlglot." in body
    assert "### Not verified on DuckDB" not in body


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _base_and_branch(example_copy: Path, branch: str) -> None:
    _git(example_copy, "init", "-q", "-b", "main")
    _git(example_copy, "add", ".")
    _git(example_copy, "commit", "-q", "-m", "base")
    _git(example_copy, "checkout", "-q", "-b", branch)


def _run_against_main(example_copy: Path, comment: Path):
    return CliRunner().invoke(
        app,
        [
            "run",
            "--base-ref",
            "main",
            "--repo-root",
            str(example_copy),
            "--config",
            str(example_copy / ".dbt-preflight.yml"),
            "--comment-file",
            str(comment),
        ],
    )


def test_unit_test_failure_fails_the_run(example_copy: Path, tmp_path: Path) -> None:
    # Discounts applied per unit instead of per line. A dbt unit test on the intermediate
    # model covers exactly this, and its failure has to be visible and fatal.
    _base_and_branch(example_copy, "discount-per-unit")
    model = example_copy / "dbt/models/intermediate/int_orders__items_aggregated.sql"
    text = model.read_text()
    old = "sum(quantity * unit_price_cents - discount_cents) as net_amount_cents"
    assert old in text
    model.write_text(
        text.replace(old, "sum(quantity * (unit_price_cents - discount_cents)) as net_amount_cents")
    )
    comment = tmp_path / "comment.md"
    result = _run_against_main(example_copy, comment)
    assert result.exit_code == 1
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ❌ failed" in body
    assert (
        "- ❌ unit test `int_orders__items_aggregated_sums_lines_per_order` on "
        "`int_orders__items_aggregated`: actual output differs from the expected rows" in body
    )
    assert "actual differs from expected" in body  # dbt's own diff, in the details block
    assert "| `int_orders__items_aggregated` | ⏭️ skipped |" in body


def test_silent_logic_change_shows_up_in_the_diff(example_copy: Path, tmp_path: Path) -> None:
    # Lifetime revenue now counts cancelled orders. No test says anything about it; only
    # the comparison against the base branch, on the same fixtures, can.
    _base_and_branch(example_copy, "count-cancelled")
    model = example_copy / "dbt/models/marts/dim_customers.sql"
    text = model.read_text()
    old = "    where order_status != 'cancelled'\n"
    assert old in text
    model.write_text(text.replace(old, ""))
    comment = tmp_path / "comment.md"
    result = _run_against_main(example_copy, comment)
    assert result.exit_code == 0, result.output
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ✅ passed" in body
    assert "### What changed in the output" in body
    assert "**`dim_customers`** — rows 150 (unchanged) · " in body
    assert "rows with different values" in body
    assert "Identical output to the base branch" not in body
    assert not (example_copy / ".preflight").exists()


def test_syntax_error_is_a_failure_even_with_a_dialect(example_copy: Path, tmp_path: Path) -> None:
    # With a dialect set, a model with a genuine typo must fail, not be filed as "not
    # verified": sqlglot cannot parse it, DuckDB cannot either, and that is the PR's fault.
    (example_copy / "dbt/profiles.yml").write_text(
        "webshop:\n  target: prod\n  outputs:\n    prod:\n      type: bigquery\n"
        "      method: service-account\n      project: x\n      dataset: y\n"
    )
    model = example_copy / "dbt/models/marts/dim_products.sql"
    model.write_text(model.read_text().replace("select * from final", "selectt * from final"))
    comment = tmp_path / "comment.md"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--repo-root",
            str(example_copy),
            "--config",
            str(example_copy / ".dbt-preflight.yml"),
            "--comment-file",
            str(comment),
        ],
    )
    assert result.exit_code == 1
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ❌ failed" in body
    assert "| `dim_products` | ❌ failed |" in body
    assert "### Not verified on DuckDB" not in body
    assert "Ran as written because sqlglot could not parse them: `dim_products`" in body


def test_seed_only_project_runs_without_fixtures(tmp_path: Path) -> None:
    # No sources at all: the seeds are the input, dbt loads them, nothing is generated.
    src = Path(__file__).parent / "fixtures" / "seed_only"
    dest = tmp_path / "seedshop"
    shutil.copytree(src, dest)
    comment = tmp_path / "comment.md"
    result = CliRunner().invoke(
        app, ["run", "--repo-root", str(dest), "--comment-file", str(comment)]
    )
    assert result.exit_code == 0, result.output
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ✅ passed" in body
    assert "| `customers` | ✅ built | 3 | 2 passed |" in body
    assert "The project declares no sources, so its seeds were the only input" in body
