-- 03 · How many working days did it really take?
--
-- Question: the promise is "closed within 3 working days". How many orders missed it?
-- closed_on - opened_on counts calendar days, so every weekly day off and every holiday in
-- between is charged to the team. SQL has no calendar to subtract them with, so build one:
--
--   generate_series(opened_on + 1, closed_on, '1 day')   one row for each day the order was open
--   CROSS JOIN LATERAL                                   run once per order, with that order's own dates
--   WHERE ...                                            drop the weekly day off and the holidays
--
-- What is left to count is the working days. Runs on DuckDB and PostgreSQL.
-- In this synthetic calendar Friday is the weekly day off and there are two holidays.

WITH holidays (day) AS (
  VALUES (DATE '2026-01-28'), (DATE '2026-02-17')
),
orders AS (                        -- demo mapping: a parcel is an order, a hub region is a region
  SELECT p.parcel_id AS order_id, h.region,
         p.created_at::date AS opened_on,
         p.closed_at::date  AS closed_on
  FROM parcels p
  JOIN hubs h ON h.hub_id = p.hub_id
  WHERE p.closed_at IS NOT NULL
),
measured AS (
  SELECT o.order_id, o.region,
         o.closed_on - o.opened_on AS calendar_days,
         w.working_days
  FROM orders o
  CROSS JOIN LATERAL (             -- this order's own calendar, minus the days nobody works
    SELECT COUNT(*) AS working_days
    FROM generate_series(o.opened_on + 1, o.closed_on, INTERVAL '1 day') AS g (day)
    WHERE EXTRACT(dow FROM g.day) <> 5
      AND g.day::date NOT IN (SELECT day FROM holidays)
  ) w
)
SELECT region,
       COUNT(*)                                                      AS orders_closed,
       ROUND(AVG(calendar_days), 2)                                  AS avg_calendar_days,
       ROUND(AVG(working_days), 2)                                   AS avg_working_days,
       ROUND(100.0 * COUNT(*) FILTER (WHERE calendar_days > 3) / COUNT(*), 1) AS late_by_calendar_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE working_days > 3) / COUNT(*), 1)  AS late_by_working_days_pct,
       COUNT(*) FILTER (WHERE calendar_days > 3 AND working_days <= 3)        AS wrongly_marked_late
FROM measured
GROUP BY ROLLUP (region)
ORDER BY region NULLS LAST;
