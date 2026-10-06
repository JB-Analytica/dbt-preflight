"""The review comment.

One comment per pull request, updated on every push. It leads with the verdict, then the
changed models, then anything that needs a human, and closes with what the check does not
claim. It is written to be read in the pull-request sidebar, so brevity is a feature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
    tests_failed: int = 0  # failures this change caused: new on head, or worse than on base
    tests_warned: int = 0
    tests_failed_on_base: int = 0  # failing the same way on the base branch: not this change
    # Judged against the base branch (dbt_preflight/baseline.py): failed to build there
    # too, the same way, without this change touching it; or skipped only because a model
    # like that upstream of it failed. Neither fails the check.
    broken_on_base: bool = False
    skipped_by_base: bool = False
    # Fails on the base the same way, but the change reaches it from upstream, so a new
    # error could hide behind the old one: it counts, as "could not be checked". What it
    # alone skips is `skipped_by_unverified`, and counts too.
    unverified_broken_on_base: bool = False
    reached_from: list[str] = field(default_factory=list)  # what the change modified upstream
    # "<table>.<column>" source columns it reads, directly or upstream, whose type preflight
    # guessed: a failure on both branches may be the guess, so it is not "broken on main".
    guessed_inputs: list[str] = field(default_factory=list)
    # Fails on both branches with an error about a generated value (`baseline.
    # FIXTURE_SHAPED_ERRORS`): what kind, e.g. "malformed JSON". Not "broken on main".
    fixture_error: str | None = None
    skipped_by_unverified: bool = False
    # Fails the same way on both branches over preflight's data (`fixture_error`, or a
    # guessed column in `guessed_inputs`), and the change does not reach it: identical SQL
    # on identical data, so the change cannot affect it. A warning, never a failure.
    fixture_limited: bool = False
    skipped_by_fixture_limited: bool = False  # skipped only because of a model like that
    # Skipped on both branches behind a model broken on the base or one preflight's data
    # cannot build, but the change reaches it through another parent: it counts, unchecked.
    skipped_unchecked: bool = False
    # Tests the change added or edited on it: why a model failing on the base too counts.
    edited_tests: list[str] = field(default_factory=list)

    @property
    def not_this_change(self) -> bool:
        return (
            self.broken_on_base
            or self.skipped_by_base
            or self.fixture_limited
            or self.skipped_by_fixture_limited
        )


@dataclass
class FailedTest:
    name: str
    model: str
    status: str  # fail | error | warn
    failures: int | None
    message: str
    compiled_code: str | None = None
    kind: str = "test"  # test | unit_test
    # From TestNode, for rendering a generic test's own name and target instead of dbt's
    # generated one. None/empty for singular tests, which keep dbt's name as `name` above.
    test_name: str | None = None  # unique | not_null | accepted_values | relationships | ...
    column_name: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)
    unique_id: str = ""
    # Judged against the base branch (dbt_preflight/baseline.py). `preexisting` is a test
    # that already failed there the same way, so it does not fail the check;
    # `base_failures` is the base branch's failing-row count when it had one to compare.
    preexisting: bool = False
    base_failures: int | None = None
    # "<table>.<column>" source columns it reads, directly or upstream, whose type
    # preflight guessed: never pre-existing, and the comment says so.
    guessed_inputs: list[str] = field(default_factory=list)


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
    # Snapshots with a legacy fixed `target_schema`, built for the pull request only.
    shared_snapshots: list[str] = field(default_factory=list)
    # (schema, table) of every relation the project builds or reads, from the manifest;
    # None when unknown. See `_missing_table_reading`.
    relations: set[tuple[str, str]] | None = None
    diffs: list[ModelDiff] = field(default_factory=list)  # base vs head, for changed models
    metrics_defined: int = 0  # how many metric definitions the project has, across sources

    @property
    def changed(self) -> list[ModelReport]:
        return [m for m in self.models if m.changed]

    @property
    def failed_models(self) -> list[ModelReport]:
        """Models that failed to build because of this change."""
        return [
            m
            for m in self.models
            if m.status == FAILED and not m.broken_on_base and not m.fixture_limited
        ]

    @property
    def broken_on_base_models(self) -> list[ModelReport]:
        """Models that fail to build on the base branch too, the same way."""
        return [m for m in self.models if m.broken_on_base]

    @property
    def fixture_limited_models(self) -> list[ModelReport]:
        """Models the change does not reach that preflight's generated data cannot build."""
        return [m for m in self.models if m.fixture_limited]

    @property
    def unverified_broken_models(self) -> list[ModelReport]:
        """Models broken on the base too that this change reaches: counted, not checked."""
        return [m for m in self.models if m.unverified_broken_on_base]

    @property
    def build_error_models(self) -> list[ModelReport]:
        """Failed models whose errors are shown as this change's: not the unverified ones."""
        return [m for m in self.failed_models if not m.unverified_broken_on_base]

    @property
    def skipped_by_base_models(self) -> list[ModelReport]:
        """Models skipped only because a model broken on the base branch too is upstream."""
        return [m for m in self.models if m.skipped_by_base]

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
        """Failing tests this change answers for. Pre-existing failures are not among them."""
        return [t for t in self.tests if t.status in {"fail", "error"} and not t.preexisting]

    @property
    def preexisting_tests(self) -> list[FailedTest]:
        """Failing tests that already failed the same way on the base branch."""
        return [t for t in self.tests if t.status in {"fail", "error"} and t.preexisting]

    @property
    def warning_tests(self) -> list[FailedTest]:
        return [t for t in self.tests if t.status == "warn"]

    @property
    def unbuilt_models(self) -> list[ModelReport]:
        """Models in the selection that never produced a table: skipped or without a result."""
        return [
            m
            for m in self.models
            if m.status in {SKIPPED, NO_RESULT}
            and not m.skipped_by_base
            and not m.skipped_by_fixture_limited
        ]

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
            or self.preexisting_tests
            or self.broken_on_base_models
            or self.fixture_limited_models
        )


