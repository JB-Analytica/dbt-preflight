from __future__ import annotations

from pathlib import Path

import pytest

from dbt_preflight.config import CONFIG_FILENAME, ConfigError, load_config


def _repo(tmp_path: Path, project_dir: str = ".") -> Path:
    (tmp_path / project_dir).mkdir(parents=True, exist_ok=True)
    (tmp_path / project_dir / "dbt_project.yml").write_text("name: p\nprofile: p\n")
    return tmp_path


def test_defaults_without_a_config_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = load_config(repo)
    assert config.project_dir == repo.resolve()
    assert config.schema is None
    assert config.rows == 200
    assert config.seed == 42
    assert config.loader_columns["dlt"] == {"_dlt_load_id": "varchar", "_dlt_id": "varchar"}
    assert config.workdir == repo.resolve() / ".preflight"
    assert config.describe_schema_source() == "sources.yml (derived)"


def test_full_config_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "dbt")
    (repo / "shop.dbml").write_text("Table t {\n  id int [pk]\n}\n")
    (repo / CONFIG_FILENAME).write_text(
        "project_dir: dbt\nschema: shop.dbml\nrows: 50\nrows_for:\n  t: 500\nseed: 7\n"
        "locale: nl_BE\nenv:\n  GCP_PROJECT: preflight\nloader_columns:\n  fivetran:\n"
        "    _fivetran_synced: timestamp\ndialect_failures: error\ncheck_all: true\n"
    )
    config = load_config(repo)
    assert config.project_relpath == Path("dbt")
    assert config.describe_schema_source() == "shop.dbml"
    assert config.rows == 50 and config.rows_for == {"t": 500} and config.seed == 7
    assert config.locale == "nl_BE"
    assert config.env == {"GCP_PROJECT": "preflight"}
    assert config.loader_columns["fivetran"] == {"_fivetran_synced": "timestamp"}
    assert "dlt" in config.loader_columns
    assert config.dialect_failures == "error" and config.check_all


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("rows: lots\n", "`rows`"),
        ("rows: 5\n", "`rows`"),
        ("seed: '1'\n", "`seed`"),
        ("rows_for:\n  t: 0\n", "`rows_for`"),
        ("dialect_failures: maybe\n", "`dialect_failures`"),
        ("schema: missing.dbml\n", "does not exist"),
        ("project_dir: nowhere\n", "No dbt_project.yml"),
        ("surprise: 1\n", "Unknown keys"),
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, body: str, fragment: str) -> None:
    repo = _repo(tmp_path)
    (repo / CONFIG_FILENAME).write_text(body)
    with pytest.raises(ConfigError, match=fragment):
        load_config(repo)


def test_version_matches_the_packaging_metadata() -> None:
    """`--version` printed 0.1.0 on the 0.2.0 release: the version was hardcoded in
    __init__.py and pyproject.toml had moved on without it."""
    from pathlib import Path

    import tomllib

    from dbt_preflight import __version__

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert __version__ == declared
