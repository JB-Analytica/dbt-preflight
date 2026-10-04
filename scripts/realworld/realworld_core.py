"""The pure parts of the real-world suite: manifest, result extraction, baseline comparison.

No git, no network, no subprocess here, so all of it is unit-tested (tests/test_realworld.py).
`run.py` does the I/O around it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.yml"
BASELINE_PATH = HERE / "baseline.json"
FILES_DIR = HERE / "files"

CHANGE_NAMES = ("harmless", "rename", "full")

# Lower is better. `nothing_changed` after we made a change means the diff was not seen.
VERDICT_RANK = {
    "passed": 0,
    "passed_with_warnings": 1,
    "failed": 2,
    "nothing_changed": 3,
    "could_not_run": 4,
}
PASSING = ("passed", "passed_with_warnings")


class ManifestError(ValueError):
    pass


@dataclass
class Edit:
    """One reproducible change to a file in the checked-out project."""

    file: str | None = None
    prepend: str | None = None
    replace_old: str | None = None
    replace_new: str | None = None
    copy: str | None = None  # a name under scripts/realworld/files/
    to: str | None = None  # where `copy` lands, relative to the repo root
    workaround: str | None = None
    remove_when: str | None = None


@dataclass
class Project:
    name: str
    repo: str
    sha: str
    description: str = ""
    setup: list[Edit] = field(default_factory=list)
    changes: dict[str, list[Edit]] = field(default_factory=dict)
    expect: dict[str, str] = field(default_factory=dict)
    rename_old: str | None = None
    rename_new: str | None = None


def _edit(raw: Any, where: str) -> Edit:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: an edit must be a mapping")
    known = {"file", "prepend", "replace", "copy", "to", "workaround", "remove_when"}
    extra = set(raw) - known
    if extra:
        raise ManifestError(f"{where}: unknown edit key(s) {sorted(extra)}")
    edit = Edit(
        file=raw.get("file"),
        prepend=raw.get("prepend"),
        copy=raw.get("copy"),
        to=raw.get("to"),
        workaround=raw.get("workaround"),
        remove_when=raw.get("remove_when"),
    )
    rep = raw.get("replace")
    if rep is not None:
        if not isinstance(rep, dict) or set(rep) != {"old", "new"}:
            raise ManifestError(f"{where}: `replace` needs exactly `old` and `new`")
        edit.replace_old, edit.replace_new = str(rep["old"]), str(rep["new"])
    if edit.copy:
        if not edit.to or edit.file or edit.prepend is not None or rep is not None:
            raise ManifestError(f"{where}: `copy` takes `to` and nothing else")
    else:
        if not edit.file:
            raise ManifestError(f"{where}: an edit needs `file` (or `copy` and `to`)")
        if (edit.prepend is None) == (rep is None):
            raise ManifestError(f"{where}: give exactly one of `prepend` and `replace`")
    return edit


def parse_manifest(raw: Any) -> dict[str, Project]:
    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), dict):
        raise ManifestError("the manifest needs a top-level `projects` mapping")
    projects: dict[str, Project] = {}
    for name, body in raw["projects"].items():
        where = f"project {name!r}"
        if not isinstance(body, dict):
            raise ManifestError(f"{where}: must be a mapping")
        for key in ("repo", "sha"):
            if not body.get(key):
                raise ManifestError(f"{where}: `{key}` is required")
        sha = str(body["sha"])
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ManifestError(f"{where}: `sha` must be a full 40-character commit SHA")
        changes_raw = body.get("changes") or {}
        missing = [c for c in CHANGE_NAMES if c not in changes_raw]
        if missing:
            raise ManifestError(f"{where}: missing change(s) {missing}")
        unknown = [c for c in changes_raw if c not in CHANGE_NAMES]
        if unknown:
            raise ManifestError(f"{where}: unknown change(s) {unknown}")
        changes = {
            c: [_edit(e, f"{where}, change {c!r}") for e in (changes_raw[c] or [])]
            for c in changes_raw
        }
        for c, edits in changes.items():
            if not edits:
                raise ManifestError(f"{where}: change {c!r} has no edits")
        rename = body.get("rename_column") or {}
        project = Project(
            name=str(name),
            repo=str(body["repo"]),
            sha=sha,
            description=str(body.get("description", "")),
            setup=[_edit(e, f"{where}, setup") for e in (body.get("setup") or [])],
            changes=changes,
            expect={str(k): str(v) for k, v in (body.get("expect") or {}).items()},
            rename_old=rename.get("old"),
            rename_new=rename.get("new"),
        )
        for c, want in project.expect.items():
            if c not in CHANGE_NAMES or want not in ("passes", "caught"):
                raise ManifestError(f"{where}: bad expect {c}: {want}")
        if project.expect.get("rename") and not (project.rename_old and project.rename_new):
            raise ManifestError(f"{where}: `rename_column` needs `old` and `new`")
        projects[project.name] = project
    return projects


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Project]:
    return parse_manifest(yaml.safe_load(path.read_text()))


# --- results -----------------------------------------------------------------------------


@dataclass
class Row:
    """One (project, change) result. Everything compared against the baseline is here."""

    verdict: str
    in_scope: int
    built: int
    failed: int
    skipped: int
    not_verified: int
    no_result: int
    tests_passed: int
    tests_failed: int
    rename_detected: bool | None  # None for changes that are not a rename
    wall_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def rename_detected(summary: dict[str, Any], old: str, new: str) -> bool:
    """Whether the run reported the rename: preflight's own diff entry for the column, or a
    failing test whose message says the old column is gone."""
    for d in summary.get("diffs") or []:
        for r in d.get("columns_renamed") or []:
            if r.get("old_name") == old and r.get("new_name") == new:
                return True
    needle = f"`{old}`"
    for t in summary.get("failing_tests") or []:
        reading = t.get("reading") or ""
        if needle in reading and ("no column" in reading or "renamed" in reading):
            return True
    return False


def extract_row(
    summary: dict[str, Any],
    *,
    change: str,
    rename_old: str | None = None,
    rename_new: str | None = None,
    wall_seconds: float | None = None,
) -> Row:
    models = summary.get("counts", {}).get("models", {})
    tests = summary.get("counts", {}).get("tests", {})
    n = {
        k: int(models.get(k, 0))
        for k in ("built", "failed", "skipped", "not_verified", "no_result")
    }
    detected: bool | None = None
    if change == "rename":
        detected = bool(rename_old and rename_new) and rename_detected(
            summary, rename_old or "", rename_new or ""
        )
    return Row(
        verdict=str(summary.get("verdict", "could_not_run")),
        in_scope=sum(n.values()),
        tests_passed=int(tests.get("passed", 0)),
        tests_failed=int(tests.get("failed", 0)),
        rename_detected=detected,
        wall_seconds=None if wall_seconds is None else round(wall_seconds, 1),
        **n,
    )


def failed_row(wall_seconds: float | None = None) -> Row:
    """The row for a run that produced no summary at all (crash, timeout)."""
    return Row("could_not_run", 0, 0, 0, 0, 0, 0, 0, 0, None, wall_seconds)


def key(project: str, change: str) -> str:
    return f"{project}/{change}"


# --- targets -------------------------------------------------------------------------------


def target_met(project: Project, change: str, row: Row) -> bool | None:
    """Whether the manifest's 0.4.0 target holds for this row; None when there is none."""
    want = project.expect.get(change)
    if want == "passes":
        return row.verdict in PASSING
    if want == "caught":
        return bool(row.rename_detected)
    return None


