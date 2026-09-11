from __future__ import annotations

from dbt_preflight.checks import Violation
from dbt_preflight.fixtures import FixtureSummary, LoadedTable
from dbt_preflight.report import (
    BUILT,
    FAILED,
    MARKER,
    NOT_VERIFIED,
    SKIPPED,
    FailedTest,
    ModelReport,
    PreflightReport,
    render,
)
from dbt_preflight.schema import InferredSource


def _model(name: str, status: str = BUILT, changed: bool = True, **kw) -> ModelReport:
    return ModelReport(
        unique_id=f"model.p.{name}",
        name=name,
        path=f"models/{name}.sql",
        status=status,
        changed=changed,
        **kw,
    )


def _fixtures() -> FixtureSummary:
    return FixtureSummary(tables=[LoadedTable("shop", "customers", "customers", "raw", 150)])


def test_clean_run_passes() -> None:
    report = PreflightReport(
        models=[_model("stg_shop__customers", rows=150, tests_passed=2)],
        fixtures=_fixtures(),
        base_ref="origin/main",
        schema_source="shop.dbml",
        seed=42,
        elapsed=3.2,
    )
    body = render(report)
    assert report.passed and not report.has_warnings
    assert body.startswith(MARKER)
    assert "## 🛫 dbt preflight: ✅ passed" in body
    assert "| `stg_shop__customers` | ✅ built | 150 | 2 passed |" in body
    assert "Built 1 of 1 models (1 changed)" in body
    assert "What this checks, and what it cannot" in body


def test_fixtures_block_shows_inference_and_model2data_warnings() -> None:
    fixtures = FixtureSummary(
        tables=[LoadedTable("jaffle_shop", "customers", "customers", "raw", 100)],
        inferred_sources=[
            InferredSource(
                source_name="jaffle_shop",
                table="customers",
                identifier="customers",
                models=["stg_customers"],
                total_columns=6,
                guessed_columns=["name", "email"],
            )
        ],
        unmapped_columns=[("notes", "varchar")],
        cyclic_tables=["orders"],
        unresolved_composite_keys=["order_items (order_id, product_id)"],
    )
    report = PreflightReport(
        models=[_model("stg_customers", rows=100, tests_passed=1)],
        fixtures=fixtures,
        schema_source="sources.yml (derived)",
        seed=42,
    )
    body = render(report)
    assert "`customers`: 6 columns inferred from `stg_customers`" in body
    assert "types guessed for `name`, `email`" in body
    assert "filled with placeholder text" in body.lower()
    assert "`notes`" in body
    assert "`orders`" in body and "unresolved foreign-key cycle" in body
    assert "`order_items (order_id, product_id)`" in body


