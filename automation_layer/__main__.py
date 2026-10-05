"""Command line.

    python -m automation_layer generate     create the three synthetic source databases
    python -m automation_layer run          refresh, check, build and deliver the weekly report
    python -m automation_layer next-day     move the source systems forward one day
    python -m automation_layer status       what the cache holds and when it last changed
    python -m automation_layer sql NAME     run a query from the sql/ folder against the cache
"""
from __future__ import annotations

import argparse

from . import cache, config as C, pipeline, sqlbook, synthetic


def main() -> None:
    ap = argparse.ArgumentParser(prog="automation_layer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="Create the synthetic source databases")
    r = sub.add_parser("run", help="Run the pipeline")
    r.add_argument("--full", action="store_true", help="Reload everything instead of only what changed")
    r.add_argument("--no-deliver", action="store_true", help="Refresh and check only")
    n = sub.add_parser("next-day", help="Advance the source systems")
    n.add_argument("--days", type=int, default=1)
    sub.add_parser("status", help="Show the cache contents")
    q = sub.add_parser("sql", help="Run a query from the sql/ folder")
    q.add_argument("name", nargs="?", help="File name without .sql; leave empty to list them")
    args = ap.parse_args()

    if args.cmd == "generate":
        as_of = synthetic.write_sources()
        synthetic.write_samples()
        print(f"Sources written to {C.SOURCES_DIR} (state as of {as_of:%Y-%m-%d %H:%M})")
    elif args.cmd == "next-day":
        as_of = synthetic.advance(args.days)
        print(f"Sources moved forward to {as_of:%Y-%m-%d %H:%M}")
    elif args.cmd == "run":
        out = pipeline.run(full=args.full, send=not args.no_deliver)
        print("\n" + (C.OUTPUT_DIR / "brief.txt").read_text(encoding="utf-8") if out["delivered"] else "Refreshed.")
    elif args.cmd == "status":
        con = cache.connect()
        for table in cache.SCHEMA:
            n_rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:10s} {n_rows:>8,} rows   last change seen: {cache.watermark(con, table)}")
    elif args.cmd == "sql":
        if not args.name:
            print(*sqlbook.available(), sep="\n")
            return
        df = sqlbook.run(cache.connect(), args.name)
        print(df.to_string(index=False))
        print(f"\n{len(df):,} rows -> {C.OUTPUT_DIR / 'sql' / (args.name + '.csv')}")


if __name__ == "__main__":
    main()
