"""The JSON summary, built by hand rather than through a run: one PreflightReport, every
verdict, and a check that every value is JSON-native (no dataclass slipped through)."""

from __future__ import annotations

import json
from pathlib import Path

from dbt_preflight.checks import Violation
from dbt_preflight.diff import MetricDiff, ModelDiff
from dbt_preflight.report import (
    BUILT,
    FAILED,
    NOT_VERIFIED,
    FailedTest,
    ModelReport,
    PreflightReport,
)
from dbt_preflight.summary import SCHEMA_VERSION, build_summary


def _model(name: str, status: str = BUILT, changed: bool = True, **kw) -> ModelReport:
    return ModelReport(
        unique_id=f"model.p.{name}",
        name=name,
        path=f"models/{name}.sql",
        status=status,
        changed=changed,
        **kw,
    )


def test_schema_version_and_json_native() -> None:
    report = PreflightReport(
        models=[_model("stg_shop__customers", rows=150, tests_passed=2)],
        base_ref="origin/main",
        head="abc123",
        elapsed=3.2,
    )
    summary = build_summary(report, exit_code=0, comment_file=Path("comment.md"))
    assert summary["schema_version"] == SCHEMA_VERSION
    # No dataclass or Path survives into the payload: everything round-trips through JSON.
    encoded = json.dumps(summary)
    assert json.loads(encoded) == summary
    assert summary["comment_file"] == "comment.md"


def test_verdict_passed() -> None:
    report = PreflightReport(models=[_model("m", rows=10, tests_passed=1)], base_ref="main")
    summary = build_summary(report, exit_code=0, comment_file=None)
    assert summary["verdict"] == "passed"
    assert summary["exit_code"] == 0
    assert summary["counts"]["models"] == {
        "built": 1,
        "failed": 0,
        "skipped": 0,
        "not_verified": 0,
        "no_result": 0,
    }


def test_verdict_passed_with_warnings() -> None:
    report = PreflightReport(
        models=[_model("m", status=NOT_VERIFIED, dialect_function="initcap")], base_ref="main"
    )
    summary = build_summary(report, exit_code=0, comment_file=None)
    assert summary["verdict"] == "passed_with_warnings"
    assert summary["counts"]["models"]["not_verified"] == 1


def test_verdict_failed() -> None:
    report = PreflightReport(
        models=[_model("m", status=FAILED, changed=False, message="boom")], base_ref="main"
    )
    summary = build_summary(report, exit_code=1, comment_file=None)
    assert summary["verdict"] == "failed"
    assert summary["exit_code"] == 1
    assert summary["counts"]["models"]["failed"] == 1


def test_verdict_could_not_run() -> None:
    report = PreflightReport(fatal="Configuration error: no dbt_project.yml")
    summary = build_summary(report, exit_code=1, comment_file=None)
    assert summary["verdict"] == "could_not_run"
    assert summary["fatal"] == "Configuration error: no dbt_project.yml"
    assert summary["models"] == []


def test_verdict_nothing_changed() -> None:
    report = PreflightReport(nothing_changed=True, base_ref="main")
    summary = build_summary(report, exit_code=0, comment_file=None)
    assert summary["verdict"] == "nothing_changed"


def test_failing_tests_carry_readable_name_and_reading() -> None:
    report = PreflightReport(
        models=[_model("stg_webshop__customers", tests_failed=1)],
        tests=[
            FailedTest(
                name="unique_stg_webshop__customers_customer_id",
                model="stg_webshop__customers",
                status="error",
                failures=None,
                message=(
                    "Runtime Error in test\n"
                    '  Binder Error: Referenced column "customer_id" not found in FROM clause!'
                ),
                test_name="unique",
                column_name="customer_id",
            ),
            FailedTest(
                name="assert_positive_revenue",
                model="dim_customers",
                status="fail",
                failures=1,
                message="",
            ),
        ],
        base_ref="main",
    )
    summary = build_summary(report, exit_code=1, comment_file=None)
    generic, singular = summary["failing_tests"]
    assert generic["name"] == "unique_stg_webshop__customers_customer_id"
    assert generic["readable_name"] == "`unique` on `stg_webshop__customers.customer_id`"
    assert (
        generic["reading"] == "this model has no column `customer_id`: renamed or dropped upstream?"
    )
    assert singular["readable_name"] is None
    assert singular["reading"] is None


def test_violations_and_diffs_shape() -> None:
    report = PreflightReport(
        models=[_model("dim_customers", changed=False)],
        violations=[
            Violation("primary_key", "error", "dim_customers", "models/dim_customers.sql", "no pk"),
            Violation(
                "description", "warn", "dim_customers", "models/dim_customers.sql", "no description"
            ),
        ],
        diffs=[
            ModelDiff(
                unique_id="model.p.dim_customers",
                name="dim_customers",
                base_exists=True,
                rows_base=150,
                rows_head=142,
                columns_removed=[("segment", "VARCHAR")],
                references={"segment": ["its YAML column entry (models/_marts.yml)"]},
                metrics=[MetricDiff("net_revenue", "Net revenue (EUR)", "dbt", 8190.0, 7830.0)],
            )
        ],
        metrics_defined=1,
        base_ref="main",
    )
    summary = build_summary(report, exit_code=1, comment_file=None)
    assert summary["counts"]["violations"] == {"error": 1, "warn": 1}
    assert summary["counts"]["metrics"] == {"defined": 1, "moved": 1}
    (diff,) = summary["diffs"]
    assert diff["rows_base"] == 150 and diff["rows_head"] == 142
    assert diff["columns_removed"] == [{"name": "segment", "type": "VARCHAR"}]
    assert diff["removed_column_references"] == {
        "segment": ["its YAML column entry (models/_marts.yml)"]
    }
    assert diff["moved_metrics"] == [
        {
            "name": "net_revenue",
            "label": "Net revenue (EUR)",
            "base": 8190.0,
            "head": 7830.0,
            "spans": [],
        }
    ]


def test_fixtures_none_when_report_has_none() -> None:
    report = PreflightReport(models=[_model("m")], base_ref="main")
    summary = build_summary(report, exit_code=0, comment_file=None)
    assert summary["fixtures"] is None
