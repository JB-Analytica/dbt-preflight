with orders as (

    select * from {{ ref('stg_webshop__orders') }}

),

order_money as (

    select * from {{ ref('int_orders__items_aggregated') }}

),

final as (

    select
        orders.order_id,
        orders.customer_id,
        orders.ordered_at,
        date(orders.ordered_at) as order_date,
        orders.order_status,
        orders.sales_channel,
        orders.shipping_city,
        orders.shipping_country,
        orders.shipped_at,
        orders.delivered_at,
        coalesce(order_money.line_item_count, 0) as line_item_count,
        coalesce(order_money.units, 0) as units,
        {{ cents_to_eur('coalesce(order_money.gross_amount_cents, 0)') }} as gross_amount_eur,
        {{ cents_to_eur('coalesce(order_money.discount_cents, 0)') }} as discount_eur,
        {{ cents_to_eur('coalesce(order_money.net_amount_cents, 0)') }} as net_amount_eur,
        orders.order_status = 'cancelled' as is_cancelled,
        orders.order_status = 'delivered' as is_delivered,
        case
            when orders.order_status in ('shipped', 'delivered') and orders.shipped_at is not null
                then {{ hours_between('orders.shipped_at', 'orders.ordered_at') }} / 24.0
        end as days_to_ship
    from orders
    left join order_money on orders.order_id = order_money.order_id

)

select * from final
