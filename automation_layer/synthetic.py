"""Synthetic source systems: three separate databases that cannot be joined in one query.

Everything here is invented by a seeded random model. No real company, merchant, hub or
figure is used. The model first draws a full timeline (every parcel's creation time, close
time, outcome and invoice), then writes each database *as it would look at a given moment*:
parcels not yet closed are still "in_transit", invoices not yet raised do not exist. Moving
that moment forward by a day therefore changes and adds rows exactly like a live system,
which is what the incremental refresh is tested against.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from . import config as C

SEED = 20260104
TIMELINE_START = datetime(2026, 1, 5)            # a Monday
TIMELINE_DAYS = 84                               # 12 weeks are drawn up front
DEFAULT_AS_OF = TIMELINE_START + timedelta(days=58, hours=6)   # "today" on first generate

REGIONS = {"Metro": 8, "Suburban": 6, "Regional": 10}          # hubs per region
REGION_SUCCESS = {"Metro": 0.89, "Suburban": 0.85, "Regional": 0.80}
REGION_CLOSE_DAYS = {"Metro": 1.2, "Suburban": 1.8, "Regional": 2.7}
REGION_FEE = {"Metro": 60, "Suburban": 80, "Regional": 110}
SEGMENTS = ["Fashion", "Electronics", "Beauty", "Home", "Grocery", "Books"]
NAME_A = ["Blue", "Amber", "Silver", "Maple", "River", "Cedar", "Coral", "Indigo", "Olive", "Saffron", "Ivory",
          "Juniper", "Copper", "Meadow", "Harbor", "Lotus", "Willow", "Granite", "Sunrise", "Pebble"]
NAME_B = ["Lantern", "Thread", "Basket", "Market", "Studio", "Corner", "Supply", "Works", "Gallery", "Cart",
          "Pantry", "Trunk", "Loft", "Wardrobe", "Shelf"]
MISSING_INVOICE_RATE = 0.004    # deliberate defect: closed parcels the billing system never invoiced


@lru_cache(maxsize=1)
def build_timeline() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(SEED)

    hubs = []
    for region, n in REGIONS.items():
        for i in range(1, n + 1):
            hubs.append({"hub_id": len(hubs) + 1, "hub_name": f"{region} {i:02d}", "region": region,
                         "quality": rng.normal(0, 0.025)})
    hubs = pd.DataFrame(hubs)
    # Two hubs slip in the final weeks so the report has something real to flag.
    slipping = {int(hubs.loc[hubs.region == "Regional", "hub_id"].iloc[3]): 0.14,
                int(hubs.loc[hubs.region == "Suburban", "hub_id"].iloc[1]): 0.10}

    n_merchants = 320
    names = [f"{rng.choice(NAME_A)} {rng.choice(NAME_B)}" for _ in range(n_merchants)]
    merchants = pd.DataFrame({
        "merchant_id": np.arange(1001, 1001 + n_merchants),
        "merchant_name": [f"{nm} {i % 97 + 1}" for i, nm in enumerate(names)],
        "segment": rng.choice(SEGMENTS, n_merchants),
        "account_manager": rng.choice([f"AM-{k:02d}" for k in range(1, 9)] + [None, None], n_merchants),
        "daily_rate": np.clip(rng.lognormal(0.3, 1.1, n_merchants), 0.05, 60),
        "return_bias": rng.normal(0, 0.03, n_merchants),
    })
    # Most merchants signed up before the timeline; some join during it, a few go quiet.
    join_day = np.where(rng.random(n_merchants) < 0.18, rng.integers(0, TIMELINE_DAYS, n_merchants), -30)
    quit_day = np.where(rng.random(n_merchants) < 0.07, rng.integers(20, TIMELINE_DAYS, n_merchants), 10_000)
    merchants["signup_date"] = [TIMELINE_START + timedelta(days=int(d)) for d in join_day]
    # A handful of big merchants cut their volume late on (the "order dropped" trigger).
    dropper = set(merchants.sort_values("daily_rate", ascending=False).head(40)
                  .sample(6, random_state=SEED)["merchant_id"])

    rows = []
    for day in range(TIMELINE_DAYS):
        weekday = (TIMELINE_START + timedelta(days=day)).weekday()
        season = (0.55 if weekday == 4 else 1.0) * (1 + 0.004 * day)       # quiet Fridays, slow growth
        active = (join_day <= day) & (quit_day > day)
        rate = merchants["daily_rate"].to_numpy() * season * active
        rate = np.where(merchants["merchant_id"].isin(dropper) & (day >= 49), rate * 0.45, rate)
        counts = rng.poisson(rate)
        ids = np.repeat(merchants["merchant_id"].to_numpy(), counts)
        rows.append(pd.DataFrame({"merchant_id": ids, "day": day}))
    parcels = pd.concat(rows, ignore_index=True)
    n = len(parcels)
    parcels.insert(0, "parcel_id", np.arange(500001, 500001 + n))
    weights = hubs["region"].map({"Metro": 3.0, "Suburban": 1.4, "Regional": 1.0}).to_numpy()
    parcels["hub_id"] = rng.choice(hubs["hub_id"], n, p=weights / weights.sum())
    parcels = parcels.merge(hubs[["hub_id", "region", "quality"]], on="hub_id")
    parcels = parcels.merge(merchants[["merchant_id", "return_bias"]], on="merchant_id")
    parcels = parcels.sort_values("parcel_id").reset_index(drop=True)
    parcels["created_at"] = [TIMELINE_START + timedelta(days=int(d), seconds=int(s))
                             for d, s in zip(parcels["day"], rng.integers(8 * 3600, 21 * 3600, n))]
    close_days = rng.gamma(2.2, parcels["region"].map(REGION_CLOSE_DAYS).to_numpy() / 2.2)
    parcels["close_at"] = parcels["created_at"] + pd.to_timedelta(np.clip(close_days, 0.3, 14), unit="D")
    slip = parcels["hub_id"].map(slipping).fillna(0) * (parcels["day"] >= 42)
    p_ok = parcels["region"].map(REGION_SUCCESS) + parcels["quality"] - parcels["return_bias"] - slip
    parcels["outcome"] = np.where(rng.random(n) < np.clip(p_ok, 0.3, 0.98), "delivered", "returned")
    parcels["weight_g"] = np.clip(rng.lognormal(6.2, 0.8, n), 50, 15000).astype(int)

    fee = parcels["region"].map(REGION_FEE) + (parcels["weight_g"] // 1000) * 15
    invoices = pd.DataFrame({
        "parcel_id": parcels["parcel_id"],
        "fee": np.where(parcels["outcome"] == "delivered", fee, (fee * 0.5).round()),
        "cod_collected": np.where(parcels["outcome"] == "delivered", rng.integers(300, 3000, n), 0),
        "invoiced_at": parcels["close_at"] + pd.to_timedelta(rng.uniform(1, 6, n), unit="h"),
    })
    invoices["paid_at"] = invoices["invoiced_at"] + pd.to_timedelta(rng.uniform(2, 5, n), unit="D")
    invoices = invoices[rng.random(n) >= MISSING_INVOICE_RATE].reset_index(drop=True)
    return {"hubs": hubs, "merchants": merchants, "parcels": parcels, "invoices": invoices}


def snapshot(as_of: datetime) -> dict[str, pd.DataFrame]:
    """The three systems as they stand at `as_of`."""
    t = build_timeline()
    p = t["parcels"][t["parcels"]["created_at"] <= as_of].copy()
    closed = p["close_at"] <= as_of
    p["status"] = np.where(closed, p["outcome"], "in_transit")
    p["closed_at"] = p["close_at"].where(closed)
    p["updated_at"] = p["closed_at"].fillna(p["created_at"])
    parcels = p[["parcel_id", "merchant_id", "hub_id", "created_at", "closed_at", "status", "weight_g", "updated_at"]]

    i = t["invoices"][t["invoices"]["invoiced_at"] <= as_of].copy()
    i["paid"] = (i["paid_at"] <= as_of).astype(int)
    i["updated_at"] = i["paid_at"].where(i["paid"] == 1, i["invoiced_at"])
    invoices = i[["parcel_id", "fee", "cod_collected", "invoiced_at", "paid", "updated_at"]]

    m = t["merchants"][t["merchants"]["signup_date"] <= as_of].copy()
    m["updated_at"] = m["signup_date"]
    merchants = m[["merchant_id", "merchant_name", "segment", "account_manager", "signup_date", "updated_at"]]

    hubs = t["hubs"][["hub_id", "hub_name", "region"]].copy()
    hubs["updated_at"] = TIMELINE_START - timedelta(days=30)
    return {"parcels": parcels, "invoices": invoices, "merchants": merchants, "hubs": hubs}


def _write(db: str, tables: dict[str, pd.DataFrame]) -> None:
    path = C.SOURCES_DIR / db
    if path.exists():
        path.unlink()
    with closing(sqlite3.connect(path)) as con, con:      # closing(): sqlite's own context manager only commits
        for name, df in tables.items():
            out = df.copy()
            for col in out.columns:
                if pd.api.types.is_datetime64_any_dtype(out[col]):
                    out[col] = out[col].dt.strftime("%Y-%m-%d %H:%M:%S")
            out.to_sql(name, con, index=False)


def write_sources(as_of: datetime | None = None) -> datetime:
    as_of = as_of or DEFAULT_AS_OF
    C.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    s = snapshot(as_of)
    _write("orders.db", {"parcels": s["parcels"]})
    _write("crm.db", {"merchants": s["merchants"], "hubs": s["hubs"]})
    _write("finance.db", {"invoices": s["invoices"]})
    C.STATE_FILE.write_text(json.dumps({"as_of": as_of.isoformat(sep=" ")}), encoding="utf-8")
    C.TARGETS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not C.TARGETS_CSV.exists():      # stands in for a sheet the business team maintains
        pd.DataFrame({"region": list(REGIONS), "success_rate_target": [0.88, 0.84, 0.79]}).to_csv(C.TARGETS_CSV, index=False)
    return as_of


def write_samples(rows: int = 300) -> None:
    """Small CSV extracts of each source table, so the data can be read on GitHub without running anything."""
    out = C.ROOT / "data" / "samples"
    out.mkdir(parents=True, exist_ok=True)
    for name, df in snapshot(DEFAULT_AS_OF).items():
        df.tail(rows).to_csv(out / f"{name}.csv", index=False)


def current_as_of() -> datetime:
    return datetime.fromisoformat(json.loads(C.STATE_FILE.read_text(encoding="utf-8"))["as_of"])


def advance(days: int = 1) -> datetime:
    """Move the source systems forward in time (new parcels, closures, invoices, payments)."""
    return write_sources(current_as_of() + timedelta(days=days))
