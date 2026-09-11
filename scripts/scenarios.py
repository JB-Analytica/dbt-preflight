"""Day 7 of the spike: break the example on purpose and keep the comments.

Runs four pull-request shapes against the bundled webshop project, each on its own branch
of a throwaway git repository, and writes the comment preflight produced to
docs/scenarios/. Four must fail for the stated reason; two must pass, one of them with a visible diff.

    uv run python scripts/scenarios.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "webshop"
OUT = ROOT / "docs" / "scenarios"
PREFLIGHT = [sys.executable, "-m", "dbt_preflight.cli"]


@dataclass
class Scenario:
    name: str
    title: str
    must_pass: bool
    expect: list[str]

    def apply(self, repo: Path) -> None:
        raise NotImplementedError


class RenameSourceColumn(Scenario):
    """Rename a column in the source schema without touching the staging model."""

    def apply(self, repo: Path) -> None:
        dbml = repo / "webshop.dbml"
        text = dbml.read_text()
        assert "  email varchar [not null, unique]" in text
        dbml.write_text(
            text.replace(
                "  email varchar [not null, unique]", "  email_address varchar [not null, unique]"
            )
        )


class DropRef(Scenario):
    """Delete the intermediate model two marts depend on."""

    def apply(self, repo: Path) -> None:
        (repo / "dbt/models/intermediate/int_orders__items_aggregated.sql").unlink()


class BreakJoin(Scenario):
    """Shift the foreign key in staging so the relationship test has no parent rows."""

    def apply(self, repo: Path) -> None:
        model = repo / "dbt/models/staging/webshop/stg_webshop__orders.sql"
        text = model.read_text()
        assert "        customer_id,\n" in text
        model.write_text(
            text.replace(
                "        customer_id,\n", "        customer_id + 100000 as customer_id,\n", 1
            )
        )


class UnitTestCatch(Scenario):
    """Apply the line discount per unit instead of per line. A dbt unit test covers it."""

    def apply(self, repo: Path) -> None:
        model = repo / "dbt/models/intermediate/int_orders__items_aggregated.sql"
        text = model.read_text()
        old = "sum(quantity * unit_price_cents - discount_cents) as net_amount_cents"
        assert old in text
        model.write_text(
            text.replace(
                old, "sum(quantity * (unit_price_cents - discount_cents)) as net_amount_cents"
            )
        )


class SilentLogicChange(Scenario):
    """Count cancelled orders in customer lifetime revenue. No test covers it."""

    def apply(self, repo: Path) -> None:
        model = repo / "dbt/models/marts/dim_customers.sql"
        text = model.read_text()
        old = "    where order_status != 'cancelled'\n"
        assert old in text
        model.write_text(text.replace(old, ""))


class HarmlessRefactor(Scenario):
    """Reformat a staging model. Nothing about its output changes."""

    def apply(self, repo: Path) -> None:
        model = repo / "dbt/models/staging/webshop/stg_webshop__products.sql"
        text = model.read_text()
        assert "select * from renamed" in text
        model.write_text(text.replace("select * from renamed", "select *\nfrom renamed"))


SCENARIOS: list[Scenario] = [
    RenameSourceColumn(
        "rename-source-column",
        "Rename a source column without updating staging",
        must_pass=False,
        expect=["❌ failed", "`stg_webshop__customers`", "email"],
    ),
    DropRef(
        "drop-ref",
        "Delete a model that two marts `ref()`",
        must_pass=False,
        expect=["could not run", "int_orders__items_aggregated"],
    ),
    BreakJoin(
        "break-join",
        "Break a foreign key so a relationships test fails",
        must_pass=False,
        expect=["❌ failed", "relationships_stg_webshop__orders_customer_id", "failing rows"],
    ),
    UnitTestCatch(
        "unit-test-catch",
        "Change discount arithmetic a unit test covers",
        must_pass=False,
        expect=["❌ failed", "unit test `int_orders__items_aggregated_sums_lines_per_order`"],
    ),
    SilentLogicChange(
        "silent-logic-change",
        "Count cancelled orders in lifetime revenue, with no test on it",
        must_pass=True,
        expect=[
            "✅ passed",
            "What changed in the output",
            "rows with different values",
            "`dim_customers`",
        ],
    ),
    HarmlessRefactor(
        "harmless-refactor",
        "Reformat a staging model",
        must_pass=True,
        expect=["✅ passed", "`stg_webshop__products`"],
    ),
]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="preflight-scenarios-"))
    repo = tmp / "webshop"
    shutil.copytree(EXAMPLE, repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=preflight@example.com", "-c", "user.name=preflight", "add", ".")
    git(
        repo,
        "-c",
        "user.email=preflight@example.com",
        "-c",
        "user.name=preflight",
        "commit",
        "-q",
        "-m",
        "base",
    )

    failures = 0
    index_lines = [
        "# Scenarios",
        "",
        "Six pull-request shapes run against `examples/webshop`, recorded by "
        "`scripts/scenarios.py`. Four must fail for the stated reason; two must pass, one of "
        "them with every test green and the numbers moved.",
        "",
        "| Scenario | Expected | Result |",
        "| --- | --- | --- |",
    ]
    for s in SCENARIOS:
        git(repo, "checkout", "-q", "-b", s.name, "main")
        s.apply(repo)
        comment = tmp / f"{s.name}.md"
        result = subprocess.run(
            [
                *PREFLIGHT,
                "run",
                "--base-ref",
                "main",
                "--repo-root",
                str(repo),
                "--config",
                str(repo / ".dbt-preflight.yml"),
                "--comment-file",
                str(comment),
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        body = comment.read_text() if comment.exists() else result.stderr
        passed = result.returncode == 0
        missing = [e for e in s.expect if e not in body]
        ok = passed == s.must_pass and not missing
        failures += 0 if ok else 1
        verdict = (
            "as expected" if ok else f"UNEXPECTED (exit {result.returncode}, missing {missing})"
        )
        print(f"{'✅' if ok else '❌'} {s.name}: {verdict}")
        (OUT / f"{s.name}.md").write_text(
            f"# {s.title}\n\n_{type(s).__doc__.strip()}_\n\n"
            f"Expected: **{'pass' if s.must_pass else 'fail'}**. Got: **{'pass' if passed else 'fail'}**.\n\n"
            "---\n\n" + body + "\n"
        )
        index_lines.append(
            f"| [{s.title}]({s.name}.md) | {'pass' if s.must_pass else 'fail'} | {'✅' if ok else '❌'} {verdict} |"
        )
        git(repo, "checkout", "-q", "--", ".")
        git(repo, "clean", "-fdq")
        git(repo, "checkout", "-q", "main")

    (OUT / "README.md").write_text("\n".join(index_lines) + "\n")
    shutil.rmtree(tmp, ignore_errors=True)
    print(
        f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios as expected; comments in {OUT.relative_to(ROOT)}/"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