# --- baseline ------------------------------------------------------------------------------


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"preflight_version": None, "results": {}}
    return json.loads(path.read_text())


def merge_baseline(
    existing: dict[str, Any], rows: dict[str, Row], version: str, shas: dict[str, str]
) -> dict[str, Any]:
    results = dict(existing.get("results") or {})
    for k, row in rows.items():
        results[k] = row.as_dict()
    return {
        "preflight_version": version,
        "shas": {**(existing.get("shas") or {}), **shas},
        "results": dict(sorted(results.items())),
    }


def regressions(base: dict[str, Any], row: Row) -> list[str]:
    """Ways `row` is worse than the baseline entry `base`; empty when it is not worse."""
    out: list[str] = []
    if row.built < base["built"]:
        out.append(f"built {base['built']} -> {row.built}")
    if row.failed > base["failed"]:
        out.append(f"failed {base['failed']} -> {row.failed}")
    if row.not_verified > base["not_verified"]:
        out.append(f"not verified {base['not_verified']} -> {row.not_verified}")
    if row.no_result > base.get("no_result", 0):
        out.append(f"no result {base.get('no_result', 0)} -> {row.no_result}")
    if base.get("rename_detected") and not row.rename_detected:
        out.append("rename no longer detected")
    old_rank = VERDICT_RANK.get(base["verdict"], 99)
    new_rank = VERDICT_RANK.get(row.verdict, 99)
    if new_rank > old_rank:
        out.append(f"verdict {base['verdict']} -> {row.verdict}")
    return out


