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
from sqlglot.errors import SqlglotError

from dbt_preflight.checks import relation
from dbt_preflight.manifest import Manifest, ModelNode
from dbt_preflight.metrics import MetricDef, combine, evaluate
from dbt_preflight.transpile import transpile_sql

# How many dimensions, and how many rows per dimension, a moved metric's breakdown carries.
# Breaking a metric down is one extra query per (dimension, side); capped so the cost of a
# moved metric stays bounded regardless of how many dimensions the model has.
_BREAKDOWN_DIMENSIONS = 3
_BREAKDOWN_ROWS = 3
# A dimension with only one distinct value says nothing (every row falls in one bucket); one
# with too many is closer to a row identifier than a grouping (`full_name`, `city`) and would
# swamp the comment with singleton rows. Both are skipped before a dimension is even offered.
_BREAKDOWN_MIN_DISTINCT = 2
_BREAKDOWN_MAX_DISTINCT = 12


@dataclass
class MetricDiff:
    name: str
    label: str
    source: str
    base: float | int | None
    head: float | int | None
    unsupported: str | None = None
    # dimension name -> up to 3 (value, base, head) rows, the largest contributors to the
    # metric's move; only filled in for a metric that moved, over dimensions the model has.
    breakdown: dict[str, list[tuple[str, float | int | None, float | int | None]]] = field(
        default_factory=dict
    )
    # The models a metric reads when it reads more than one (a ratio of orders to
    # customers): empty for the usual metric over a single model.
    spans: list[str] = field(default_factory=list)

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
    # Set when `rows_differing` was compared on fewer than all columns because the schema
    # changed: how many columns (name and type both matching) the comparison used.
    rows_differing_common_columns: int | None = None
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


