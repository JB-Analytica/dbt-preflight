with source as (

    select * from {{ source('webshop', 'order_items') }}

),

renamed as (

    select
        id as order_item_id,
        order_id,
        product_id,
        quantity,
        unit_price_cents,
        discount_cents,
        _dlt_load_id as dlt_load_id
    from source

)

select * from renamed