# How many moved metrics get their dimension breakdown shown in full; the rest fold.
_BREAKDOWN_METRICS_SHOWN = 3
# Pre-existing failures carry their details block (dbt name, compiled SQL) up to this many;
# past it, one line each. A project whose fixtures fail 40 tests on every branch would
# otherwise spend the comment's 65,536 characters on what this change did not do.
_PREEXISTING_DETAILED = 10

_STATUS_LABEL = {
    BUILT: "✅ built",
    FAILED: "❌ failed",
    NOT_VERIFIED: "⚠️ not verified",
    SKIPPED: "⏭️ skipped",
    NO_RESULT: "– not built",
}


def _tests_total(m: ModelReport) -> int:
    return m.tests_passed + m.tests_failed + m.tests_warned + m.tests_failed_on_base


def _tests_cell(m: ModelReport) -> str:
    if _tests_total(m) == 0:
        return "none"
    parts = [f"{m.tests_passed} passed"]
    if m.tests_failed:
        parts.append(f"**{m.tests_failed} failed**")
    if m.tests_warned:
        parts.append(f"{m.tests_warned} warned")
    if m.tests_failed_on_base:
        parts.append(f"{m.tests_failed_on_base} failing on base too")
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


# DuckDB's error text, translated to what it means for the pull request. Tried in order;
# a shape none of these match falls through to the raw message, unchanged.
_ERROR_READINGS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r'Column "([^"]+)" referenced that exists in the SELECT clause'),
        "the input no longer has a column called `{0}`",
    ),
    (
        re.compile(r'Referenced column "([^"]+)" not found in FROM clause'),
        "this model has no column `{0}`: renamed or dropped upstream?",
    ),
    (
        re.compile(r"Scalar Function with name (\w+) does not exist"),
        "`{0}` is not a DuckDB function",
    ),
    (re.compile(r"Parser Error"), "DuckDB could not parse this SQL"),
]


_MISSING_TABLE = re.compile(r"Table with name (\S+) does not exist")


def _missing_table_reading(name: str, relations: set[tuple[str, str]] | None) -> str:
    """What a missing table means: a node that did not build, or one nothing builds.

    Only a relation the project builds or declares (a model, seed, snapshot or source)
    can have "failed or been skipped upstream". Anything else is read from outside dbt,
    usually a table hard-coded in the SQL, which no branch of this project will create.
    """
    shown = name.strip('"')
    parts = [part.strip('"').lower() for part in shown.split(".") if part]
    known = relations is None or (
        any(parts[-1] == table for _, table in relations)
        if len(parts) == 1
        else (parts[-2], parts[-1]) in relations
    )
    if known:
        return f"`{shown}` was not built, it failed or was skipped upstream"
    return (
        f"reads `{shown}`, which no model, seed or source in this project builds "
        "(a hard-coded table?)"
    )


def _human_reading(message: str, relations: set[tuple[str, str]] | None = None) -> str | None:
    """A one-line plain-English reading of a DuckDB error, or None for a shape not covered.

    `relations` (the project's own, see `Manifest.relations`) tells a model that did not
    build from a table nothing builds; without it a missing table reads as the former.
    """
    for pattern, template in _ERROR_READINGS[:2]:
        m = pattern.search(message or "")
        if m:
            return template.format(*m.groups())
    missing = _MISSING_TABLE.search(message or "")
    if missing:
        return _missing_table_reading(missing.group(1), relations)
    for pattern, template in _ERROR_READINGS[2:]:
        m = pattern.search(message or "")
        if m:
            return template.format(*m.groups())
    return None


def _target_name(to_kwarg: str) -> str | None:
    """The model or table name a `to` kwarg points at: ref('x') -> x, source('a', 'b') -> b."""
    names = re.findall(r"""['"]([^'"]+)['"]""", to_kwarg or "")
    return names[-1] if names else None


def _generic_test_label(t: FailedTest) -> str:
    """A generic test's own name and target, rather than dbt's generated test name.

    `unique` on `stg_webshop__customers.customer_id`; for `relationships`, both sides of
    the join, parsed from the `to` and `field` kwargs: `relationships` `stg_webshop__orders.
    customer_id` -> `stg_webshop__customers.customer_id`.
    """
    target = f"{t.model}.{t.column_name}" if t.column_name else t.model
    if t.test_name == "relationships":
        to_name = _target_name(str(t.kwargs.get("to", "")))
        to_field = t.kwargs.get("field")
        if to_name and to_field:
            return f"relationships `{target}` → `{to_name}.{to_field}`"
        return f"`relationships` on `{target}`"
    return f"`{t.test_name}` on `{target}`"


