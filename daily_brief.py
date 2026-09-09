"""Daily pre-market brief — deterministic markdown from the DuckDB store.

Reuses the dashboard's banner / regime / screener logic (read-only on the DB)
so the scheduled job and the Streamlit UI can never disagree.

Usage:
    python daily_brief.py                 # brief from the latest snapshot
    python daily_brief.py --ingest        # run ingest.py first, then brief
    python daily_brief.py --out PATH      # override output path

Writes briefs/YYYY-MM-DD.md (UTF-8) and prints the same text to stdout.
Exit code: 0 ok, 1 ingest failed (brief still written from last snapshot),
2 no database.
"""
from __future__ import annotations
import argparse, datetime as dt, os, subprocess, sys
import duckdb, pandas as pd
import config as C
import dashboard as D          # headless import: only the pure helpers are used
import exhaustion_trigger as et
import call_setups as cs

HERE = os.path.dirname(os.path.abspath(__file__))
BRIEF_DIR = os.path.join(HERE, "briefs")


def run_ingest() -> tuple[bool, str]:
    r = subprocess.run([sys.executable, os.path.join(HERE, "ingest.py")],
                       capture_output=True, text=True, cwd=HERE)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def _table(df: pd.DataFrame, cols: list[str], limit: int = 15) -> str:
    if df is None or df.empty:
        return "_none_"
    keep = [c for c in cols if c in df.columns]
    d = df[keep].head(limit).copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].round(2)
    head = "| " + " | ".join(keep) + " |"
    sep = "|" + "---|" * len(keep)
    rows = ["| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |"
            for r in d.itertuples(index=False)]
    return "\n".join([head, sep, *rows])


def _fmt(v, nd=1, suffix=""):
    return "—" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{v:.{nd}f}{suffix}"


def data_quality(panel: pd.DataFrame, snap_date) -> list[str]:
    """Cheap sanity checks on what ingest loaded. Returns human-readable issues."""
    issues = []
    if panel.empty:
        return ["no price rows for the universe"]
    last = panel.groupby("ticker")["date"].max().dt.date
    stale = sorted(last[last < snap_date].index) if snap_date else []
    if stale:
        issues.append(f"{len(stale)} symbols missing the {snap_date} bar: {', '.join(stale[:15])}"
                      + (" …" if len(stale) > 15 else ""))
    short = panel.groupby("ticker").size()
    short = sorted(short[short < 260].index)
    if short:
        issues.append(f"{len(short)} symbols with <260 bars (skipped by tight scan): {', '.join(short)}")
    bad = panel[(panel.volume <= 0) | (panel.high < panel.low) | panel.close.isna()]
    if not bad.empty:
        issues.append(f"{len(bad)} rows with zero volume / inverted range / null close "
                      f"(latest: {bad.ticker.iloc[-1]} {bad.date.iloc[-1].date()})")
    dup = panel.duplicated(["ticker", "date"]).sum()
    if dup:
        issues.append(f"{dup} duplicate (symbol, date) rows")
    return issues


