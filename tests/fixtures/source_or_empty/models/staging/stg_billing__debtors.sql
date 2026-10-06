{#- The select list is built in a loop: raw SQL cannot say which columns it reads. -#}
{%- set columns = ['number', 'name'] -%}

with source as (

    {{ source_or_empty('raw_billing', 'debtors') }}

)

select
    {%- for column in columns %}
    cast({{ column }} as {{ dbt.type_string() }}) as debtor_{{ column }}{{ "," if not loop.last }}
    {%- endfor %}
from source
