with products as (

    select * from {{ ref('stg_webshop__products') }}

),

final as (

    select
        product_id,
        sku,
        -- The source has no product name; the catalogue is described by attributes.
        -- Compose a human name once, here, so every chart labels products the same way.
        {{ title_case('coffee_origin') }} || ' · ' || {{ title_case('roast_level') }} || ' roast · '
            || {{ title_case('product_category') }} || ' ' || cast(weight_grams as string) || 'g'
            as product_name,
        coffee_origin,
        roast_level,
        product_category,
        weight_grams,
        {{ cents_to_eur('unit_price_cents') }} as unit_price_eur,
        {{ cents_to_eur('cost_cents') }} as unit_cost_eur,
        {{ cents_to_eur('unit_price_cents - cost_cents') }} as unit_margin_eur,
        is_active,
        created_at as listed_at
    from products

)

select * from final
