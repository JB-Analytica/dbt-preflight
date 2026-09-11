{#- The two places the webshop marts needed warehouse-specific SQL. Each is one macro with
    `adapter.dispatch`, so the model reads the same on BigQuery and on the DuckDB that
    dbt-preflight runs it on. The default branch is the DuckDB / Postgres spelling. -#}

{% macro hours_between(later, earlier) -%}
    {{ return(adapter.dispatch('hours_between')(later, earlier)) }}
{%- endmacro %}

{% macro default__hours_between(later, earlier) -%}
    date_diff('hour', {{ earlier }}, {{ later }})
{%- endmacro %}

{% macro bigquery__hours_between(later, earlier) -%}
    timestamp_diff({{ later }}, {{ earlier }}, hour)
{%- endmacro %}


{% macro title_case(text) -%}
    {{ return(adapter.dispatch('title_case')(text)) }}
{%- endmacro %}

{% macro default__title_case(text) -%}
    (upper(substr({{ text }}, 1, 1)) || lower(substr({{ text }}, 2)))
{%- endmacro %}

{% macro bigquery__title_case(text) -%}
    initcap({{ text }})
{%- endmacro %}
