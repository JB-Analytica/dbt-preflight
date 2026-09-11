"""Build the throwaway repository the demo tape records against.

    uv run python scripts/demo_repo.py /tmp/preflight-demo

Creates a git repo from examples/webshop with a `main` branch and a checked-out
`rename-customer-id` branch that renames `customer_id` in the customers staging model.
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


def main(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(EXAMPLE, dest)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", ".")
    git(dest, "commit", "-q", "-m", "Webshop dbt project")
    git(dest, "checkout", "-q", "-b", "rename-customer-id")
    model = dest / "dbt/models/staging/webshop/stg_webshop__customers.sql"
    model.write_text(model.read_text().replace("id as customer_id,", "id as cust_id,"))
    git(dest, "commit", "-q", "-am", "Rename customer_id to cust_id")
    print(f"demo repo at {dest} on branch rename-customer-id")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/preflight-demo"))
