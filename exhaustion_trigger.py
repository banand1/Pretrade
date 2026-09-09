#!/usr/bin/env python3
"""Leader leg-down exhaustion trigger — exact version of the DeepVue screens,
plus the two things DeepVue can't express:
  1. literal "3 of last 4 days down" leg detection
  2. "1+ consecutive tight/inside days" (not just today's bar)
Includes a forward-return backtest to calibrate thresholds.

Usage:
    python exhaustion_trigger.py --csv ohlcv.csv                      # ticker,date,open,high,low,close,volume
    python exhaustion_trigger.py --tickers CRWD,AVGO,ANET --src yf
    python exhaustion_trigger.py --tickers-file uni.txt --src eodhd --key YOURKEY
    python exhaustion_trigger.py --csv ohlcv.csv --backtest           # stats instead of today's hits
"""
import argparse
import numpy as np
import pandas as pd

P = dict(
    # context: leader in a leg down, structure intact
    min_price=5.0, min_dollar_vol=50e6,
    off_high_min=-0.30, off_high_max=-0.08, high_lookback=252,
    min_6m_gain=0.15,          # leader proxy; swap for RS vs SPY if benchmark given
    # leg down
    down_days=3, down_window=4, leg_ret_5d=-0.03,
    # quiet candle
    nr_frac=0.60,              # range <= frac * ATR20  -> tight day
    doji_body=0.15,            # |C-O| <= frac of range
    dfly_upper=0.20, dfly_lower=0.50,
    min_tight_run=1,           # 1+ consecutive tight days required
    # backtest
    fwd=(5, 10, 20),
)


def _atr(d, n=20):
    pc = d.close.shift()
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def annotate(df, p=P):
    d = df.sort_index().copy()
    d["sma5"] = d.close.rolling(5).mean()
    d["sma200"] = d.close.rolling(200).mean()
    d["vol50"] = d.volume.rolling(50).mean()
    d["atr20"] = _atr(d)
    rng = (d.high - d.low)
    body = (d.close - d.open).abs()
    up_sh = d.high - d[["open", "close"]].max(axis=1)
    lo_sh = d[["open", "close"]].min(axis=1) - d.low
    hi252 = d.close.rolling(p["high_lookback"], min_periods=60).max()

    # ---- context ----
    d["ctx"] = (
        (d.close >= p["min_price"])
        & (d.close * d.vol50 >= p["min_dollar_vol"])
        & (d.close > d.sma200)
        & (d.close / hi252 - 1).between(p["off_high_min"], p["off_high_max"])
        & (d.close / d.close.shift(126) - 1 >= p["min_6m_gain"])
    )

    # ---- leg down: 3 of last 4 red OR -3% in 5d; and below 5SMA ----
    red = (d.close < d.close.shift()).astype(int)
    d["leg"] = (
        ((red.rolling(p["down_window"]).sum() >= p["down_days"])
         | (d.close.pct_change(5) <= p["leg_ret_5d"]))
        & (d.close < d.sma5)
    )

    # ---- quiet candle types (today's bar) ----
    safe_rng = rng.replace(0, np.nan)
    d["nr"] = rng <= p["nr_frac"] * d.atr20.shift()
    d["inside"] = (d.high < d.high.shift()) & (d.low > d.low.shift())
    d["doji"] = body <= p["doji_body"] * safe_rng
    d["dfly"] = d.doji & (up_sh <= p["dfly_upper"] * safe_rng) & (lo_sh >= p["dfly_lower"] * safe_rng)
    quiet = d.nr | d.inside | d.doji
    d["tight_run"] = np.where(quiet, quiet.groupby((~quiet).cumsum()).cumcount() + 1, 0)

    # ---- signal: leg down was active going into the quiet cluster ----
    leg_recent = d.leg.rolling(3, min_periods=1).max().astype(bool)
    d["signal"] = d.ctx & leg_recent & (d.tight_run >= p["min_tight_run"])
    return d


def todays_hits(panel, p=P):
    rows = []
    for tkr, g in panel.groupby("ticker"):
        g = g.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        if len(g) < 260:
            continue
        d = annotate(g, p)
        r = d.iloc[-1]
        if r.signal:
            rows.append(dict(ticker=tkr, date=d.index[-1].date(), close=round(r.close, 2),
                             tight_run=int(r.tight_run), doji=bool(r.doji), dfly=bool(r.dfly),
                             inside=bool(r.inside), entry=round(r.high + 0.01, 2),
                             stop=round(r.low - 0.01, 2)))
    return pd.DataFrame(rows).sort_values("tight_run", ascending=False) if rows else pd.DataFrame()


