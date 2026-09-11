from __future__ import annotations

from pathlib import Path

import pytest

from dbt_preflight.checks import check_manifest
from dbt_preflight.config import CONFIG_FILENAME, ConfigError, load_config
from dbt_preflight.conventions import ERROR, OFF, WARN, ConventionError, from_config, jba, none
from dbt_preflight.manifest import Manifest


def test_default_is_the_house_preset() -> None:
    c = from_config(None)
    assert c.severity("naming") == ERROR and c.severity("description") == WARN
    assert c.source_layer == "staging"
    assert c.pattern("marts") and c.pattern("marts").match("fct_orders")
    assert c.any_enabled


def test_without_a_config_file_the_house_rules_only_warn() -> None:
    c = from_config(None, configured=False)
    assert c.severity("naming") == WARN and c.severity("primary_key") == WARN
    assert c.severity("description") == WARN
    assert c.layers == jba().layers


def test_config_file_without_conventions_block_keeps_full_strength(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text("name: p\nprofile: p\n")
    assert load_config(tmp_path).conventions.severity("naming") == WARN  # no file at all
    (tmp_path / CONFIG_FILENAME).write_text("rows: 50\n")
    assert load_config(tmp_path).conventions.severity("naming") == ERROR  # a file, no block


def test_none_preset_switches_everything_off() -> None:
    c = from_config({"preset": "none"})
    assert not c.any_enabled
    assert c == none()


def test_rules_can_be_softened_or_disabled() -> None:
    c = from_config({"rules": {"description": "off", "primary_key": "warn", "naming": True}})
    assert c.severity("description") == OFF
    assert c.severity("primary_key") == WARN
    assert c.severity("naming") == ERROR
    assert c.severity("layering") == ERROR  # untouched


def test_layers_replace_the_preset_patterns() -> None:
    c = from_config({"layers": {"marts": r"^(dim|fct|rpt)_[a-z_]+$"}, "source_layer": None})
    assert set(c.layers) == {"marts"}
    assert c.pattern("staging") is None
    assert c.source_layer is None
    assert "rpt" in c.hints["marts"]


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"preset": "acme"}, "preset"),
        ({"rules": {"colour": "warn"}}, "Unknown rule"),
        ({"rules": {"naming": "loud"}}, "off, warn or error"),
        ({"layers": {"marts": "("}}, "not a valid regex"),
        ({"surprise": 1}, "Unknown keys"),
        ("jba", "must be a mapping"),
    ],
)
def test_bad_convention_config_is_rejected(raw, fragment: str) -> None:
    with pytest.raises(ConventionError, match=fragment):
        from_config(raw)


def test_config_file_carries_conventions(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text("name: p\nprofile: p\n")
    (tmp_path / CONFIG_FILENAME).write_text(
        "conventions:\n  rules:\n    column_naming: off\n  layers:\n    marts: '^rpt_.*$'\n"
    )
    config = load_config(tmp_path)
    assert config.conventions.severity("column_naming") == OFF
    assert config.conventions.layers == {"marts": "^rpt_.*$"}

    (tmp_path / CONFIG_FILENAME).write_text("conventions:\n  rules:\n    naming: sometimes\n")
    with pytest.raises(ConfigError, match="off, warn or error"):
        load_config(tmp_path)


def test_checks_respect_severity_and_off(raw_manifest: dict) -> None:
    manifest = Manifest.from_dict(raw_manifest)
    orders = ["model.p.stg_shop__orders"]  # lacks a primary-key test

    default = check_manifest(manifest, orders, ".", jba())
    assert [(v.rule, v.severity) for v in default] == [("primary_key", ERROR)]

    softened = from_config({"rules": {"primary_key": "warn"}})
    assert [(v.rule, v.severity) for v in check_manifest(manifest, orders, ".", softened)] == [
        ("primary_key", WARN)
    ]

    assert check_manifest(manifest, orders, ".", none()) == []


def test_custom_layers_and_source_layer(raw_manifest: dict) -> None:
    # A project whose marts are called rpt_ and whose staging folder is "base".
    node = raw_manifest["nodes"]["model.p.dim_customers"]
    node["name"], node["path"] = "rpt_customers", "marts/rpt_customers.sql"
    manifest = Manifest.from_dict(raw_manifest)
    c = from_config({"layers": {"marts": r"^rpt_[a-z_]+$"}, "source_layer": "base"})
    violations = check_manifest(manifest, ["model.p.dim_customers"], ".", c)
    assert [v.rule for v in violations] == ["primary_key"]  # name fits, no source() read

    # Under the house preset the same model is misnamed.
    assert "naming" in [
        v.rule for v in check_manifest(manifest, ["model.p.dim_customers"], ".", jba())
    ]
