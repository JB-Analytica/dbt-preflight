"""Judging a failing test against the base branch.

A pull request is only answerable for what it changed. The base branch is built on the
same fixtures and runs the same tests, so a test that already fails there is a fact about
the project (often: synthetic data that can never satisfy it), not about the change. This
module holds the one rule that decides which is which, so the build, the comment and the
summary cannot apply it differently.
"""

from __future__ import annotations

from dbt_preflight.dbt_runner import NodeResult

FAILING = frozenset({"fail", "error"})


def _first_error_line(message: str, base_schema: str, head_schema: str) -> str:
    """The error's first meaningful line, with the base target's schema read as the head's.

    The two sides build into different schemas (`preflight_base_*` versus `preflight_*`),
    so an otherwise identical DuckDB error names a different relation on each side.
    """
    for line in (message or "").splitlines():
        line = line.strip()
        if line and not line.startswith(("Runtime Error", "Compilation Error", "Database Error")):
            return line.replace(base_schema, head_schema)
    return ""


def is_preexisting(
    head: NodeResult,
    base: NodeResult | None,
    base_schema: str = "preflight_base",
    head_schema: str = "preflight",
) -> bool:
    """Whether a failing head test failed the same way, or worse, on the base branch.

    Tests are matched by dbt's unique id, which for a generic test encodes its arguments:
    a test that is new on head, or whose definition changed, has no base result and so
    always counts against the pull request. Otherwise:

    - failed on both: pre-existing unless the head returns *more* failing rows. The same
      test returning more rows is the change making things worse, and counts.
    - errored on both, with the same error: pre-existing. A different error is a new one.
    - failed on base and errored on head, or the reverse: counts. An error where rows came
      back before means the change broke the test's SQL; rows where the base could not
      even evaluate the test mean the base result tells us nothing.
    """
    if head.status not in FAILING or base is None or base.status not in FAILING:
        return False
    if head.status != base.status:
        return False
    if head.status == "fail":
        if head.failures is None or base.failures is None:
            return True
        return head.failures <= base.failures
    return _first_error_line(head.message, base_schema, head_schema) == _first_error_line(
        base.message, base_schema, head_schema
    )
