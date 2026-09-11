with order_items as (

    select * from {{ ref('stg_webshop__order_items') }}

),

orders as (

    select * from {{ ref('stg_webshop__orders') }}

),

final as (

    select
        order_items.order_item_id,
        order_items.order_id,
        order_items.product_id,
        orders.customer_id,
        orders.ordered_at,
        orders.order_status,
        orders.sales_channel,
        order_items.quantity,
        {{ cents_to_eur('order_items.unit_price_cents') }} as unit_price_eur,
        {{ cents_to_eur('order_items.quantity * order_items.unit_price_cents') }} as gross_amount_eur,
        {{ cents_to_eur('order_items.discount_cents') }} as discount_eur,
        {{ cents_to_eur('order_items.quantity * order_items.unit_price_cents - order_items.discount_cents') }}
            as net_amount_eur
    from order_items
    inner join orders on order_items.order_id = orders.order_id

)

select * from final
