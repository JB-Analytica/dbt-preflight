"""Which conventions preflight checks, and how hard.

The JB Analytica house rules are one preset. A project switches rules off, softens them to
warnings, or replaces the layer patterns with its own, from a `conventions:` block in
`.dbt-preflight.yml`:

    conventions:
      preset: jba            # jba (default) or none
      rules:
        description: off     # off | warn | error
        column_naming: warn
      layers:                # folder under models/ -> regex a model name must match
        staging: "^stg_[a-z0-9]+__[a-z0-9_]+$"
        marts: "^(dim|fct|rpt)_[a-z0-9_]+$"
      source_layer: staging  # the only folder allowed to read source(); null to allow any
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

OFF = "off"
WARN = "warn"
ERROR = "error"
SEVERITIES = (OFF, WARN, ERROR)

RULES = ("naming", "layering", "description", "primary_key", "column_naming")


@dataclass
class ConventionSet:
    severities: dict[str, str] = field(default_factory=dict)  # rule -> off | warn | error
    layers: dict[str, str] = field(default_factory=dict)  # folder -> regex for model names
    hints: dict[str, str] = field(default_factory=dict)  # folder -> how a name should look
    source_layer: str | None = None  # folder allowed to read source(); None allows any
    timestamp_suffix: str = "_at"
    date_suffix: str = "_date"
    boolean_prefixes: tuple[str, ...] = ("is_", "has_")

    def severity(self, rule: str) -> str:
        return self.severities.get(rule, OFF)

    def enabled(self, rule: str) -> bool:
        return self.severity(rule) != OFF

    def pattern(self, layer: str) -> re.Pattern[str] | None:
        regex = self.layers.get(layer)
        return re.compile(regex) if regex else None

    @property
    def any_enabled(self) -> bool:
        return any(self.enabled(r) for r in RULES)


def jba() -> ConventionSet:
    """The JB Analytica warehouse conventions."""
    return ConventionSet(
        severities={
            "naming": ERROR,
            "layering": ERROR,
            "description": WARN,
            "primary_key": ERROR,
            "column_naming": WARN,
        },
        layers={
            "staging": r"^stg_[a-z0-9]+__[a-z0-9_]+$",
            "intermediate": r"^int_[a-z0-9_]+__[a-z0-9_]+$",
            "marts": r"^(dim|fct)_[a-z0-9_]+$",
        },
        hints={
            "staging": "stg_<source>__<entity>",
            "intermediate": "int_<entity>__<verb>",
            "marts": "dim_<entity> or fct_<event>",
        },
        source_layer="staging",
    )


def soften(conventions: ConventionSet) -> ConventionSet:
    """The same rules, none of them fatal: every `error` becomes `warn`."""
    conventions.severities = {
        rule: (WARN if sev == ERROR else sev) for rule, sev in conventions.severities.items()
    }
    return conventions


def none() -> ConventionSet:
    """No conventions at all: every rule off."""
    return ConventionSet(severities=dict.fromkeys(RULES, OFF))


PRESETS = {"jba": jba, "none": none}


class ConventionError(ValueError):
    pass


def from_config(raw: object, configured: bool = True) -> ConventionSet:
    """A ConventionSet from the `conventions:` block of the config file (or None).

    `configured` is whether a config file exists at all. A project that never wrote one did
    not sign up for anyone's conventions, so the house rules run as warnings there; a
    project with a config file gets them at full strength unless it says otherwise.
    """
    if raw is None:
        return jba() if configured else soften(jba())
    if not isinstance(raw, dict):
        raise ConventionError("`conventions` must be a mapping.")
    unknown = sorted(set(raw) - {"preset", "rules", "layers", "source_layer"})
    if unknown:
        raise ConventionError(f"Unknown keys under `conventions`: {', '.join(unknown)}.")

    preset = str(raw.get("preset", "jba")).lower()
    if preset not in PRESETS:
        raise ConventionError(
            f"`conventions.preset` must be one of {', '.join(PRESETS)}, got `{preset}`."
        )
    conventions = PRESETS[preset]()

    rules = raw.get("rules") or {}
    if not isinstance(rules, dict):
        raise ConventionError("`conventions.rules` must map rule names to off, warn or error.")
    for rule, sev in rules.items():
        if rule not in RULES:
            raise ConventionError(
                f"Unknown rule `{rule}` under `conventions.rules`; known: {', '.join(RULES)}."
            )
        sev_text = str(sev).lower() if not isinstance(sev, bool) else (ERROR if sev else OFF)
        if sev_text not in SEVERITIES:
            raise ConventionError(f"`conventions.rules.{rule}` must be off, warn or error.")
        conventions.severities[rule] = sev_text

    layers = raw.get("layers")
    if layers is not None:
        if not isinstance(layers, dict):
            raise ConventionError("`conventions.layers` must map folder names to regexes.")
        conventions.layers = {}
        conventions.hints = {}
        for folder, regex in layers.items():
            if not isinstance(regex, str):
                raise ConventionError(f"`conventions.layers.{folder}` must be a regex string.")
            try:
                re.compile(regex)
            except re.error as exc:
                raise ConventionError(
                    f"`conventions.layers.{folder}` is not a valid regex: {exc}"
                ) from None
            conventions.layers[str(folder)] = regex
            conventions.hints[str(folder)] = f"a name matching `{regex}`"

    if "source_layer" in raw:
        value = raw["source_layer"]
        if value is not None and not isinstance(value, str):
            raise ConventionError("`conventions.source_layer` must be a folder name or null.")
        conventions.source_layer = value

    return conventions
