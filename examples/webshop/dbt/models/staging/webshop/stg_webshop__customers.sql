with source as (

    select * from {{ source('webshop', 'customers') }}

),

renamed as (

    select
        id as customer_id,
        first_name,
        last_name,
        lower(email) as email,
        phone,
        city,
        country,
        segment as customer_segment,
        is_marketing_opt_in,
        created_at,
        updated_at,
        _dlt_load_id as dlt_load_id
    from source

)

select * from renamed
