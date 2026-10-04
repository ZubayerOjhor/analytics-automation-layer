"""Put the report where people already look: Excel, a dashboard page, a chat message, a sheet.

Excel, the dashboard and the brief file are always written. Telegram and Google Sheets
switch on when their environment variables are set, and stay a dry run otherwise.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font

from . import config as C
from .transform import Report

log = logging.getLogger("layer.deliver")
REGION_COLOR = {"Metro": "--s1", "Suburban": "--s2", "Regional": "--s3"}


def _pct(x, dp=1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{dp}f}%"


def _delta(cur, prev, kind="pct") -> str:
    if prev in (0, None) or (isinstance(prev, float) and math.isnan(prev)):
        return "no prior week"
    if kind == "pp":
        return f"{(cur - prev) * 100:+.1f}pp WoW"
    if kind == "abs":
        return f"{cur - prev:+,} WoW"
    return f"{(cur - prev) / prev * 100:+.1f}% WoW"


# ---------------------------------------------------------------- brief

def brief_text(rep: Report, quality: pd.DataFrame, recon: pd.DataFrame) -> str:
    h = rep.headline
    lines = [
        f"Weekly brief | {h['week']}",
        f"Parcels created: {h['created']:,} ({_delta(h['created'], h['created_prev'])})",
        f"Success rate: {_pct(h['success_rate'])} ({_delta(h['success_rate'], h['success_rate_prev'], 'pp')})",
        f"Revenue: {h['revenue']:,.0f} ({_delta(h['revenue'], h['revenue_prev'])}), unpaid {h['unpaid']:,.0f}",
        f"Active merchants: {h['active_merchants']:,} ({_delta(h['active_merchants'], h['active_merchants_prev'], 'abs')})",
        f"Flags: {h['hubs_flagged']} hubs below target, {h['merchants_flagged']} merchants with a "
        f"{C.MERCHANT_DROP_PCT:.0f}%+ order drop",
    ]
    worst = rep.hubs[rep.hubs["flagged"]].head(1)
    if not worst.empty:
        w = worst.iloc[0]
        lines.append(f"Worst hub: {w['hub']} at {_pct(w['success_rate'])} (target {_pct(w['target'], 0)})")
    gap = quality.iloc[0]
    lines.append(f"Checks: reconciliation {int(recon['match'].sum())}/{len(recon)} passed; "
                 f"{gap['issues']} closed parcels have no invoice")
    return "\n".join(lines)


def send_telegram(text: str) -> str:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return "dry run (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to send)"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20) as r:
        return "sent" if json.load(r).get("ok") else "failed"


def push_google_sheet(frames: dict[str, pd.DataFrame]) -> str:
    sheet_id = os.getenv("GSHEET_ID")
    if not sheet_id:
        return "dry run (set GSHEET_ID and GOOGLE_APPLICATION_CREDENTIALS to publish)"
    try:
        import gspread
    except ImportError:
        return "skipped (pip install gspread)"
    book = gspread.service_account(filename=os.environ["GOOGLE_APPLICATION_CREDENTIALS"]).open_by_key(sheet_id)
    for title, df in frames.items():
        try:
            ws = book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title, rows=len(df) + 10, cols=len(df.columns) + 2)
        ws.clear()
        ws.update([list(df.columns)] + df.astype(object).where(df.notna(), "").values.tolist())
    return f"published {len(frames)} tabs"


# ---------------------------------------------------------------- Excel

def write_excel(path: Path, frames: dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        for name, df in frames.items():
            df.to_excel(xl, sheet_name=name, index=False)
            ws = xl.sheets[name]
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for i, col in enumerate(df.columns, start=1):
                width = max(len(str(col)), *(len(str(v)) for v in df[col].head(200))) + 2
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width, 46)


# ---------------------------------------------------------------- dashboard (one self-contained HTML file)

def _trend_svg(weekly: pd.DataFrame) -> str:
    weeks = sorted(weekly["week_start"].astype(str).unique())
    W, H, L, R, T, B = 760, 300, 46, 96, 16, 34
    lo = math.floor(weekly["success_rate"].min() * 100 - 2)
    hi = math.ceil(weekly["success_rate"].max() * 100 + 2)
    x = lambda i: L + i * (W - L - R) / max(1, len(weeks) - 1)
    y = lambda v: T + (hi - v * 100) / (hi - lo) * (H - T - B)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Success rate by region, weekly">']
    step = max(1, round((hi - lo) / 5))
    for g in range(lo, hi + 1, step):
        yy = y(g / 100)
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>'
                     f'<text x="{L - 8}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{g}%</text>')
    for i, wk in enumerate(weeks):
        parts.append(f'<text x="{x(i):.1f}" y="{H - 10}" class="tick" text-anchor="middle">{wk[5:]}</text>')
    for region, var in REGION_COLOR.items():
        d = weekly[weekly["region"] == region].set_index(weekly[weekly["region"] == region]["week_start"].astype(str))
        pts = [(x(i), y(d.loc[wk, "success_rate"]), d.loc[wk, "success_rate"], wk) for i, wk in enumerate(weeks) if wk in d.index]
        if not pts:
            continue
        parts.append(f'<polyline fill="none" stroke="var({var})" stroke-width="2" points="'
                     + " ".join(f"{px:.1f},{py:.1f}" for px, py, _, _ in pts) + '"/>')
        for px, py, v, wk in pts:
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" fill="var({var})" class="dot">'
                         f'<title>{region}, week of {wk}: {v * 100:.1f}%</title></circle>')
        parts.append(f'<text x="{pts[-1][0] + 10:.1f}" y="{pts[-1][1] + 4:.1f}" class="lab">{region}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _table(df: pd.DataFrame, columns: list[tuple], limit: int = 12) -> str:
    """columns: (dataframe column, header label, formatter or None for text)."""
    if df.empty:
        return '<p class="empty">Nothing flagged this week.</p>'
    head = "".join(f'<th class="{"num" if f else ""}">{html.escape(label)}</th>' for _, label, f in columns)
    body = ""
    for _, r in df.head(limit).iterrows():
        body += "<tr>" + "".join(f'<td class="{"num" if f else ""}">{html.escape(f(r[c]) if f else str(r[c]))}</td>'
                                 for c, _, f in columns) + "</tr>"
    more = f'<p class="more">Showing {min(limit, len(df))} of {len(df)}. The Excel file has every row.</p>' if len(df) > limit else ""
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def dashboard_html(rep: Report, quality: pd.DataFrame, recon: pd.DataFrame) -> str:
    h = rep.headline
    tiles = [
        ("Parcels created", f"{h['created']:,}", _delta(h["created"], h["created_prev"])),
        ("Success rate", _pct(h["success_rate"]), _delta(h["success_rate"], h["success_rate_prev"], "pp")),
        ("Revenue", f"{h['revenue']:,.0f}", _delta(h["revenue"], h["revenue_prev"])),
        ("Active merchants", f"{h['active_merchants']:,}", _delta(h["active_merchants"], h["active_merchants_prev"], "abs")),
        ("Hubs below target", str(h["hubs_flagged"]), f"more than {C.HUB_GAP_PP:.0f}pp under region target"),
        ("Merchants dropping", str(h["merchants_flagged"]), f"orders down {C.MERCHANT_DROP_PCT:.0f}% or more"),
    ]
    num, pc = (lambda v: f"{v:,.0f}"), (lambda v: _pct(v))
    hubs = _table(rep.hubs[rep.hubs["flagged"]], [
        ("hub", "Hub", None), ("region", "Region", None), ("closed", "Closed", num), ("success_rate", "Success rate", pc),
        ("last_week", "Last week", pc), ("target", "Target", lambda v: _pct(v, 0)), ("gap_pp", "Gap", lambda v: f"{v:+.1f}pp")])
    merch = _table(rep.merchants[rep.merchants["flagged"]], [
        ("merchant", "Merchant", None), ("account_manager", "Account manager", None),
        ("orders_last_week", "Last week", num), ("orders", "This week", num), ("change_pct", "Change", lambda v: f"{v:+.0f}%")])
    recon_t = _table(recon.assign(result=recon["match"].map({True: "✓ match", False: "✗ MISMATCH"})), [
        ("check", "Check", None), ("source", "Source systems", num), ("cache", "Cache", num), ("result", "Result", None)])
    qual = _table(quality, [("check", "Check", None), ("issues", "Issues", num), ("out_of", "Out of", num),
                            ("rate", "Rate", lambda v: f"{v * 100:.2f}%")])
    legend = "".join(f'<span><i style="background:var({v})"></i>{r}</span>' for r, v in REGION_COLOR.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly operations report</title>
<style>
:root{{--bg:#f4f5f7;--card:#fff;--ink:#14213d;--muted:#5f6b85;--line:#e3e7ef;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--tag:#fff4db}}
@media (prefers-color-scheme:dark){{:root{{--bg:#101217;--card:#171a20;--ink:#e9eaed;--muted:#a0a6b1;--line:#2b3039;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--tag:#3a3212}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1120px;margin:0 auto;padding:24px 16px 56px}}h1{{font-size:24px;margin:0}}h2{{font-size:16px;margin:0 0 12px}}
.sub{{color:var(--muted);margin:4px 0 20px}}.tag{{display:inline-block;background:var(--tag);border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;margin-left:8px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin-bottom:16px}}
.tile,.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.tile .l{{font-size:12.5px;color:var(--muted)}}.tile .v{{font-size:26px;font-weight:700;margin:2px 0}}.tile .d{{font-size:12.5px;color:var(--muted)}}
.card{{margin-bottom:16px}}.cols{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:16px}}
.legend{{display:flex;gap:16px;font-size:12.5px;color:var(--muted);margin-bottom:6px}}.legend i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}}
svg{{width:100%;height:auto;display:block}}.grid{{stroke:var(--line);stroke-width:1}}.tick{{font-size:11px;fill:var(--muted)}}.lab{{font-size:12px;fill:var(--muted)}}.dot{{stroke:var(--card);stroke-width:2}}
.tw{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:13.5px}}th{{text-align:left;color:var(--muted);font-weight:600;font-size:12px;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}td.num,th.num{{font-variant-numeric:tabular-nums;text-align:right}}tr:last-child td{{border-bottom:0}}td:first-child{{white-space:normal}}
.more,.empty,.foot{{font-size:12.5px;color:var(--muted)}}
</style></head><body><main>
<h1>Weekly operations report <span class="tag">synthetic data</span></h1>
<p class="sub">Week of {html.escape(h['week'])}, compared with the week before. Data as of {rep.as_of:%d %b %Y %H:%M}.</p>
<div class="tiles">{"".join(f'<div class="tile"><div class="l">{l}</div><div class="v">{v}</div><div class="d">{d}</div></div>' for l, v, d in tiles)}</div>
<div class="card"><h2>Success rate by region, last {len(rep.weekly['week_start'].unique())} weeks</h2><div class="legend">{legend}</div>{_trend_svg(rep.weekly)}</div>
<div class="cols"><div class="card"><h2>Hubs below their region target</h2>{hubs}</div>
<div class="card"><h2>Merchants with a sharp order drop</h2>{merch}</div></div>
<div class="cols"><div class="card"><h2>Reconciliation: cache vs source systems</h2>{recon_t}</div>
<div class="card"><h2>Cross-system data checks</h2>{qual}</div></div>
<p class="foot">Built by the analytics automation layer: three separate source databases, joined in Python, cached in DuckDB, checked, then delivered. Every figure is generated; nothing here is real company data.</p>
</main></body></html>"""


# ---------------------------------------------------------------- everything

def deliver(rep: Report, quality: pd.DataFrame, recon: pd.DataFrame) -> dict[str, str]:
    C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frames = {"Weekly by region": rep.weekly, "Hubs": rep.hubs, "Merchants": rep.merchants,
              "Reconciliation": recon, "Data checks": quality}
    write_excel(C.OUTPUT_DIR / "weekly_report.xlsx", frames)
    (C.OUTPUT_DIR / "dashboard.html").write_text(dashboard_html(rep, quality, recon), encoding="utf-8")
    text = brief_text(rep, quality, recon)
    (C.OUTPUT_DIR / "brief.txt").write_text(text + "\n", encoding="utf-8")
    result = {"excel": "output/weekly_report.xlsx", "dashboard": "output/dashboard.html", "brief": "output/brief.txt",
              "telegram": send_telegram(text), "google_sheet": push_google_sheet(frames)}
    for channel, status in result.items():
        log.info("delivered %-13s %s", channel, status)
    return result
