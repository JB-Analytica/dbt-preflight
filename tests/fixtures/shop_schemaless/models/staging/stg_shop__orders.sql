select
    id as order_id,
    customer_id,
    cast(placed as timestamp) as placed_at,
    total_amount
from {{ source('raw_shop', 'orders') }}
