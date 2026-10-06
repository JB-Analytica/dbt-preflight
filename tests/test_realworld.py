"""The pure parts of scripts/realworld: manifest loading, result extraction, baseline
comparison. The runner itself needs network and is not exercised here."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "realworld"))

import realworld_core as rw  # noqa: E402

SHA = "a" * 40


def _edit(**kw):
    return {"file": "models/a.sql", "prepend": "-- c\n", **kw}


def _raw():
    changes = {c: [_edit()] for c in rw.CHANGE_NAMES}
    changes["rename"] = [{"file": "models/a.sql", "replace": {"old": "id as a", "new": "id as b"}}]
    return {
        "projects": {
            "p": {
                "repo": "https://example.invalid/p.git",
                "sha": SHA,
                "rename_column": {"old": "a", "new": "b"},
                "expect": {"harmless": "passes", "rename": "caught"},
                "setup": [{"copy": "x.yml", "to": ".dbt-preflight.yml"}],
                "changes": changes,
            }
        }
    }


def _summary(**over):
    s = {
        "verdict": "failed",
        "counts": {
            "models": {"built": 4, "failed": 1, "skipped": 2, "not_verified": 3, "no_result": 0},
            "tests": {"passed": 7, "failed": 2, "warned": 0},
        },
        "failing_tests": [],
        "diffs": [],
    }
    s.update(over)
    return s


# --- the committed manifest -----------------------------------------------------------


def test_committed_manifest_loads_and_is_pinned():
    projects = rw.load_manifest()
    assert set(projects) == {
        "jaffle_shop_classic",
        "jaffle_shop_current",
        "mattermost",
        "shopify",
        "shopify_derived",
        "dbt_ga4",
    }
    for p in projects.values():
        assert set(p.changes) == set(rw.CHANGE_NAMES)
        assert len(p.sha) == 40
        for edit in [*p.setup, *(e for es in p.changes.values() for e in es)]:
            if edit.copy:
                assert (rw.FILES_DIR / edit.copy).is_file()


def test_committed_manifest_marks_the_workaround():
    marked = [e for e in rw.load_manifest()["mattermost"].setup if e.workaround]
    assert [e.workaround for e in marked] == ["duplicate-source-table-crash"]
    assert marked[0].remove_when


# --- manifest parsing -----------------------------------------------------------------


def test_parse_valid_manifest():
    p = rw.parse_manifest(_raw())["p"]
    assert p.rename_old == "a" and p.rename_new == "b"
    assert p.changes["rename"][0].replace_new == "id as b"
    assert p.setup[0].copy == "x.yml"


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["projects"]["p"].update(sha="main"), "40-character"),
        (lambda r: r["projects"]["p"].pop("repo"), "`repo` is required"),
        (lambda r: r["projects"]["p"]["changes"].pop("full"), "missing change"),
        (lambda r: r["projects"]["p"]["changes"].update(bogus=[_edit()]), "unknown change"),
        (lambda r: r["projects"]["p"]["changes"].update(full=[]), "no edits"),
        (lambda r: r["projects"]["p"]["changes"]["full"][0].update(typo=1), "unknown edit key"),
        (
            lambda r: r["projects"]["p"]["changes"]["full"][0].update(
                replace={"old": "a", "new": "b"}
            ),
            "exactly one of",
        ),
        (lambda r: r["projects"]["p"]["expect"].update(full="caught?"), "bad expect"),
        (lambda r: r["projects"]["p"].pop("rename_column"), "rename_column"),
    ],
)
def test_parse_rejects(mutate, message):
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises(rw.ManifestError, match=message):
        rw.parse_manifest(raw)


def test_parse_rejects_non_manifest():
    with pytest.raises(rw.ManifestError):
        rw.parse_manifest({"nope": 1})


# --- result extraction ----------------------------------------------------------------


def test_extract_row_counts():
    row = rw.extract_row(_summary(), change="harmless", wall_seconds=12.34)
    assert (row.built, row.failed, row.skipped, row.not_verified) == (4, 1, 2, 3)
    assert row.in_scope == 10
    assert (row.tests_passed, row.tests_failed) == (7, 2)
    assert row.rename_detected is None
    assert row.wall_seconds == 12.3


def test_extract_row_missing_counts_is_could_not_run():
    row = rw.extract_row({"fatal": "boom"}, change="harmless")
    assert row.verdict == "could_not_run" and row.in_scope == 0


def test_rename_detected_from_diff_entry():
    s = _summary(diffs=[{"columns_renamed": [{"old_name": "a", "new_name": "b"}]}])
    assert rw.extract_row(s, change="rename", rename_old="a", rename_new="b").rename_detected


def test_rename_detected_from_failing_test():
    s = _summary(
        failing_tests=[{"reading": "this model has no column `a`: renamed or dropped upstream?"}]
    )
    assert rw.extract_row(s, change="rename", rename_old="a", rename_new="b").rename_detected


def test_rename_not_detected_by_unrelated_failure_or_other_rename():
    s = _summary(
        failing_tests=[{"reading": "this model has no column `zzz`: renamed or dropped"}],
        diffs=[{"columns_renamed": [{"old_name": "x", "new_name": "y"}]}],
    )
    assert not rw.extract_row(s, change="rename", rename_old="a", rename_new="b").rename_detected


def test_rename_flag_only_on_rename_change():
    s = _summary(diffs=[{"columns_renamed": [{"old_name": "a", "new_name": "b"}]}])
    assert rw.extract_row(s, change="full", rename_old="a", rename_new="b").rename_detected is None


def test_target_met():
    p = rw.parse_manifest(_raw())["p"]
    ok = rw.extract_row(_summary(verdict="passed"), change="harmless")
    bad = rw.extract_row(_summary(verdict="failed"), change="harmless")
    assert rw.target_met(p, "harmless", ok) is True
    assert rw.target_met(p, "harmless", bad) is False
    assert rw.target_met(p, "full", bad) is None
    miss = rw.extract_row(_summary(), change="rename", rename_old="a", rename_new="b")
    assert rw.target_met(p, "rename", miss) is False


# --- baseline comparison --------------------------------------------------------------


def _base(**over):
    row = rw.extract_row(
        _summary(
            verdict="failed",
            diffs=[{"columns_renamed": [{"old_name": "a", "new_name": "b"}]}],
        ),
        change="rename",
        rename_old="a",
        rename_new="b",
    ).as_dict()
    row.update(over)
    return {"results": {"p/rename": row}}


def _row(**over):
    row = rw.extract_row(
        _summary(diffs=[{"columns_renamed": [{"old_name": "a", "new_name": "b"}]}]),
        change="rename",
        rename_old="a",
        rename_new="b",
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def test_identical_is_not_a_regression():
    assert rw.compare(_base(), {"p/rename": _row()}) == {}


def test_improvements_are_not_regressions():
    row = _row(built=9, failed=0, not_verified=0, verdict="passed")
    assert rw.compare(_base(), {"p/rename": row}) == {}


@pytest.mark.parametrize(
    "over, text",
    [
        ({"built": 3}, "built 4 -> 3"),
        ({"failed": 2}, "failed 1 -> 2"),
        ({"not_verified": 4}, "not verified 3 -> 4"),
        ({"no_result": 1}, "no result"),
        ({"rename_detected": False}, "rename no longer detected"),
        ({"verdict": "could_not_run"}, "verdict failed -> could_not_run"),
    ],
)
def test_regressions(over, text):
    found = rw.compare(_base(), {"p/rename": _row(**over)})
    assert any(text in problem for problem in found["p/rename"])


def test_verdict_regression_from_passing():
    base = _base(verdict="passed")
    found = rw.compare(base, {"p/rename": _row(verdict="passed_with_warnings")})
    assert "verdict passed -> passed_with_warnings" in found["p/rename"]


def test_lost_rename_only_counts_if_baseline_had_it():
    base = _base(rename_detected=False)
    assert rw.compare(base, {"p/rename": _row(rename_detected=False)}) == {}


def test_a_project_that_could_not_run_before_can_only_improve():
    # shopify_derived: could_not_run on 0.4.0, then running with a failing model is progress.
    base = _base(verdict="could_not_run", built=0, failed=0, not_verified=0, no_result=0)
    assert rw.compare(base, {"p/rename": _row(failed=2, verdict="failed")}) == {}
    assert rw.compare(base, {"p/rename": _row(verdict="could_not_run", built=0)}) == {}


def test_new_key_without_baseline_is_not_a_regression():
    assert rw.compare({"results": {}}, {"p/rename": _row()}) == {}
    assert rw.compare(rw.load_baseline(Path("/nonexistent.json")), {"p/rename": _row()}) == {}


def test_merge_baseline_keeps_other_rows():
    existing = {"results": {"q/full": {"built": 1}}, "shas": {"q": "b" * 40}}
    merged = rw.merge_baseline(existing, {"p/rename": _row()}, "0.9.9", {"p": SHA})
    assert set(merged["results"]) == {"p/rename", "q/full"}
    assert merged["preflight_version"] == "0.9.9"
    assert merged["shas"] == {"q": "b" * 40, "p": SHA}


def test_committed_baseline_covers_every_cell():
    baseline = rw.load_baseline()
    if not baseline["results"]:
        pytest.skip("no baseline written yet")
    expected = {rw.key(n, c) for n in rw.load_manifest() for c in rw.CHANGE_NAMES}
    assert set(baseline["results"]) == expected


def test_render_table_shows_before_and_after():
    projects = rw.parse_manifest(_raw())
    table = rw.render_table({"p/rename": _row(built=3, wall_seconds=5.0)}, _base(), projects)
    assert "p/rename" in table
    assert "4 -> 3" in table
    assert "met" in table