def compare(baseline: dict[str, Any], rows: dict[str, Row]) -> dict[str, list[str]]:
    """Regressions per result key. A key with no baseline entry is new, not a regression."""
    found: dict[str, list[str]] = {}
    for k, row in rows.items():
        base = (baseline.get("results") or {}).get(k)
        if base is not None:
            problems = regressions(base, row)
            if problems:
                found[k] = problems
    return found


# --- table ---------------------------------------------------------------------------------


def _fmt(before: Any, after: Any) -> str:
    if before is None:
        return f"{after}"
    return f"{before}" if before == after else f"{before} -> {after}"


def render_table(
    rows: dict[str, Row],
    baseline: dict[str, Any],
    projects: dict[str, Project],
) -> str:
    header = [
        "project/change",
        "verdict",
        "scope",
        "built",
        "failed",
        "skipped",
        "not verif.",
        "tests ok/fail",
        "rename",
        "target",
        "wall",
    ]
    lines: list[list[str]] = [header]
    base_results = baseline.get("results") or {}
    for k, row in rows.items():
        project, change = k.split("/", 1)
        b = base_results.get(k) or {}

        def pair(field_name: str, b: dict[str, Any] = b, row: Row = row) -> str:
            return _fmt(b.get(field_name), getattr(row, field_name))

        met = target_met(projects[project], change, row) if project in projects else None
        rename = "-" if row.rename_detected is None else ("yes" if row.rename_detected else "NO")
        if row.rename_detected is not None and b.get("rename_detected") is not None:
            before = "yes" if b["rename_detected"] else "NO"
            rename = _fmt(before, rename)
        tests = f"{row.tests_passed}/{row.tests_failed}"
        if b:
            tests = f"{b['tests_passed']}/{b['tests_failed']}"
            if (b["tests_passed"], b["tests_failed"]) != (row.tests_passed, row.tests_failed):
                tests += f" -> {row.tests_passed}/{row.tests_failed}"
        lines.append(
            [
                k,
                _fmt(b.get("verdict"), row.verdict),
                pair("in_scope"),
                pair("built"),
                pair("failed"),
                pair("skipped"),
                pair("not_verified"),
                tests,
                rename,
                "-" if met is None else ("met" if met else "not met"),
                "" if row.wall_seconds is None else f"{row.wall_seconds:.0f}s",
            ]
        )
    widths = [max(len(r[i]) for r in lines) for i in range(len(header))]
    out = []
    for n, r in enumerate(lines):
        out.append("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)
