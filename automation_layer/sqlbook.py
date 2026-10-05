"""Run a query from the sql/ folder against the local cache.

    python -m automation_layer sql 01_winback_customers
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config as C

SQL_DIR = C.ROOT / "sql"


def available() -> list[str]:
    return sorted(p.stem for p in SQL_DIR.glob("*.sql"))


def run(con, name: str, save: bool = True) -> pd.DataFrame:
    path = SQL_DIR / f"{Path(name).stem}.sql"
    if not path.exists():
        raise FileNotFoundError(f"No query named {name!r}. Available: {', '.join(available())}")
    df = con.execute(path.read_text(encoding="utf-8")).df()
    if save:
        out = C.OUTPUT_DIR / "sql" / f"{path.stem}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, date_format="%Y-%m-%d")
    return df
