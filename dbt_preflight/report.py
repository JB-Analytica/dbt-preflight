"""The review comment.

One comment per pull request, updated on every push. It leads with the verdict, then the
changed models, then anything that needs a human, and closes with what the check does not
claim. It is written to be read in the pull-request sidebar, so brevity is a feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dbt_preflight.checks import SEVERITY_ERROR, Violation
from dbt_preflight.diff import MetricDiff, ModelDiff
from dbt_preflight.fixtures import FixtureSummary

MARKER = "<!-- dbt-preflight -->"

# Model statuses, in the order they are explained to a reader.
BUILT = "built"
FAILED = "failed"
NOT_VERIFIED = "not_verified"
SKIPPED = "skipped"
NO_RESULT = "no_result"


@dataclass
class ModelReport:
    unique_id: str
    name: str
    path: str
    status: str
    changed: bool
    rows: int | None = None
    message: str = ""
    dialect_function: str | None = None
    tests_passed: int = 0
    tests_failed: int = 0
    tests_warned: int = 0


@dataclass
class FailedTest:
    name: str
    model: str
    status: str  # fail | error | warn
    failures: int | None
    message: str
    compiled_code: str | None = None
    kind: str = "test"  # test | unit_test


@dataclass
class PreflightReport:
    models: list[ModelReport] = field(default_factory=list)
    tests: list[FailedTest] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    fixtures: FixtureSummary | None = None
    schema_source: str = ""
    seed: int = 0
    base_ref: str | None = None
    head: str | None = None
    elapsed: float = 0.0
    dialect_failures_are_errors: bool = False
    fatal: str | None = None
    nothing_changed: bool = False
    note: str | None = None  # one line of context under the summary, e.g. why all models ran
    dialect: str | None = None  # SQL dialect transpiled to DuckDB, when one was
    untranspiled: dict[str, str] = field(default_factory=dict)  # model -> why sqlglot gave up
    diffs: list[ModelDiff] = field(default_factory=list)  # base vs head, for changed models
    metrics_defined: int = 0  # how many metric definitions the project has, across sources

    @property
    def changed(self) -> list[ModelReport]:
        return [m for m in self.models if m.changed]

    @property
    def failed_models(self) -> list[ModelReport]:
        return [m for m in self.models if m.status == FAILED]

    @property
    def unverified_models(self) -> list[ModelReport]:
        """Changed models DuckDB could not run. An unchanged one is old news, not a warning."""
        return [m for m in self.models if m.status == NOT_VERIFIED and m.changed]

    @property
    def error_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == SEVERITY_ERROR]

    @property
    def warn_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.severity != SEVERITY_ERROR]

    @property
    def failing_tests(self) -> list[FailedTest]:
        return [t for t in self.tests if t.status in {"fail", "error"}]

    @property
    def warning_tests(self) -> list[FailedTest]:
        return [t for t in self.tests if t.status == "warn"]

    @property
    def unbuilt_models(self) -> list[ModelReport]:
        """Models in the selection that never produced a table: skipped or without a result."""
        return [m for m in self.models if m.status in {SKIPPED, NO_RESULT}]

    @property
    def passed(self) -> bool:
        if self.fatal:
            return False
        if self.failed_models or self.failing_tests or self.error_violations:
            return False
        if self.unbuilt_models:
            return False
        if self.dialect_failures_are_errors and self.unverified_models:
            return False
        return True

    @property
    def breaking_diffs(self) -> list[ModelDiff]:
        """Changed models that lost a column or changed a column's type."""
        return [d for d in self.diffs if d.breaking]

    @property
    def has_warnings(self) -> bool:
        return bool(
            self.unverified_models
            or self.warn_violations
            or self.warning_tests
            or self.breaking_diffs
        )


_STATUS_LABEL = {
    BUILT: "✅ built",
    FAILED: "❌ failed",
    NOT_VERIFIED: "⚠️ not verified",
    SKIPPED: "⏭️ skipped",
    NO_RESULT: "– not built",
}


def _tests_cell(m: ModelReport) -> str:
    total = m.tests_passed + m.tests_failed + m.tests_warned
    if total == 0:
        return "none"
    parts = [f"{m.tests_passed} passed"]
    if m.tests_failed:
        parts.append(f"**{m.tests_failed} failed**")
    if m.tests_warned:
        parts.append(f"{m.tests_warned} warned")
    return ", ".join(parts)


