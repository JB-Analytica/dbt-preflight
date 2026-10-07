"""The two pieces of configuration friction real projects hit first.

Both were found on public projects by the real-world suite: a `profiles.yml` kept at the
repository root rather than beside `dbt_project.yml`, and `env_var()` failures that surface
one per run, so finding them all is one run per variable.
"""

from __future__ import annotations

from pathlib import Path

from dbt_preflight.config import env_var_help, missing_env_vars
from dbt_preflight.transpile import detect_dialect, profiles_candidates

PROFILE = """
shop:
  target: prod
  outputs:
    prod:
      type: bigquery
      project: analytics
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "repo" / "dbt"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: shop\nprofile: shop\n")
    return project


def test_dialect_is_found_beside_the_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "profiles.yml").write_text(PROFILE)
    assert detect_dialect(project, "shop", tmp_path / "repo") == "bigquery"


def test_dialect_is_found_at_the_repository_root(tmp_path: Path) -> None:
    """A repository that keeps the dbt project in a subdirectory very often keeps the
    profile at the root, next to the CI workflow that uses it. Looking only beside the
    project meant no dialect, so every warehouse function went untranspiled."""
    project = _project(tmp_path)
    (tmp_path / "repo" / "profiles.yml").write_text(PROFILE)
    assert detect_dialect(project, "shop", tmp_path / "repo") == "bigquery"


def test_the_project_wins_over_the_root(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "profiles.yml").write_text(PROFILE)
    (tmp_path / "repo" / "profiles.yml").write_text(PROFILE.replace("bigquery", "snowflake"))
    assert detect_dialect(project, "shop", tmp_path / "repo") == "bigquery"


def test_no_profile_anywhere_is_not_an_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert detect_dialect(project, "shop", tmp_path / "repo") is None


def test_candidates_are_unique_and_nearest_first(tmp_path: Path) -> None:
    project = _project(tmp_path)
    found = profiles_candidates(project, tmp_path / "repo")
    assert found[0] == (project / "profiles.yml").resolve()
    assert len(found) == len(set(found))


def test_every_missing_env_var_is_found_at_once(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "dbt"
    (project / "models").mkdir(parents=True)
    (project / "models" / "_sources.yml").write_text(
        "sources:\n"
        "  - name: shop\n"
        "    database: \"{{ env_var('GCP_PROJECT') }}\"\n"
        "    schema: \"{{ env_var('RAW_SCHEMA') }}\"\n"
    )
    (project / "models" / "m.sql").write_text(
        "select '{{ env_var(\"API_REGION\") }}' as region, "
        '\'{{ env_var("OPTIONAL_ONE", "eu") }}\' as fallback'
    )
    # dbt's own output and installed packages are not the project's to fix.
    (project / "target").mkdir()
    (project / "target" / "manifest.yml").write_text("{{ env_var('INTERNAL_ONLY') }}")

    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("RAW_SCHEMA", raising=False)
    monkeypatch.setenv("API_REGION", "eu-west1")

    missing = missing_env_vars(project, {})
    assert missing == ["GCP_PROJECT", "RAW_SCHEMA"]  # API_REGION is set; the rest excluded

    # A value supplied in .dbt-preflight.yml counts as supplied.
    assert missing_env_vars(project, {"GCP_PROJECT": "x"}) == ["RAW_SCHEMA"]


def test_the_help_is_a_block_to_paste(tmp_path: Path) -> None:
    text = env_var_help(["GCP_PROJECT", "RAW_SCHEMA"])
    assert "env:\n  GCP_PROJECT: <value>\n  RAW_SCHEMA: <value>" in text
    assert "2 environment variables" in text