def _test_label(t: FailedTest) -> str:
    """How a test is introduced in its bullet: a human label, or its own name."""
    if t.kind == "unit_test":
        return f"unit test `{t.name}` on `{t.model}`"
    if t.test_name:
        return _generic_test_label(t)
    return f"`{t.name}` on `{t.model}`"


def _test_detail(t: FailedTest) -> str:
    """What went wrong, in a few words: rows failing, the error, or the unit test's verdict."""
    if t.kind == "unit_test":
        return "actual output differs from the expected rows"
    if t.status == "error":
        return _one_line_error(t.message)
    if t.failures is not None:
        detail = f"{t.failures} failing {'row' if t.failures == 1 else 'rows'}"
        if t.base_failures is not None and t.base_failures != t.failures:
            detail += f" ({t.base_failures} on the base branch)"
        return detail
    return t.status


def _test_entry_lines(
    t: FailedTest, details: bool = True, relations: set[tuple[str, str]] | None = None
) -> list[str]:
    """The bullet for one failing or warning test, with its details block where there is one."""
    if t.preexisting:
        icon = "⚪"
    elif t.status in {"fail", "error"}:
        icon = "❌"
    else:
        icon = "⚠️"
    line = f"- {icon} {_test_label(t)}: {_test_detail(t)}"
    if t.guessed_inputs and t.status in {"fail", "error"}:
        cols = ", ".join(f"`{c}`" for c in t.guessed_inputs[:_GUESSED_SHOWN])
        more = len(t.guessed_inputs) - _GUESSED_SHOWN
        cols += f" and {more} more" if more > 0 else ""
        if t.preexisting:
            line += (
                f". It reads {cols}, whose type preflight guessed (see Fixtures), so the "
                "failure may be the guess rather than the project"
            )
        elif t.status == "error":
            line += (
                f". It reads {cols}, whose type preflight guessed (see Fixtures), so it "
                "could not be checked against the base branch: the error may be the guess "
                "rather than the project"
            )
        else:
            line += f". It reads {cols}, whose type preflight guessed (see Fixtures)"
    lines = [line]
    if not details:
        return lines
    if t.compiled_code or t.status == "error" or t.kind == "unit_test" or t.test_name:
        lines.append("  <details><summary>details</summary>")
        lines.append("")
        if t.test_name:
            # dbt's own generated name, kept for anyone searching logs or `dbt test -s`.
            lines.append(f"  dbt test name: `{t.name}`")
            lines.append("")
        if t.status == "error" and t.message.strip():
            reading = _human_reading(t.message, relations)
            if reading:
                lines.append(f"  {reading}")
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
    return lines


def _build_error_lines(m: ModelReport, relations: set[tuple[str, str]] | None = None) -> list[str]:
    """One model's build error: its path, a plain-English reading when there is one, then
    the raw message."""
    lines = [f"**`{m.name}`** — {m.path}", ""]
    reading = _human_reading(m.message, relations)
    if reading:
        lines.append(reading)
        lines.append("")
    lines.append("```")
    lines.append(m.message.strip()[:1500])
    lines.append("```")
    lines.append("")
    return lines


def _row_pct(base: int | None, head: int | None) -> str | None:
    """Row count change as a percentage of the base count, or None when it cannot be computed."""
    if base is None or head is None or base == 0:
        return None
    pct = (head - base) / base * 100
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"


