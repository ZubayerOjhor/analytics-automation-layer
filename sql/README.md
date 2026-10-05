# SQL notebook

Queries that answer what a plain `GROUP BY` cannot. Each one is a single file, runs on the
synthetic cache this project builds, and is written so the pattern can be lifted onto any table.

```bash
python -m automation_layer generate
python -m automation_layer run
python -m automation_layer next-day
python -m automation_layer run
python -m automation_layer sql 01_winback_customers
python -m automation_layer sql 02_normal_for_this_weekday
```

The result is printed and saved to `output/sql/<name>.csv`.

| # | Query | The question | The technique |
|---|---|---|---|
| 01 | [01_winback_customers.sql](01_winback_customers.sql) | The active-customer count is steady. Who left and came back underneath it? | `LAG` over each customer's own active days (gaps and islands) |
| 02 | [02_normal_for_this_weekday.sql](02_normal_for_this_weekday.sql) | Today is down 46% on yesterday. Is that a problem, or just a Friday? | A window frame per weekday: `ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING` |

## 01 · Customers who went quiet and came back

A steady count can mean nothing changed. It can also mean some customers went quiet while others
returned, and those are two very different periods. Counting activity cannot tell them apart,
because the signal is the silence between two activities.

Two steps make it work:

1. **Collapse to one row per customer per active day.** Without this, ten orders on the same day
   look like ten separate returns.
2. **`LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date)`** puts each customer's
   previous active day beside the current one. The difference is the length of the silence.

On the synthetic data (after the commands above) weekly active customers stay between 252 and 283.
Inside that steady line the query finds **21 comebacks by 20 customers**, each after 14 or more
silent days ([output/sql/01_winback_customers.csv](../output/sql/01_winback_customers.csv)):

```
customer_id  went_quiet_on  came_back_on  days_silent
       1175     2026-01-11    2026-01-25           14
       1262     2026-01-12    2026-01-26           14
       1147     2026-01-13    2026-01-27           14
       1023     2026-01-12    2026-01-28           16
        ...
       1138     2026-01-26    2026-03-03           36
```

The same pattern, other tables:

- **Subscriptions**: users who return after a lapsed plan
- **Banking**: dormant accounts that become active again
- **Retail**: shoppers reactivated by a campaign
- **Healthcare**: patients who missed follow-ups, then returned
- **Any event stream**: gaps in sensor readings, logins or payments

Change the `14` to your own definition of quiet, and replace the first CTE with your table.

## 02 · Is today really worse than normal?

"Down 46% on yesterday" only means something if yesterday was a fair comparison. In this dataset
Fridays are quiet every week, so two common yardsticks raise an alarm every Friday:

- **Yesterday** is a different weekday.
- **The last 7 days** average six ordinary days and one quiet one.

A fair baseline is the same weekday in the weeks before it:

```sql
AVG(orders) OVER (
  PARTITION BY EXTRACT(dow FROM day)          -- Fridays with Fridays
  ORDER BY day
  ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING    -- the last four, never today itself
)
```

Three details carry it:

1. **`PARTITION BY` the weekday**, so each day is compared with its own kind.
2. **The frame ends at `1 PRECEDING`.** If it included the current row, a bad day would pull its
   own baseline down and partly hide itself.
3. **Four weeks, not one.** Comparing only with last week makes one odd week the yardstick.

On the synthetic data, Friday 27 February had 445 orders
([output/sql/02_normal_for_this_weekday.csv](../output/sql/02_normal_for_this_weekday.csv)):

```
compared with            baseline   difference
yesterday (Thursday)        823       -45.9%
the last 7 days             781.4     -43.1%
the last four Fridays       445.5      -0.1%
```

Over the 31 days that have a full baseline, a "20% below normal" rule fires four times under each
of the first two yardsticks, every time on a Friday, and not once under the third.

One caution: `ROWS` counts rows, not dates. The frame is only right when there is one row per day
with no days missing, so sparse data needs a calendar to join to first.

The same pattern, other tables:

- **Retail and restaurants**: weekend peaks that are not growth, Monday dips that are not decline
- **Support**: ticket volume by weekday, so a normal Monday is not escalated
- **Web and apps**: traffic and sign-ups with weekly cycles
- **Finance**: payment volumes around month ends (partition by day of month instead)
- **Operations**: per-site or per-region baselines, by adding the site to `PARTITION BY`
