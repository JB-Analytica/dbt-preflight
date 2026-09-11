"""Generate a synthetic dbt project of a configurable size, for scale testing preflight.

    uv run python scripts/big_project.py /tmp/big-project --total 120

Starts from a copy of `examples/webshop`'s `.dbt-preflight.yml`, `webshop.dbml` and the four
staging models (with their YAML), then adds intermediate models and marts on top, each
`ref()`-ing one or two existing models in a plausible DAG:

- **aggregate**: roll a model up to the grain of something it has a foreign key to (the
  shape of the bundled `int_orders__items_aggregated`).
- **join**: combine two models that already share a grain (same primary-key column name).
- **enrich**: pass a model through with one derived column, at the same grain.

Every generated model gets a description, a `unique` + `not_null` test on its primary key,
and a name that matches the house conventions: `int_<entity>__<verb>` for intermediate
models, `dim_<entity>` or `fct_<event>` for marts. The result is a project `dbt parse`
accepts and preflight builds clean with no base ref.

A git repository is created too, with everything committed to `main`, so
`--base-ref main` works against the output directory right away.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "webshop"

# Verbs cycled through when naming generated models, purely for readability -- uniqueness
# comes from the sequence number every name carries.
_AGG_VERBS = ["rollup", "summary", "aggregated", "totals", "stats", "counts"]
_JOIN_VERBS = ["combined", "joined", "matched", "paired"]
_ENRICH_VERBS = ["enriched", "flagged", "derived", "adjusted", "tagged", "scored"]

_DIM_PREFIX = "dim"
_FCT_PREFIX = "fct"


def _ref(model_name: str) -> str:
    return "{{ ref('" + model_name + "') }}"


@dataclass
class ModelSpec:
    name: str
    layer: str  # "staging" | "intermediate" | "marts"
    pk_column: str
    entity: str
    # column name -> name of the pool model whose grain that column identifies.
    fk_columns: dict[str, str] = field(default_factory=dict)
    # columns in this model's own output that a later model can sum/average.
    numeric_columns: list[str] = field(default_factory=list)


def _seed_pool() -> list[ModelSpec]:
    """The four bundled staging models, described the way generation needs them."""
    return [
        ModelSpec(
            name="stg_webshop__customers",
            layer="staging",
            pk_column="customer_id",
            entity="customer",
        ),
        ModelSpec(
            name="stg_webshop__products",
            layer="staging",
            pk_column="product_id",
            entity="product",
            numeric_columns=["unit_price_cents", "cost_cents", "weight_grams"],
        ),
        ModelSpec(
            name="stg_webshop__orders",
            layer="staging",
            pk_column="order_id",
            entity="order",
            fk_columns={"customer_id": "stg_webshop__customers"},
        ),
        ModelSpec(
            name="stg_webshop__order_items",
            layer="staging",
            pk_column="order_item_id",
            entity="order_item",
            fk_columns={
                "order_id": "stg_webshop__orders",
                "product_id": "stg_webshop__products",
            },
            numeric_columns=["quantity", "unit_price_cents", "discount_cents"],
        ),
    ]


def _pick_aggregate(pool: list[ModelSpec], rng) -> tuple | None:
    candidates = [(m, col, target) for m in pool for col, target in m.fk_columns.items()]
    if not candidates:
        return None
    child, fk_col, target_name = rng.choice(candidates)
    target = next(p for p in pool if p.name == target_name)
    metric = rng.choice(child.numeric_columns) if child.numeric_columns else None
    return child, fk_col, target, metric


def _pick_join(pool: list[ModelSpec], rng) -> tuple | None:
    by_pk: dict[str, list[ModelSpec]] = {}
    for m in pool:
        by_pk.setdefault(m.pk_column, []).append(m)
    groups = [g for g in by_pk.values() if len(g) >= 2]
    if not groups:
        return None
    a, b = rng.sample(rng.choice(groups), 2)
    return a, b


# The three generation strategies below return (ModelSpec, sql, description).


def _make_aggregate(seq: int, layer: str, picked: tuple, rng) -> tuple[ModelSpec, str, str]:
    child, fk_col, target, metric = picked
    verb = rng.choice(_AGG_VERBS)
    name = f"int_{target.entity}__{verb}_{seq:04d}"
    count_col = f"{child.entity}_count"
    lines = [
        "with source as (",
        "",
        f"    select * from {_ref(child.name)}",
        "",
        "),",
        "",
        "aggregated as (",
        "",
        "    select",
        f"        {fk_col} as {target.pk_column},",
        f"        count(*) as {count_col}",
    ]
    numeric_columns = [count_col]
    if metric is not None:
        sum_col = f"{metric}_sum"
        lines[-1] += ","
        lines.append(f"        sum({metric}) as {sum_col}")
        numeric_columns.append(sum_col)
    lines += [
        "    from source",
        f"    group by {fk_col}",
        "",
        ")",
        "",
        "select * from aggregated",
        "",
    ]
    sql = "\n".join(lines)
    description = f"`{child.name}` rolled up to the `{target.pk_column}` grain: row counts" + (
        f" and `{metric}` summed." if metric else "."
    )
    spec = ModelSpec(
        name=name,
        layer=layer,
        pk_column=target.pk_column,
        entity=target.entity,
        fk_columns={},
        numeric_columns=numeric_columns,
    )
    return spec, sql, description


def _make_join(seq: int, layer: str, picked: tuple, rng) -> tuple[ModelSpec, str, str]:
    a, b = picked
    verb = rng.choice(_JOIN_VERBS)
    name = f"int_{a.entity}__{verb}_{seq:04d}"
    select_lines = [f"        a.{a.pk_column}"]
    numeric_columns: list[str] = []
    if a.numeric_columns:
        col = rng.choice(a.numeric_columns)
        alias = f"a_{col}_{seq:04d}"
        select_lines.append(f"        a.{col} as {alias}")
        numeric_columns.append(alias)
    if b.numeric_columns:
        col = rng.choice(b.numeric_columns)
        alias = f"b_{col}_{seq:04d}"
        select_lines.append(f"        b.{col} as {alias}")
        numeric_columns.append(alias)
    select_sql = ",\n".join(select_lines)
    sql = (
        "select\n"
        f"{select_sql}\n"
        f"from {_ref(a.name)} as a\n"
        f"inner join {_ref(b.name)} as b on a.{a.pk_column} = b.{b.pk_column}\n"
    )
    description = f"`{a.name}` joined with `{b.name}` at the `{a.pk_column}` grain."
    spec = ModelSpec(
        name=name,
        layer=layer,
        pk_column=a.pk_column,
        entity=a.entity,
        fk_columns={},
        numeric_columns=numeric_columns,
    )
    return spec, sql, description


def _make_enrich(seq: int, layer: str, parent: ModelSpec, rng) -> tuple[ModelSpec, str, str]:
    verb = rng.choice(_ENRICH_VERBS)
    name = f"int_{parent.entity}__{verb}_{seq:04d}"
    select_cols = [parent.pk_column, *parent.fk_columns.keys()]
    numeric_columns: list[str] = []
    if parent.numeric_columns:
        col = rng.choice(parent.numeric_columns)
        derived = f"{col}_adj_{seq:04d}"
        derived_expr = f"round({col} * 1.1, 2) as {derived}"
        numeric_columns.append(derived)
    else:
        derived = f"is_flagged_{seq:04d}"
        derived_expr = f"{parent.pk_column} is not null as {derived}"
    select_sql = ",\n".join(f"    {c}" for c in select_cols) + f",\n    {derived_expr}"
    sql = f"select\n{select_sql}\nfrom {_ref(parent.name)}\n"
    description = f"`{parent.name}`, passed through with one derived column."
    spec = ModelSpec(
        name=name,
        layer=layer,
        pk_column=parent.pk_column,
        entity=parent.entity,
        fk_columns=dict(parent.fk_columns),
        numeric_columns=numeric_columns,
    )
    return spec, sql, description


def _generate_model(seq: int, layer: str, pool: list[ModelSpec], rng) -> tuple[ModelSpec, str, str]:
    weighted: list[tuple[str, float]] = []
    agg = _pick_aggregate(pool, rng)
    if agg is not None:
        weighted.append(("aggregate", 0.45))
    join = _pick_join(pool, rng)
    if join is not None:
        weighted.append(("join", 0.35))
    weighted.append(("enrich", 0.20))

    total_weight = sum(w for _, w in weighted)
    roll = rng.random() * total_weight
    upto = 0.0
    mode = weighted[-1][0]
    for name, weight in weighted:
        upto += weight
        if roll <= upto:
            mode = name
            break

    if mode == "aggregate":
        assert agg is not None
        spec, sql, description = _make_aggregate(seq, layer, agg, rng)
    elif mode == "join":
        assert join is not None
        spec, sql, description = _make_join(seq, layer, join, rng)
    else:
        parent = rng.choice(pool)
        spec, sql, description = _make_enrich(seq, layer, parent, rng)

    if layer == "marts":
        prefix = _FCT_PREFIX if mode == "aggregate" else _DIM_PREFIX
        spec.name = f"{prefix}_{spec.entity}_{seq:04d}"

    return spec, sql, description


def _model_yaml_entry(spec: ModelSpec, description: str) -> str:
    # json.dumps produces a double-quoted scalar YAML accepts as-is, so a description with
    # a backtick, colon or quote in it (all of ours have backticks) never breaks the parser.
    lines = [
        f"  - name: {spec.name}",
        f"    description: {json.dumps(description)}",
        "    columns:",
        f"      - name: {spec.pk_column}",
        "        description: Primary key.",
        "        data_tests: [unique, not_null]",
    ]
    return "\n".join(lines) + "\n"


def _copy_seed(out_dir: Path) -> None:
    """The pieces generation starts from: config, schema, and the four staging models."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    shutil.copy2(EXAMPLE / ".dbt-preflight.yml", out_dir / ".dbt-preflight.yml")
    shutil.copy2(EXAMPLE / "webshop.dbml", out_dir / "webshop.dbml")

    dbt_dir = out_dir / "dbt"
    staging_src = EXAMPLE / "dbt" / "models" / "staging" / "webshop"
    staging_dst = dbt_dir / "models" / "staging" / "webshop"
    staging_dst.mkdir(parents=True)
    for f in staging_src.iterdir():
        shutil.copy2(f, staging_dst / f.name)

    (dbt_dir / "dbt_project.yml").write_text(
        "\n".join(
            [
                "name: bigproject",
                'version: "1.0.0"',
                "config-version: 2",
                "profile: bigproject",
                "",
                'model-paths: ["models"]',
                'test-paths: ["tests"]',
                'target-path: "target"',
                'clean-targets: ["target", "dbt_packages"]',
                "",
                "flags:",
                "  send_anonymous_usage_stats: false",
                "",
                "models:",
                "  bigproject:",
                "    staging:",
                "      +schema: staging",
                "      +materialized: view",
                "    intermediate:",
                "      +schema: intermediate",
                "      +materialized: view",
                "    marts:",
                "      +schema: marts",
                "      +materialized: table",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (dbt_dir / "models" / "intermediate").mkdir(parents=True)
    (dbt_dir / "models" / "marts").mkdir(parents=True)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=big-project@example.com", "-c", "user.name=big-project", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def generate(
    out_dir: Path,
    total: int = 120,
    seed: int = 42,
    intermediate: int | None = None,
    marts: int | None = None,
) -> None:
    """Write a synthetic dbt project with roughly `total` models into `out_dir`."""
    import random

    rng = random.Random(seed)

    remaining = max(total - 4, 0)
    if intermediate is None or marts is None:
        intermediate = round(remaining * 0.65) if intermediate is None else intermediate
        marts = remaining - intermediate if marts is None else marts

    _copy_seed(out_dir)
    dbt_dir = out_dir / "dbt"

    pool = _seed_pool()
    seq = 1
    int_entries: list[str] = []
    marts_entries: list[str] = []

    for _ in range(intermediate):
        spec, sql, description = _generate_model(seq, "intermediate", pool, rng)
        (dbt_dir / "models" / "intermediate" / f"{spec.name}.sql").write_text(sql, encoding="utf-8")
        int_entries.append(_model_yaml_entry(spec, description))
        pool.append(spec)
        seq += 1

    for _ in range(marts):
        spec, sql, description = _generate_model(seq, "marts", pool, rng)
        (dbt_dir / "models" / "marts" / f"{spec.name}.sql").write_text(sql, encoding="utf-8")
        marts_entries.append(_model_yaml_entry(spec, description))
        pool.append(spec)
        seq += 1

    if int_entries:
        (dbt_dir / "models" / "intermediate" / "_generated__models.yml").write_text(
            "version: 2\n\nmodels:\n" + "".join(int_entries), encoding="utf-8"
        )
    if marts_entries:
        (dbt_dir / "models" / "marts" / "_generated__models.yml").write_text(
            "version: 2\n\nmodels:\n" + "".join(marts_entries), encoding="utf-8"
        )

    _git(out_dir, "init", "-q", "-b", "main")
    _git(out_dir, "add", ".")
    _git(out_dir, "commit", "-q", "-m", f"Synthetic project: {4 + intermediate + marts} models")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path, help="Directory to generate the project into.")
    parser.add_argument("--total", type=int, default=120, help="Total models, staging included.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--intermediate", type=int, default=None, help="Override the model count.")
    parser.add_argument("--marts", type=int, default=None, help="Override the mart count.")
    args = parser.parse_args()

    generate(
        args.out_dir.resolve(),
        total=args.total,
        seed=args.seed,
        intermediate=args.intermediate,
        marts=args.marts,
    )
    print(f"generated project at {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
