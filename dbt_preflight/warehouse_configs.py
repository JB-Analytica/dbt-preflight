"""Drop warehouse-only materialisation settings while building on DuckDB.

A model written for BigQuery carries `partition_by={'field': ..., 'granularity': ...}`,
one for Snowflake carries `cluster_by` and `transient`, one for Databricks carries
`zorder`. None of it changes what a model returns: it tells the warehouse how to lay the
table out on disk. dbt-duckdb has no such layout, and rather than ignore the settings it
rejects them, so a project with partitioned marts loses every one of those models to
`partitioned_by/partition_by must be a list of strings or a string` before a single row is
compared. On dbt-ga4 that was 54 models.

So the settings are removed from each node as it compiles, the same hook point the
transpiler uses, and recorded so the comment can say which models were affected. This is
sound for what preflight does and only for that: it builds into a throwaway DuckDB file
that is deleted at the end of the run, and never writes to a warehouse, so how a table
would have been laid out cannot change any answer it reports.

A setting that *does* change a model's output is not in this list and never should be:
`materialized`, `unique_key`, `incremental_strategy`, `partition_expiration_days` and
their like stay where they are, even when DuckDB reads them differently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Config keys that describe physical layout in a warehouse and nothing about the rows a
# model returns. Grouped by the warehouse that introduced them; the names are matched
# across all of them, since a project is free to carry another adapter's keys.
WAREHOUSE_ONLY_CONFIGS = frozenset(
    {
        # BigQuery
        "partition_by",
        "cluster_by",
        "require_partition_filter",
        "partition_expiration_days",
        "hours_to_expiration",
        "kms_key_name",
        "labels",
        "partitions",
        "enable_refresh",
        "refresh_interval_minutes",
        "max_staleness",
        # Snowflake
        "transient",
        "automatic_clustering",
        "snowflake_warehouse",
        "query_tag",
        "secure",
        "target_lag",
        # Databricks and Spark
        "zorder",
        "file_format",
        "location_root",
        "tblproperties",
        "liquid_clustered_by",
        "buckets",
        "clustered_by",
        # Redshift
        "sort",
        "sort_type",
        "dist",
        "auto_refresh",
        "backup",
    }
)


@dataclass
class WarehouseConfigHook:
    """Strips `WAREHOUSE_ONLY_CONFIGS` from every node dbt compiles.

    Records `model name -> the settings dropped`, so a reader can see that preflight
    ignored something the warehouse would have honoured.
    """

    dropped: dict[str, list[str]] = field(default_factory=dict)
    _original: Any = None

    def install(self) -> None:
        from dbt.compilation import Compiler

        if self._original is not None:
            return
        original = Compiler._compile_code
        hook = self

        def _compile_code(compiler, node, manifest, extra_context=None):
            node = original(compiler, node, manifest, extra_context)
            hook._strip(node)
            return node

        Compiler._compile_code = _compile_code  # ty: ignore[invalid-assignment]
        self._original = original

    def uninstall(self) -> None:
        if self._original is None:
            return
        from dbt.compilation import Compiler

        Compiler._compile_code = self._original
        self._original = None

    def __enter__(self) -> WarehouseConfigHook:
        self.install()
        return self

    def __exit__(self, *exc: object) -> None:
        self.uninstall()

    def _strip(self, node: Any) -> None:
        config = getattr(node, "config", None)
        if config is None:
            return
        # An adapter that does not declare a key keeps it in `_extra`, which is where a
        # BigQuery `partition_by` lands on dbt-duckdb. A key the adapter *does* declare is
        # a real attribute, so both are cleared.
        extra = getattr(config, "_extra", None)
        dropped: list[str] = []
        for key in sorted(WAREHOUSE_ONLY_CONFIGS):
            if isinstance(extra, dict) and extra.pop(key, None) is not None:
                dropped.append(key)
            elif getattr(config, key, None) is not None:
                try:
                    setattr(config, key, None)
                except (AttributeError, TypeError):  # frozen or validated: leave it alone
                    continue
                dropped.append(key)
        if dropped:
            name = getattr(node, "name", None) or getattr(node, "unique_id", "?")
            # A node compiles once per build, but a run builds both branches: keep the
            # first record rather than appending the same keys twice.
            self.dropped.setdefault(name, dropped)
