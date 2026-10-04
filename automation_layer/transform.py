"""Join the cached systems and build the weekly report tables.

This is the query no single source could answer: parcels (operations) x hubs and
merchants (CRM) x invoices (billing) x targets (a file the business maintains).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from . import config as C

TREND_WEEKS = 8


@dataclass
class Report:
    as_of: datetime
    week_start: datetime          # report week = latest complete Monday-Sunday week
    week_end: datetime
    weekly: pd.DataFrame          # region x week
    hubs: pd.DataFrame            # report week vs previous, with target and flag
    merchants: pd.DataFrame       # report week vs previous, with drop flag
    headline: dict


def _ts(dt: datetime) -> str:
    return f"TIMESTAMP '{dt:%Y-%m-%d %H:%M:%S}'"


def build(con) -> Report:
    as_of = con.execute("SELECT MAX(updated_at) FROM parcels").fetchone()[0]
    if as_of is None:
        raise RuntimeError("The cache is empty. Run the refresh first.")
    this_monday = datetime.combine((as_of - timedelta(days=as_of.weekday())).date(), datetime.min.time())
    week_start, week_end = this_monday - timedelta(days=7), this_monday
    prev_start = week_start - timedelta(days=7)

    con.execute("CREATE OR REPLACE TEMP TABLE targets AS SELECT * FROM read_csv_auto(?)", [str(C.TARGETS_CSV)])
    con.execute("""
        CREATE OR REPLACE TEMP VIEW fact AS
        SELECT p.parcel_id, p.merchant_id, p.hub_id, p.created_at, p.closed_at, p.status, p.weight_g,
               h.hub_name, h.region, m.merchant_name, m.segment, m.account_manager,
               i.fee, i.paid, i.parcel_id IS NOT NULL AS invoiced,
               date_trunc('week', p.created_at) AS created_week,
               date_trunc('week', p.closed_at) AS closed_week
        FROM parcels p
        LEFT JOIN hubs h ON h.hub_id = p.hub_id
        LEFT JOIN merchants m ON m.merchant_id = p.merchant_id
        LEFT JOIN invoices i ON i.parcel_id = p.parcel_id
    """)

    weekly = con.execute(f"""
        WITH closed AS (
          SELECT closed_week AS week_start, region, COUNT(*) AS closed,
                 SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                 SUM(CASE WHEN status = 'returned' THEN 1 ELSE 0 END) AS returned,
                 AVG(date_diff('second', created_at, closed_at)) / 86400.0 AS avg_days_to_close,
                 COALESCE(SUM(fee), 0) AS revenue,
                 COALESCE(SUM(CASE WHEN paid = 0 THEN fee END), 0) AS unpaid
          FROM fact WHERE closed_at IS NOT NULL GROUP BY 1, 2
        ), created AS (
          SELECT created_week AS week_start, region, COUNT(*) AS created FROM fact GROUP BY 1, 2
        )
        SELECT CAST(c.week_start AS DATE) AS week_start, c.region, COALESCE(n.created, 0) AS created, c.closed,
               c.delivered, c.returned, ROUND(c.delivered * 1.0 / c.closed, 4) AS success_rate,
               ROUND(c.avg_days_to_close, 2) AS avg_days_to_close, ROUND(c.revenue, 0) AS revenue,
               ROUND(c.unpaid, 0) AS unpaid
        FROM closed c LEFT JOIN created n ON n.week_start = c.week_start AND n.region = c.region
        WHERE c.week_start < {_ts(week_end)} AND c.week_start >= {_ts(week_end - timedelta(weeks=TREND_WEEKS))}
        ORDER BY 1, 2
    """).df()

    hubs = con.execute(f"""
        WITH w AS (
          SELECT hub_id, hub_name, region, closed_week,
                 COUNT(*) AS closed, SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered
          FROM fact WHERE closed_week IN ({_ts(week_start)}, {_ts(prev_start)}) GROUP BY 1, 2, 3, 4
        )
        SELECT c.hub_name AS hub, c.region, c.closed, ROUND(c.delivered * 1.0 / c.closed, 4) AS success_rate,
               ROUND(p.delivered * 1.0 / p.closed, 4) AS last_week, t.success_rate_target AS target,
               ROUND((c.delivered * 1.0 / c.closed - t.success_rate_target) * 100, 1) AS gap_pp,
               (c.closed >= {C.MIN_HUB_PARCELS}
                 AND (t.success_rate_target - c.delivered * 1.0 / c.closed) * 100 > {C.HUB_GAP_PP}) AS flagged
        FROM w c
        LEFT JOIN w p ON p.hub_id = c.hub_id AND p.closed_week = {_ts(prev_start)}
        LEFT JOIN targets t ON t.region = c.region
        WHERE c.closed_week = {_ts(week_start)}
        ORDER BY gap_pp
    """).df()

    merchants = con.execute(f"""
        WITH w AS (
          SELECT merchant_id, merchant_name, segment, COALESCE(account_manager, 'Unassigned') AS account_manager,
                 SUM(CASE WHEN created_week = {_ts(week_start)} THEN 1 ELSE 0 END) AS orders,
                 SUM(CASE WHEN created_week = {_ts(prev_start)} THEN 1 ELSE 0 END) AS orders_last_week,
                 SUM(CASE WHEN closed_week = {_ts(week_start)} AND status = 'returned' THEN 1 ELSE 0 END) AS returned,
                 SUM(CASE WHEN closed_week = {_ts(week_start)} THEN 1 ELSE 0 END) AS closed
          FROM fact WHERE created_week >= {_ts(prev_start)} OR closed_week = {_ts(week_start)}
          GROUP BY 1, 2, 3, 4
        )
        SELECT merchant_id, merchant_name AS merchant, segment, account_manager, orders, orders_last_week,
               CASE WHEN orders_last_week > 0 THEN ROUND((orders - orders_last_week) * 100.0 / orders_last_week, 1) END AS change_pct,
               CASE WHEN closed > 0 THEN ROUND(returned * 1.0 / closed, 4) END AS return_rate,
               (orders_last_week >= {C.MERCHANT_MIN_ORDERS}
                 AND (orders_last_week - orders) * 100.0 / orders_last_week >= {C.MERCHANT_DROP_PCT}) AS flagged
        FROM w WHERE orders > 0 OR orders_last_week > 0
        ORDER BY orders_last_week - orders DESC
    """).df()

    def total(start: datetime) -> dict:
        w = weekly[weekly["week_start"].astype(str) == f"{start:%Y-%m-%d}"]
        closed, delivered = int(w["closed"].sum()), int(w["delivered"].sum())
        return {"created": int(w["created"].sum()), "closed": closed, "revenue": float(w["revenue"].sum()),
                "unpaid": float(w["unpaid"].sum()), "success_rate": delivered / closed if closed else float("nan")}

    cur, prev = total(week_start), total(prev_start)
    headline = {
        "week": f"{week_start:%d %b} - {week_end - timedelta(days=1):%d %b %Y}",
        "created": cur["created"], "created_prev": prev["created"],
        "success_rate": cur["success_rate"], "success_rate_prev": prev["success_rate"],
        "revenue": cur["revenue"], "revenue_prev": prev["revenue"], "unpaid": cur["unpaid"],
        "active_merchants": int((merchants["orders"] > 0).sum()),
        "active_merchants_prev": int((merchants["orders_last_week"] > 0).sum()),
        "hubs_flagged": int(hubs["flagged"].sum()), "merchants_flagged": int(merchants["flagged"].sum()),
    }
    return Report(as_of, week_start, week_end, weekly, hubs, merchants, headline)
