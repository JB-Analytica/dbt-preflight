"""Source columns a project reads as JSON, and valid JSON to fill them with.

model2data fills a text column with placeholder sentences ("Weight reason."). A model that
parses one with `json_extract_string(receipt, '$.charges')` then fails on DuckDB, on the
pull request and on main alike, and the comment called that "broken on main" although the
cause was preflight's own data. So a column some model reads with a JSON function gets a
JSON object instead, with every path the SQL reads present.

Three parts, none of which knows about dbt:

- `json_reads_in_tree`: which column names a parsed query passes to a JSON function, and
  the paths and leaf types it reads (`cast(json_extract_string(x, '$.amount') as double)`
  reads `amount` as a number). `schema.json_reads` traces those names back to the source
  columns, through staging aliases.
- `format_note` / `parse_note`: the shape as a prose DBML column note, `JSON, keys read:
  address.city, amount (number)`, so the file `dbt-preflight schema` writes builds the same
  fixtures. model2data reads a note that is not a JSON object as a description, so the note
  never changes what model2data generates.
- `json_values`: the values, from the run's seed and the table and column names, so the
  same input gives byte-identical output.

A path is written with `.` between keys and `[]` after a key that holds an array (`$[0]`,
`[*]`, an `unnest`): `charges.data[].exchange_rate`, or `[]` alone for an array at the root.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

from sqlglot import exp

# path text -> leaf type, or None when nothing in the SQL says what the leaf holds.
Shape = dict[str, str | None]

# Leaf types, strongest first: on a conflict the first one wins. An integer satisfies a
# numeric cast and a string read too; a number satisfies both but an integer cast.
LEAF_TYPES = ("integer", "number", "boolean", "timestamp", "date", "string")

# DBML types a JSON value can be loaded into.
JSON_CAPABLE_TYPES = frozenset(
    {"varchar", "text", "string", "char", "nvarchar", "json", "jsonb", "variant", "super"}
)

# Functions that read their first argument as JSON. Typed sqlglot classes first.
_JSON_CLASSES: tuple[type[exp.Expr], ...] = (
    exp.JSONExtract,
    exp.JSONExtractScalar,
    exp.JSONBExtract,
    exp.JSONBExtractScalar,
    exp.JSONExtractArray,
    exp.JSONType,
    exp.JSONKeys,
    exp.JSONExists,
    exp.ParseJSON,
)
# What sqlglot leaves as an anonymous call in some dialect: name -> how its other
# arguments read ("path": the second is a path; "keys": every other one is a key, as in
# `json_extract_path_text(x, 'a', 'b')`; "none": no path).
_JSON_ANONYMOUS = {
    "json_extract": "path",
    "json_extract_string": "path",
    "json_extract_scalar": "path",
    "json_extract_path": "keys",
    "json_extract_path_text": "keys",
    "json_value": "path",
    "json_query": "path",
    "json_exists": "path",
    "json_type": "path",
    "json_keys": "path",
    "json_array_length": "path",
    "json_extract_array": "path",
    "json_query_array": "path",
    "json_value_array": "path",
    "json_valid": "none",
    "json_structure": "none",
    "from_json": "none",
    "from_json_strict": "none",
    "json_transform": "none",
    "json": "none",
    "parse_json": "none",
    "try_parse_json": "none",
}
# Of those, the ones whose path names an array.
_ARRAY_RESULTS = {"json_array_length", "json_extract_array", "json_query_array", "json_value_array"}

_KEY = re.compile(r"[A-Za-z0-9_-]+")
_PATH_TEXT = re.compile(r"^(?:\[\]|[A-Za-z0-9_-]+(?:\[\])*)(?:\.[A-Za-z0-9_-]+(?:\[\])*)*$")


# --- reading the SQL ------------------------------------------------------------------


def _anonymous_name(node: exp.Expr) -> str | None:
    if isinstance(node, exp.Anonymous) and isinstance(node.this, str):
        name = node.this.lower()
        return name if name in _JSON_ANONYMOUS else None
    return None


def _is_json_cast(node: exp.Expr) -> bool:
    return (
        isinstance(node, exp.Cast)
        and isinstance(node.to, exp.DataType)
        and node.to.this in {exp.DType.JSON, exp.DType.JSONB}
    )


def _is_json_call(node: exp.Expr) -> bool:
    return isinstance(node, _JSON_CLASSES) or _anonymous_name(node) is not None


def _json_argument(node: exp.Expr) -> exp.Expr | None:
    if isinstance(node, exp.Anonymous):
        return node.expressions[0] if node.expressions else None
    arg = node.this
    return arg if isinstance(arg, exp.Expr) else None


def _path_arguments(node: exp.Expr) -> list[list[str]]:
    """The paths one JSON call reads, as segment lists (`[]` marks an array)."""
    name = _anonymous_name(node)
    if name is not None:
        kind = _JSON_ANONYMOUS[name]
        args = node.expressions[1:]
        if kind == "none" or not args:
            paths = [[]]
        elif kind == "keys":
            segments: list[str] = []
            for a in args:
                key = _literal(a)
                if key is None:
                    return []
                segments += ["[]"] if key.isdigit() else [key]
            paths = [segments]
        else:
            paths = _paths_of(args[0])
        if name in _ARRAY_RESULTS:
            paths = [p + ["[]"] for p in paths]
        return paths
    if isinstance(node, (exp.ParseJSON, exp.JSONKeys)) and not node.expression:
        return [[]]
    paths = _paths_of(node.args.get("expression"))
    if isinstance(node, exp.JSONExtractArray):
        paths = [p + ["[]"] for p in paths]
    return paths


def _literal(node: exp.Expr | None) -> str | None:
    if isinstance(node, exp.Literal):
        return str(node.this)
    return None


def _paths_of(node: exp.Expr | None) -> list[list[str]]:
    """Segment lists for a path argument: a parsed JSONPath, a `'$.a.b[0]'` literal, a
    bare key (`->>'a'`), an index (`->>0`), or a list of paths."""
    if node is None:
        return [[]]
    if isinstance(node, exp.JSONPath):
        segments: list[str] = []
        for part in node.expressions:
            if isinstance(part, exp.JSONPathRoot):
                continue
            if isinstance(part, exp.JSONPathKey):
                if not isinstance(part.this, str) or not _KEY.fullmatch(part.this):
                    break  # a wildcard or odd key: keep what came before it
                segments.append(part.this)
            elif isinstance(part, (exp.JSONPathSubscript, exp.JSONPathSlice)):
                segments.append("[]")
            else:
                break
        return [segments]
    if isinstance(node, exp.Array):
        return [p for e in node.expressions for p in _paths_of(e)]
    if isinstance(node, exp.Literal):
        if not node.is_string:
            return [["[]"]]
        return [_parse_path_literal(str(node.this))]
    return []  # a computed path: nothing can be said about it


def _parse_path_literal(text: str) -> list[str]:
    text = text.strip()
    if not text.startswith("$"):
        if text.isdigit():
            return ["[]"]
        return [text] if _KEY.fullmatch(text) else []
    segments: list[str] = []
    for m in re.finditer(r"\.\"?([A-Za-z0-9_-]+)\"?|\[[^\]]*\]|(\S)", text[1:]):
        if m.group(1):
            segments.append(m.group(1))
        elif m.group(0).startswith("["):
            inner = m.group(0)[1:-1].strip().strip("'\"")
            segments.append(
                "[]" if not inner or not _KEY.fullmatch(inner) or inner.isdigit() else inner
            )
        else:
            break
    return segments


def _base(node: exp.Expr | None) -> tuple[exp.Column, list[str]] | None:
    """The column a JSON argument comes from, and the path already applied to it:
    `p->'a'` passed on to `->>'b'` is column `p` with prefix `a`."""
    while isinstance(node, (exp.Paren, exp.Cast)):
        node = node.this
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Identifier):
        return node, []
    if node is None or not _is_json_call(node):
        return None
    inner = _base(_json_argument(node))
    if inner is None:
        return None
    name = _anonymous_name(node)
    # Only calls that still return JSON carry a prefix on: an extract, a parse, a cast.
    passes_json = isinstance(node, (exp.JSONExtract, exp.JSONBExtract, exp.ParseJSON)) or name in {
        "json_extract",
        "json_extract_path",
        "json_query",
        "json",
        "parse_json",
        "try_parse_json",
    }
    if not passes_json:
        return None
    paths = _path_arguments(node)
    if len(paths) != 1:
        return None
    return inner[0], inner[1] + paths[0]


_CAST_LEAF = {
    exp.DType.BOOLEAN: "boolean",
    exp.DType.DATE: "date",
}


def _cast_leaf(dtype: exp.DataType) -> str | None:
    t = dtype.this
    if t in exp.DataType.INTEGER_TYPES:
        return "integer"
    if t in exp.DataType.REAL_TYPES:
        return "number"
    if t in exp.DataType.TEMPORAL_TYPES and t.name.startswith(("TIMESTAMP", "DATETIME")):
        return "timestamp"
    if t in exp.DataType.TEXT_TYPES:
        return "string"
    return _CAST_LEAF.get(t)


def _leaf_type(node: exp.Expr) -> str | None:
    """What a cast around a JSON read implies about the value: `cast(nullif(x, '') as
    numeric)` reads a number. Seen through `nullif`, `coalesce`, `trim` and parentheses."""
    child, parent = node, node.parent
    while parent is not None:
        if isinstance(parent, exp.Cast) and parent.this is child:
            return _cast_leaf(parent.to) if isinstance(parent.to, exp.DataType) else None
        if isinstance(parent, (exp.Paren, exp.Coalesce, exp.Trim)) or (
            isinstance(parent, exp.Nullif) and parent.this is child
        ):
            child, parent = parent, parent.parent
            continue
        return None
    return None


def path_text(segments: list[str]) -> str:
    """`["charges", "data", "[]", "rate"]` -> `charges.data[].rate`."""
    out = ""
    for seg in segments:
        if seg == "[]":
            out += "[]"
        else:
            out += f".{seg}" if out else seg
    return out


def json_reads_in_tree(tree: exp.Expr) -> dict[str, Shape]:
    """{column name: the paths read from it} for every column a JSON function reads in
    parsed SQL, by name only: which table it belongs to is `schema.json_reads`'s question.

    A JSON call nested in another (`p->'a'->>'b'`) reads one path, `a.b`; the inner `a`
    is recorded too, as an object. A cast to JSON (`p::json`) reads the whole value."""
    out: dict[str, Shape] = {}
    for node in tree.walk():
        if _is_json_cast(node):
            found = _base(node.this)
            if found is not None and not _feeds_json(node):
                merge_shape(out.setdefault(found[0].name.lower(), {}), {path_text(found[1]): None})
            continue
        if not _is_json_call(node):
            continue
        found = _base(_json_argument(node))
        if found is None:
            continue
        if _feeds_json(node):
            continue  # the outer call reads the whole path, this one's included
        col, prefix = found
        leaf = _leaf_type(node)
        shape: Shape = {}
        for segments in _path_arguments(node):
            shape[path_text(prefix + segments)] = leaf if segments or prefix else None
        merge_shape(out.setdefault(col.name.lower(), {}), shape)
    return out


def is_json_argument(col: exp.Column) -> bool:
    """Whether a column occurrence is what a JSON function reads (directly, or through a
    cast): it holds text, whatever its name says."""
    return _feeds_json(col)


def _feeds_json(node: exp.Expr) -> bool:
    """Whether `node` is the JSON argument of a JSON call or cast, seen through casts."""
    parent = node.parent
    while isinstance(parent, (exp.Paren, exp.Cast)) and parent.this is node:
        if _is_json_cast(parent):
            return True
        node, parent = parent, parent.parent
    return parent is not None and _is_json_call(parent) and _json_argument(parent) is node


# --- the shape --------------------------------------------------------------------------


def merge_shape(into: Shape, other: Shape) -> Shape:
    """Add `other`'s paths to `into`; on a leaf-type conflict the type earlier in
    `LEAF_TYPES` wins, and a known type wins over none."""
    for path, leaf in other.items():
        current = into.get(path)
        if path not in into or current is None:
            into[path] = leaf
        elif leaf is not None and LEAF_TYPES.index(leaf) < LEAF_TYPES.index(current):
            into[path] = leaf
    return into


def format_note(shape: Shape) -> str:
    """`JSON, keys read: address.city, amount (number)`, or `JSON` with no path read."""
    keys = []
    for path in sorted(p for p in shape if p):
        leaf = shape[path]
        keys.append(f"{path} ({leaf})" if leaf else path)
    return "JSON, keys read: " + ", ".join(keys) if keys else "JSON"


_NOTE = re.compile(r"(?:^|;\s*)JSON(?:, keys read: (?P<keys>[^;]*))?\s*(?=;|$)")
_NOTE_KEY = re.compile(r"^(?P<path>\S+?)(?: \((?P<leaf>[a-z]+)\))?$")


def parse_note(text: str | None) -> Shape | None:
    """The shape a column note written by `format_note` describes, or None when the note
    says nothing about JSON. Other prose in the note, separated by `; `, is ignored."""
    if not text:
        return None
    m = _NOTE.search(text.strip())
    if m is None:
        return None
    shape: Shape = {"": None}
    for entry in (m.group("keys") or "").split(","):
        km = _NOTE_KEY.match(entry.strip())
        if km is None or not _PATH_TEXT.match(km.group("path")):
            continue
        leaf = km.group("leaf")
        shape[km.group("path")] = leaf if leaf in LEAF_TYPES else None
    return shape


# --- the values -------------------------------------------------------------------------


class _Node:
    __slots__ = ("keys", "items", "leaf")

    def __init__(self) -> None:
        self.keys: dict[str, _Node] = {}
        self.items: _Node | None = None
        self.leaf: str | None = None


def _segments(path: str) -> list[str]:
    out: list[str] = []
    for part in path.split(".") if path else []:
        key = part.replace("[]", "")
        if key:
            out.append(key)
        out += ["[]"] * part.count("[]")
    return out


def _tree(shape: Shape) -> _Node:
    root = _Node()
    for path in sorted(shape):
        node = root
        for seg in _segments(path):
            if seg == "[]":
                node.items = node.items or _Node()
                node = node.items
            else:
                node = node.keys.setdefault(seg, _Node())
        if shape[path] is not None:
            node.leaf = merge_shape({"": node.leaf}, {"": shape[path]})[""]
    return root


def _hash(*parts: object) -> int:
    key = ":".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big")


_EPOCH = datetime(2024, 1, 1)


def _scalar(leaf: str | None, name: str, h: int) -> object:
    if leaf == "integer":
        return h % 1000
    if leaf == "number":
        return round((h % 100_000) / 100, 2)
    if leaf == "boolean":
        return h % 2 == 0
    if leaf == "date":
        return (date(2024, 1, 1) + timedelta(days=h % 730)).isoformat()
    if leaf == "timestamp":
        return (_EPOCH + timedelta(seconds=h % (730 * 86_400))).isoformat(sep=" ")
    return f"{name or 'value'}_{h % 1000}"


def _value(node: _Node, name: str, key: str) -> object:
    h = _hash(key)
    if node.keys:  # an object wins over an array or a leaf at the same path
        return {k: _value(child, k, f"{key}.{k}") for k, child in sorted(node.keys.items())}
    if node.items is not None:
        return [_value(node.items, name, f"{key}[{i}]") for i in range(1 + h % 3)]
    return _scalar(node.leaf, name, h)


def json_values(
    shape: Shape, table: str, column: str, seed: int, present: list[bool]
) -> list[str | None]:
    """One JSON text per row, None where `present` is False (the column's own nulls).

    Every path in `shape` is in every object, an array has one to three elements, and a
    leaf is a scalar of its type. Derived from the seed, the table and the column alone,
    so the same input gives byte-identical output; the shape's root is always an object
    unless the only thing read is an array at the root."""
    root = _tree(shape)
    out: list[str | None] = []
    for i, keep in enumerate(present):
        if not keep:
            out.append(None)
            continue
        value = _value(root, column, f"{seed}:{table}:{column}:{i}")
        if not root.keys and root.items is None:
            value = {}
        out.append(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return out
