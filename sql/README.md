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
python -m automation_layer sql 03_working_days_to_close
```

The result is printed and saved to `output/sql/<name>.csv`.

| # | Query | The question | The technique |
|---|---|---|---|
| 01 | [01_winback_customers.sql](01_winback_customers.sql) | The active-customer count is steady. Who left and came back underneath it? | `LAG` over each customer's own active days (gaps and islands) |
| 02 | [02_normal_for_this_weekday.sql](02_normal_for_this_weekday.sql) | Today is down 46% on yesterday. Is that a problem, or just a Friday? | A window frame per weekday: `ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING` |
| 03 | [03_working_days_to_close.sql](03_working_days_to_close.sql) | The promise is 3 working days. How many orders really missed it? | A calendar per row: `CROSS JOIN LATERAL generate_series(...)` minus days off and holidays |

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

## 03 · How many working days did it really take?

A promise made in working days is usually measured in calendar days, because that is what
`closed_on - opened_on` returns. Every weekly day off and every holiday in between is then charged
to the team. SQL has no calendar to subtract them with, so the query builds one for each order:

```sql
CROSS JOIN LATERAL (
  SELECT COUNT(*) AS working_days
  FROM generate_series(o.opened_on + 1, o.closed_on, INTERVAL '1 day') AS g (day)
  WHERE EXTRACT(dow FROM g.day) <> 5                      -- the weekly day off
    AND g.day::date NOT IN (SELECT day FROM holidays)     -- the holidays
) w
```

Three details carry it:

1. **`generate_series`** turns two dates into one row per day in between: the calendar SQL lacks.
2. **`LATERAL`** lets the subquery read the current order's own dates, so each order gets its own
   calendar. A plain subquery in `FROM` cannot see the row beside it.
3. **Holidays are data, not code.** They sit in one small list, so next year is an edit to a table,
   not to every report.

On the synthetic data (Friday is the weekly day off, plus two holidays), with a promise of
3 working days ([output/sql/03_working_days_to_close.csv](../output/sql/03_working_days_to_close.csv)):

```
region     orders_closed  late_by_calendar  late_by_working_days  wrongly_marked_late
Metro             23,779        1.9%               0.8%                   250
Regional           9,546       27.0%              17.4%                   913
Suburban           8,199       10.0%               5.5%                   363
All               41,524        9.2%               5.6%                 1,526
```

1,526 of the 3,840 orders a calendar count marks late were inside the promise.

Two cautions. The synthetic closing dates are not aware of days off, so this shows the difference
between the two ways of measuring, not a claim about how any team works. And at large volumes a
stored calendar table joined on a date range does the same job with less work per row.

The same pattern, other tables:

- **Support**: resolution-time promises that exclude weekends
- **Banking and payments**: settlement in T+2 business days
- **HR**: leave days taken, net of weekends and public holidays
- **Procurement and finance**: supplier lead times and payment terms
- **Legal and compliance**: response deadlines counted in business days
