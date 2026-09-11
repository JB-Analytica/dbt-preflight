"""The synthetic project generator (scripts/big_project.py), kept honest.

A small generated project must still be a project preflight can build and diff: this is
the test that catches the generator rotting out of sync with naming, layering or the
`unique` + `not_null` primary-key rule as those evolve.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from dbt_preflight.cli import app

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "big_project.py"

_spec = importlib.util.spec_from_file_location("big_project", SCRIPT)
assert _spec is not None and _spec.loader is not None
big_project = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = big_project  # dataclasses needs the module registered to introspect it
_spec.loader.exec_module(big_project)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_generated_project_passes_with_a_base_ref(tmp_path: Path) -> None:
    project = tmp_path / "bigproject"
    big_project.generate(project, total=30, seed=7)

    # A small, harmless edit on its own branch, so `--base-ref main` has something to diff.
    _git(project, "checkout", "-q", "-b", "tweak")
    model = next((project / "dbt" / "models" / "marts").glob("*.sql"))
    model.write_text(model.read_text() + "\n")
    _git(project, "commit", "-q", "-am", "tweak a mart")

    comment = tmp_path / "comment.md"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--base-ref",
            "main",
            "--repo-root",
            str(project),
            "--config",
            str(project / ".dbt-preflight.yml"),
            "--comment-file",
            str(comment),
        ],
    )
    assert result.exit_code == 0, result.output
    body = comment.read_text()
    assert "## 🛫 dbt preflight: ✅ passed" in body
    assert "### Failing tests" not in body
