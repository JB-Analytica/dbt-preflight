"""Which edits to `.dbt-preflight.yml` count as a change to the fixtures.

A config change used to make every source count as modified, so adding a `conventions:`
block to audience-analytics rebuilt all 33 models and failed on a fixture weakness the
pull request had nothing to do with.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dbt_preflight.cli import _config_reshapes_fixtures
from dbt_preflight.config import CONFIG_FILENAME, load_config

BASE = "project_dir: .\nrows: 100\nseed: 42\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "dbt_project.yml").write_text("name: p\nprofile: p\n")
    (tmp_path / CONFIG_FILENAME).write_text(BASE)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return tmp_path


def _reshapes(repo: Path, head: str) -> bool:
    (repo / CONFIG_FILENAME).write_text(head)
    return _config_reshapes_fixtures(load_config(repo), "main")


def test_a_conventions_block_does_not_reshape_the_fixtures(repo: Path) -> None:
    assert not _reshapes(repo, BASE + "conventions:\n  preset: jba\n")


def test_metrics_and_judging_keys_do_not_reshape_the_fixtures(repo: Path) -> None:
    head = BASE + "dialect_failures: error\nmetrics:\n  - {name: n, model: m, sql: count(*)}\n"
    assert not _reshapes(repo, head)


@pytest.mark.parametrize(
    "head",
    [
        "project_dir: .\nrows: 200\nseed: 42\n",
        "project_dir: .\nrows: 100\nseed: 7\n",
        BASE + "rows_for:\n  orders: 800\n",
        BASE + "locale: nl_BE\n",
        BASE + "env:\n  GCP_PROJECT: x\n",
        BASE + "dialect: snowflake\n",
        BASE + "vars:\n  shopify_api: graphql\n",
    ],
)
def test_a_fixture_key_does_reshape_them(repo: Path, head: str) -> None:
    assert _reshapes(repo, head)


def test_a_config_new_on_head_reshapes_them(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", CONFIG_FILENAME)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "drop")
    assert _reshapes(repo, BASE + "conventions:\n  preset: jba\n")
