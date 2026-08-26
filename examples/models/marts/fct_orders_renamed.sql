SELECT
    order_id,
    customer_id,
    revenue AS amount
FROM {{ ref('stg_orders') }}