def _share_pct(part: int | None, total: int | None) -> str | None:
    """`part` as a whole-number percentage of `total`, or None when it cannot be computed."""
    if part is None or total is None or total == 0:
        return None
    return f"{round(part / total * 100)}%"


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

    headline_text = headline_line(report)
    if headline_text:
        lines.append(headline_text)
        lines.append("")

    if report.nothing_changed:
        lines.append("No models changed against the base branch, so there was nothing to build.")
        lines.append("")
        lines.append(_scope_block())
        return "\n".join(lines)

    built = sum(1 for m in report.models if m.status == BUILT)
    changed = len(report.changed)
    tests_total = sum(_tests_total(m) for m in report.models)
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
    if report.shared_snapshots:
        names = ", ".join(f"`{n}`" for n in report.shared_snapshots)
        lines.append(
            f"{names} {'writes' if len(report.shared_snapshots) == 1 else 'write'} to a fixed "
            "`target_schema` that both branches would share, so "
            f"{'it was' if len(report.shared_snapshots) == 1 else 'they were'} built for this "
            "pull request only: the models reading them have no base branch to be compared "
            "with, and every failing test on them counts."
        )
        lines.append("")

    lines += _broken_on_base_section(report)
    lines += _fixture_limited_section(report)
    lines += _skipped_unchecked_lines(report)

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
        if m.broken_on_base:
            build_cell = f"⚠️ fails on {_base_name(report)} too"
        elif m.fixture_limited:
            build_cell = "⚠️ preflight's data cannot build it"
        elif m.unverified_broken_on_base:
            build_cell = "❓ could not be checked"
        elif m.skipped_by_base:
            build_cell += f" (broken on {_base_name(report)} upstream)"
        elif m.skipped_by_fixture_limited:
            build_cell += " (preflight's data cannot build what it reads)"
        if m.status == NOT_VERIFIED and m.dialect_function:
            build_cell += f" (`{m.dialect_function}`)"
        lines.append(f"| `{m.name}` | {build_cell} | {rows_cell} | {_tests_cell(m)} |")
    lines.append("")

    lines += _unverified_section(report)

    # Models broken on the base branch, and what they skip, are in the section above.
    around = [
        m
        for m in report.models
        if not m.changed
        and not m.not_this_change
        and not m.unverified_broken_on_base
        and not m.skipped_by_unverified
        and not m.skipped_unchecked
    ]
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
            shown, extra = names[:5], len(names) - 5
            tail = f" and {extra} more" if extra > 0 else ""
            lines.append(f"Also rebuilt, no new issues: {', '.join(shown)}{tail}.")
            lines.append("")

    if report.build_error_models:
        lines.append("### Build errors")
        lines.append("")
        for i, m in enumerate(report.build_error_models):
            entry = _build_error_lines(m, report.relations)
            if i == 0:
                lines += entry
            else:
                # Keep the first error visible; the rest fold, one model per details block.
                lines.append(f"<details><summary>`{m.name}`</summary>")
                lines.append("")
                lines += entry
                lines.append("</details>")
                lines.append("")

    if report.failing_tests or report.warning_tests:
        lines.append("### Failing tests")
        lines.append("")
        all_tests = report.failing_tests + report.warning_tests
        shown_tests, rest_tests = all_tests[:3], all_tests[3:]
        for t in shown_tests:
            lines += _test_entry_lines(t, relations=report.relations)
        if rest_tests:
            lines.append(f"<details><summary>{len(rest_tests)} more failing tests</summary>")
            lines.append("")
            for t in rest_tests:
                lines += _test_entry_lines(t, relations=report.relations)
            lines.append("</details>")
        lines.append("")

    if report.preexisting_tests:
        lines += _preexisting_section(report.preexisting_tests, report.relations)

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

    pointer = _schema_pointer(report)
    if pointer:
        lines += [pointer, ""]
    fixtures = _fixtures_block(report)
    if fixtures:
        lines.append(fixtures)
        lines.append("")
    lines.append(_scope_block())
    return "\n".join(lines)


def broken_on_base_error(m: ModelReport) -> str:
    """The error of a model broken on the base too, as DuckDB said it.

    Not the plain-English reading the build errors get: "`x` was not built upstream" is
    the likely story for a pull request's own failure, but a model broken on the base
    too is as often reading a relation that no branch builds, such as a hard-coded
    `finance.account_daily_arr`, and the raw line says so.
    """
    return _one_line_error(m.message)


def _unverified_section(report: PreflightReport) -> list[str]:
    """Models that fail on the base too, but that the change reaches: counted, unchecked.

    Not "broken by this change", which is not established, and not "broken on main too",
    which would let a new error hide behind the old one: DuckDB reports only the first.
    """
    unverified = report.unverified_broken_models
    if not unverified:
        return []
    base = _base_name(report)
    one = len(unverified) == 1
    lines = [
        f"### ❓ Could not be checked ({len(unverified)})",
        "",
        f"{'This model fails' if one else 'These models fail'} on `{base}` too, the same way, "
        f"but for the reason given {'it still counts' if one else 'each still counts'} "
        "against this pull request.",
        "",
    ]
    skipped = [m for m in report.models if m.skipped_by_unverified]
    for m in unverified:
        lines.append(f"- `{m.name}` — {_unverified_reason(m)}: {broken_on_base_error(m)}")
    if skipped:
        names = [f"`{m.name}`" for m in skipped]
        shown, rest = names[:_SKIPPED_BY_BASE_SHOWN], names[_SKIPPED_BY_BASE_SHOWN:]
        lines += [
            "",
            f"Skipped because of {'it' if len(unverified) == 1 else 'them'}: {', '.join(shown)}.",
        ]
        if rest:
            lines += [
                f"<details><summary>{len(rest)} more skipped</summary>",
                "",
                ", ".join(rest) + ".",
                "</details>",
            ]
    lines.append("")
    return lines


_GUESSED_SHOWN = 5  # guessed columns named per model before "and N more"


def _guessed_list(m: ModelReport) -> str:
    cols = ", ".join(f"`{c}`" for c in m.guessed_inputs[:_GUESSED_SHOWN])
    more = len(m.guessed_inputs) - _GUESSED_SHOWN
    return cols + (f" and {more} more" if more > 0 else "")


