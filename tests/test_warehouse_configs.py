"""Warehouse-only layout settings must not stop a build on DuckDB.

A model written for BigQuery carries `partition_by={'field': ..., 'granularity': ...}`.
dbt-duckdb rejects it outright, so a project with partitioned marts used to lose every one
of those models before a single row was compared. On dbt-ga4 that was 54 models.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbt_preflight.cli import app
from dbt_preflight.warehouse_configs import WAREHOUSE_ONLY_CONFIGS, WarehouseConfigHook

BIGQUERY_MODEL = """{{ config(
    materialized='table',
    partition_by={'field': 'event_date', 'data_type': 'date', 'granularity': 'day'},
    cluster_by=['user_id'],
    require_partition_filter=true
) }}
select cast(created_at as date) as event_date, customer_id as user_id
from {{ ref('stg_webshop__customers') }}
"""


def test_the_list_holds_layout_only_settings() -> None:
    """Anything that changes what a model returns must never be dropped. This is the whole
    safety argument for the hook, so it is asserted rather than left to review."""
    for key in ("materialized", "unique_key", "incremental_strategy", "sql_header", "alias"):
        assert key not in WAREHOUSE_ONLY_CONFIGS


def test_a_node_without_config_is_left_alone() -> None:
    hook = WarehouseConfigHook()
    hook._strip(object())  # a node dbt handed over with no config at all
    assert hook.dropped == {}


def test_dropped_settings_are_recorded_once_per_model() -> None:
    class Config:
        def __init__(self) -> None:
            self._extra = {"partition_by": {"field": "d"}, "cluster_by": ["x"]}

    class Node:
        name = "fct_events"

        def __init__(self) -> None:
            self.config = Config()

    hook = WarehouseConfigHook()
    node = Node()
    hook._strip(node)
    assert hook.dropped == {"fct_events": ["cluster_by", "partition_by"]}
    assert node.config._extra == {}
    # A run builds both branches, so the same node compiles twice; the record must not grow.
    hook._strip(Node())
    assert hook.dropped == {"fct_events": ["cluster_by", "partition_by"]}


@pytest.fixture
def example_copy(tmp_path: Path) -> Path:
    import shutil

    dest = tmp_path / "webshop"
    shutil.copytree(Path(__file__).parent.parent / "examples" / "webshop", dest)
    return dest


def test_a_bigquery_partitioned_model_builds(example_copy: Path, tmp_path: Path) -> None:
    """End to end: the model builds, the comment says the settings were ignored, and the
    summary JSON carries them."""
    import json

    model = example_copy / "dbt" / "models" / "marts" / "fct_events.sql"
    model.write_text(BIGQUERY_MODEL)

    comment = tmp_path / "comment.md"
    summary_path = tmp_path / "summary.json"
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
            "--summary-file",
            str(summary_path),
        ],
    )
    # The throwaway model has no description and no tested key, so the house conventions
    # flag it; that is incidental here. What matters is that it built at all.
    assert result.exit_code in (0, 1), result.output

    body = comment.read_text()
    assert "partitioned_by/partition_by must be" not in body  # the dbt-duckdb rejection
    assert "`fct_events`" in body
    assert "Warehouse-only layout settings ignored" in body

    summary = json.loads(summary_path.read_text())
    assert summary["warehouse_configs_dropped"]["fct_events"] == [
        "cluster_by",
        "partition_by",
        "require_partition_filter",
    ]
    built = {m["name"]: m["status"] for m in summary["models"]}
    assert built["fct_events"] == "built"
