select id as customer_id, name as customer_name, country from {{ ref('raw_customers') }}
