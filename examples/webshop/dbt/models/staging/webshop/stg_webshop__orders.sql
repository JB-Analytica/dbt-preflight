with source as (

    select * from {{ source('webshop', 'orders') }}

),

renamed as (

    select
        id as order_id,
        customer_id,
        status as order_status,
        channel as sales_channel,
        ordered_at,
        shipped_at,
        delivered_at,
        shipping_city,
        shipping_country,
        created_at,
        updated_at,
        _dlt_load_id as dlt_load_id
    from source

)

select * from renamed
