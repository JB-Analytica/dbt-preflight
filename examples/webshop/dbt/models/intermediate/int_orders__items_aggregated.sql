{#- Order-grain money. Discounts are per line, prices per unit, so the arithmetic lives here
    exactly once and both fct_orders and dim_customers read the result. -#}

with order_items as (

    select * from {{ ref('stg_webshop__order_items') }}

),

aggregated as (

    select
        order_id,
        count(*) as line_item_count,
        sum(quantity) as units,
        sum(quantity * unit_price_cents) as gross_amount_cents,
        sum(discount_cents) as discount_cents,
        sum(quantity * unit_price_cents - discount_cents) as net_amount_cents
    from order_items
    group by order_id

)

select * from aggregated