def _unverified_reason(m: ModelReport) -> str:
    """Why one model failing the same way on the base still counts, in its own words."""
    via = ", ".join(f"`{n}`" for n in m.reached_from) or "upstream"
    if m.edited_tests and not m.reached_from:
        via = "a test on it: " + ", ".join(f"`{n}`" for n in m.edited_tests)
    if m.fixture_error:
        return (
            f"fails on a value preflight generated ({m.fixture_error}), and this change "
            f"reaches it ({via}), so a new error could be hiding behind that one"
        )
    if m.guessed_inputs:
        return (
            f"reads {_guessed_list(m)}, whose type preflight guessed (see Fixtures), so the "
            "failure may be the guess rather than the project, and this change reaches it "
            f"({via})"
        )
    if m.edited_tests and not m.reached_from:
        return (
            f"this change adds or edits {via}, which needs the model built, so it cannot be "
            "excused as broken on the base"
        )
    return (
        f"this change reaches it from upstream ({via}), and DuckDB reports only the first "
        "error in a statement, so a new one could be hiding behind the old one"
    )


def fixture_limited_reason(m: ModelReport) -> str:
    """`malformed JSON`, or "reads `x.y`, whose type preflight guessed"."""
    if m.fixture_error:
        return m.fixture_error
    return f"reads {_guessed_list(m)}, whose type preflight guessed"


def _skipped_unchecked_lines(report: PreflightReport) -> list[str]:
    """Models skipped behind one of the above that this change reaches anyway: counted."""
    names = [f"`{m.name}`" for m in report.models if m.skipped_unchecked]
    if not names:
        return []
    shown, rest = names[:_SKIPPED_BY_BASE_SHOWN], names[_SKIPPED_BY_BASE_SHOWN:]
    one = len(names) == 1
    lines = [
        f"Also skipped behind {'it' if one else 'those'} on both branches, and counted against "
        "this pull request because the change reaches "
        f"{'it' if one else 'them'} through another model, so "
        f"{'it was' if one else 'they were'} never checked: {', '.join(shown)}."
    ]
    if rest:
        lines += [
            f"<details><summary>{len(rest)} more</summary>",
            "",
            ", ".join(rest) + ".",
            "</details>",
        ]
    return [*lines, ""]


def _fixture_limited_section(report: PreflightReport) -> list[str]:
    """Models the change cannot affect that preflight's data cannot build: a warning.

    Next to "Broken on main too", because it is the same kind of news: these were not
    checked, and the change did not do it. Unlike that section, main is not to blame
    either: the failure is in the data preflight generated."""
    limited = report.fixture_limited_models
    if not limited:
        return []
    one = len(limited) == 1
    lines = [
        f"### ⚠️ Preflight's generated data cannot build {'this model' if one else 'these models'}"
        f" ({len(limited)})",
        "",
        f"{'It fails' if one else 'They fail'} the same way on `{_base_name(report)}`, on "
        "values preflight generated, and this change does not reach "
        f"{'it' if one else 'them'}, so it does not fail this check. Typing the columns "
        "involved, in `sources.yml` or a schema file (`dbt-preflight schema`), usually fixes "
        "it.",
        "",
    ]
    for m in limited:
        lines.append(f"- `{m.name}` — {fixture_limited_reason(m)}: {broken_on_base_error(m)}")
    lines.append("")
    skipped = [f"`{m.name}`" for m in report.models if m.skipped_by_fixture_limited]
    if skipped:
        shown, rest = skipped[:_SKIPPED_BY_BASE_SHOWN], skipped[_SKIPPED_BY_BASE_SHOWN:]
        lines.append(f"Skipped because of {'it' if one else 'them'}: {', '.join(shown)}.")
        if rest:
            lines += [
                f"<details><summary>{len(rest)} more skipped</summary>",
                "",
                ", ".join(rest) + ".",
                "</details>",
            ]
        lines.append("")
    return lines


def _base_name(report: PreflightReport) -> str:
    """The base ref as a reader says it: `origin/main` is `main`, a full SHA its first 7."""
    ref = report.base_ref or "the base branch"
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
        return ref[:7]
    return ref


# How many models skipped because of a broken-on-base model are named before the rest fold.
_SKIPPED_BY_BASE_SHOWN = 5


def _broken_on_base_section(report: PreflightReport) -> list[str]:
    """What is already broken on the base branch, near the top and not folded.

    It does not fail the check, but it is the first thing a reviewer should know: those
    models, and everything downstream of them, were not checked at all.
    """
    broken = report.broken_on_base_models
    tests = report.preexisting_tests
    if not broken and not tests:
        return []
    base = _base_name(report)
    test_line = (
        f"{len(tests)} {'test' if len(tests) == 1 else 'tests'} already "
        f"{'fails' if len(tests) == 1 else 'fail'} on `{base}` (details below)."
    )
    if not broken:
        return [f"### ⚠️ Broken on {base} too", "", test_line, ""]
    lines = [
        f"### ⚠️ Broken on {base} too ({len(broken)})",
        "",
        f"These models also fail on `{base}`, without this change:",
        "",
    ]
    for m in broken:
        lines.append(f"- `{m.name}` — {broken_on_base_error(m)}")
    lines.append("")
    skipped = [f"`{m.name}`" for m in report.skipped_by_base_models]
    if skipped:
        shown, rest = skipped[:_SKIPPED_BY_BASE_SHOWN], skipped[_SKIPPED_BY_BASE_SHOWN:]
        lines.append(
            f"Skipped because of {'it' if len(broken) == 1 else 'them'}: {', '.join(shown)}."
        )
        if rest:
            lines.append(f"<details><summary>{len(rest)} more skipped</summary>")
            lines.append("")
            lines.append(", ".join(rest) + ".")
            lines.append("</details>")
        lines.append("")
    if tests:
        lines += [f"And {test_line}", ""]
    return lines


