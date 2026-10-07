"""Build the throwaway repository the demo tape records against.

    uv run python scripts/demo_repo.py /tmp/preflight-demo              # silent-change
    uv run python scripts/demo_repo.py /tmp/preflight-demo rename       # the other shape

Creates a git repo from examples/webshop with a `main` branch and a checked-out branch
carrying one of two pull-request shapes:

- `silent-change` (default): `dim_customers` stops excluding cancelled orders from
  lifetime revenue. Every test still passes, and only the comparison against the base
  branch shows that the numbers moved. This is what the demo records, because it is the
  thing no other warehouse-free check does.
- `rename`: a staging model renames `customer_id`, which fails loudly. Kept because it is
  the clearest illustration of catching breakage before merge.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "webshop"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=demo@example.com", "-c", "user.name=demo", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _silent_change(repo: Path) -> tuple[str, str]:
    model = repo / "dbt/models/marts/dim_customers.sql"
    text = model.read_text()
    old = "    where order_status != 'cancelled'\n"
    assert old in text, "dim_customers no longer filters cancelled orders"
    model.write_text(text.replace(old, ""))
    return "count-cancelled-orders", "Count cancelled orders in lifetime revenue"


def _rename(repo: Path) -> tuple[str, str]:
    model = repo / "dbt/models/staging/webshop/stg_webshop__customers.sql"
    text = model.read_text()
    assert "id as customer_id," in text, "stg_webshop__customers no longer aliases customer_id"
    model.write_text(text.replace("id as customer_id,", "id as cust_id,"))
    return "rename-customer-id", "Rename customer_id to cust_id"


SCENARIOS = {"silent-change": _silent_change, "rename": _rename}


def main(dest: Path, scenario: str) -> None:
    apply = SCENARIOS[scenario]
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(EXAMPLE, dest)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", ".")
    git(dest, "commit", "-q", "-m", "Webshop dbt project")
    # The branch is cut before the edit so the diff against main is the edit alone.
    git(dest, "checkout", "-q", "-b", "pr")
    branch, message = apply(dest)
    git(dest, "branch", "-m", branch)
    git(dest, "commit", "-q", "-am", message)
    print(f"demo repo at {dest} on branch {branch}")


if __name__ == "__main__":
    name = sys.argv[2] if len(sys.argv) > 2 else "silent-change"
    if name not in SCENARIOS:
        raise SystemExit(f"unknown scenario {name!r}; pick one of {', '.join(SCENARIOS)}")
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/preflight-demo"), name)
