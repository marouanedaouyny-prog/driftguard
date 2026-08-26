SELECT
    order_id,
    customer_id,
    o.amount AS amount,
    status
FROM raw_orders o