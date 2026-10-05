"""When a failing test counts against the pull request, and when it was already failing."""

from __future__ import annotations

from dbt_preflight.baseline import is_broken_on_base, is_preexisting, same_error
from dbt_preflight.dbt_runner import NodeResult


def _result(status: str, failures: int | None = None, message: str = "") -> NodeResult:
    return NodeResult(
        unique_id="test.p.t1",
        name="t1",
        resource_type="test",
        status=status,
        message=message,
        failures=failures,
        execution_time=0.0,
    )


def test_same_failure_on_both_sides_is_preexisting() -> None:
    assert is_preexisting(_result("fail", 108), _result("fail", 108))


def test_fewer_failing_rows_on_head_is_still_preexisting() -> None:
    assert is_preexisting(_result("fail", 90), _result("fail", 108))


def test_more_failing_rows_on_head_is_worse_and_counts() -> None:
    assert not is_preexisting(_result("fail", 120), _result("fail", 108))


def test_new_test_or_passing_on_base_counts() -> None:
    assert not is_preexisting(_result("fail", 3), None)
    assert not is_preexisting(_result("fail", 3), _result("pass", 0))
    assert not is_preexisting(_result("fail", 3), _result("skipped"))


def test_passing_or_warning_on_head_is_never_preexisting() -> None:
    assert not is_preexisting(_result("pass", 0), _result("fail", 3))
    assert not is_preexisting(_result("warn", 3), _result("fail", 3))


def test_error_where_base_failed_counts_and_so_does_the_reverse() -> None:
    assert not is_preexisting(_result("error", message="Binder Error: x"), _result("fail", 3))
    assert not is_preexisting(_result("fail", 3), _result("error", message="Binder Error: x"))


def test_same_error_on_both_sides_is_preexisting_across_schemas() -> None:
    head = 'Runtime Error in test t1\n  Catalog Error: Table "preflight_marts"."x" missing'
    base = 'Runtime Error in test t1\n  Catalog Error: Table "preflight_base_marts"."x" missing'
    assert is_preexisting(_result("error", message=head), _result("error", message=base))


def test_a_different_error_on_head_counts() -> None:
    head = "Runtime Error\n  Binder Error: column customer_id not found"
    base = "Runtime Error\n  Catalog Error: function initcap does not exist"
    assert not is_preexisting(_result("error", message=head), _result("error", message=base))


def _model(status: str, message: str = "", uid: str = "model.p.m") -> NodeResult:
    return NodeResult(
        unique_id=uid,
        name="m",
        resource_type="model",
        status=status,
        message=message,
        failures=None,
        execution_time=0.0,
    )


def test_model_broken_the_same_way_on_base_is_broken_on_base() -> None:
    head = _model("error", 'Runtime Error\n  Catalog Error: Table "preflight_raw"."x" missing')
    base = _model("error", 'Runtime Error\n  Catalog Error: Table "preflight_base_raw"."x" missing')
    assert is_broken_on_base(head, base, untrusted=set())


def test_a_modified_model_or_a_different_error_or_a_base_build_counts() -> None:
    head = _model("error", "Runtime Error\n  Binder Error: column a not found")
    assert not is_broken_on_base(head, _model("error", head.message), untrusted={"model.p.m"})
    assert not is_broken_on_base(
        head, _model("error", "Runtime Error\n  Binder Error: column b not found"), set()
    )
    assert not is_broken_on_base(head, _model("success"), set())
    assert not is_broken_on_base(head, None, set())


def test_two_different_contract_violations_are_different_errors() -> None:
    # Every enforced-contract error opens with the same line; the mismatch is below it.
    head = (
        "Compilation Error in model m (models/m.sql)\n"
        "  This model has an enforced contract that failed.\n"
        "  | column_name | definition_type | contract_type | mismatch_reason |\n"
        "  | amount      | VARCHAR         | INTEGER       | data type mismatch |\n"
    )
    base = (
        "Compilation Error in model m (models/m.sql)\n"
        "  This model has an enforced contract that failed.\n"
        "  | column_name | definition_type | contract_type | mismatch_reason |\n"
        "  | ordered_at  | VARCHAR         | TIMESTAMP     | data type mismatch |\n"
    )
    assert not same_error(head, base)
    assert same_error(head, head.replace("amount     ", "amount"))  # whitespace only
    assert not is_broken_on_base(_model("error", head), _model("error", base), set())


def test_caret_offsets_and_base_schemas_do_not_make_an_error_different() -> None:
    head = (
        "Runtime Error\n  Binder Error: column x not found\n"
        '  LINE 3: from "preflight_staging"."t" where x > 1\n'
        "                                           ^\n"
    )
    base = (
        "Runtime Error\n  Binder Error: column x not found\n"
        '  LINE 3: from "preflight_base_staging"."t" where x > 1\n'
        "                                                ^\n"
    )
    assert same_error(head, base)
