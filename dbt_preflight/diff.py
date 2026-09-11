"""What the pull request did to the output: columns, rows and metrics, base versus head.

Both builds ran on the same fixtures with the same seed, so any difference here was caused
by the change and by nothing else. That is a cleaner signal than a production comparison
gives, and it needs no credential. The flip side is stated in the comment: a metric that
does not move on synthetic data can still move on production, because the fixtures do not
carry production's distribution. The diff proves the logic changed, not the size of the
effect on real data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import duckdb

from dbt_preflight.checks import relation
from dbt_preflight.manifest import Manifest, ModelNode
from dbt_preflight.metrics import MetricDef, evaluate


@dataclass
class MetricDiff:
    name: str
    label: str
    source: str
    base: float | int | None
    head: float | int | None
    unsupported: str | None = None

    @property
    def moved(self) -> bool:
        if self.unsupported:
            return False
        if self.base is None or self.head is None:
            return self.base is not self.head
        return abs(float(self.head) - float(self.base)) > 1e-9


@dataclass
class ColumnProfile:
    """What an added column holds, so a reviewer can judge it without a second run."""

    name: str
    data_type: str
    rows: int
    nulls: int
    distinct: int
    top: list[tuple[str, int]] = field(default_factory=list)  # value -> count, low cardinality

    def describe(self) -> str:
        if self.data_type == "BOOLEAN" and self.top:
            counts = dict(self.top)
            return f"{counts.get('true', 0):,} true, {counts.get('false', 0):,} false" + (
                f", {self.nulls:,} null" if self.nulls else ""
            )
        if self.top:
            values = ", ".join(f"{v} {n:,}" for v, n in self.top)
            return f"{self.distinct} distinct: {values}" + (
                f"; {self.nulls:,} null" if self.nulls else ""
            )
        parts = [f"{self.distinct:,} distinct"]
        if self.nulls:
            parts.append(f"{self.nulls:,} null")
        return ", ".join(parts)


@dataclass
class ModelDiff:
    unique_id: str
    name: str
    base_exists: bool
    columns_added: list[tuple[str, str]] = field(default_factory=list)
    columns_removed: list[tuple[str, str]] = field(default_factory=list)
    columns_retyped: list[tuple[str, str, str]] = field(default_factory=list)
    columns_renamed: list[tuple[str, str]] = field(default_factory=list)  # old -> new, same values
    profiles: dict[str, ColumnProfile] = field(default_factory=dict)  # added columns
    references: dict[str, list[str]] = field(default_factory=dict)  # removed/renamed col -> where
    rows_base: int | None = None
    rows_head: int | None = None
    rows_differing: int | None = None  # head rows with no identical row in the base build
    metrics: list[MetricDiff] = field(default_factory=list)

    @property
    def schema_changed(self) -> bool:
        return bool(
            self.columns_added
            or self.columns_removed
            or self.columns_retyped
            or self.columns_renamed
        )

    @property
    def breaking(self) -> bool:
        """A column a consumer may read is gone, renamed or changed type."""
        return bool(self.columns_removed or self.columns_retyped or self.columns_renamed)

    @property
    def rows_changed(self) -> bool:
        return self.base_exists and self.rows_base != self.rows_head

    @property
    def moved_metrics(self) -> list[MetricDiff]:
        return [m for m in self.metrics if m.moved]

    @property
    def identical(self) -> bool:
        return (
            self.base_exists
            and not self.schema_changed
            and not self.rows_changed
            and not self.rows_differing
            and not self.moved_metrics
        )


def _columns(con: duckdb.DuckDBPyConnection, model: ModelNode) -> dict[str, str]:
    rows = con.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_catalog = coalesce(?, table_catalog) "
        "and table_schema = ? and table_name = ? order by ordinal_position",
        [model.database, model.schema, model.alias],
    ).fetchall()
    return {str(c): str(t).upper() for c, t in rows}


def _count(con: duckdb.DuckDBPyConnection, model: ModelNode) -> int | None:
    try:
        row = con.execute(f"select count(*) from {relation(model)}").fetchone()
    except duckdb.Error:
        return None
    return int(row[0]) if row else None


def _rows_differing(con: duckdb.DuckDBPyConnection, head: ModelNode, base: ModelNode) -> int | None:
    """Head rows with no identical row in the base build. Only meaningful when the
    columns match; the caller checks that first."""
    try:
        row = con.execute(
            f"select count(*) from (select * from {relation(head)} "
            f"except all select * from {relation(base)})"
        ).fetchone()
    except duckdb.Error:
        return None
    return int(row[0]) if row else None


def _same_values(
    con: duckdb.DuckDBPyConnection, head: ModelNode, new: str, base: ModelNode, old: str
) -> bool:
    """Whether column `new` on head holds exactly the values column `old` held on base."""
    try:
        a = con.execute(
            f'select count(*) from (select "{new}" from {relation(head)} '
            f'except all select "{old}" from {relation(base)})'
        ).fetchone()
        b = con.execute(
            f'select count(*) from (select "{old}" from {relation(base)} '
            f'except all select "{new}" from {relation(head)})'
        ).fetchone()
    except duckdb.Error:
        return False
    return bool(a and b and a[0] == 0 and b[0] == 0)


def _profile(
    con: duckdb.DuckDBPyConnection, model: ModelNode, column: str, data_type: str
) -> ColumnProfile | None:
    try:
        row = con.execute(
            f'select count(*), count("{column}"), count(distinct "{column}") from {relation(model)}'
        ).fetchone()
        if row is None:
            return None
        rows, non_null, distinct = row
        top: list[tuple[str, int]] = []
        if distinct <= 8:
            top = [
                (str(v).lower() if isinstance(v, bool) else str(v), int(n))
                for v, n in con.execute(
                    f'select "{column}", count(*) from {relation(model)} '
                    f'where "{column}" is not null group by 1 order by 2 desc, 1 limit 8'
                ).fetchall()
            ]
    except duckdb.Error:
        return None
    return ColumnProfile(column, data_type, int(rows), int(rows - non_null), int(distinct), top)


def _references(base: Manifest, model: ModelNode, column: str) -> list[str]:
    """Where `column` of `model` was referenced on the base branch. Empty means nowhere."""
    refs: list[str] = []
    if column in model.column_names:
        where = f" ({model.patch_path})" if model.patch_path else ""
        refs.append(f"its YAML column entry{where}")
    token = re.compile(rf"\b{re.escape(column)}\b")
    # Lightdash meta names a column in `${col}` references, filter keys and `sql` snippets.
    meta_text = json.dumps(model.meta) + json.dumps(
        {k: v for k, v in model.column_meta.items() if k != column}
    )
    if f"${{{column}}}" in meta_text or token.search(meta_text):
        refs.append(f"Lightdash meta on `{model.name}`")
    for sm in base.semantic_models.values():
        if sm.model_uid != model.unique_id:
            continue
        exprs = list(sm.dimensions.values()) + list(sm.entities.values())
        exprs += [m.expr for m in sm.measures.values()]
        if any(token.search(e or "") for e in exprs):
            refs.append(f"semantic model `{sm.name}`")
    for child_uid in base.child_map.get(model.unique_id, []):
        child = base.models.get(child_uid)
        if child is not None and token.search(child.raw_code):
            refs.append(f"downstream model `{child.name}`")
    for test in base.tests_for_model(model.unique_id):
        if test.column_name == column and test.test_name:
            refs.append(f"`{test.test_name}` test")
            break
    return refs


def compute_diffs(
    db_path,
    head: Manifest,
    base: Manifest,
    model_ids: list[str],
    metrics: list[MetricDef],
    dialect: str | None,
) -> list[ModelDiff]:
    """Diffs for `model_ids` (head unique ids) that built on both sides."""
    by_model: dict[str, list[MetricDef]] = {}
    for m in metrics:
        by_model.setdefault(m.model_uid, []).append(m)

    out: list[ModelDiff] = []
    con = duckdb.connect(str(db_path))
    try:
        for uid in model_ids:
            head_node = head.models.get(uid)
            if head_node is None:
                continue
            base_node = base.models.get(uid)
            diff = ModelDiff(unique_id=uid, name=head_node.name, base_exists=base_node is not None)

            head_cols = _columns(con, head_node)
            if not head_cols:
                continue  # did not build on head; the build section already says so
            diff.rows_head = _count(con, head_node)

            if base_node is None:
                diff.columns_added = list(head_cols.items())
                out.append(diff)
                continue

            base_cols = _columns(con, base_node)
            if not base_cols:
                # In the base manifest but never built there: nothing to compare against.
                diff.base_exists = False
                diff.columns_added = list(head_cols.items())
                out.append(diff)
                continue
            diff.rows_base = _count(con, base_node)
            diff.columns_added = [(c, t) for c, t in head_cols.items() if c not in base_cols]
            diff.columns_removed = [(c, t) for c, t in base_cols.items() if c not in head_cols]
            diff.columns_retyped = [
                (c, base_cols[c], t)
                for c, t in head_cols.items()
                if c in base_cols and base_cols[c] != t
            ]
            if head_cols == base_cols:
                diff.rows_differing = _rows_differing(con, head_node, base_node)

            # A dropped column and an added one of the same type with the same values is a
            # rename; say so instead of reporting a loss and a gain.
            for old_name, old_type in list(diff.columns_removed):
                for new_name, new_type in list(diff.columns_added):
                    if old_type == new_type and _same_values(
                        con, head_node, new_name, base_node, old_name
                    ):
                        diff.columns_renamed.append((old_name, new_name))
                        diff.columns_removed.remove((old_name, old_type))
                        diff.columns_added.remove((new_name, new_type))
                        break

            for name, data_type in diff.columns_added:
                profile = _profile(con, head_node, name, data_type)
                if profile is not None:
                    diff.profiles[name] = profile
            for name, _ in diff.columns_removed:
                diff.references[name] = _references(base, base_node, name)
            for old_name, _ in diff.columns_renamed:
                diff.references[old_name] = _references(base, base_node, old_name)

            defs = by_model.get(uid, [])
            if defs:
                head_vals = evaluate(con, relation(head_node), defs, dialect)
                base_vals = evaluate(con, relation(base_node), defs, dialect)
                for d in defs:
                    diff.metrics.append(
                        MetricDiff(
                            name=d.name,
                            label=d.label,
                            source=d.source,
                            base=base_vals.get(d.name),
                            head=head_vals.get(d.name),
                            unsupported=d.unsupported,
                        )
                    )
            out.append(diff)
    finally:
        con.close()
    return out
