"""Typed views over the parts of dbt's manifest.json that preflight reads.

The manifest is a large, versioned document. Everything preflight needs is pulled into
small dataclasses here so the rest of the code never indexes raw dicts, and so a manifest
schema change surfaces in one file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _where_templates(filter_obj: Any) -> list[str]:
    """The `where_sql_template` strings of a metric or measure filter, if any."""
    if not isinstance(filter_obj, dict):
        return []
    return [
        str(f.get("where_sql_template", "")).strip()
        for f in filter_obj.get("where_filters") or []
        if isinstance(f, dict) and f.get("where_sql_template")
    ]


def _strip_project(patch_path: str | None) -> str | None:
    """`project://models/x.yml` -> `models/x.yml`."""
    if not patch_path:
        return None
    return patch_path.split("://", 1)[1] if "://" in patch_path else patch_path


@dataclass
class SourceColumn:
    name: str
    data_type: str | None
    description: str = ""


@dataclass
class SourceTable:
    unique_id: str
    source_name: str
    name: str
    identifier: str
    database: str | None
    schema: str
    loader: str
    description: str = ""
    columns: list[SourceColumn] = field(default_factory=list)


@dataclass
class Measure:
    name: str
    agg: str
    expr: str


@dataclass
class SemanticModel:
    unique_id: str
    name: str
    model_uid: str | None
    measures: dict[str, Measure]
    dimensions: dict[str, str]  # name -> SQL expression
    entities: dict[str, str]  # name -> SQL expression


@dataclass
class MetricNode:
    unique_id: str
    name: str
    label: str
    type: str  # simple | ratio | derived | cumulative | conversion
    measure: str | None
    measure_filters: list[str]
    numerator: str | None
    denominator: str | None
    expr: str | None
    input_metrics: list[str]
    filters: list[str]  # where_sql_template strings
    depends_on: list[str]


@dataclass
class ModelNode:
    unique_id: str
    name: str
    path: str  # relative to the models directory, e.g. staging/webshop/stg_x.sql
    original_file_path: str  # relative to the project directory
    patch_path: str | None  # the YAML file that describes the model, relative to the project
    database: str | None
    schema: str
    alias: str
    description: str
    depends_on: list[str]
    materialized: str
    column_names: list[str]
    meta: dict[str, Any] = field(default_factory=dict)
    column_meta: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def layer(self) -> str:
        """First folder under the models directory, or "" for a model at the root."""
        parts = Path(self.path).parts
        return parts[0] if len(parts) > 1 else ""


@dataclass
class TestNode:
    unique_id: str
    name: str
    test_name: str | None  # unique, not_null, relationships, ... (None for singular tests)
    column_name: str | None
    attached_node: str | None
    depends_on: list[str]
    kwargs: dict[str, Any]
    original_file_path: str


@dataclass
class Manifest:
    sources: dict[str, SourceTable]
    models: dict[str, ModelNode]
    tests: dict[str, TestNode]
    parent_map: dict[str, list[str]] = field(default_factory=dict)
    child_map: dict[str, list[str]] = field(default_factory=dict)
    semantic_models: dict[str, SemanticModel] = field(default_factory=dict)
    metrics: dict[str, MetricNode] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    def affected_models(self, modified: set[str]) -> list[str]:
        """The models a change to `modified` can break, closed so dbt can build them.

        Downstream models are affected directly. A model whose *test* reads a modified
        model (a `relationships` test to it) is affected too, and would be missed by
        `state:modified+`. Then every ancestor of that set, because on a fresh DuckDB
        file nothing exists until it is built.
        """
        affected: set[str] = {m for m in modified if m in self.models}
        # A modified source has no row of its own but everything reading it is affected.
        frontier = [m for m in modified if m in self.models or m in self.sources]
        while frontier:
            uid = frontier.pop()
            for child in self.child_map.get(uid, []):
                if child in self.models and child not in affected:
                    affected.add(child)
                    frontier.append(child)

        for test in self.tests.values():
            parents = [p for p in test.depends_on if p in self.models]
            if any(p in affected for p in parents):
                for p in parents:
                    if p not in affected:
                        affected.add(p)

        closed = set(affected)
        frontier = list(affected)
        while frontier:
            uid = frontier.pop()
            for parent in self.parent_map.get(uid, []):
                if parent in self.models and parent not in closed:
                    closed.add(parent)
                    frontier.append(parent)
        return sorted(closed)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Manifest:
        sources: dict[str, SourceTable] = {}
        for uid, src in raw.get("sources", {}).items():
            columns = [
                SourceColumn(
                    name=col.get("name", name),
                    data_type=col.get("data_type"),
                    description=col.get("description") or "",
                )
                for name, col in (src.get("columns") or {}).items()
            ]
            sources[uid] = SourceTable(
                unique_id=uid,
                source_name=src["source_name"],
                name=src["name"],
                identifier=src.get("identifier") or src["name"],
                database=src.get("database"),
                schema=src["schema"],
                loader=src.get("loader") or "",
                description=src.get("description") or "",
                columns=columns,
            )

        models: dict[str, ModelNode] = {}
        tests: dict[str, TestNode] = {}
        for uid, node in raw.get("nodes", {}).items():
            rtype = node.get("resource_type")
            if rtype == "model":
                models[uid] = ModelNode(
                    unique_id=uid,
                    name=node["name"],
                    path=node.get("path", ""),
                    original_file_path=node.get("original_file_path", ""),
                    patch_path=_strip_project(node.get("patch_path")),
                    database=node.get("database"),
                    schema=node.get("schema", ""),
                    alias=node.get("alias") or node["name"],
                    description=node.get("description") or "",
                    depends_on=list((node.get("depends_on") or {}).get("nodes") or []),
                    materialized=str((node.get("config") or {}).get("materialized", "")),
                    column_names=list((node.get("columns") or {}).keys()),
                    meta=dict((node.get("config") or {}).get("meta") or node.get("meta") or {}),
                    column_meta={
                        name: dict((col.get("config") or {}).get("meta") or col.get("meta") or {})
                        for name, col in (node.get("columns") or {}).items()
                    },
                )
            elif rtype == "test":
                meta = node.get("test_metadata") or {}
                kwargs = dict(meta.get("kwargs") or {})
                tests[uid] = TestNode(
                    unique_id=uid,
                    name=node["name"],
                    test_name=meta.get("name"),
                    column_name=node.get("column_name") or kwargs.get("column_name"),
                    attached_node=node.get("attached_node"),
                    depends_on=list((node.get("depends_on") or {}).get("nodes") or []),
                    kwargs=kwargs,
                    original_file_path=node.get("original_file_path", ""),
                )
        semantic_models: dict[str, SemanticModel] = {}
        for uid, sm in (raw.get("semantic_models") or {}).items():
            deps = list((sm.get("depends_on") or {}).get("nodes") or [])
            semantic_models[uid] = SemanticModel(
                unique_id=uid,
                name=sm["name"],
                model_uid=next((d for d in deps if d.startswith("model.")), None),
                measures={
                    m["name"]: Measure(
                        name=m["name"], agg=str(m.get("agg", "")), expr=m.get("expr") or m["name"]
                    )
                    for m in sm.get("measures") or []
                },
                dimensions={
                    d["name"]: d.get("expr") or d["name"] for d in sm.get("dimensions") or []
                },
                entities={e["name"]: e.get("expr") or e["name"] for e in sm.get("entities") or []},
            )

        metrics: dict[str, MetricNode] = {}
        for uid, mt in (raw.get("metrics") or {}).items():
            tp = mt.get("type_params") or {}
            measure = tp.get("measure") or {}
            metrics[uid] = MetricNode(
                unique_id=uid,
                name=mt["name"],
                label=mt.get("label") or mt["name"],
                type=str(mt.get("type", "")),
                measure=measure.get("name") if isinstance(measure, dict) else None,
                measure_filters=_where_templates(measure.get("filter"))
                if isinstance(measure, dict)
                else [],
                numerator=(tp.get("numerator") or {}).get("name"),
                denominator=(tp.get("denominator") or {}).get("name"),
                expr=tp.get("expr"),
                input_metrics=[m["name"] for m in tp.get("metrics") or [] if m.get("name")],
                filters=_where_templates(mt.get("filter")),
                depends_on=list((mt.get("depends_on") or {}).get("nodes") or []),
            )

        return cls(
            sources=sources,
            models=models,
            tests=tests,
            parent_map={k: list(v) for k, v in (raw.get("parent_map") or {}).items()},
            child_map={k: list(v) for k, v in (raw.get("child_map") or {}).items()},
            semantic_models=semantic_models,
            metrics=metrics,
        )

    def tests_for_model(self, model_uid: str) -> list[TestNode]:
        """Tests declared on a model, by attachment first and by dependency as fallback."""
        out = []
        for test in self.tests.values():
            if test.attached_node == model_uid:
                out.append(test)
            elif test.attached_node is None and model_uid in test.depends_on:
                out.append(test)
        return out

    def column_tests(self, model_uid: str) -> dict[str, set[str]]:
        """{column_name: {test names}} for the generic tests declared on a model."""
        result: dict[str, set[str]] = {}
        for test in self.tests_for_model(model_uid):
            if test.column_name and test.test_name:
                result.setdefault(test.column_name, set()).add(test.test_name)
        return result

    def source_tests(self) -> list[TestNode]:
        return [t for t in self.tests.values() if t.attached_node in self.sources]
