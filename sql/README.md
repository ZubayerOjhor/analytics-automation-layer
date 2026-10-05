# SQL notebook

Queries that answer what a plain `GROUP BY` cannot. Each one is a single file, runs on the
synthetic cache this project builds, and is written so the pattern can be lifted onto any table.

```bash
python -m automation_layer generate
python -m automation_layer run
python -m automation_layer next-day
python -m automation_layer run
python -m automation_layer sql 01_winback_customers
```

The result is printed and saved to `output/sql/<name>.csv`.

| # | Query | The question | The technique |
|---|---|---|---|
| 01 | [01_winback_customers.sql](01_winback_customers.sql) | The active-customer count is steady. Who left and came back underneath it? | `LAG` over each customer's own active days (gaps and islands) |

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