def build(today: dt.date, ingest_ok: bool | None, ingest_log: str) -> str:
    con = duckdb.connect(C.DB_PATH, read_only=True)
    try:
        li, snap_date = D.last_ingest(con), D.latest_snapshot_date(con)
        use_date = snap_date or today
        ev = D.gather_events(today)
        regime = D._regime_vals(con, use_date)
        level, reasons = D.compute_banner(today, ev, regime)
        leg, tight, doji, spy_dd, qqq_dd, leader_regime = D._screen_all(C.DB_PATH, use_date, li)
        ydf = con.execute("SELECT label, yld, chg_bps FROM yields WHERE snapshot_date=? "
                          "ORDER BY CASE label WHEN '13-wk' THEN 1 WHEN '5-yr' THEN 2 "
                          "WHEN '10-yr' THEN 3 ELSE 4 END", [use_date]).df()
        wl = con.execute("SELECT symbol, close, pct_1d, pct_5d, dist20_pct, dist50_pct, "
                         "pullback_flag, setup_score FROM snapshot WHERE snapshot_date=? "
                         "AND symbol IN (SELECT UNNEST(?)) ORDER BY setup_score DESC, symbol",
                         [use_date, list(C.WATCHLIST)]).df()
        universe = sorted(set(C.SCREENER_UNIVERSE))
        panel = cs.load_panel_from_con(con, universe)
        hits = et.todays_hits(panel) if not panel.empty else pd.DataFrame()
        flags = cs.tight_flags(panel) if not panel.empty else pd.DataFrame()
        dq = data_quality(panel, snap_date)
    finally:
        con.close()

    L = [f"# Pre-Trade Brief — {today:%A, %B %d, %Y}", ""]
    L.append(f"**{level}** — " + "; ".join(reasons))
    stale = " ⚠️ stale (today's ingest did not run)" if snap_date != today else ""
    L.append(f"Data: snapshot **{snap_date}**{stale} · last ingest {li}")
    if ingest_ok is not None:
        L.append(f"Ingest this run: {'✅ ok' if ingest_ok else '❌ FAILED — see log at bottom'}")
    L.append("")

    L.append("## Data quality")
    L.extend([f"- {i}" for i in dq] if dq else ["- clean: every universe symbol has the latest bar, no bad rows"])
    L.append("")
    L.append("## Event gate")
    e = [f"NFP {ev['nfp'].date:%b %d} ({ev['nfp'].calendar_days}d)"]
    if ev["fomc"]:
        e.append(f"FOMC {ev['fomc'].date:%b %d} ({ev['fomc'].calendar_days}d)")
    o = ev["opex"]
    e.append(f"OPEX {o.date:%b %d} ({o.trading_days} td{', witching' if o.is_quarterly else ''})")
    if ev["qe_in"]:
        e.append(f"quarter-end window: {ev['qe_left']} td left")
    for x in ev["extra"]:
        e.append(f"{x.name} {x.date:%b %d} ({x.calendar_days}d)")
    L.append(" · ".join(e)); L.append("")

    L.append("## Regime")
    L.append(f"VIX {_fmt(regime.get('vix'))} ({_fmt(regime.get('vix_chg'), 2, ' Δ')}, "
             f"{_fmt(regime.get('vix_pct'), 1, '%')}) · "
             f"Fear&Greed {_fmt(regime.get('fng_score'), 0)} {regime.get('fng_rating') or '—'} · "
             f"SPY>20DMA {regime.get('spy_above20')} · QQQ>20 {regime.get('qqq_above20')} "
             f">50 {regime.get('qqq_above50')}")
    L.append(f"Leader regime **{leader_regime}** · SPY dd_days {spy_dd} · QQQ dd_days {qqq_dd}")
    if not ydf.empty:
        L.append(" · ".join(f"{r.label} {_fmt(r.yld, 2, '%')} ({_fmt(r.chg_bps, 0, 'bp')})"
                            for r in ydf.itertuples()))
    L.append("")

    L.append("## Tight flags (last bar NR / inside / doji)")
    L.append(f"{len(flags)} symbols tight · "
             f"{int(flags.signal.sum()) if not flags.empty else 0} full call-setup signals · "
             f"{int((flags.leader_ctx & flags.leg_down).sum()) if not flags.empty else 0} leaders in a leg-down")
    L.append(_table(flags, ["ticker", "close", "tight_run", "rng_vs_atr", "nr", "inside", "doji",
                            "leader_ctx", "leg_down", "signal"], limit=40))
    L.append("")
    L.append("## Call setups (leg-down exhaustion, screener universe)")
    L.append(_table(hits, ["ticker", "close", "tight_run", "doji", "dfly", "inside", "entry", "stop"]))
    L.append("")
    L.append("## Leader screens")
    L.append("**Leg Down**"); L.append(_table(leg, ["symbol", "status", "close", "dd_days",
             "off_high_ATR", "dist_20ema_ATR", "RVOL", "rs_63d", "sector", "reclaim_level",
             "next_earnings", "earnings_blackout"]))
    L.append(""); L.append("**Tightness**")
    L.append(_table(tight, list(tight.columns) if tight is not None and not tight.empty else []))
    L.append(""); L.append("**Doji Snapback**")
    L.append(_table(doji, list(doji.columns) if doji is not None and not doji.empty else []))
    L.append("")
    L.append("## Watchlist")
    L.append(_table(wl, ["symbol", "close", "pct_1d", "pct_5d", "dist20_pct", "dist50_pct",
                         "pullback_flag", "setup_score"], limit=30))
    L.append("")
    if ingest_ok is False:
        L.append("## Ingest log"); L.append("```"); L.append(ingest_log.strip()[-4000:]); L.append("```")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true", help="run ingest.py before building")
    ap.add_argument("--out", help="output path (default briefs/YYYY-MM-DD.md)")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    today = dt.date.today()
    ingest_ok, log = None, ""
    if a.ingest:
        ingest_ok, log = run_ingest()
    if not os.path.exists(C.DB_PATH):
        print("no database — run ingest.py first"); return 2
    text = build(today, ingest_ok, log)
    out = a.out or os.path.join(BRIEF_DIR, f"{today:%Y-%m-%d}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text); print(f"[written] {out}")
    return 1 if ingest_ok is False else 0


if __name__ == "__main__":
    sys.exit(main())
