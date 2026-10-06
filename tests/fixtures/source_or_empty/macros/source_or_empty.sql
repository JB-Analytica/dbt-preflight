{#- The pattern from a real project: a source that may not have been loaded yet. When the
    relation exists this is a plain `select *`; when it does not, and the source declares
    typed columns, an empty stand-in of typed nulls. Either way preflight's raw-SQL reading
    never sees a `source()` call in the model. -#}
{% macro source_or_empty(source_name, table_name) %}
  {%- set relation = source(source_name, table_name) -%}
  {%- set ns = namespace(exists=true, columns=[]) -%}
  {%- if execute -%}
    {%- set found = adapter.get_relation(
        database=relation.database, schema=relation.schema, identifier=relation.identifier) -%}
    {%- set ns.exists = found is not none -%}
    {%- for node in graph.sources.values() -%}
      {%- if node.source_name == source_name and node.name == table_name -%}
        {%- for column in node.columns.values() if column.data_type -%}
          {%- do ns.columns.append(column) -%}
        {%- endfor -%}
      {%- endif -%}
    {%- endfor -%}
  {%- endif -%}
  {%- if ns.exists or ns.columns | length == 0 -%}
    {{ return("select * from " ~ relation) }}
  {%- else -%}
    {%- set select_list = [] -%}
    {%- for column in ns.columns -%}
      {%- do select_list.append("cast(null as " ~ column.data_type ~ ") as " ~ column.name) -%}
    {%- endfor -%}
    {{ return("select " ~ select_list | join(", ")
        ~ " from (select 1 as seed) as source_or_empty_seed where false") }}
  {%- endif -%}
{% endmacro %}