def _one_line_error(message: str) -> str:
    """The line of a dbt error that says what went wrong, without the framing around it."""
    lines = [ln.strip() for ln in message.strip().splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith(("Runtime Error", "Compilation Error", "Database Error")):
            continue
        return ln[:200]
    return (lines[0] if lines else "error")[:200]


def _seconds(value: float) -> str:
    return f"{value:.0f} s" if value >= 10 else f"{value:.1f} s"


def render(report: PreflightReport) -> str:
    lines: list[str] = [MARKER]

    if report.fatal:
        lines += [
            "## 🛫 dbt preflight: ❌ could not run",
            "",
            report.fatal.strip(),
            "",
            _scope_block(),
        ]
        return "\n".join(lines)

    if report.passed and report.has_warnings:
        verdict = "⚠️ passed with warnings"
    elif report.passed:
        verdict = "✅ passed"
    else:
        verdict = "❌ failed"
    lines.append(f"## 🛫 dbt preflight: {verdict}")
    lines.append("")

    if report.nothing_changed:
        lines.append("No models changed against the base branch, so there was nothing to build.")
        lines.append("")
        lines.append(_scope_block())
        return "\n".join(lines)

    built = sum(1 for m in report.models if m.status == BUILT)
    changed = len(report.changed)
    tests_total = sum(m.tests_passed + m.tests_failed + m.tests_warned for m in report.models)
    scope = f"{changed} changed" if report.base_ref else "no base branch, all built"
    summary = (
        f"Built {built} of {len(report.models)} models "
        f"({scope}) against synthetic data · "
        f"{tests_total} tests · {len(report.violations)} convention "
        f"{'issue' if len(report.violations) == 1 else 'issues'} · {_seconds(report.elapsed)}"
    )
    lines.append(summary)
    lines.append("")
    if report.note:
        lines.append(report.note)
        lines.append("")

    # Changed models. When there is no base to diff against, every model is "changed".
    rows = report.changed or report.models
    heading = "Changed models" if report.base_ref else "Models"
    lines.append(f"### {heading}")
    lines.append("")
    lines.append("| Model | Build | Rows | Tests |")
    lines.append("| --- | --- | ---: | --- |")
    for m in rows:
        rows_cell = f"{m.rows:,}" if m.rows is not None else "–"
        build_cell = _STATUS_LABEL.get(m.status, m.status)
        if m.status == NOT_VERIFIED and m.dialect_function:
            build_cell += f" (`{m.dialect_function}`)"
        lines.append(f"| `{m.name}` | {build_cell} | {rows_cell} | {_tests_cell(m)} |")
    lines.append("")

    around = [m for m in report.models if not m.changed]
    if around and report.base_ref:
        broken = [m for m in around if m.status in {FAILED, SKIPPED} or m.tests_failed]
        if broken:
            lines.append("Unchanged models this change breaks:")
            lines.append("")
            for m in broken:
                what = _STATUS_LABEL.get(m.status, m.status)
                if m.status == SKIPPED:
                    what += " (an upstream model or test failed)"
                if m.tests_failed:
                    what += (
                        f", {m.tests_failed} failing {'test' if m.tests_failed == 1 else 'tests'}"
                    )
                lines.append(f"- `{m.name}` — {what}")
            lines.append("")
        fine = [m for m in around if m not in broken]
        if fine:
            names = []
            for m in fine:
                if m.status == NOT_VERIFIED:
                    names.append(f"`{m.name}` (not verified: `{m.dialect_function}`)")
                else:
                    names.append(f"`{m.name}`")
            lines.append(f"Also rebuilt, no new issues: {', '.join(names)}.")
            lines.append("")

    if report.failed_models:
        lines.append("### Build errors")
        lines.append("")
        for m in report.failed_models:
            lines.append(f"**`{m.name}`** — {m.path}")
            lines.append("")
            lines.append("```")
            lines.append(m.message.strip()[:1500])
            lines.append("```")
            lines.append("")

    if report.failing_tests or report.warning_tests:
        lines.append("### Failing tests")
        lines.append("")
        for t in report.failing_tests + report.warning_tests:
            icon = "❌" if t.status in {"fail", "error"} else "⚠️"
            if t.kind == "unit_test":
                detail = "actual output differs from the expected rows"
            elif t.status == "error":
                detail = _one_line_error(t.message)
            elif t.failures is not None:
                detail = f"{t.failures} failing {'row' if t.failures == 1 else 'rows'}"
            else:
                detail = t.status
            label = "unit test " if t.kind == "unit_test" else ""
            lines.append(f"- {icon} {label}`{t.name}` on `{t.model}`: {detail}")
            if t.compiled_code or t.status == "error" or t.kind == "unit_test":
                lines.append("  <details><summary>details</summary>")
                lines.append("")
                if (t.status == "error" or t.kind == "unit_test") and t.message.strip():
                    lines.append("  ```")
                    for msg_line in t.message.strip().splitlines()[:20]:
                        lines.append(f"  {msg_line}")
                    lines.append("  ```")
                if t.compiled_code:
                    lines.append("  ```sql")
                    for code_line in t.compiled_code.strip().splitlines()[:40]:
                        lines.append(f"  {code_line}")
                    lines.append("  ```")
                lines.append("  </details>")
        lines.append("")

    if report.diffs:
        lines += _diff_section(report)

    if report.unverified_models:
        lines.append("### Not verified on DuckDB")
        lines.append("")
        if report.dialect:
            lines.append(
                f"These models still use something DuckDB does not have after transpiling from "
                f"{report.dialect}, so preflight cannot run them. Consider `adapter.dispatch` or "
                "a macro if the project should stay portable."
            )
        else:
            lines.append(
                "These models use a function DuckDB does not have, so preflight cannot run them. "
                "Consider `adapter.dispatch` or a macro if the project should stay portable."
            )
        lines.append("")
        for m in report.unverified_models:
            fn = f" — `{m.dialect_function}`" if m.dialect_function else ""
            lines.append(f"- `{m.name}` ({m.path}){fn}")
        lines.append("")

    if report.violations:
        lines.append("### Conventions")
        lines.append("")
        for v in sorted(report.violations, key=lambda v: (v.severity != SEVERITY_ERROR, v.path)):
            icon = "❌" if v.severity == SEVERITY_ERROR else "⚠️"
            lines.append(f"- {icon} **{v.rule}** `{v.path}` — {v.message}")
        lines.append("")

    fixtures = _fixtures_block(report)
    if fixtures:
        lines.append(fixtures)
        lines.append("")
    lines.append(_scope_block())
    return "\n".join(lines)


def _num(value: float | int | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    v = float(value)
    return f"{v:,.4f}" if abs(v) < 1 else f"{v:,.2f}"


def _delta(m: MetricDiff) -> str:
    if m.base is None or m.head is None:
        return "–"
    base, head = float(m.base), float(m.head)
    diff = head - base
    if base == 0:
        return f"{'+' if diff > 0 else ''}{_num(diff)}"
    pct = diff / abs(base) * 100
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"


def _diff_section(report: PreflightReport) -> list[str]:
    lines = ["### What changed in the output", ""]
    lines.append(
        "Base branch and pull request, built on the same synthetic data. A difference here was "
        "caused by this change and nothing else."
    )
    lines.append("")

    identical: list[str] = []
    for d in report.diffs:
        if d.identical:
            identical.append(d.name)
            continue
        if not d.base_exists:
            cols = len(d.columns_added)
            rows = _num(d.rows_head)
            lines.append(f"**`{d.name}`** — new in this pull request: {rows} rows, {cols} columns.")
            lines.append("")
            continue

        bits: list[str] = []
        if d.rows_changed:
            bits.append(f"rows {_num(d.rows_base)} → {_num(d.rows_head)}")
        elif d.rows_head is not None:
            bits.append(f"rows {_num(d.rows_head)} (unchanged)")
        if d.rows_differing:
            bits.append(
                f"{_num(d.rows_differing)} {'row' if d.rows_differing == 1 else 'rows'} "
                "with different values"
            )
        cols: list[str] = []
        cols += [f"+`{c}`" for c, _ in d.columns_added]
        cols += [f"−`{c}` ⚠️" for c, _ in d.columns_removed]
        cols += [f"`{c}` {old} → {new} ⚠️" for c, old, new in d.columns_retyped]
        if cols:
            bits.append("columns: " + ", ".join(cols))
        lines.append(f"**`{d.name}`** — " + " · ".join(bits))
        lines.append("")

        moved = d.moved_metrics
        if moved:
            lines.append("| Metric | Base | PR | Δ |")
            lines.append("| --- | ---: | ---: | ---: |")
            for m in moved:
                lines.append(f"| {m.label} | {_num(m.base)} | {_num(m.head)} | {_delta(m)} |")
            lines.append("")
        steady = [m for m in d.metrics if not m.moved and not m.unsupported]
        skipped = [m for m in d.metrics if m.unsupported]
        notes: list[str] = []
        if steady:
            notes.append(
                f"{len(steady)} {'metric' if len(steady) == 1 else 'metrics'} unchanged"
                + (
                    ""
                    if moved
                    else f" ({', '.join(m.label for m in steady[:6])}{', …' if len(steady) > 6 else ''})"
                )
            )
        if skipped:
            notes.append(
                "not evaluated: "
                + ", ".join(f"{m.label} ({m.unsupported})" for m in skipped[:4])
                + (", …" if len(skipped) > 4 else "")
            )
        if notes:
            lines.append("; ".join(notes) + ".")
            lines.append("")

    if identical:
        with_metrics = sum(1 for d in report.diffs if d.identical and d.metrics)
        tail = " and every defined metric" if with_metrics else ""
        lines.append(
            "Identical output to the base branch, same columns, rows and values"
            + tail
            + ": "
            + ", ".join(f"`{n}`" for n in identical)
            + "."
        )
        lines.append("")

    if report.metrics_defined == 0:
        lines.append(
            "_No metrics are defined, so only columns and row counts were compared. Preflight "
            "reads dbt semantic-layer metrics, Lightdash `meta.metrics`, or `metrics:` in "
            "`.dbt-preflight.yml`._"
        )
        lines.append("")
    return lines


def _fixtures_block(report: PreflightReport) -> str:
    fx = report.fixtures
    if fx is None:
        return ""
    tables = ", ".join(f"`{t.identifier}` {t.rows:,}" for t in fx.tables)
    parts = [
        "<details><summary>Fixtures</summary>",
        "",
        f"Synthetic source data from {report.schema_source}, seed {report.seed}: "
        f"{len(fx.tables)} tables, {fx.total_rows:,} rows.",
        "",
        tables,
    ]
    if fx.unmatched_sources:
        parts += [
            "",
            "Sources with no matching table in the schema (models reading them will fail): "
            + ", ".join(f"`{s}`" for s in fx.unmatched_sources),
        ]
    if fx.unused_dbml_tables:
        parts += [
            "",
            "Schema tables no source declares: "
            + ", ".join(f"`{t}`" for t in fx.unused_dbml_tables),
        ]
    if report.dialect:
        parts += ["", f"Model SQL transpiled from {report.dialect} to DuckDB with sqlglot."]
        if report.untranspiled:
            parts += [
                "",
                "Ran as written because sqlglot could not parse them: "
                + ", ".join(f"`{m}` ({why})" for m, why in report.untranspiled.items()),
            ]
    ref = f"base `{report.base_ref}`" if report.base_ref else "no base branch"
    head = f", head `{report.head}`" if report.head else ""
    parts += ["", f"Compared against {ref}{head}.", "</details>"]
    return "\n".join(parts)


def _scope_block() -> str:
    return "\n".join(
        [
            "<details><summary>What this checks, and what it cannot</summary>",
            "",
            "**Checks:** the changed models compile and run against a schema-faithful "
            "synthetic dataset; their schema and relationship tests pass; the change follows "
            "the house conventions.",
            "",
            "**Cannot check:** that production numbers are unchanged. A metric that does not "
            "move on synthetic data can still move on production, because the fixtures do not "
            "carry production's distribution; the diff proves the logic changed, not the size "
            "of the effect. Also outside scope: warehouse SQL that survives transpiling, and "
            "incremental behaviour across runs.",
            "",
            "Generated by [dbt-preflight](https://github.com/JB-Analytica/dbt-preflight) "
            "with fixtures from [model2data](https://github.com/JB-Analytica/model2data).",
            "</details>",
        ]
    )