def _rows_differing(
    con: duckdb.DuckDBPyConnection,
    head: ModelNode,
    base: ModelNode,
    columns: list[str] | None = None,
) -> int | None:
    """Head rows with no identical row in the base build.

    Compared on every column when `columns` is None, i.e. when the schema is unchanged.
    Otherwise compared on just `columns` (the intersection with matching types the caller
    worked out), so a schema change does not hide every other row-value change."""
    select = "*" if columns is None else ", ".join(f'"{c}"' for c in columns)
    try:
        row = con.execute(
            f"select count(*) from (select {select} from {relation(head)} "
            f"except all select {select} from {relation(base)})"
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


def _distinct_count(
    con: duckdb.DuckDBPyConnection, expr: str, model: ModelNode, dialect: str | None
) -> int | None:
    sql = f"select count(distinct {expr}) from {relation(model)}"
    if dialect:
        try:
            sql = transpile_sql(sql, dialect)
        except SqlglotError:
            pass
    try:
        row = con.execute(sql).fetchone()
    except duckdb.Error:
        return None
    return int(row[0]) if row else None


def _cardinality_gate(
    con: duckdb.DuckDBPyConnection,
    candidates: list[tuple[str, str]],
    model: ModelNode,
    dialect: str | None,
) -> list[tuple[str, str]]:
    """`candidates` with between `_BREAKDOWN_MIN_DISTINCT` and `_BREAKDOWN_MAX_DISTINCT`
    distinct values on `model`'s head build, ordered by fewest distinct values first: one
    query per candidate, so kept to the handful of dimensions a model actually offers."""
    scored = []
    for name, expr in candidates:
        distinct = _distinct_count(con, expr, model, dialect)
        if distinct is None or not (_BREAKDOWN_MIN_DISTINCT <= distinct <= _BREAKDOWN_MAX_DISTINCT):
            continue
        scored.append((distinct, name, expr))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [(name, expr) for _, name, expr in scored]


def _categorical_dimensions(
    con: duckdb.DuckDBPyConnection, head: Manifest, model_uid: str, dialect: str | None
) -> list[tuple[str, str]]:
    """Up to `_BREAKDOWN_DIMENSIONS` (name, SQL expression) categorical dimensions available
    on `model_uid`, gated by cardinality and ordered by fewest distinct values first: from
    the dbt semantic model tied to it, or Lightdash meta on its own columns when the semantic
    layer has none that survive the gate, or nothing."""
    model = head.models.get(model_uid)
    if model is None:
        return []

    for sm in head.semantic_models.values():
        if sm.model_uid != model_uid:
            continue
        categorical = [
            (name, expr)
            for name, expr in sm.dimensions.items()
            if sm.dimension_types.get(name) == "categorical"
        ]
        if categorical:
            gated = _cardinality_gate(con, categorical, model, dialect)
            if gated:
                return gated[:_BREAKDOWN_DIMENSIONS]

    primary_key = model.meta.get("primary_key")  # one row per value: not a real breakdown
    lightdash = []
    for col, meta in model.column_meta.items():
        dimension = meta.get("dimension") or {}
        if col == primary_key or dimension.get("hidden") or dimension.get("type") != "string":
            continue
        lightdash.append((col, f'"{col}"'))
    return _cardinality_gate(con, lightdash, model, dialect)[:_BREAKDOWN_DIMENSIONS]


def _grouped_metric(
    con: duckdb.DuckDBPyConnection,
    metric_sql: str,
    dim_expr: str,
    model: ModelNode,
    dialect: str | None,
) -> dict[str, float | int | None] | None:
    """`metric_sql` evaluated once per value of `dim_expr` over `model`'s relation. Every
    aggregate in `metric_sql` sits inside the `select`, so this wraps a ratio or derived
    metric's expression just as well as a plain aggregate."""
    sql = (
        f"select {dim_expr} as dbt_preflight_dim, ({metric_sql}) as dbt_preflight_val "
        f"from {relation(model)} group by 1"
    )
    if dialect:
        try:
            sql = transpile_sql(sql, dialect)
        except SqlglotError:
            pass
    try:
        rows = con.execute(sql).fetchall()
    except duckdb.Error:
        return None
    return {str(value): metric_value for value, metric_value in rows}


def _metric_breakdown(
    con: duckdb.DuckDBPyConnection,
    metric: MetricDef,
    dimensions: list[tuple[str, str]],
    head_node: ModelNode,
    base_node: ModelNode,
    dialect: str | None,
) -> dict[str, list[tuple[str, float | int | None, float | int | None]]]:
    """A moved metric, grouped by each of `dimensions`, keeping the rows whose contribution
    to the total delta is largest, capped at `_BREAKDOWN_ROWS` per dimension."""
    breakdown: dict[str, list[tuple[str, float | int | None, float | int | None]]] = {}
    for name, expr in dimensions:
        head_vals = _grouped_metric(con, metric.sql, expr, head_node, dialect)
        base_vals = _grouped_metric(con, metric.sql, expr, base_node, dialect)
        if head_vals is None or base_vals is None:
            continue
        scored = []
        for value in set(head_vals) | set(base_vals):
            b, h = base_vals.get(value), head_vals.get(value)
            contribution = abs(float(h or 0) - float(b or 0))
            scored.append((contribution, value, b, h))
        scored.sort(key=lambda row: (-row[0], row[1]))
        rows = [(value, b, h) for _, value, b, h in scored[:_BREAKDOWN_ROWS]]
        if rows:
            breakdown[name] = rows
    return breakdown


def _spanning_metric_diff(
    con: duckdb.DuckDBPyConnection,
    metric: MetricDef,
    head: Manifest,
    base: Manifest,
    compared: dict[str, ModelDiff],
    dialect: str | None,
) -> MetricDiff:
    """A metric whose inputs live on different models: each input is evaluated on its own
    relation, on both sides, and the scalars are combined.

    `compared` holds the models the change reached and that built on head. An input model
    outside it was not touched by the change and was not built on the base branch, so its
    head value stands for both sides: same SQL, same fixtures, same output."""
    spans = [head.models[uid].name for uid in metric.model_uids if uid in head.models]
    diff = MetricDiff(
        name=metric.name,
        label=metric.label,
        source=metric.source,
        base=None,
        head=None,
        unsupported=metric.unsupported,
        spans=spans,
    )
    if diff.unsupported:
        return diff
    head_vals: dict[str, float | int | None] = {}
    base_vals: dict[str, float | int | None] = {}
    for token, inp in metric.inputs.items():
        node = head.models.get(inp.model_uid)
        if node is None or not _columns(con, node):
            diff.unsupported = f"`{node.name if node else inp.model_uid}` was not built in this run"
            return diff
        head_vals[token] = evaluate(con, relation(node), [inp], dialect).get(inp.name)
        model_diff = compared.get(inp.model_uid)
        if model_diff is None:
            base_vals[token] = head_vals[token]
        elif not model_diff.base_exists:
            base_vals[token] = None  # new in this pull request: nothing on the base side
        else:
            base_node = base.models[inp.model_uid]
            base_vals[token] = evaluate(con, relation(base_node), [inp], dialect).get(inp.name)
        if inp.unsupported:
            diff.unsupported = f"{inp.name}: {inp.unsupported}"
            return diff
    diff.head = combine(con, metric, head_vals)
    diff.base = combine(con, metric, base_vals)
    diff.unsupported = metric.unsupported
    return diff


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
        if not m.spans_models:
            by_model.setdefault(m.model_uid, []).append(m)
    spanning = [m for m in metrics if m.spans_models]

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
            else:
                common = [c for c, t in head_cols.items() if base_cols.get(c) == t]
                if common:
                    diff.rows_differing = _rows_differing(con, head_node, base_node, common)
                    diff.rows_differing_common_columns = len(common)

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
                dims: list[tuple[str, str]] | None = None  # computed lazily, at most once
                for d in defs:
                    metric_diff = MetricDiff(
                        name=d.name,
                        label=d.label,
                        source=d.source,
                        base=base_vals.get(d.name),
                        head=head_vals.get(d.name),
                        unsupported=d.unsupported,
                    )
                    if metric_diff.moved:
                        if dims is None:
                            dims = _categorical_dimensions(con, head, uid, dialect)
                        if dims:
                            metric_diff.breakdown = _metric_breakdown(
                                con, d, dims, head_node, base_node, dialect
                            )
                    diff.metrics.append(metric_diff)
            out.append(diff)

        # A metric that spans models is reported under the first of its models the change
        # reached; one none of whose models the change reached cannot have moved.
        compared = {d.unique_id: d for d in out}
        for metric in spanning:
            home = next((compared[uid] for uid in metric.model_uids if uid in compared), None)
            if home is None:
                continue
            home.metrics.append(_spanning_metric_diff(con, metric, head, base, compared, dialect))
    finally:
        con.close()
    return out
