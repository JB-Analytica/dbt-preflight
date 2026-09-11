{#- The source stores money as integer cents (the right call for an OLTP system).
    Marts expose euros because that is what a person reads; convert exactly once, here. -#}
{% macro cents_to_eur(column) -%}
    round({{ column }} / 100, 2)
{%- endmacro %}
