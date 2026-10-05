-- 02 · Is today really worse than normal?
--
-- Question: Friday's orders are down 46% on Thursday. Is that a problem?
-- It depends on what "normal" means. Yesterday is a different weekday, and a 7-day average
-- mixes six ordinary days into the yardstick. A fair baseline is the same weekday in the
-- weeks before it, and a window frame builds exactly that:
--
--   PARTITION BY weekday                       Fridays are compared with Fridays
--   ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING   the last four of them, and never today itself
--
-- Stopping the frame at 1 PRECEDING matters: a frame that includes the current row lets a bad
-- day pull its own baseline down and partly hide.
--
-- Runs on DuckDB. For PostgreSQL replace dayname(day) with to_char(day, 'Day').
-- The frame counts rows, not dates, so it assumes one row per day with no days missing.

WITH daily_orders AS (             -- demo mapping: a parcel is an order
  SELECT created_at::date AS day, COUNT(*) AS orders
  FROM parcels
  GROUP BY 1
),
baselines AS (
  SELECT day, orders,
         LAG(orders) OVER (ORDER BY day)       AS yesterday,
         AVG(orders) OVER (ORDER BY day
                           ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS last_7_days,
         AVG(orders) OVER same_weekday         AS normal_for_this_weekday,
         COUNT(*)    OVER same_weekday         AS weeks_in_baseline
  FROM daily_orders
  WINDOW same_weekday AS (
    PARTITION BY EXTRACT(dow FROM day)
    ORDER BY day
    ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING
  )
)
SELECT day,
       dayname(day)                                            AS weekday,
       orders,
       ROUND(normal_for_this_weekday, 1)                       AS normal_for_this_weekday,
       ROUND(100.0 * (orders - yesterday) / yesterday, 1)      AS vs_yesterday_pct,
       ROUND(100.0 * (orders - last_7_days) / last_7_days, 1)  AS vs_last_7_days_pct,
       ROUND(100.0 * (orders - normal_for_this_weekday)
                   / normal_for_this_weekday, 1)               AS vs_same_weekday_pct
FROM baselines
WHERE weeks_in_baseline = 4        -- judge a day only once it has four earlier weeks to compare with
ORDER BY day;