def test_failing_test_fails_the_run_and_shows_one_line() -> None:
    report = PreflightReport(
        models=[_model("stg_shop__customers", tests_passed=1, tests_failed=1)],
        tests=[
            FailedTest(
                name="unique_x",
                model="stg_shop__customers",
                status="error",
                failures=None,
                message=(
                    "Runtime Error in test unique_x (models/x.yml)\n"
                    '  Binder Error: Referenced column "customer_id" not found\n'
                    "  LINE 3: ..."
                ),
                compiled_code="select 1",
            )
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert not report.passed
    assert "❌ failed" in body
    assert (
        '- ❌ `unique_x` on `stg_shop__customers`: Binder Error: Referenced column "customer_id" not found'
        in body
    )
    assert "<details><summary>details</summary>" in body


def test_dialect_failure_on_changed_model_is_a_warning_only() -> None:
    report = PreflightReport(
        models=[_model("fct_orders", status=NOT_VERIFIED, dialect_function="timestamp_diff")],
        base_ref="origin/main",
    )
    body = render(report)
    assert report.passed and report.has_warnings
    assert "⚠️ passed with warnings" in body
    assert "### Not verified on DuckDB" in body
    assert "`fct_orders` (models/fct_orders.sql) — `timestamp_diff`" in body


def test_dialect_failure_can_be_made_an_error() -> None:
    report = PreflightReport(
        models=[_model("fct_orders", status=NOT_VERIFIED, dialect_function="initcap")],
        dialect_failures_are_errors=True,
        base_ref="origin/main",
    )
    assert not report.passed


def test_unchanged_not_verified_model_is_mentioned_not_warned() -> None:
    report = PreflightReport(
        models=[
            _model("stg_shop__products", tests_passed=3),
            _model("dim_products", status=NOT_VERIFIED, changed=False, dialect_function="initcap"),
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert report.passed and not report.has_warnings
    assert "Also rebuilt, no new issues: `dim_products` (not verified: `initcap`)." in body
    assert "### Not verified on DuckDB" not in body


def test_broken_unchanged_models_are_listed() -> None:
    report = PreflightReport(
        models=[
            _model("stg_shop__customers", tests_passed=3),
            _model("stg_shop__orders", changed=False, tests_failed=1),
            _model("dim_customers", status=SKIPPED, changed=False),
            _model("fct_orders", status=FAILED, changed=False, message="boom"),
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert not report.passed
    assert "Unchanged models this change breaks:" in body
    assert "- `stg_shop__orders` — ✅ built, 1 failing test" in body
    assert "- `dim_customers` — ⏭️ skipped (an upstream model or test failed)" in body
    assert "- `fct_orders` — ❌ failed" in body
    assert "### Build errors" in body


def test_convention_errors_fail_and_warnings_do_not() -> None:
    warn_only = PreflightReport(
        models=[_model("dim_customers")],
        violations=[
            Violation(
                "description", "warn", "dim_customers", "models/dim_customers.sql", "no description"
            )
        ],
        base_ref="origin/main",
    )
    assert warn_only.passed and warn_only.has_warnings
    assert "- ⚠️ **description** `models/dim_customers.sql` — no description" in render(warn_only)

    with_error = PreflightReport(
        models=[_model("dim_customers")],
        violations=[
            Violation("primary_key", "error", "dim_customers", "models/dim_customers.sql", "no pk")
        ],
        base_ref="origin/main",
    )
    assert not with_error.passed
    assert "- ❌ **primary_key**" in render(with_error)


def test_fatal_and_nothing_changed_have_their_own_shapes() -> None:
    fatal = render(PreflightReport(fatal="Configuration error: no dbt_project.yml"))
    assert "❌ could not run" in fatal and "no dbt_project.yml" in fatal

    quiet = render(PreflightReport(nothing_changed=True, base_ref="origin/main"))
    assert "✅ passed" in quiet and "No models changed" in quiet


def test_no_base_run_says_so() -> None:
    body = render(PreflightReport(models=[_model("dim_customers")], base_ref=None))
    assert "(no base branch, all built)" in body
    assert "### Models" in body


def test_diff_section_lists_moves_and_identical_models() -> None:
    from dbt_preflight.diff import MetricDiff, ModelDiff

    report = PreflightReport(
        models=[_model("fct_orders"), _model("dim_products")],
        diffs=[
            ModelDiff(
                unique_id="model.p.fct_orders",
                name="fct_orders",
                base_exists=True,
                rows_base=800,
                rows_head=742,
                columns_removed=[("shipped_at", "TIMESTAMP")],
                metrics=[
                    MetricDiff("net_revenue", "Net revenue (EUR)", "dbt", 12345.6789, 11002.1),
                    MetricDiff("orders", "Orders", "dbt", 800, 742),
                    MetricDiff("aov", "Average order value", "dbt", 15.43, 15.43),
                    MetricDiff(
                        "cum", "Cumulative", "dbt", None, None, unsupported="needs a time spine"
                    ),
                ],
            ),
            ModelDiff(
                unique_id="model.p.dim_products",
                name="dim_products",
                base_exists=True,
                rows_base=60,
                rows_head=60,
                metrics=[MetricDiff("skus", "SKUs", "lightdash", 60, 60)],
            ),
        ],
        metrics_defined=5,
        base_ref="origin/main",
    )
    body = render(report)
    assert report.passed and report.has_warnings  # a removed column is a warning
    assert "### What changed in the output" in body
    assert "**`fct_orders`** — rows 800 → 742 (-7.2%) · columns: −`shipped_at` ⚠️" in body
    assert "| Net revenue (EUR) | 12,345.68 | 11,002.10 | -10.9% |" in body
    assert "| Orders | 800 | 742 | -7.2% |" in body
    assert "1 metric unchanged; not evaluated: Cumulative (needs a time spine)." in body
    assert (
        "Identical output to the base branch, same columns, rows and values and every defined metric: `dim_products`."
        in body
    )
    assert "No metrics are defined" not in body


def test_diff_section_without_metrics_says_where_to_define_them() -> None:
    from dbt_preflight.diff import ModelDiff

    report = PreflightReport(
        models=[_model("dim_customers")],
        diffs=[
            ModelDiff(
                unique_id="model.p.dim_customers",
                name="dim_customers",
                base_exists=False,
                rows_head=150,
                columns_added=[("a", "INT")],
            )
        ],
        metrics_defined=0,
        base_ref="origin/main",
    )
    body = render(report)
    assert "**`dim_customers`** — new in this pull request: 150 rows, 1 columns." in body
    assert "No metrics are defined" in body


def test_parser_error_classification() -> None:
    from dbt_preflight.dbt_runner import NodeResult

    def node(message: str) -> NodeResult:
        return NodeResult("model.p.x", "x", "model", "error", message, None, 0.0)

    missing = node("Catalog Error: Scalar Function with name timestamp_diff does not exist!")
    assert missing.dialect_function == "timestamp_diff" and not missing.is_parser_error

    typo = node('Parser Error: syntax error at or near "selectt"')
    assert typo.dialect_function == "syntax" and typo.is_parser_error

    binder = node('Binder Error: Referenced column "email" not found in FROM clause!')
    assert binder.dialect_function is None and not binder.is_parser_error


def test_diff_section_renders_renames_profiles_and_references() -> None:
    from dbt_preflight.diff import ColumnProfile, ModelDiff

    report = PreflightReport(
        models=[_model("dim_customers")],
        diffs=[
            ModelDiff(
                unique_id="model.p.dim_customers",
                name="dim_customers",
                base_exists=True,
                rows_base=150,
                rows_head=150,
                columns_added=[("is_business", "BOOLEAN")],
                columns_renamed=[("customer_segment", "segment")],
                profiles={
                    "is_business": ColumnProfile(
                        "is_business", "BOOLEAN", 150, 0, 2, [("false", 138), ("true", 12)]
                    )
                },
                references={
                    "customer_segment": [
                        "its YAML column entry (models/_marts.yml)",
                        "Lightdash meta on `dim_customers`",
                    ]
                },
            )
        ],
        metrics_defined=3,
        base_ref="origin/main",
    )
    body = render(report)
    assert report.has_warnings
    assert "⚠️ marks a column that was removed, renamed or retyped" in body
    assert (
        "+`is_business` (BOOLEAN, 12 true, 138 false), `customer_segment` → `segment` (renamed, same values) ⚠️"
        in body
    )
    assert (
        "`customer_segment` was referenced on the base branch by its YAML column entry (models/_marts.yml), Lightdash meta on `dim_customers`; each of those needs the new name or the column back."
        in body
    )


# --- Size budget --------------------------------------------------------------------


def _generic_failing_test(i: int) -> FailedTest:
    return FailedTest(
        name=f"not_null_stg_shop__customers_col_{i}",
        model="stg_shop__customers",
        status="fail",
        failures=i,
        message="",
        test_name="not_null",
        column_name=f"col_{i}",
    )


def test_more_than_three_failing_tests_fold_under_one_details_block() -> None:
    tests = [_generic_failing_test(i) for i in range(5)]
    report = PreflightReport(
        models=[_model("stg_shop__customers", tests_failed=5)], tests=tests, base_ref="origin/main"
    )
    body = render(report)
    for i in range(3):
        assert f"`not_null` on `stg_shop__customers.col_{i}`" in body
    assert "<details><summary>2 more failing tests</summary>" in body
    # the folded tests are still present, inside that block
    assert "`not_null` on `stg_shop__customers.col_3`" in body
    assert "`not_null` on `stg_shop__customers.col_4`" in body
    # they land after the fold marker, not before it
    fold_at = body.index("2 more failing tests")
    assert body.index("col_3") > fold_at


def test_three_or_fewer_failing_tests_do_not_fold() -> None:
    tests = [_generic_failing_test(i) for i in range(3)]
    report = PreflightReport(
        models=[_model("stg_shop__customers", tests_failed=3)], tests=tests, base_ref="origin/main"
    )
    body = render(report)
    assert "more failing tests" not in body


def test_also_rebuilt_line_caps_at_five_names() -> None:
    around = [_model(f"m{i}", changed=False) for i in range(7)]
    report = PreflightReport(models=[_model("changed_model")] + around, base_ref="origin/main")
    body = render(report)
    assert "Also rebuilt, no new issues: `m0`, `m1`, `m2`, `m3`, `m4` and 2 more." in body


def test_also_rebuilt_line_lists_all_names_at_or_under_five() -> None:
    around = [_model(f"m{i}", changed=False) for i in range(3)]
    report = PreflightReport(models=[_model("changed_model")] + around, base_ref="origin/main")
    body = render(report)
    assert "Also rebuilt, no new issues: `m0`, `m1`, `m2`." in body
    assert "more" not in body


def test_build_errors_keep_the_first_visible_and_fold_the_rest() -> None:
    models = [
        _model("a", status=FAILED, changed=False, message="Binder Error: boom a"),
        _model("b", status=FAILED, changed=False, message="Binder Error: boom b"),
        _model("c", status=FAILED, changed=False, message="Binder Error: boom c"),
    ]
    report = PreflightReport(models=models, base_ref="origin/main")
    body = render(report)
    assert "**`a`** — models/a.sql" in body
    assert "<details><summary>`b`</summary>" in body
    assert "<details><summary>`c`</summary>" in body
    assert "boom b" in body and "boom c" in body
    # the first error is not itself hidden inside a details block
    assert body.index("**`a`**") < body.index("<details><summary>`b`</summary>")


def test_single_build_error_is_not_folded() -> None:
    models = [_model("a", status=FAILED, changed=False, message="Binder Error: boom a")]
    report = PreflightReport(models=models, base_ref="origin/main")
    body = render(report)
    assert "**`a`** — models/a.sql" in body
    assert "<details><summary>`a`</summary>" not in body


# --- Readable test names -------------------------------------------------------------


def test_generic_test_renders_from_metadata_and_keeps_raw_name_in_details() -> None:
    report = PreflightReport(
        models=[_model("stg_webshop__customers", tests_failed=1)],
        tests=[
            FailedTest(
                name="unique_stg_webshop__customers_customer_id",
                model="stg_webshop__customers",
                status="fail",
                failures=2,
                message="",
                test_name="unique",
                column_name="customer_id",
            )
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert "- ❌ `unique` on `stg_webshop__customers.customer_id`: 2 failing rows" in body
    assert "dbt test name: `unique_stg_webshop__customers_customer_id`" in body


def test_relationships_test_parses_to_and_field_kwargs() -> None:
    report = PreflightReport(
        models=[_model("stg_webshop__orders", tests_failed=1)],
        tests=[
            FailedTest(
                name="relationships_stg_webshop__orders_customer_id__customer_id__ref_stg_webshop__customers_",
                model="stg_webshop__orders",
                status="fail",
                failures=800,
                message="",
                test_name="relationships",
                column_name="customer_id",
                kwargs={"to": "ref('stg_webshop__customers')", "field": "customer_id"},
            )
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert (
        "- ❌ relationships `stg_webshop__orders.customer_id` → "
        "`stg_webshop__customers.customer_id`: 800 failing rows" in body
    )
    assert (
        "dbt test name: `relationships_stg_webshop__orders_customer_id__customer_id__"
        "ref_stg_webshop__customers_`" in body
    )


def test_relationships_test_parses_source_kwarg() -> None:
    report = PreflightReport(
        models=[_model("stg_webshop__orders", tests_failed=1)],
        tests=[
            FailedTest(
                name="relationships_x",
                model="stg_webshop__orders",
                status="fail",
                failures=1,
                message="",
                test_name="relationships",
                column_name="customer_id",
                kwargs={"to": "source('webshop', 'customers')", "field": "id"},
            )
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert "relationships `stg_webshop__orders.customer_id` → `customers.id`" in body


def test_accepted_values_test_renders_from_metadata() -> None:
    report = PreflightReport(
        models=[_model("stg_webshop__orders", tests_failed=1)],
        tests=[
            FailedTest(
                name="accepted_values_stg_webshop__orders_order_status__pending__paid",
                model="stg_webshop__orders",
                status="fail",
                failures=3,
                message="",
                test_name="accepted_values",
                column_name="order_status",
            )
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert "`accepted_values` on `stg_webshop__orders.order_status`" in body


def test_singular_and_unit_tests_keep_their_own_names() -> None:
    report = PreflightReport(
        models=[_model("dim_customers", tests_failed=1)],
        tests=[
            FailedTest(
                name="assert_positive_revenue",
                model="dim_customers",
                status="fail",
                failures=1,
                message="",
            ),
            FailedTest(
                name="int_orders__items_aggregated_sums_lines_per_order",
                model="int_orders__items_aggregated",
                status="fail",
                failures=None,
                message="actual differs from expected",
                kind="unit_test",
            ),
        ],
        base_ref="origin/main",
    )
    body = render(report)
    assert "- ❌ `assert_positive_revenue` on `dim_customers`: 1 failing row" in body
    assert (
        "- ❌ unit test `int_orders__items_aggregated_sums_lines_per_order` "
        "on `int_orders__items_aggregated`: actual output differs from the expected rows" in body
    )


# --- Human readings for DuckDB errors -------------------------------------------------


def test_human_readings_for_known_duckdb_error_shapes() -> None:
    cases = [
        (
            'Binder Error: Column "email" referenced that exists in the SELECT clause - but '
            "this column cannot be referenced before it is defined",
            "the input no longer has a column called `email`",
        ),
        (
            'Binder Error: Referenced column "customer_id" not found in FROM clause!',
            "this model has no column `customer_id`: renamed or dropped upstream?",
        ),
        (
            "Catalog Error: Table with name stg_webshop__orders does not exist!",
            "`stg_webshop__orders` was not built, it failed or was skipped upstream",
        ),
        (
            "Catalog Error: Scalar Function with name timestamp_diff does not exist!",
            "`timestamp_diff` is not a DuckDB function",
        ),
        (
            'Parser Error: syntax error at or near "selectt"',
            "DuckDB could not parse this SQL",
        ),
    ]
    for message, reading in cases:
        report = PreflightReport(
            models=[_model("m", status=FAILED, message=message)], base_ref="origin/main"
        )
        body = render(report)
        assert reading in body
        assert body.index(reading) < body.index(message.strip()[:30])


def test_unknown_error_shape_falls_through_unchanged() -> None:
    from dbt_preflight.report import _human_reading

    message = "Some completely different error DuckDB has never said before"
    report = PreflightReport(
        models=[_model("m", status=FAILED, message=message)], base_ref="origin/main"
    )
    body = render(report)
    assert message in body
    assert _human_reading(message) is None


def test_human_reading_shown_above_raw_message_in_an_errored_test() -> None:
    report = PreflightReport(
        models=[_model("stg_shop__customers", tests_failed=1)],
        tests=[
            FailedTest(
                name="unique_x",
                model="stg_shop__customers",
                status="error",
                failures=None,
                message=(
                    "Runtime Error in test unique_x\n"
                    '  Binder Error: Referenced column "customer_id" not found in FROM clause!'
                ),
            )
        ],
        base_ref="origin/main",
    )
    body = render(report)
    reading = "this model has no column `customer_id`: renamed or dropped upstream?"
    # the raw message is repeated in the bullet's one-line summary too; only the details
    # block's fenced copy is "the raw message" the reading must sit above.
    detail = body[body.index("<details><summary>details</summary>") :]
    assert reading in detail
    assert detail.index(reading) < detail.index('Referenced column "customer_id" not found')


# --- Percentages next to counts --------------------------------------------------------


def test_row_count_change_shows_a_percentage() -> None:
    from dbt_preflight.diff import ModelDiff

    report = PreflightReport(
        models=[_model("fct_orders")],
        diffs=[
            ModelDiff(
                unique_id="model.p.fct_orders",
                name="fct_orders",
                base_exists=True,
                rows_base=800,
                rows_head=742,
            )
        ],
        metrics_defined=0,
        base_ref="origin/main",
    )
    body = render(report)
    assert "rows 800 → 742 (-7.2%)" in body


def test_differing_rows_shows_share_of_head_rows() -> None:
    from dbt_preflight.diff import ModelDiff

    report = PreflightReport(
        models=[_model("dim_customers")],
        diffs=[
            ModelDiff(
                unique_id="model.p.dim_customers",
                name="dim_customers",
                base_exists=True,
                rows_base=150,
                rows_head=150,
                rows_differing=30,
            )
        ],
        metrics_defined=0,
        base_ref="origin/main",
    )
    body = render(report)
    assert "30 rows with different values (20%)" in body


def test_differing_rows_notes_when_restricted_to_shared_columns() -> None:
    """When the schema changed, `rows_differing` was compared on fewer columns; say so."""
    from dbt_preflight.diff import ModelDiff

    report = PreflightReport(
        models=[_model("dim_customers")],
        diffs=[
            ModelDiff(
                unique_id="model.p.dim_customers",
                name="dim_customers",
                base_exists=True,
                rows_base=150,
                rows_head=150,
                rows_differing=30,
                rows_differing_common_columns=12,
                columns_added=[("is_active", "BOOLEAN")],
            )
        ],
        metrics_defined=0,
        base_ref="origin/main",
    )
    body = render(report)
    assert "30 rows with different values (20%, on the 12 columns both sides share)" in body


def test_moved_metric_breakdown_renders_under_the_metric_table() -> None:
    from dbt_preflight.diff import MetricDiff, ModelDiff

    report = PreflightReport(
        models=[_model("dim_customers")],
        diffs=[
            ModelDiff(
                unique_id="model.p.dim_customers",
                name="dim_customers",
                base_exists=True,
                rows_base=150,
                rows_head=150,
                metrics=[
                    MetricDiff(
                        "net_revenue",
                        "Net revenue (EUR)",
                        "dbt",
                        base=8190.0,
                        head=7830.0,
                        breakdown={
                            "sales_channel": [
                                ("web", 5210.0, 4980.0),
                                ("mobile_app", 2980.0, 2850.0),
                            ]
                        },
                    )
                ],
            )
        ],
        metrics_defined=1,
        base_ref="origin/main",
    )
    body = render(report)
    assert "| Net revenue (EUR) | 8,190 | 7,830 | -4.4% |" in body
    assert (
        "- Net revenue (EUR) by sales_channel: web 5,210 → 4,980 (-4.4%), "
        "mobile_app 2,980 → 2,850 (-4.4%)" in body
    )
    # the breakdown sits after the metric table, before the closing blank line's next section
    assert body.index("| Net revenue (EUR) |") < body.index("by sales_channel")
