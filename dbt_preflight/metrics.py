"""Metric definitions, from wherever the project keeps them, as SQL preflight can run.

Three sources, in the order a reviewer would expect them:

1. **dbt's semantic layer.** Semantic models and metrics in the manifest. Simple, ratio and
   derived metrics are rewritten as aggregate expressions over the semantic model's dbt
   model; cumulative and conversion metrics need a time spine and are reported as not
   evaluated rather than approximated.
2. **Lightdash meta.** `meta.metrics` on a model and on its columns: the aggregate types
   (`sum`, `count_distinct`, ...) with their `filters`, and `number` metrics whose `sql`
   references other metrics with `${...}`.
3. **The preflight config.** `metrics:` entries with a `name`, a `model` and an aggregate
   `sql` expression, for projects that define metrics nowhere else.

Every definition becomes one aggregate expression evaluated as `select <expr> from <model>`,
once on the base build and once on the pull request's, on the same fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb
from sqlglot.errors import SqlglotError

from dbt_preflight.manifest import Manifest, MetricNode, ModelNode, SemanticModel
from dbt_preflight.transpile import transpile_sql

SOURCE_DBT = "dbt"
SOURCE_LIGHTDASH = "lightdash"
SOURCE_CONFIG = "config"


@dataclass
class MetricDef:
    name: str
    label: str
    model_uid: str
    sql: str  # an aggregate expression over the model's relation
    source: str
    unsupported: str | None = None  # why it cannot be evaluated, when it cannot

    @property
    def evaluable(self) -> bool:
        return self.unsupported is None


class MetricError(ValueError):
    pass


# ---------------------------------------------------------------- dbt semantic layer

_AGG_SQL = {
    "sum": "sum({e})",
    "count": "count({e})",
    "count_distinct": "count(distinct {e})",
    "average": "avg({e})",
    "min": "min({e})",
    "max": "max({e})",
    "median": "median({e})",
    "sum_boolean": "sum(case when {e} then 1 else 0 end)",
}
_JINJA_REF = re.compile(
    r"\{\{\s*(Dimension|TimeDimension|Entity|Metric)\(\s*'([^']+)'(?:\s*,\s*'[^']*')?\s*\)"
    r"(?:\.\w+\([^)]*\))*\s*\}\}"
)


def _resolve_filter(template: str, sm: SemanticModel) -> str:
    """A MetricFlow `where` template as plain SQL over the semantic model's own table."""

    def repl(m: re.Match[str]) -> str:
        kind, ref = m.group(1), m.group(2)
        name = ref.split("__")[-1]  # strip the entity path: order__order_status -> order_status
        if kind in {"Dimension", "TimeDimension"}:
            if name in sm.dimensions:
                return sm.dimensions[name]
            raise MetricError(f"dimension `{name}` is not on semantic model `{sm.name}`")
        if kind == "Entity":
            if name in sm.entities:
                return sm.entities[name]
            raise MetricError(f"entity `{name}` is not on semantic model `{sm.name}`")
        raise MetricError("filters on other metrics are not evaluated")

    out = _JINJA_REF.sub(repl, template).strip()
    if "{{" in out or "{%" in out:
        raise MetricError("filter uses Jinja preflight does not evaluate")
    return out


def _measure_sql(measure_name: str, filters: list[str], sm: SemanticModel) -> str:
    measure = sm.measures.get(measure_name)
    if measure is None:
        raise MetricError(f"measure `{measure_name}` is not on semantic model `{sm.name}`")
    pattern = _AGG_SQL.get(measure.agg)
    if pattern is None:
        raise MetricError(f"aggregation `{measure.agg}` is not evaluated")
    sql = pattern.format(e=measure.expr)
    if filters:
        cond = " and ".join(f"({_resolve_filter(f, sm)})" for f in filters)
        sql = f"{sql} filter (where {cond})"
    return sql


def _semantic_model_for_measure(manifest: Manifest, measure: str) -> SemanticModel:
    owners = [sm for sm in manifest.semantic_models.values() if measure in sm.measures]
    if not owners:
        raise MetricError(f"no semantic model defines measure `{measure}`")
    return owners[0]


