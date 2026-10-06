"""The one-line summary under the verdict heading, and its twin in the summary JSON."""

from __future__ import annotations

from dbt_preflight.checks import Violation
from dbt_preflight.diff import MetricDiff, ModelDiff
from dbt_preflight.report import (
    BUILT,
    FAILED,
    FailedTest,
    ModelReport,
    PreflightReport,
    headline,
    render,
)
from dbt_preflight.summary import build_summary


def _model(name: str, status: str = BUILT, changed: bool = True, **kw) -> ModelReport:
    folder = "marts" if name.startswith(("dim_", "fct_")) else "staging"
    return ModelReport(
        unique_id=f"model.p.{name}",
        name=name,
        path=f"models/{folder}/{name}.sql",
        status=status,
        changed=changed,
        **kw,
    )


def _metric(label: str, base: float | None, head: float | None, name: str = "") -> MetricDiff:
    return MetricDiff(name=name or label.lower(), label=label, source="s", base=base, head=head)


def _diff(name: str = "fct_orders", metrics=(), **kw) -> ModelDiff:
    return ModelDiff(
        unique_id=f"model.p.{name}", name=name, base_exists=True, metrics=list(metrics), **kw
    )


def _test(name: str = "t") -> FailedTest:
    return FailedTest(name=name, model="m", status="fail", failures=3, message="")


def _top_line(report: PreflightReport) -> str:
    lines = render(report).splitlines()
    assert lines[1].startswith("## 🛫 dbt preflight:")
    assert lines[2] == ""
    return lines[3]


def _report(**kw) -> PreflightReport:
    kw.setdefault("models", [_model("stg_a")])
    kw.setdefault("base_ref", "origin/main")
    return PreflightReport(**kw)


def test_clean_pull_request_with_no_metrics_defined() -> None:
    assert _top_line(_report()) == "No new failures · touches 1 model"


def test_line_sits_between_heading_and_built_line() -> None:
    lines = render(_report()).splitlines()
    assert lines[4] == ""
    assert lines[5].startswith("Built 1 of 1 models")


def test_failures_count_models_tests_and_convention_errors() -> None:
    report = _report(
        models=[_model("stg_a", FAILED), _model("stg_b", FAILED), _model("stg_c")],
        tests=[_test()],
        violations=[Violation("naming", "error", "stg_c", "models/stg_c.sql", "bad")],
    )
    assert headline(report)["new_failures"] == 4  # type: ignore[index]
    assert _top_line(report) == "4 new failures · touches 3 models"


def test_singular_forms_and_warning_tests_do_not_count() -> None:
    warn = FailedTest(name="w", model="m", status="warn", failures=1, message="")
    report = _report(models=[_model("stg_a")], tests=[_test(), warn])
    assert _top_line(report) == "1 new failure · touches 1 model"


def test_preexisting_and_broken_on_base_are_not_new_failures() -> None:
    old = FailedTest(name="o", model="m", status="fail", failures=1, message="", preexisting=True)
    report = _report(
        models=[_model("stg_a"), _model("stg_b", FAILED, changed=False, broken_on_base=True)],
        tests=[old],
    )
    assert _top_line(report) == "No new failures · touches 2 models · 1 broken on main"


def test_unverified_count_includes_models_reached_through_a_base_error_and_dialect_gaps() -> None:
    report = _report(
        models=[
            _model("stg_a", FAILED, unverified_broken_on_base=True),
            _model("stg_b", "not_verified"),
        ],
    )
    h = headline(report)
    assert h["new_failures"] == 0  # type: ignore[index]
    assert h["unverified"] == 2  # type: ignore[index]
    assert _top_line(report) == "No new failures · touches 2 models · 2 could not be checked"


def test_moved_metrics_name_the_biggest_by_relative_delta() -> None:
    report = _report(
        models=[_model("fct_orders"), _model("dim_customers")],
        diffs=[
            _diff(
                metrics=[
                    _metric("Revenue", 1_000_000, 1_020_000),
                    _metric("Total lifetime value", 100, 104.6),
                    _metric("Orders", 10, 10),
                ]
            )
        ],
        metrics_defined=3,
    )
    assert _top_line(report) == (
        "No new failures · moves 2 metrics (Total lifetime value +4.6%) · touches 2 marts"
    )
    top = headline(report)["top_metric"]  # type: ignore[index]
    assert top["label"] == "Total lifetime value"


def test_biggest_metric_uses_absolute_value_of_the_delta() -> None:
    report = _report(
        diffs=[_diff(metrics=[_metric("Up", 100, 110), _metric("Down", 100, 80)])],
        metrics_defined=2,
    )
    assert "(Down -20.0%)" in _top_line(report)


