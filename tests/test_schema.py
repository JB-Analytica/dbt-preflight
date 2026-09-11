from __future__ import annotations

import pytest

from dbt_preflight.manifest import Manifest
from dbt_preflight.schema import SchemaError, derive_dbml, resolve_schema


def test_derived_dbml_carries_types_keys_and_refs(manifest: Manifest) -> None:
    dbml = derive_dbml(manifest)
    assert "Table customers {" in dbml
    assert "  id int [pk]" in dbml
    assert "  email varchar" in dbml
    assert "  created_at timestamp" in dbml
    assert "  customer_id int [ref: > customers.id]" in dbml
    assert "Note: 'One row per customer'" in dbml


def test_derived_dbml_parses_with_model2data(manifest: Manifest, tmp_path) -> None:
    resolved = resolve_schema(None, manifest, tmp_path)
    assert resolved.derived
    assert set(resolved.tables) == {"customers", "orders"}
    assert len(resolved.refs) == 1
    assert (tmp_path / "derived.dbml").exists()


def test_untyped_source_column_is_an_error(raw_manifest: dict) -> None:
    raw_manifest["sources"]["source.p.shop.orders"]["columns"]["ordered_at"] = {
        "name": "ordered_at"
    }
    manifest = Manifest.from_dict(raw_manifest)
    with pytest.raises(SchemaError) as exc:
        derive_dbml(manifest)
    assert "- name: ordered_at" in str(exc.value)
    assert "data_type: <type>" in str(exc.value)


def test_source_without_columns_is_an_error(raw_manifest: dict) -> None:
    raw_manifest["sources"]["source.p.shop.orders"]["columns"] = {}
    manifest = Manifest.from_dict(raw_manifest)
    with pytest.raises(SchemaError, match="every column the staging model reads"):
        derive_dbml(manifest)


def test_explicit_dbml_file_wins(manifest: Manifest, tmp_path) -> None:
    dbml = tmp_path / "shop.dbml"
    dbml.write_text("Table customers {\n  id int [pk]\n  email varchar\n}\n")
    resolved = resolve_schema(dbml, manifest, tmp_path / "work")
    assert not resolved.derived
    assert list(resolved.tables) == ["customers"]


def test_untyped_sources_get_a_yaml_patch(raw_manifest: dict) -> None:
    raw_manifest["sources"]["source.p.shop.orders"]["columns"]["ordered_at"] = {
        "name": "ordered_at"
    }
    raw_manifest["sources"]["source.p.shop.customers"]["columns"] = {}
    manifest = Manifest.from_dict(raw_manifest)
    with pytest.raises(SchemaError) as exc:
        derive_dbml(manifest)
    text = str(exc.value)
    assert "as YAML to paste into the sources file" in text
    assert "  - name: shop\n    tables:\n" in text
    assert (
        "      - name: customers\n        columns:  # every column the staging model reads" in text
    )
    assert (
        "      - name: orders\n        columns:\n          - name: ordered_at\n            data_type: <type>"
        in text
    )