def _dbt_metric_sql(
    metric: MetricNode, manifest: Manifest, by_name: dict[str, MetricNode], depth: int = 0
) -> tuple[str, str]:
    """(aggregate SQL, model unique id) for a dbt metric, recursing through its inputs."""
    if depth > 8:
        raise MetricError("metric definitions nest too deeply")

    if metric.type == "simple":
        if not metric.measure:
            raise MetricError("simple metric without a measure")
        sm = _semantic_model_for_measure(manifest, metric.measure)
        if sm.model_uid is None:
            raise MetricError(f"semantic model `{sm.name}` is not tied to a dbt model")
        return _measure_sql(
            metric.measure, metric.measure_filters + metric.filters, sm
        ), sm.model_uid

    if metric.type == "ratio":
        if not metric.numerator or not metric.denominator:
            raise MetricError("ratio metric without numerator and denominator")
        num, num_model = _dbt_metric_sql(
            _input(by_name, metric.numerator), manifest, by_name, depth + 1
        )
        den, den_model = _dbt_metric_sql(
            _input(by_name, metric.denominator), manifest, by_name, depth + 1
        )
        if num_model != den_model:
            raise MetricError("numerator and denominator live on different models")
        if metric.filters:
            raise MetricError("filters on ratio metrics are not evaluated")
        return f"cast(({num}) as double) / nullif(({den}), 0)", num_model

    if metric.type == "derived":
        if not metric.expr:
            raise MetricError("derived metric without an expression")
        expr = metric.expr
        model_uid: str | None = None
        for name in sorted(metric.input_metrics, key=len, reverse=True):
            sql, uid = _dbt_metric_sql(_input(by_name, name), manifest, by_name, depth + 1)
            if model_uid is None:
                model_uid = uid
            elif uid != model_uid:
                raise MetricError("inputs live on different models")
            expr = re.sub(rf"\b{re.escape(name)}\b", f"cast(({sql}) as double)", expr)
        if model_uid is None:
            raise MetricError("derived metric without inputs")
        if metric.filters:
            raise MetricError("filters on derived metrics are not evaluated")
        return f"({expr})", model_uid

    raise MetricError(f"{metric.type} metrics need a time spine and are not evaluated")


def _input(by_name: dict[str, MetricNode], name: str) -> MetricNode:
    try:
        return by_name[name]
    except KeyError:
        raise MetricError(f"input metric `{name}` does not exist") from None


def dbt_metrics(manifest: Manifest) -> list[MetricDef]:
    by_name = {m.name: m for m in manifest.metrics.values()}
    out: list[MetricDef] = []
    for metric in manifest.metrics.values():
        try:
            sql, model_uid = _dbt_metric_sql(metric, manifest, by_name)
            out.append(MetricDef(metric.name, metric.label, model_uid, sql, SOURCE_DBT))
        except MetricError as exc:
            model_uid = _guess_model(metric, manifest)
            out.append(MetricDef(metric.name, metric.label, model_uid, "", SOURCE_DBT, str(exc)))
    return out


def _guess_model(metric: MetricNode, manifest: Manifest) -> str:
    for dep in metric.depends_on:
        sm = manifest.semantic_models.get(dep)
        if sm and sm.model_uid:
            return sm.model_uid
    return ""


# ---------------------------------------------------------------- Lightdash meta

_LD_AGG = {
    "sum": "sum({e})",
    "count": "count({e})",
    "count_distinct": "count(distinct {e})",
    "average": "avg({e})",
    "min": "min({e})",
    "max": "max({e})",
    "median": "median({e})",
}
_LD_REF = re.compile(r"\$\{([^}]+)\}")


def _ld_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _ld_filter(field_name: str, value: object) -> str:
    """One Lightdash `filters:` entry as a SQL condition."""
    col = field_name.replace("${TABLE}.", "")
    if isinstance(value, list):
        return f"{col} in ({', '.join(_ld_literal(v) for v in value)})"
    if not isinstance(value, str):
        return f"{col} = {_ld_literal(value)}"
    text = value.strip()
    if text == "null":
        return f"{col} is null"
    if text == "!null":
        return f"{col} is not null"
    for op in (">=", "<=", ">", "<"):
        if text.startswith(op):
            return f"{col} {op} {text[len(op) :].strip()}"
    if text.startswith("!"):
        return f"{col} <> {_ld_literal(text[1:].strip())}"
    if "," in text:
        return f"{col} in ({', '.join(_ld_literal(v.strip()) for v in text.split(','))})"
    return f"{col} = {_ld_literal(text)}"