def _preexisting_section(
    tests: list[FailedTest], relations: set[tuple[str, str]] | None = None
) -> list[str]:
    """Tests that fail on the base branch too, folded: reported, but not this change's doing."""
    lines = [
        f"<details><summary>Already failing on the base branch ({len(tests)})</summary>",
        "",
        "These tests fail on the base branch as well, built on the same synthetic data, with "
        "as many failing rows or more, so they do not fail this check and did not stop "
        "anything downstream from building. Often the fixtures cannot satisfy them; a test "
        "that fails here on every pull request is worth a look on its own.",
        "",
    ]
    for i, t in enumerate(tests):
        lines += _test_entry_lines(t, details=i < _PREEXISTING_DETAILED, relations=relations)
    lines += ["</details>", ""]
    return lines


def _num(value: float | int | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    v = float(value)
    return f"{v:,.4f}" if abs(v) < 1 else f"{v:,.2f}"


def _pct_delta(base: float | int | None, head: float | int | None) -> str:
    if base is None or head is None:
        return "–"
    base, head = float(base), float(head)
    diff = head - base
    if base == 0:
        return f"{'+' if diff > 0 else ''}{_num(diff)}"
    pct = diff / abs(base) * 100
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"


def _delta(m: MetricDiff) -> str:
    return _pct_delta(m.base, m.head)


# The folder under models/ the headline counts as "marts". Preflight does not carry the
# conventions into the report, so the layer is read from the model's path.
_MART_FOLDER = "marts"


def _is_mart(m: ModelReport) -> bool:
    return _MART_FOLDER in m.path.replace("\\", "/").split("/")[:-1]


def _metric_size(m: MetricDiff) -> tuple[float, str, str]:
    """Sort key for the biggest mover: absolute relative change; a metric whose base is zero
    or missing is as big as it gets. Ties break on label, then name, so the pick is stable."""
    if m.base is None or m.head is None or float(m.base) == 0:
        size = float("inf")
    else:
        size = abs(float(m.head) - float(m.base)) / abs(float(m.base))
    return (size, m.label, m.name)


def _top_metric(report: PreflightReport) -> MetricDiff | None:
    moved = [m for d in report.diffs for m in d.moved_metrics]
    if not moved:
        return None
    # Largest size first, then alphabetical label and name.
    return min(moved, key=lambda m: (-_metric_size(m)[0], _metric_size(m)[1], _metric_size(m)[2]))


def headline(report: PreflightReport) -> dict[str, Any] | None:
    """The one-line answer to "what did this pull request do", as data.

    None when there is nothing to say: preflight could not run, or nothing changed. Without
    a base branch there is no change to measure, so only the failures and what could not be
    checked are filled in; `moved_metrics`, `top_metric`, `touched_models` and
    `value_changed_models` are None then.
    """
    if report.fatal or report.nothing_changed:
        return None
    compared = report.base_ref is not None
    top = _top_metric(report) if compared else None
    value_changed = (
        sum(1 for d in report.diffs if d.base_exists and (d.rows_changed or d.rows_differing))
        if compared
        else None
    )
    data: dict[str, Any] = {
        # Failing models and new failing tests, plus convention errors, which fail the run
        # as surely: "no new failures" under a red verdict would mislead. Models that could
        # not be checked are counted apart, in `unverified`.
        "new_failures": len(report.build_error_models)
        + len(report.failing_tests)
        + len(report.error_violations),
        "moved_metrics": sum(len(d.moved_metrics) for d in report.diffs) if compared else None,
        "metrics_defined": report.metrics_defined,
        "top_metric": (
            {"name": top.name, "label": top.label, "base": top.base, "head": top.head}
            if top
            else None
        ),
        "value_changed_models": value_changed,
        "touched_models": len(report.models) if compared else None,
        "touched_marts": sum(1 for m in report.models if _is_mart(m)) if compared else None,
        "unverified": len(report.unverified_broken_models) + len(report.unverified_models),
        "broken_on_base": len(report.broken_on_base_models),
        # The change does not reach these, and preflight's generated data cannot build
        # them: a warning, like broken on the base, not a failure.
        "fixture_limited": len(report.fixture_limited_models),
        # Models the change reaches that were skipped without a failure of its own above
        # them (behind a model broken on the base): they fail the run too.
        "not_built": len([m for m in report.unbuilt_models if not m.skipped_by_unverified])
        if not report.build_error_models and not report.failing_tests
        else 0,
    }
    data["text"] = _headline_text(report, data, top)
    return data


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n:,} {singular if n == 1 else plural or singular + 's'}"