def backtest(panel, p=P):
    """Forward returns from each historical signal; entry = next-day break of signal-bar high."""
    recs = []
    for tkr, g in panel.groupby("ticker"):
        g = g.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        if len(g) < 300:
            continue
        d = annotate(g, p)
        sig_idx = np.flatnonzero(d.signal.values)
        for i in sig_idx:
            if i + 1 >= len(d) or i + max(p["fwd"]) + 1 >= len(d):
                continue
            trig_hi, trig_lo = d.high.iloc[i], d.low.iloc[i]
            nxt = d.iloc[i + 1]
            if nxt.high <= trig_hi:          # never triggered -> no trade
                continue
            entry = max(nxt.open, trig_hi + 0.01)
            rec = dict(ticker=tkr, date=d.index[i].date(), entry=entry,
                       risk=(entry - trig_lo) / entry)
            for f in p["fwd"]:
                rec[f"ret_{f}d"] = d.close.iloc[i + 1 + f] / entry - 1
            stop_hit = (d.low.iloc[i + 1:i + 11] < trig_lo).any()
            rec["stopped_10d"] = bool(stop_hit)
            recs.append(rec)
    bt = pd.DataFrame(recs)
    if bt.empty:
        return bt, "no historical signals"
    s = {f"avg_ret_{f}d": f"{bt[f'ret_{f}d'].mean():.2%}" for f in p["fwd"]}
    s |= {f"win_{f}d": f"{(bt[f'ret_{f}d'] > 0).mean():.0%}" for f in p["fwd"]}
    s |= dict(n_trades=len(bt), stopped_in_10d=f"{bt.stopped_10d.mean():.0%}",
              med_risk=f"{bt.risk.median():.2%}")
    return bt, s


# ---------------- loaders ----------------
def load_yf(tickers, period="2y"):
    import yfinance as yf
    raw = yf.download(tickers, period=period, auto_adjust=False,
                      group_by="ticker", progress=False)
    out = []
    for t in tickers:
        g = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
        g = g.dropna().reset_index()
        g.columns = [c.lower() for c in g.columns]
        g["ticker"] = t
        out.append(g[["ticker", "date", "open", "high", "low", "close", "volume"]])
    return pd.concat(out, ignore_index=True)


def load_massive(tickers, key, period_days=730, rpm=5):
    """Massive (formerly Polygon.io). Free tier: EOD, 2yr history, 5 req/min."""
    import requests, time, datetime as dt
    frm = (dt.date.today() - dt.timedelta(days=period_days)).isoformat()
    to = dt.date.today().isoformat()
    out, delay = [], 60.0 / max(rpm, 1)
    for t in tickers:
        r = requests.get(
            f"https://api.massive.com/v2/aggs/ticker/{t}/range/1/day/{frm}/{to}",
            params=dict(adjusted="true", sort="asc", limit=50000, apiKey=key), timeout=30)
        js = r.json()
        for res in js.get("results", []) or []:
            out.append(dict(ticker=t, date=pd.Timestamp(res["t"], unit="ms").normalize(),
                            open=res["o"], high=res["h"], low=res["l"],
                            close=res["c"], volume=res["v"]))
        time.sleep(delay)
    return pd.DataFrame(out)


def load_eodhd(tickers, key, period_days=730):
    import requests, datetime as dt
    frm = (dt.date.today() - dt.timedelta(days=period_days)).isoformat()
    out = []
    for t in tickers:
        r = requests.get(f"https://eodhd.com/api/eod/{t}.US",
                         params=dict(api_token=key, fmt="json", from_=frm), timeout=30)
        rows = r.json()
        if not isinstance(rows, list):
            continue
        g = pd.DataFrame(rows).rename(columns={"adjusted_close": "adjclose"})
        g["ticker"] = t
        g["date"] = pd.to_datetime(g["date"])
        out.append(g[["ticker", "date", "open", "high", "low", "close", "volume"]])
    return pd.concat(out, ignore_index=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--tickers")
    ap.add_argument("--tickers-file")
    ap.add_argument("--src", choices=["yf", "eodhd", "massive"], default="yf")
    ap.add_argument("--key", help="EODHD or Massive api token")
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()

    if a.csv:
        panel = pd.read_csv(a.csv, parse_dates=["date"])
        panel.columns = [c.lower() for c in panel.columns]
    else:
        tk = (open(a.tickers_file).read().split() if a.tickers_file else a.tickers.split(","))
        tk = [t.strip().upper() for t in tk if t.strip()]
        if a.src == "massive":
            panel = load_massive(tk, a.key)
        elif a.src == "eodhd":
            panel = load_eodhd(tk, a.key)
        else:
            panel = load_yf(tk)

    if a.backtest:
        bt, stats = backtest(panel)
        print(stats)
        if not bt.empty:
            bt.to_csv("exhaustion_backtest.csv", index=False)
            print("trades -> exhaustion_backtest.csv")
    else:
        hits = todays_hits(panel)
        print(hits.to_string(index=False) if len(hits) else "no signals today")
