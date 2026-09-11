with source as (

    select * from {{ source('webshop', 'products') }}

),

renamed as (

    select
        id as product_id,
        sku,
        origin as coffee_origin,
        roast as roast_level,
        category as product_category,
        weight_grams,
        unit_price_cents,
        cost_cents,
        is_active,
        created_at,
        updated_at,
        _dlt_load_id as dlt_load_id
    from source

)

select * from renamed