def _headline_text(report: PreflightReport, h: dict[str, Any], top: MetricDiff | None) -> str:
    failures = h["new_failures"]
    parts = [_count(failures, "new failure") if failures else "No new failures"]
    if h["not_built"]:
        parts[0] += f", but {_count(h['not_built'], 'model')} not built"
    moved = h["moved_metrics"]
    if moved:
        detail = ""
        if top is not None:
            delta = _delta(top)
            detail = f" ({top.label}{'' if delta == '–' else f' {delta}'})"
        parts.append(f"moves {_count(moved, 'metric')}{detail}")
    elif moved == 0 and h["metrics_defined"]:
        parts.append("moves no metrics")
    elif h["value_changed_models"]:
        parts.append(f"changes values in {_count(h['value_changed_models'], 'model')}")
    if h["touched_models"] is not None:
        if h["touched_marts"]:
            parts.append(f"touches {_count(h['touched_marts'], 'mart')}")
        else:
            parts.append(f"touches {_count(h['touched_models'], 'model')}")
    if h["unverified"]:
        parts.append(f"{h['unverified']:,} could not be checked")
    if h["broken_on_base"]:
        parts.append(f"{h['broken_on_base']:,} broken on {_base_name(report)}")
    if h["fixture_limited"]:
        parts.append(f"{h['fixture_limited']:,} cannot be built on generated data")
    return " · ".join(parts)


def headline_line(report: PreflightReport) -> str | None:
    h = headline(report)
    return h["text"] if h else None


def _metric_label(m: MetricDiff) -> str:
    """The metric's label, naming the models it reads when it reads more than one, so a
    ratio of orders to customers is not mistaken for a metric of the model it is listed
    under."""
    if not m.spans:
        return m.label
    return f"{m.label} (across {', '.join(f'`{n}`' for n in m.spans)})"


def _breakdown_line(m: MetricDiff, dimension: str, rows: list[tuple[str, Any, Any]]) -> str:
    """One moved metric broken down by one dimension: the rows whose contribution to the
    move was largest, e.g. `Net revenue (EUR) by sales_channel: web 5,210 → 4,980 (-4.4%),
    mobile_app 3,000 → 3,200 (+6.7%)`."""
    cells = ", ".join(
        f"{value} {_num(base)} → {_num(head)} ({_pct_delta(base, head)})"
        for value, base, head in rows
    )
    return f"{m.label} by {dimension}: {cells}"


