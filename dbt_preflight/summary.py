"""The review comment's content, as JSON.

A hook or a coding agent needs the verdict and the numbers behind it without parsing the
Markdown the comment is written for. This module reads the same `PreflightReport` `render`
does and turns it into one JSON-native dict, so the comment and the summary can never say
different things about the same run: there is only one place that decides what a passed
test, a moved metric or a removed column means.

Keys are snake_case; every value is a plain string, number, boolean, list or dict, never a
dataclass. `schema_version` is bumped when a key's meaning or shape changes, not when a key
is only added.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dbt_preflight.diff import ModelDiff
from dbt_preflight.report import (
    BUILT,
    FAILED,
    NO_RESULT,
    NOT_VERIFIED,
    SKIPPED,
    FailedTest,
    ModelReport,
    PreflightReport,
    _generic_test_label,
    _human_reading,
)
from dbt_preflight.schema import InferredSource

SCHEMA_VERSION = 1


def _verdict(report: PreflightReport) -> str:
    if report.fatal:
        return "could_not_run"
    if report.nothing_changed:
        return "nothing_changed"
    if report.passed:
        return "passed_with_warnings" if report.has_warnings else "passed"
    return "failed"


def _model_counts(models: list[ModelReport]) -> dict[str, int]:
    counts = {"built": 0, "failed": 0, "skipped": 0, "not_verified": 0, "no_result": 0}
    by_status = {
        BUILT: "built",
        FAILED: "failed",
        SKIPPED: "skipped",
        NOT_VERIFIED: "not_verified",
        NO_RESULT: "no_result",
    }
    for m in models:
        key = by_status.get(m.status)
        if key:
            counts[key] += 1
    return counts


def _model(m: ModelReport) -> dict[str, Any]:
    return {
        "name": m.name,
        "path": m.path,
        "status": m.status,
        "changed": m.changed,
        "rows": m.rows,
        "tests_passed": m.tests_passed,
        "tests_failed": m.tests_failed,
        "tests_warned": m.tests_warned,
        "dialect_function": m.dialect_function,
    }


def _failing_test(t: FailedTest) -> dict[str, Any]:
    reading = _human_reading(t.message) if t.status == "error" and t.message.strip() else None
    return {
        "name": t.name,
        "readable_name": _generic_test_label(t) if t.test_name else None,
        "model": t.model,
        "status": t.status,
        "failures": t.failures,
        "reading": reading,
    }


def _diff(d: ModelDiff) -> dict[str, Any]:
    return {
        "name": d.name,
        "base_exists": d.base_exists,
        "rows_base": d.rows_base,
        "rows_head": d.rows_head,
        "rows_differing": d.rows_differing,
        "columns_added": [{"name": c, "type": t} for c, t in d.columns_added],
        "columns_removed": [{"name": c, "type": t} for c, t in d.columns_removed],
        "columns_retyped": [
            {"name": c, "base_type": bt, "head_type": ht} for c, bt, ht in d.columns_retyped
        ],
        "columns_renamed": [{"old_name": old, "new_name": new} for old, new in d.columns_renamed],
        "moved_metrics": [
            {"name": m.name, "label": m.label, "base": m.base, "head": m.head, "spans": m.spans}
            for m in d.moved_metrics
        ],
        "removed_column_references": {col: list(refs) for col, refs in d.references.items()},
    }


def _inferred_source(s: InferredSource) -> dict[str, Any]:
    return {
        "source_name": s.source_name,
        "table": s.table,
        "identifier": s.identifier,
        "models": list(s.models),
        "total_columns": s.total_columns,
        "guessed_columns": list(s.guessed_columns),
    }


def _fixtures(report: PreflightReport) -> dict[str, Any] | None:
    fx = report.fixtures
    if fx is None:
        return None
    return {
        "tables": [
            {"identifier": t.identifier, "schema": t.schema, "rows": t.rows} for t in fx.tables
        ],
        "total_rows": fx.total_rows,
        "inferred_sources": [_inferred_source(s) for s in fx.inferred_sources],
        "warnings": {
            "unmatched_sources": list(fx.unmatched_sources),
            "unused_dbml_tables": list(fx.unused_dbml_tables),
            "unmapped_columns": [{"column": c, "type": t} for c, t in fx.unmapped_columns],
            "cyclic_tables": list(fx.cyclic_tables),
            "unresolved_composite_keys": list(fx.unresolved_composite_keys),
            "parse_warnings": list(fx.parse_warnings),
        },
    }


def build_summary(
    report: PreflightReport, exit_code: int, comment_file: Path | None
) -> dict[str, Any]:
    """Everything a hook or an agent needs from one run, without parsing Markdown.

    `exit_code` is passed in rather than recomputed here: whether a failed run actually
    exits non-zero depends on `--fail-on-error`, a CLI concern this module knows nothing
    about, and the summary should report what the process actually did.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": _verdict(report),
        "exit_code": exit_code,
        "base_ref": report.base_ref,
        "head": report.head,
        "elapsed_seconds": report.elapsed,
        "fatal": report.fatal,
        "note": report.note,
        "counts": {
            "models": _model_counts(report.models),
            "tests": {
                "passed": sum(m.tests_passed for m in report.models),
                "failed": sum(m.tests_failed for m in report.models),
                "warned": sum(m.tests_warned for m in report.models),
            },
            "violations": {
                "error": len(report.error_violations),
                "warn": len(report.warn_violations),
            },
            "metrics": {
                "defined": report.metrics_defined,
                "moved": sum(len(d.moved_metrics) for d in report.diffs),
            },
        },
        "models": [_model(m) for m in report.models],
        "failing_tests": [_failing_test(t) for t in report.tests],
        "violations": [
            {
                "rule": v.rule,
                "severity": v.severity,
                "model": v.model,
                "path": v.path,
                "message": v.message,
            }
            for v in report.violations
        ],
        "diffs": [_diff(d) for d in report.diffs],
        "fixtures": _fixtures(report),
        "comment_file": str(comment_file) if comment_file is not None else None,
    }