def test_ties_break_on_label_then_name() -> None:
    a = _report(diffs=[_diff(metrics=[_metric("Zeta", 100, 110), _metric("Alpha", 100, 90)])])
    b = _report(diffs=[_diff(metrics=[_metric("Alpha", 100, 90), _metric("Zeta", 100, 110)])])
    assert headline(a)["top_metric"]["label"] == "Alpha"  # type: ignore[index]
    assert headline(b)["top_metric"]["label"] == "Alpha"  # type: ignore[index]
    same = _report(
        diffs=[_diff(metrics=[_metric("Rev", 10, 11, name="b"), _metric("Rev", 10, 11, name="a")])]
    )
    assert headline(same)["top_metric"]["name"] == "a"  # type: ignore[index]


def test_a_metric_off_a_zero_base_outranks_percentages() -> None:
    report = _report(
        diffs=[_diff(metrics=[_metric("Big", 100, 900), _metric("New", 0, 5)])],
    )
    assert headline(report)["top_metric"]["label"] == "New"  # type: ignore[index]
    assert "(New +5)" in _top_line(report)


def test_no_metric_moved_says_so_only_when_metrics_are_defined() -> None:
    defined = _report(diffs=[_diff(metrics=[_metric("Rev", 10, 10)])], metrics_defined=1)
    assert _top_line(defined) == "No new failures · moves no metrics · touches 1 model"
    assert headline(defined)["top_metric"] is None  # type: ignore[index]
    undefined = _report(diffs=[_diff()], metrics_defined=0)
    assert "metrics" not in _top_line(undefined)


def test_values_changed_is_said_only_without_metrics() -> None:
    changed = _report(
        models=[_model("stg_a"), _model("stg_b")],
        diffs=[
            _diff("stg_a", rows_base=10, rows_head=12),
            _diff("stg_b", rows_base=10, rows_head=10, rows_differing=4),
            _diff("stg_c", rows_base=10, rows_head=10, rows_differing=0),
        ],
    )
    assert _top_line(changed) == "No new failures · changes values in 2 models · touches 2 models"
    with_metrics = _report(diffs=[_diff(rows_base=1, rows_head=2)], metrics_defined=2)
    assert "changes values" not in _top_line(with_metrics)
    assert "moves no metrics" in _top_line(with_metrics)
    quiet = _report(diffs=[_diff(rows_base=1, rows_head=1, rows_differing=0)])
    assert "changes values" not in _top_line(quiet)


def test_marts_when_any_are_touched_otherwise_models() -> None:
    marts = _report(models=[_model("stg_a"), _model("fct_orders"), _model("dim_customers")])
    assert _top_line(marts).endswith("touches 2 marts")
    one = _report(models=[_model("fct_orders")])
    assert _top_line(one).endswith("touches 1 mart")
    staging = _report(models=[_model("stg_a"), _model("stg_b")])
    assert _top_line(staging).endswith("touches 2 models")


def test_full_build_has_failures_and_unverified_but_no_scope() -> None:
    report = PreflightReport(models=[_model("stg_a", FAILED), _model("fct_orders")])
    h = headline(report)
    assert h["touched_models"] is None  # type: ignore[index]
    assert h["moved_metrics"] is None  # type: ignore[index]
    assert _top_line(report) == "1 new failure"
    clean = PreflightReport(models=[_model("stg_a")])
    assert _top_line(clean) == "No new failures"


def test_nothing_changed_and_fatal_have_no_line() -> None:
    nothing = PreflightReport(base_ref="origin/main", nothing_changed=True)
    assert headline(nothing) is None
    assert "No new failures" not in render(nothing)
    assert render(nothing).splitlines()[3].startswith("No models changed")
    fatal = PreflightReport(fatal="no manifest")
    assert headline(fatal) is None
    assert render(fatal).splitlines()[3] == "no manifest"


def test_thousands_are_formatted_like_the_rest_of_the_comment() -> None:
    models = [_model(f"stg_{i}") for i in range(1200)]
    assert _top_line(_report(models=models)).endswith("touches 1,200 models")


def test_summary_carries_the_same_headline() -> None:
    report = _report(
        models=[_model("fct_orders"), _model("stg_b", FAILED, changed=False, broken_on_base=True)],
        diffs=[_diff(metrics=[_metric("Revenue", 100, 110)])],
        metrics_defined=1,
        head="abc",
    )
    summary = build_summary(report, exit_code=0, comment_file=None)
    h = summary["headline"]
    assert summary["schema_version"] == 2
    assert h == {
        "new_failures": 0,
        "moved_metrics": 1,
        "metrics_defined": 1,
        "top_metric": {"name": "revenue", "label": "Revenue", "base": 100, "head": 110},
        "value_changed_models": 0,
        "touched_models": 2,
        "touched_marts": 1,
        "unverified": 0,
        "broken_on_base": 1,
        "text": "No new failures · moves 1 metric (Revenue +10.0%) · touches 1 mart · "
        "1 broken on main",
    }
    assert h["text"] == _top_line(report)


def test_summary_headline_is_null_when_there_is_no_line() -> None:
    nothing = build_summary(
        PreflightReport(base_ref="main", nothing_changed=True), exit_code=0, comment_file=None
    )
    assert nothing["headline"] is None
