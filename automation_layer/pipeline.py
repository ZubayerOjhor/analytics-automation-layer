"""One run of the layer: refresh -> reconcile -> build -> check -> deliver -> record."""
from __future__ import annotations

import csv
import logging
import time
from datetime import datetime

from . import cache, checks, config as C, deliver, transform

log = logging.getLogger("layer")
HISTORY = ["run_at", "mode", "data_as_of", "seconds", "parcels", "merchants", "hubs", "invoices",
           "reconciled", "quality_issues", "hubs_flagged", "merchants_flagged", "status"]


def setup_logging() -> None:
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", force=True,
        handlers=[logging.StreamHandler(), logging.FileHandler(C.LOG_DIR / "run.log", encoding="utf-8")])


def _record(row: dict) -> None:
    """Append to the run history: the first place to look when a number seems off."""
    C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = C.OUTPUT_DIR / "run_history.csv"
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in HISTORY})


def run(full: bool = False, send: bool = True) -> dict:
    setup_logging()
    t0 = time.time()
    row = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "failed"}
    con = cache.connect()
    try:
        first = cache.watermark(con, "parcels") is None
        row["mode"] = "full" if (full or first) else "incremental"
        log.info("run started (%s refresh)", row["mode"])
        row.update(cache.refresh(con, full=full))

        recon = checks.reconcile(con)
        row["reconciled"] = bool(recon["match"].all())
        checks.require_reconciled(recon)                     # hard gate: nothing ships on a mismatch
        log.info("reconciliation passed (%s/%s checks)", int(recon["match"].sum()), len(recon))

        report = transform.build(con)
        quality = checks.quality(con, report.as_of)
        row.update(data_as_of=f"{report.as_of:%Y-%m-%d %H:%M}", quality_issues=int(quality["issues"].sum()),
                   hubs_flagged=report.headline["hubs_flagged"], merchants_flagged=report.headline["merchants_flagged"])
        delivered = deliver.deliver(report, quality, recon) if send else {}
        row["status"] = "success"
        return {"report": report, "quality": quality, "recon": recon, "delivered": delivered, "run": row}
    except Exception:
        log.exception("run failed")
        raise
    finally:
        row["seconds"] = round(time.time() - t0, 1)
        _record(row)
        log.info("run %s in %ss", row["status"], row["seconds"])
        con.close()
