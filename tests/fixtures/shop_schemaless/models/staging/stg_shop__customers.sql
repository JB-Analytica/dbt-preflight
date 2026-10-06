select
    id as customer_id,
    email,
    cast(signed_up as date) as signed_up_date,
    lifetime_value_eur
from {{ source('raw_shop', 'customers') }}