def _diff_section(report: PreflightReport) -> list[str]:
    lines = ["### What changed in the output", ""]
    lines.append(
        "Base branch and pull request, built on the same synthetic data. A difference here was "
        "caused by this change and nothing else."
    )
    if any(d.breaking for d in report.diffs):
        lines.append(
            "⚠️ marks a column that was removed, renamed or retyped: dashboards and ad-hoc SQL "
            "select columns by name, so this can break consumers preflight cannot see."
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
            pct = _row_pct(d.rows_base, d.rows_head)
            suffix = f" ({pct})" if pct else ""
            bits.append(f"rows {_num(d.rows_base)} → {_num(d.rows_head)}{suffix}")
        elif d.rows_head is not None:
            bits.append(f"rows {_num(d.rows_head)} (unchanged)")
        if d.rows_differing:
            share = _share_pct(d.rows_differing, d.rows_head)
            note = [share] if share else []
            if d.rows_differing_common_columns is not None:
                note.append(f"on the {d.rows_differing_common_columns} columns both sides share")
            suffix = f" ({', '.join(note)})" if note else ""
            bits.append(
                f"{_num(d.rows_differing)} {'row' if d.rows_differing == 1 else 'rows'} "
                f"with different values{suffix}"
            )
        cols: list[str] = []
        for c, t in d.columns_added:
            profile = d.profiles.get(c)
            cols.append(f"+`{c}` ({t}, {profile.describe()})" if profile else f"+`{c}` ({t})")
        cols += [f"`{old}` → `{new}` (renamed, same values) ⚠️" for old, new in d.columns_renamed]
        cols += [f"−`{c}` ⚠️" for c, _ in d.columns_removed]
        cols += [f"`{c}` {old} → {new} ⚠️" for c, old, new in d.columns_retyped]
        if cols:
            bits.append("columns: " + ", ".join(cols))
        lines.append(f"**`{d.name}`** — " + " · ".join(bits))
        lines.append("")
        for column, refs in d.references.items():
            if refs:
                lines.append(
                    f"`{column}` was referenced on the base branch by {', '.join(refs)}; "
                    "each of those needs the new name or the column back."
                )
            else:
                lines.append(
                    f"`{column}` had no reference on the base branch: no YAML entry, metric, "
                    "semantic-layer expression, test or downstream model. Only consumers "
                    "outside the repository can still break."
                )
            lines.append("")

        moved = d.moved_metrics
        if moved:
            lines.append("| Metric | Base | PR | Δ |")
            lines.append("| --- | ---: | ---: | ---: |")
            for m in moved:
                lines.append(
                    f"| {_metric_label(m)} | {_num(m.base)} | {_num(m.head)} | {_delta(m)} |"
                )
            lines.append("")
            # One bullet per (metric, dimension) adds up fast on a fact table where every
            # metric moves at once: the first few metrics' breakdowns stay visible, the
            # rest fold, the same shape the failing-tests budget uses.
            shown, rest = moved[:_BREAKDOWN_METRICS_SHOWN], moved[_BREAKDOWN_METRICS_SHOWN:]
            for m in shown:
                for dimension, rows in m.breakdown.items():
                    lines.append(f"- {_breakdown_line(m, dimension, rows)}")
            if any(m.breakdown for m in shown):
                lines.append("")
            folded = [m for m in rest if m.breakdown]
            if folded:
                noun = "metric" if len(folded) == 1 else "metrics"
                lines.append(
                    f"<details><summary>Breakdowns for {len(folded)} more moved {noun}</summary>"
                )
                lines.append("")
                for m in folded:
                    for dimension, rows in m.breakdown.items():
                        lines.append(f"- {_breakdown_line(m, dimension, rows)}")
                lines.append("</details>")
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
                    else f" ({', '.join(_metric_label(m) for m in steady[:6])}{', …' if len(steady) > 6 else ''})"
                )
            )
        if skipped:
            notes.append(
                "not evaluated: "
                + ", ".join(f"{_metric_label(m)} ({m.unsupported})" for m in skipped[:4])
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


STUDIO_URL = "https://studio.jbanalytica.com/?ref=dbt-preflight"


def _schema_pointer(report: PreflightReport) -> str:
    """One line, only when preflight had to guess a type: where to keep and refine the schema.

    Never on a clean run, and never with schema content in the URL.
    """
    fx = report.fixtures
    guessed = fx.guessed_sources if fx is not None else 0
    if not guessed:
        return ""
    noun = "source" if guessed == 1 else "sources"
    return (
        f"Columns were guessed for {guessed} {noun}. Keep and refine the schema with "
        f"`dbt-preflight schema`, then edit it in [model2data studio]({STUDIO_URL})."
    )


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
    if fx.skipped_sources:
        parts += [
            "",
            "Read by no model, snapshot or test, so no fixture: "
            + ", ".join(f"`{s}`" for s in fx.skipped_sources),
        ]
    if fx.json_columns:
        parts += [
            "",
            "Filled with JSON, with the keys the models read, because a model parses them as "
            "JSON: " + ", ".join(f"`{c}`" for c in fx.json_columns),
        ]
    if fx.json_new_keys:
        parts += [
            "",
            "JSON keys only this pull request reads, null in the fixture as in data that "
            "lacks them (a renamed key reads NULL here): "
            + ", ".join(f"`{k}`" for k in fx.json_new_keys),
        ]
    if fx.json_keys_partly_compared and fx.json_columns:
        parts += [
            "",
            "Only one branch's models compiled, so JSON keys were compared with the base in "
            "raw SQL alone: a key renamed inside a macro gets values on both sides and is not "
            "caught here.",
        ]
    if fx.inferred_sources:
        parts += ["", "Columns inferred from the staging models that read them:"]
        for src in fx.inferred_sources:
            if src.models:
                line = f"- `{src.identifier}`: {src.total_columns} columns inferred from " + (
                    ", ".join(f"`{m}`" for m in src.models)
                )
            else:
                line = f"- `{src.identifier}`: {src.total_columns} columns, read by no model"
            named = [c for c in src.guessed_columns if c not in src.unknown_columns]
            if named:
                line += ", types guessed for " + ", ".join(f"`{c}`" for c in named)
            if src.unknown_columns:
                line += (
                    "; typed varchar because a model reading it could not be followed, so "
                    "nothing says how these are used: "
                    + ", ".join(f"`{c}`" for c in src.unknown_columns)
                )
            parts.append(line)
        from_compiled = [s for s in fx.inferred_sources if s.compiled_columns]
        if from_compiled:
            n_columns = sum(len(s.compiled_columns) for s in from_compiled)
            parts += [
                "",
                f"{n_columns:,} columns for {len(from_compiled):,} sources read from compiled SQL.",
            ]
        conflicts = [f"`{s.identifier}`.{c}" for s in fx.inferred_sources for c in s.type_conflicts]
        if conflicts:
            parts += [
                "",
                "Typed differently by the compiled SQL, raw SQL's type kept: "
                + "; ".join(conflicts),
            ]
    if fx.unmapped_columns:
        parts += [
            "",
            "Filled with placeholder text, no realistic data for these: "
            + ", ".join(f"`{c}` ({t})" for c, t in fx.unmapped_columns),
        ]
    if fx.cyclic_tables:
        parts += [
            "",
            "Stuck in an unresolved foreign-key cycle, so not every relationship in their "
            "data is real: " + ", ".join(f"`{t}`" for t in fx.cyclic_tables),
        ]
    if fx.unresolved_composite_keys:
        parts += [
            "",
            "Composite keys left with duplicate combinations: "
            + ", ".join(f"`{k}`" for k in fx.unresolved_composite_keys),
        ]
    if fx.parse_warnings:
        parts += [
            "",
            "DBML lines the parser could not understand and skipped: "
            + "; ".join(fx.parse_warnings),
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
            "synthetic dataset; their tests pass, or fail no worse than on the base branch; "
            "the change follows the house conventions.",
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
