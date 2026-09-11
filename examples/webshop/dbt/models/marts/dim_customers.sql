with customers as (

    select * from {{ ref('stg_webshop__customers') }}

),

orders as (

    select * from {{ ref('stg_webshop__orders') }}
    where order_status != 'cancelled'

),

order_money as (

    select * from {{ ref('int_orders__items_aggregated') }}

),

customer_orders as (

    select
        orders.customer_id,
        count(*) as order_count,
        min(orders.ordered_at) as first_order_at,
        max(orders.ordered_at) as last_order_at,
        sum(order_money.net_amount_cents) as lifetime_net_amount_cents
    from orders
    inner join order_money on orders.order_id = order_money.order_id
    group by orders.customer_id

),

final as (

    select
        customers.customer_id,
        customers.first_name || ' ' || customers.last_name as full_name,
        customers.email,
        customers.phone,
        customers.city,
        customers.country,
        customers.customer_segment,
        customers.is_marketing_opt_in,
        customers.created_at as customer_since_at,
        customer_orders.first_order_at,
        customer_orders.last_order_at,
        coalesce(customer_orders.order_count, 0) as lifetime_order_count,
        {{ cents_to_eur('coalesce(customer_orders.lifetime_net_amount_cents, 0)') }}
            as lifetime_net_revenue_eur,
        customer_orders.order_count is not null as has_ordered
    from customers
    left join customer_orders on customers.customer_id = customer_orders.customer_id

)

select * from final
