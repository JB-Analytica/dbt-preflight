"""The derived schema as a file to keep: `dbt-preflight schema`.

`derive_dbml` writes what a run derives, with a header saying not to edit it. This module
turns that same text into a file a person is meant to edit: a new header, and a column
note on every column whose type preflight worked out rather than read from `sources.yml`,
saying where the type came from.

The notes are prose on purpose. model2data reads a column note that is, whole, a JSON
object as generation hints (null rates, weights, distributions); any other note becomes the
column's description and shapes nothing. So these notes cannot change the data, which is
what keeps a run with `schema:` pointing at the written file identical to the derived run.
The one note that does shape the data is preflight's own, not model2data's: `JSON, keys
read: ...` on a column some model reads as JSON (`json_columns.py`), which preflight's
fixture step reads back from the description. Where a column has both, they share one note,
`<where the type came from>; JSON, keys read: ...`.
"""

from __future__ import annotations

import re
from pathlib import Path

from dbt_preflight.manifest import Manifest
from dbt_preflight.schema import InferredSource, source_table_names

NOTE_GUESSED = "type guessed from the name"
NOTE_COMPILED = "type read from compiled SQL"
NOTE_CAST = "inferred from a staging cast"
NOTE_UNKNOWN = "typed varchar: a model reading it could not be followed"

HEADER = (
    "// Written by `dbt-preflight schema` from the project's sources.yml and the SQL of the models\n"
    "// that read it. Edit it: once `schema:` in .dbt-preflight.yml points here, preflight reads\n"
    "// this file instead of deriving the schema on every run.\n"
    "// A column with a note was not typed by sources.yml. Check it, then delete the note.\n"
)

_DERIVED_HEADER = re.compile(r"\A//[^\n]*\n\n?")
_TABLE_OPEN = re.compile(r"^Table (\S+) \{$")
_COLUMN = re.compile(r"^  (?P<name>[^\s:]+) (?P<type>\S+)(?: \[(?P<settings>.*)\])?$")


def default_output(base: Path, project_name: str) -> Path:
    """`source_system/<project name>.dbml` under `base`, the folder `.dbt-preflight.yml` is in.

    Relative paths in that file resolve against its own folder, so a `schema:` line that
    says `source_system/<name>.dbml` works as printed. The folder name is the one the
    reference architecture already uses for the DBML that describes a source system.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", project_name) or "project"
    return base / "source_system" / f"{safe}.dbml"


def annotate(dbml: str, manifest: Manifest, inferred: list[InferredSource]) -> str:
    """The derived DBML as a file to keep: new header, a prose note per untyped column."""
    names = source_table_names(manifest.sources.values())
    by_key = {(i.source_name, i.table): i for i in inferred}
    typed_by_yaml: dict[str, set[str]] = {}
    guessed: dict[str, set[str]] = {}
    unknown: dict[str, set[str]] = {}
    compiled: dict[str, set[str]] = {}
    for src in manifest.sources.values():
        info = by_key.get((src.source_name, src.name))
        if info is None:
            continue
        table = names[src.unique_id]
        typed_by_yaml[table] = {c.name for c in src.columns if c.data_type}
        guessed[table] = set(info.guessed_columns)
        unknown[table] = set(info.unknown_columns)
        compiled[table] = set(info.compiled_columns)

    body = _DERIVED_HEADER.sub("", dbml, count=1)
    out: list[str] = []
    table: str | None = None
    for line in body.splitlines():
        opened = _TABLE_OPEN.match(line)
        if opened:
            table = opened.group(1)
        elif line == "}":
            table = None
        elif table in typed_by_yaml and (col := _COLUMN.match(line)):
            name = col.group("name")
            if name not in typed_by_yaml[table]:
                if name in unknown[table]:
                    note = NOTE_UNKNOWN
                elif name in guessed[table]:
                    note = NOTE_GUESSED
                elif name in compiled[table]:
                    note = NOTE_COMPILED
                else:
                    note = NOTE_CAST
                settings = col.group("settings")
                if settings and "note: '" in settings:
                    # The JSON note `derive_dbml` wrote: one note per column, both in it.
                    joined = settings.replace("note: '", f"note: '{note}; ", 1)
                elif settings:
                    joined = f"{settings}, note: '{note}'"
                else:
                    joined = f"note: '{note}'"
                line = f"  {name} {col.group('type')} [{joined}]"
        out.append(line)
    return HEADER + "\n" + "\n".join(out).rstrip("\n") + "\n"


def count_notes(dbml: str) -> dict[str, int]:
    """How many columns carry each kind of note, for the command's closing summary."""
    return {
        kind: dbml.count(f"note: '{kind}")
        for kind in (NOTE_GUESSED, NOTE_UNKNOWN, NOTE_COMPILED, NOTE_CAST)
    }
