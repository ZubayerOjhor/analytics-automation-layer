"""End-to-end tests on a throwaway copy of the synthetic sources.

    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.exc import OperationalError  # noqa: E402

import duckdb  # noqa: E402

from automation_layer import cache, checks, config as C, deliver, extract, sqlbook, synthetic, transform  # noqa: E402


class LayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        C.SOURCES_DIR = base / "sources"
        C.STATE_FILE = C.SOURCES_DIR / "state.json"
        C.TARGETS_CSV = base / "lookups" / "region_targets.csv"
        C.OUTPUT_DIR = base / "output"
        C.SOURCE_URLS = {name: f"sqlite:///{(C.SOURCES_DIR / f'{name}.db').as_posix()}"
                         for name in ("orders", "crm", "finance")}
        extract.engine.cache_clear()
        synthetic.write_sources()
        cls.con = cache.connect(base / "cache.duckdb")
        cls.first = cache.refresh(cls.con)
        cls.report = transform.build(cls.con)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        for source in C.SOURCE_URLS:
            extract.engine(source).dispose()
        cls.tmp.cleanup()

    def test_1_first_run_loads_everything_and_reconciles(self):
        recon = checks.reconcile(self.con)
        self.assertTrue(recon["match"].all(), recon.to_string())
        self.assertGreater(self.first["parcels"], 30_000)

    def test_2_report_joins_all_three_systems_and_the_lookup_file(self):
        hubs = self.report.hubs
        self.assertTrue({"hub", "region", "success_rate", "target", "gap_pp", "flagged"} <= set(hubs.columns))
        self.assertFalse(hubs["target"].isna().any())                     # lookup file joined
        self.assertGreater(self.report.headline["revenue"], 0)            # billing system joined
        self.assertIn("Regional 04", set(hubs.loc[hubs["flagged"], "hub"]))   # the hub the generator degrades

    def test_3_order_drop_trigger_respects_the_minimum_volume(self):
        flagged = self.report.merchants[self.report.merchants["flagged"]]
        self.assertGreater(len(flagged), 0)
        self.assertTrue((flagged["orders_last_week"] >= C.MERCHANT_MIN_ORDERS).all())
        self.assertTrue((flagged["change_pct"] <= -C.MERCHANT_DROP_PCT).all())

    def test_4_cross_system_check_finds_the_billing_gap(self):
        q = checks.quality(self.con, self.report.as_of).set_index("check")
        self.assertGreater(q.loc["Closed parcel with no invoice (billing gap)", "issues"], 0)
        self.assertEqual(q.loc["Invoice for a parcel the operations system does not have", "issues"], 0)

    def test_5_delivery_writes_excel_dashboard_and_brief(self):
        recon = checks.reconcile(self.con)
        result = deliver.deliver(self.report, checks.quality(self.con, self.report.as_of), recon)
        for name in ("weekly_report.xlsx", "dashboard.html", "brief.txt"):
            self.assertTrue((C.OUTPUT_DIR / name).stat().st_size > 0, name)
        self.assertIn("dry run", result["telegram"])                      # no token in tests: never sends
        self.assertIn("Weekly brief", (C.OUTPUT_DIR / "brief.txt").read_text(encoding="utf-8"))
        self.assertIn("<svg", (C.OUTPUT_DIR / "dashboard.html").read_text(encoding="utf-8"))

    def test_6_reconciliation_blocks_a_run_when_the_cache_drifts(self):
        self.con.execute("BEGIN")
        self.con.execute("DELETE FROM invoices WHERE parcel_id = (SELECT MIN(parcel_id) FROM invoices)")
        try:
            with self.assertRaises(checks.ReconciliationError):
                checks.require_reconciled(checks.reconcile(self.con))
        finally:
            self.con.execute("ROLLBACK")

    def test_7_next_day_pulls_only_what_changed(self):
        before = self.con.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
        for source in C.SOURCE_URLS:
            extract.engine(source).dispose()
        synthetic.advance(1)
        pulled = cache.refresh(self.con)
        self.assertLess(pulled["parcels"], before * 0.10)                 # a day's changes, not a reload
        self.assertGreater(pulled["parcels"], 0)
        self.assertEqual(pulled["hubs"], 0)
        self.assertTrue(checks.reconcile(self.con)["match"].all())


class RetryTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OperationalError("select 1", {}, Exception("connection dropped"))
            return "ok"

        self.assertEqual(extract.with_retry(flaky, attempts=3, wait=0), "ok")
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_the_last_attempt(self):
        def down():
            raise OperationalError("select 1", {}, Exception("still down"))

        with self.assertRaises(OperationalError):
            extract.with_retry(down, attempts=2, wait=0)


class SqlNotebookTests(unittest.TestCase):
    def test_winback_finds_only_the_long_silence(self):
        con = duckdb.connect()
        con.execute("CREATE TABLE parcels (merchant_id INTEGER, created_at TIMESTAMP)")
        con.execute("""INSERT INTO parcels VALUES
            (1, '2026-01-01 09:00'), (1, '2026-01-01 15:00'),   -- two orders, one active day
            (1, '2026-01-05 10:00'),                            -- 4 days: not quiet
            (1, '2026-01-25 10:00'),                            -- 20 days: a comeback
            (2, '2026-01-01 10:00'), (2, '2026-01-14 10:00'),   -- 13 days: just under the line
            (3, '2026-01-10 10:00')                             -- a single day: nothing to compare""")
        out = sqlbook.run(con, "01_winback_customers", save=False)
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertEqual((int(row["customer_id"]), int(row["days_silent"])), (1, 20))
        self.assertEqual(str(row["went_quiet_on"])[:10], "2026-01-05")
        con.close()
    def test_weekday_baseline_ignores_the_weekly_dip_and_catches_a_real_one(self):
        start = datetime(2026, 1, 5, 10)                        # a Monday
        rows = []
        for d in range(42):                                     # six weeks: 10 a day, 5 on Fridays
            day = start + timedelta(days=d)
            n = 5 if day.weekday() == 4 or d == 38 else 10      # day 38, a Thursday, really does halve
            rows += [(1, day)] * n
        con = duckdb.connect()
        con.execute("CREATE TABLE parcels (merchant_id INTEGER, created_at TIMESTAMP)")
        con.executemany("INSERT INTO parcels VALUES (?, ?)", rows)
        out = sqlbook.run(con, "02_normal_for_this_weekday", save=False)
        out.index = out["day"].astype(str).str[:10]
        self.assertEqual(len(out), 14)                          # only days with four earlier weeks
        friday = out.loc["2026-02-06"]                          # an ordinary quiet Friday
        self.assertEqual((friday["vs_yesterday_pct"], friday["vs_same_weekday_pct"]), (-50.0, 0.0))
        self.assertEqual(out.loc["2026-02-12", "vs_same_weekday_pct"], -50.0)   # the real drop
        con.close()


if __name__ == "__main__":
    unittest.main()