def lightdash_metrics(model: ModelNode) -> list[MetricDef]:
    specs: dict[str, tuple[str | None, dict]] = {}  # metric name -> (column or None, spec)
    for col, meta in model.column_meta.items():
        for name, spec in (meta.get("metrics") or {}).items():
            if isinstance(spec, dict):
                specs[name] = (col, spec)
    for name, spec in (model.meta.get("metrics") or {}).items():
        if isinstance(spec, dict):
            specs[name] = (None, spec)
    if not specs:
        return []

    resolved: dict[str, str] = {}
    failed: dict[str, str] = {}

    def resolve(name: str, stack: tuple[str, ...] = ()) -> str:
        if name in resolved:
            return resolved[name]
        if name in failed:
            raise MetricError(failed[name])
        if name in stack:
            raise MetricError(f"`{name}` references itself")
        col, spec = specs[name]
        mtype = str(spec.get("type", "")).lower()
        try:
            if mtype in _LD_AGG:
                expr = str(spec.get("sql") or col or "").replace("${TABLE}.", "")
                if not expr:
                    raise MetricError("aggregate metric without a column")
                sql = _LD_AGG[mtype].format(e=expr)
                filters = spec.get("filters") or []
                conds = []
                for f in filters:
                    if not isinstance(f, dict):
                        raise MetricError("filter is not a mapping")
                    for k, v in f.items():
                        conds.append(_ld_filter(str(k), v))
                if conds:
                    sql = f"{sql} filter (where {' and '.join(conds)})"
            elif mtype == "number":
                raw = str(spec.get("sql") or "")
                if not raw:
                    raise MetricError("number metric without `sql`")

                def repl(m: re.Match[str]) -> str:
                    ref = m.group(1).strip()
                    if ref == "TABLE":
                        return ""
                    if "." in ref:
                        return ref.split(".", 1)[1]  # ${model.column} -> column
                    if ref in specs:
                        return f"({resolve(ref, stack + (name,))})"
                    return ref  # a dimension of the same table

                sql = _LD_REF.sub(repl, raw).replace("${TABLE}.", "")
            else:
                raise MetricError(f"metric type `{mtype or '?'}` is not evaluated")
        except MetricError as exc:
            failed[name] = str(exc)
            raise
        resolved[name] = sql
        return sql

    out: list[MetricDef] = []
    for name, (_col, spec) in specs.items():
        label = str(spec.get("label") or name)
        try:
            out.append(MetricDef(name, label, model.unique_id, resolve(name), SOURCE_LIGHTDASH))
        except MetricError as exc:
            out.append(MetricDef(name, label, model.unique_id, "", SOURCE_LIGHTDASH, str(exc)))
    return out


# ---------------------------------------------------------------- config


def config_metrics(entries: list[dict[str, str]], manifest: Manifest) -> list[MetricDef]:
    by_name = {m.name: uid for uid, m in manifest.models.items()}
    out: list[MetricDef] = []
    for entry in entries:
        uid = by_name.get(entry["model"])
        label = entry.get("label") or entry["name"]
        if uid is None:
            out.append(
                MetricDef(
                    entry["name"],
                    label,
                    "",
                    "",
                    SOURCE_CONFIG,
                    f"no model named `{entry['model']}`",
                )
            )
            continue
        out.append(MetricDef(entry["name"], label, uid, entry["sql"], SOURCE_CONFIG))
    return out


# ---------------------------------------------------------------- collect and evaluate


def collect_metrics(manifest: Manifest, config_entries: list[dict[str, str]]) -> list[MetricDef]:
    """Every metric the project defines, in source order, de-duplicated by (model, name)."""
    seen: set[tuple[str, str]] = set()
    out: list[MetricDef] = []
    candidates = dbt_metrics(manifest)
    for model in manifest.models.values():
        candidates += lightdash_metrics(model)
    candidates += config_metrics(config_entries, manifest)
    for m in candidates:
        key = (m.model_uid, m.name)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def evaluate(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    metrics: list[MetricDef],
    dialect: str | None,
) -> dict[str, float | int | None]:
    """Metric values over `relation`; a metric that errors maps to None with its reason kept."""
    values: dict[str, float | int | None] = {}
    evaluable = [m for m in metrics if m.evaluable]
    if not evaluable:
        return values

    def run(subset: list[MetricDef]) -> list:
        select = ", ".join(f'({m.sql}) as "{m.name}"' for m in subset)
        sql = f"select {select} from {relation}"
        if dialect:
            try:
                sql = transpile_sql(sql, dialect)
            except SqlglotError:
                pass
        row = con.execute(sql).fetchone()
        return list(row) if row else [None] * len(subset)

    try:
        for m, v in zip(evaluable, run(evaluable), strict=True):
            values[m.name] = v
        return values
    except duckdb.Error:
        pass  # one bad metric should not hide the others: retry one at a time

    for m in evaluable:
        try:
            values[m.name] = run([m])[0]
        except duckdb.Error as exc:
            m.unsupported = str(exc).splitlines()[0][:160]
            values[m.name] = None
    return values
