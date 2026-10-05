-- 01 · Customers who went quiet and came back
--
-- Question: the active-customer count is steady. Is anything actually happening underneath?
-- COUNT and GROUP BY count activity; they cannot see the silence between two activities.
-- LAG can: it puts each customer's previous active day next to the current one.
--
-- Runs on DuckDB and PostgreSQL. To use it on your own data, replace the first CTE with
-- your table: one customer id and one activity timestamp are all it needs.

WITH orders AS (                   -- demo mapping: a parcel is an order, a merchant is a customer
  SELECT merchant_id AS customer_id, created_at AS order_date
  FROM parcels
),
active_days AS (                   -- one row per customer per active day,
  SELECT DISTINCT customer_id,     -- otherwise ten orders on one day look like ten returns
         order_date::date AS order_date
  FROM orders
),
gaps AS (                          -- when was this customer's previous active day?
  SELECT customer_id, order_date,
         LAG(order_date) OVER (
           PARTITION BY customer_id ORDER BY order_date
         ) AS previous_order_date
  FROM active_days
)
SELECT customer_id,
       previous_order_date              AS went_quiet_on,
       order_date                       AS came_back_on,
       order_date - previous_order_date AS days_silent
FROM gaps
WHERE order_date - previous_order_date >= 14     -- your own definition of "quiet"
ORDER BY came_back_on, customer_id;
