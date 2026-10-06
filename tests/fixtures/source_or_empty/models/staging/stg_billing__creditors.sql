with source as (

    {{ source_or_empty('raw_billing', 'creditors') }}

),

renamed as (

    select
        cast(id as {{ dbt.type_int() }}) as creditor_id,
        cast(creditor_number as {{ dbt.type_string() }}) as creditor_number,
        cast(name as {{ dbt.type_string() }}) as creditor_name
    from source

)

select * from renamed
