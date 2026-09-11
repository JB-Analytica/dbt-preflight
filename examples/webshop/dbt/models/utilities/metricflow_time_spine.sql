{#- The dbt semantic layer needs a day-grain time spine to exist, even for metrics that never
    use one. A recursive CTE keeps it free of warehouse-specific date generators. -#}

{{ config(materialized='table') }}

with recursive days as (

    select cast('2024-01-01' as date) as date_day

    union all

    select date_day + interval 1 day
    from days
    where date_day < cast('2027-12-31' as date)

)

select cast(date_day as date) as date_day from days
