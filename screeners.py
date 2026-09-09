"""
screeners.py
------------
Pure screening logic for the three leader screener panels (Leg Down, Tightness,
Doji Snapback) plus the shared leader gate and macro-regime helpers.

No I/O here: everything takes pandas DataFrames / plain values and returns
DataFrames / dicts / bools. dashboard.py owns the DuckDB reads and rendering,
ingest.py owns the writes. Keep these functions pure so they stay unit-testable
offline (see CLAUDE.md).

Every OHLCV frame passed in is expected to be sorted ascending by date with a
plain RangeIndex (0..n-1) and columns: open, high, low, close, volume.
"""
from __future__ import annotations
import datetime as dt
import numpy as np
import pandas as pd
import config as C
import market_calendar as mc

# --------------------------------------------------------------------------- #
# generic price-series math
# --------------------------------------------------------------------------- #
def true_range(df: pd.DataFrame) -> pd.Series:
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Same formula as ingest._atr (SMA of true range), but returns the full series."""
    return true_range(df).rolling(period).mean()

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def pct_change_n(close: pd.Series, n: int) -> float | None:
    c = close.dropna()
    if len(c) < n + 1:
        return None
    a, b = c.iloc[-1], c.iloc[-1 - n]
    return round((a - b) / b * 100, 2) if b else None

def dd_days(close: pd.Series) -> int | None:
    """Sessions since the last 5-session closing high.

    Walk backward from the latest close; return how many bars back the most
    recent bar sits whose close was itself a 5-session closing high. 0 means
    today made a fresh high. A single up-close that is NOT a new high simply
    keeps incrementing (it is "tolerated" rather than mistaken for a reset),
    which falls out of this definition naturally since only genuine new highs
    reset the counter.
    """
    c = close.dropna().reset_index(drop=True)
    n = len(c)
    if n < 5:
        return None
    roll_max = c.rolling(5, min_periods=5).max()
    for back in range(n):
        i = n - 1 - back
        rm = roll_max.iloc[i]
        if pd.notna(rm) and c.iloc[i] >= rm - 1e-9:
            return back
    return n - 5

def rvol(df: pd.DataFrame, i: int | None = None) -> float | None:
    """Volume at bar i vs the trailing 20-session average ending the PRIOR bar."""
    i = len(df) - 1 if i is None else i
    if i < 1:
        return None
    avg = df["volume"].shift(1).rolling(20).mean().iloc[i]
    v = df["volume"].iloc[i]
    return round(v / avg, 2) if avg else None

def ad_diverging(df: pd.DataFrame, lookback: int = 10, min_distribution_days: int = 3) -> bool:
    """Per-stock accumulation/distribution proxy: True when higher-volume
    down-closes ("distribution") outnumber higher-volume up-closes over the
    trailing window and clear a minimum count — i.e. the stock is under
    quiet volume-backed selling even while price may still look fine.
    """
    if df is None or len(df) < lookback + 1:
        return False
    w = df.iloc[-lookback:]
    prior_vol = df["volume"].shift(1).iloc[-lookback:]
    down = w["close"] < w["close"].shift(1)
    up = w["close"] > w["close"].shift(1)
    higher_vol = w["volume"] > prior_vol
    dist_days = int((down & higher_vol).sum())
    accum_days = int((up & higher_vol).sum())
    return dist_days >= min_distribution_days and dist_days > accum_days

# --------------------------------------------------------------------------- #
# indicator frame + leader gate
# --------------------------------------------------------------------------- #
MIN_BARS_FOR_LEADER = 206  # 200sma + 5-bar "rising" lookback + 1

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame | None:
    """Attach sma20/50/200, ema20, atr14, vol20 columns. None if too little history."""
    if df is None or df.empty:
        return None
    d = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    if len(d) < 30:
        return None
    d["sma20"] = d["close"].rolling(20).mean()
    d["sma50"] = d["close"].rolling(50).mean()
    d["sma200"] = d["close"].rolling(200).mean()
    d["ema20"] = ema(d["close"], 20)
    d["atr14"] = atr(d, 14)
    d["vol20"] = d["volume"].rolling(20).mean()
    return d

def leader_gate(d: pd.DataFrame, sector_ret_63d: float | None, sector_ret_126d: float | None) -> tuple[bool, dict]:
    """Global pre-filter. Returns (is_leader, diagnostics)."""
    diag = {"ret_63d": None, "ret_126d": None, "rs_63d": None}
    if d is None or len(d) < MIN_BARS_FOR_LEADER:
        return False, diag
    last = d.iloc[-1]
    close, sma50, sma200 = last["close"], last["sma50"], last["sma200"]
    if pd.isna(sma50) or pd.isna(sma200) or pd.isna(close):
        return False, diag
    sma200_5ago = d["sma200"].iloc[-6]
    if pd.isna(sma200_5ago):
        return False, diag
    above_both = close > sma50 and close > sma200 and sma50 > sma200
    sma200_rising = sma200 > sma200_5ago
    ret63 = pct_change_n(d["close"], 63)
    ret126 = pct_change_n(d["close"], 126)
    diag["ret_63d"], diag["ret_126d"] = ret63, ret126
    momentum_ok = ((ret63 is not None and ret63 >= C.LEADER_RET_63D) or
                    (ret126 is not None and ret126 >= C.LEADER_RET_126D))
    rs63 = None
    if ret63 is not None and sector_ret_63d is not None:
        rs63 = round(ret63 - sector_ret_63d, 2)
    diag["rs_63d"] = rs63
    rs_ok = rs63 is not None and rs63 > 0
    leader = bool(above_both and sma200_rising and momentum_ok and rs_ok)
    return leader, diag

# --------------------------------------------------------------------------- #
# earnings blackout
# --------------------------------------------------------------------------- #
def earnings_blackout(today: dt.date, earnings_date: dt.date | None,
                       blackout_days: int = None) -> bool:
    blackout_days = C.EARNINGS_BLACKOUT_DAYS if blackout_days is None else blackout_days
    if earnings_date is None or earnings_date < today:
        return False
    if earnings_date == today:
        return True
    return 0 < mc.trading_days_between(today, earnings_date) <= blackout_days

# --------------------------------------------------------------------------- #
# PANEL 1 — Leg Down
# --------------------------------------------------------------------------- #
def _pullback_window(d: pd.DataFrame, dd: int) -> pd.DataFrame:
    n = len(d)
    start = max(0, n - 1 - dd)
    return d.iloc[start:n]

def _leg_down_snapshot(sub: pd.DataFrame) -> dict | None:
    """dd_days / off_high_ATR / dist_20ema_ATR / pullback_low as of the last bar of `sub`."""
    if len(sub) < 6:
        return None
    close, atr_v, ema20 = sub["close"].iloc[-1], sub["atr14"].iloc[-1], sub["ema20"].iloc[-1]
    if pd.isna(atr_v) or atr_v == 0 or pd.isna(ema20):
        return None
    dd = dd_days(sub["close"])
    if dd is None:
        return None
    high5 = sub["high"].rolling(5).max().iloc[-1]
    off = round(float((high5 - close) / atr_v), 2) if pd.notna(high5) else None
    dist = round(float((close - ema20) / atr_v), 2)
    win = _pullback_window(sub, dd)
    return {"dd": dd, "off": off, "dist": dist, "pullback_low": round(float(win["low"].min()), 2)}

def _in_legdown_band(snap: dict | None) -> bool:
    return bool(snap and C.LEGDOWN_DD_MIN <= snap["dd"] <= C.LEGDOWN_DD_MAX and
                snap["off"] is not None and C.LEGDOWN_OFF_HIGH_ATR[0] <= snap["off"] <= C.LEGDOWN_OFF_HIGH_ATR[1] and
                C.LEGDOWN_DIST_20EMA_ATR[0] <= snap["dist"] <= C.LEGDOWN_DIST_20EMA_ATR[1])

def screen_leg_down_symbol(sym: str, d: pd.DataFrame, sector: str, rs_63d: float | None,
                            sector_rs: float | None, next_earnings: dt.date | None,
                            today: dt.date) -> dict | None:
    """Returns a row dict for Panel 1, or None if the symbol has no active signal.

    Eligibility/off-high/dist-20ema are evaluated against whichever of "today" or
    "yesterday" actually falls in the leg-down band — a reclaim day's own close can
    push dd_days / off_high_ATR back out of range, and RESUMING must still fire off
    the pullback that was established as of yesterday.
    """
    n = len(d)
    if n < 7:
        return None
    if ad_diverging(d):
        return None  # hard exclude, unlike earnings blackout below

    today_snap = _leg_down_snapshot(d)
    yday_snap = _leg_down_snapshot(d.iloc[:-1])
    if today_snap is None:
        return None
    if today_snap["dd"] > C.LEGDOWN_DD_MAX + 5 and (yday_snap is None or yday_snap["dd"] > C.LEGDOWN_DD_MAX + 5):
        return None  # stale — no plausible recent pullback

    blackout = earnings_blackout(today, next_earnings)
    close, atr_v = d["close"].iloc[-1], d["atr14"].iloc[-1]
    prior_high = d["high"].iloc[-2]
    rv = rvol(d)

    today_eligible, yday_eligible = _in_legdown_band(today_snap), _in_legdown_band(yday_snap)
    basis = today_snap if today_eligible else (yday_snap if yday_eligible else today_snap)
    pullback_low = basis["pullback_low"]

    status = None
    if today_eligible or yday_eligible:
        resuming = ((close > prior_high or close >= pullback_low + 0.5 * atr_v) and
                    rv is not None and rv >= C.LEGDOWN_RVOL)
        if resuming:
            status = "PULLBACK" if blackout else "RESUMING"  # blackout suppresses the trigger
        elif today_eligible:
            status = "PULLBACK"
    if status is None:
        failed = close < pullback_low or today_snap["dist"] < C.LEGDOWN_DIST_20EMA_ATR[0]
        if failed:
            status = "FAILED"
    if status is None:
        return None

    return {
        "symbol": sym, "status": status, "close": round(float(close), 2),
        "dd_days": today_snap["dd"], "off_high_ATR": today_snap["off"], "dist_20ema_ATR": today_snap["dist"],
        "RVOL": rv, "rs_63d": rs_63d,
        "sector": sector, "sector_RS": sector_rs,
        "pullback_low": pullback_low, "reclaim_level": round(float(prior_high), 2),
        "next_earnings": next_earnings, "earnings_blackout": blackout,
    }

# --------------------------------------------------------------------------- #
# PANEL 2 — Tightness
# --------------------------------------------------------------------------- #
def three_tight_ok(d: pd.DataFrame, i: int) -> tuple[bool, dict]:
    """Evaluate the 3-tight test using bars (i-2, i-1, i) as the base."""
    if i < 22 or i >= len(d):
        return False, {}
    atr_i = d["atr14"].iloc[i]
    if pd.isna(atr_i) or atr_i == 0:
        return False, {}
    base = d.iloc[i - 2:i + 1]
    highs, lows, closes = base["high"], base["low"], base["close"]
    span_atr = round(float((highs.max() - lows.min()) / atr_i), 2)
    close_spread_atr = round(float((closes.max() - closes.min()) / atr_i), 2)
    low_cluster_atr = round(float((lows.max() - lows.min()) / atr_i), 2)
    two_day_net_atr = round(float(abs(d["close"].iloc[i] - d["close"].iloc[i - 2]) / atr_i), 2)
    vol3 = d["volume"].iloc[i - 2:i + 1].mean()
    vol20_before = d["volume"].shift(3).rolling(20).mean().iloc[i]
    vol_dry_ratio = round(float(vol3 / vol20_before), 2) if vol20_before else None
    rng = (highs - lows)
    close_pos = ((closes - lows) / rng.replace(0, np.nan)).fillna(1.0)
    upper_close_count = int((close_pos >= 0.5).sum())
    tr = true_range(d).iloc[i - 2:i + 1]
    shrinking_tr = bool(tr.notna().all() and tr.iloc[2] < tr.iloc[1] < tr.iloc[0])
    ok = (span_atr <= C.TIGHT_SPAN_ATR and close_spread_atr <= C.TIGHT_CLOSE_SPREAD_ATR and
          low_cluster_atr <= C.TIGHT_LOW_CLUSTER_ATR and two_day_net_atr <= C.TIGHT_TWO_DAY_NET_ATR and
          vol_dry_ratio is not None and vol_dry_ratio <= C.TIGHT_VOL_DRY and
          upper_close_count >= 2)
    metrics = dict(span_atr=span_atr, close_spread_atr=close_spread_atr,
                   low_cluster_atr=low_cluster_atr, two_day_net_atr=two_day_net_atr,
                   vol_dry_ratio=vol_dry_ratio, upper_close_count=upper_close_count,
                   shrinking_tr=shrinking_tr, breakout=round(float(highs.max()), 2),
                   stop=round(float(lows.min()), 2))
    return ok, metrics

def tight_days_count(d: pd.DataFrame) -> int:
    n = len(d)
    days, i = 0, n - 1
    while i >= 0:
        ok, _ = three_tight_ok(d, i)
        if not ok:
            break
        days += 1
        i -= 1
    return days

def screen_tightness_symbol(sym: str, d: pd.DataFrame, sector: str, sector_rs: float | None,
                             next_earnings: dt.date | None, today: dt.date) -> dict | None:
    n = len(d)
    if n < 24:
        return None
    close = d["close"].iloc[-1]
    atr_v = d["atr14"].iloc[-1]
    if pd.isna(atr_v) or atr_v == 0:
        return None
    blackout = earnings_blackout(today, next_earnings)

    # Earnings blackout never excludes the row — it only greys it and suppresses the
    # bullish TRIGGERED signal below (INVALIDATED is a risk warning, not a buy trigger).
    prior_ok, prior_m = three_tight_ok(d, n - 2)
    status, metrics = None, None
    if prior_ok:
        rv = rvol(d)
        broke_out = close > prior_m["breakout"] and rv is not None and rv >= C.TIGHT_RVOL
        if broke_out and not blackout:
            status, metrics = "TRIGGERED", prior_m
        elif close < prior_m["stop"]:
            status, metrics = "INVALIDATED", prior_m
        elif broke_out and blackout:
            status, metrics = "BUILDING", prior_m

    if status is None:
        now_ok, now_m = three_tight_ok(d, n - 1)
        td = tight_days_count(d)
        if now_ok and td >= 3:
            status, metrics = "BUILDING", {**now_m, "tight_days": td}

    if status is None:
        return None

    ret63 = pct_change_n(d["close"], 63)
    high63 = d["high"].iloc[-63:].max() if n >= 63 else d["high"].max()
    off_63d_high = round(float((close - high63) / high63 * 100), 2) if high63 else None
    atr_pct = round(float(atr_v / close * 100), 2) if close else None
    return {
        "symbol": sym, "status": status, "close": round(float(close), 2),
        "tight_days": metrics.get("tight_days", tight_days_count(d)),
        "coil_age": metrics.get("tight_days", tight_days_count(d)) + 2,
        "ATR_dollar": round(float(atr_v), 2), "ATR_pct": atr_pct,
        "span_ATR": metrics.get("span_atr"), "low_cluster_ATR": metrics.get("low_cluster_atr"),
        "vol_dry_ratio": metrics.get("vol_dry_ratio"), "shrinking_TR": metrics.get("shrinking_tr"),
        "return_63d": ret63, "off_63d_high_pct": off_63d_high,
        "breakout": metrics.get("breakout"), "stock_stop": metrics.get("stop"),
        "sector": sector, "sector_RS": sector_rs,
        "next_earnings": next_earnings, "earnings_blackout": blackout,
    }

# --------------------------------------------------------------------------- #
# PANEL 3 — Doji Snapback
# --------------------------------------------------------------------------- #
def doji_check(row, atr_val: float | None, vol20avg: float | None) -> tuple[bool, dict]:
    o, h, l, c, v = row["open"], row["high"], row["low"], row["close"], row["volume"]
    rng = h - l
    body = abs(c - o)
    body_ratio = 0.0 if rng == 0 else round(float(body / rng), 2)
    close_position = 1.0 if rng == 0 else round(float((c - l) / rng), 2)
    range_atr = round(float(rng / atr_val), 2) if atr_val else None
    vol_ratio = round(float(v / vol20avg), 2) if vol20avg else None
    is_doji = (body_ratio <= C.DOJI_BODY_MAX and close_position >= 0.5 and
               range_atr is not None and range_atr <= C.DOJI_RANGE_ATR and
               vol_ratio is not None and vol_ratio <= C.DOJI_VOL)
    return is_doji, dict(body_ratio=body_ratio, close_position=close_position,
                          range_atr=range_atr, vol_ratio=vol_ratio, low=l, high=h)

def _holding_support(d: pd.DataFrame, i: int) -> bool:
    close, ema20 = d["close"].iloc[i], d["ema20"].iloc[i]
    if pd.notna(ema20) and close > ema20:
        return True
    if i >= 13:
        prior_low = d["low"].iloc[i - 13:i - 3].min()
        return bool(pd.notna(prior_low) and close > prior_low)
    return False

def screen_doji_symbol(sym: str, d: pd.DataFrame, sector: str, rs_63d: float | None,
                        next_earnings: dt.date | None, today: dt.date, spy_dd: int) -> dict | None:
    if spy_dd is None or spy_dd < C.SPY_FLUSH_DAYS:
        return None
    n = len(d)
    if n < 24:
        return None
    blackout = earnings_blackout(today, next_earnings)
    atr_v = d["atr14"].iloc[-1]
    vol20_prior = d["volume"].shift(1).rolling(20).mean()

    today_row = d.iloc[-1]
    is_doji_today, m_today = doji_check(today_row, atr_v, vol20_prior.iloc[-1])
    if is_doji_today and _holding_support(d, n - 1):
        status, doji_low, doji_high = "WATCH", m_today["low"], m_today["high"]
        metrics = m_today
    else:
        yst_row = d.iloc[-2]
        atr_y = d["atr14"].iloc[-2]
        is_doji_y, m_y = doji_check(yst_row, atr_y, vol20_prior.iloc[-2])
        if is_doji_y and _holding_support(d, n - 2):
            doji_low, doji_high, metrics = m_y["low"], m_y["high"], m_y
            close = today_row["close"]
            if close > doji_high:
                status = "WATCH" if blackout else "CONFIRMED"  # blackout suppresses the trigger
            elif close < doji_low:
                status = "FAILED"
            else:
                status = "WATCH"
        else:
            return None

    return {
        "symbol": sym, "status": status, "close": round(float(today_row["close"]), 2),
        "spy_dd_days": spy_dd, "doji_body_ratio": metrics["body_ratio"],
        "close_position": metrics["close_position"], "vol_ratio": metrics["vol_ratio"],
        "dist_20ema_ATR": round(float((today_row["close"] - d["ema20"].iloc[-1]) / atr_v), 2) if atr_v else None,
        "rs_63d": rs_63d, "sector": sector,
        "doji_low": round(float(doji_low), 2), "reclaim_level": round(float(doji_high), 2),
        "next_earnings": next_earnings, "earnings_blackout": blackout,
    }

# --------------------------------------------------------------------------- #
# macro regime
# --------------------------------------------------------------------------- #
def market_ad_diverging(pct1d_series: pd.Series, spy_pct1d: float | None) -> bool:
    """True when SPY is up on the day but breadth across the screener universe
    doesn't confirm (decliners outnumber advancers)."""
    if pct1d_series is None or spy_pct1d is None:
        return False
    s = pct1d_series.dropna()
    if s.empty:
        return False
    adv, dec = int((s > 0).sum()), int((s < 0).sum())
    return bool(spy_pct1d > 0 and dec > adv)

def regime_tag(ema50_rising: bool | None, ad_diverging_market: bool) -> str:
    return "GREEN" if (ema50_rising and not ad_diverging_market) else "RED"

# --------------------------------------------------------------------------- #
# orchestration — builds the shared leader universe once per run
# --------------------------------------------------------------------------- #
def build_universe(prices_map: dict[str, pd.DataFrame], sector_prices_map: dict[str, pd.DataFrame],
                    spy_df: pd.DataFrame | None = None, sector_of=None) -> dict[str, dict]:
    """prices_map: {symbol: OHLCV df (date,open,high,low,close,volume) ascending}.
    sector_prices_map: same shape, keyed by sector ETF symbol (e.g. 'XLK').
    spy_df: SPY's OHLCV, used to compute sector_RS = sector's 20d return vs SPY's 20d
    return (the existing sector-rotation RS already shown in panel_sectors, reused here
    at the per-stock level via each stock's mapped sector).

    Returns {symbol: {"d": indicator_df, "leader": bool, "sector": str,
                       "rs_63d": float|None (stock vs sector, from the leader gate),
                       "sector_RS": float|None (sector vs SPY), "ret_63d": float|None}}
    for every symbol with enough history.
    """
    sector_ind = {s: compute_indicators(df) for s, df in sector_prices_map.items()}
    sector_ret63 = {s: pct_change_n(d["close"], 63) if d is not None else None
                    for s, d in sector_ind.items()}
    sector_ret126 = {s: pct_change_n(d["close"], 126) if d is not None else None
                     for s, d in sector_ind.items()}
    spy_ind = compute_indicators(spy_df) if spy_df is not None else None
    spy_ret20 = pct_change_n(spy_ind["close"], 20) if spy_ind is not None else None
    sector_ret20 = {s: pct_change_n(d["close"], 20) if d is not None else None
                    for s, d in sector_ind.items()}
    sector_rs_map = {s: round(r - spy_ret20, 2) if r is not None and spy_ret20 is not None else None
                     for s, r in sector_ret20.items()}

    out = {}
    min_price = getattr(C, "MIN_PRICE", 0.0)
    sector_of = sector_of or C.sector_of
    for sym, raw in prices_map.items():
        d = compute_indicators(raw)
        if d is not None and len(d) and float(d["close"].iloc[-1]) < min_price:
            continue                      # price floor: not screened at all
        sector = sector_of(sym)
        is_leader, diag = leader_gate(d, sector_ret63.get(sector), sector_ret126.get(sector))
        out[sym] = {"d": d, "leader": is_leader, "sector": sector,
                    "sector_RS": sector_rs_map.get(sector), **diag}
    return out

def screen_leg_down(universe: dict, earnings: dict, today: dt.date) -> pd.DataFrame:
    rows = [r for sym, u in universe.items() if u["leader"]
            for r in [screen_leg_down_symbol(sym, u["d"], u["sector"], u["rs_63d"],
                                              u["sector_RS"], earnings.get(sym), today)] if r]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["dd_days", "rs_63d"], ascending=[False, False])
    return df

def screen_tightness(universe: dict, earnings: dict, today: dt.date) -> pd.DataFrame:
    rows = [r for sym, u in universe.items() if u["leader"]
            for r in [screen_tightness_symbol(sym, u["d"], u["sector"], u["sector_RS"],
                                               earnings.get(sym), today)] if r]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("tight_days", ascending=False)
    return df

def screen_doji(universe: dict, earnings: dict, today: dt.date, spy_dd: int | None) -> pd.DataFrame:
    if spy_dd is None or spy_dd < C.SPY_FLUSH_DAYS:
        return pd.DataFrame()
    rows = [r for sym, u in universe.items() if u["leader"]
            for r in [screen_doji_symbol(sym, u["d"], u["sector"], u["rs_63d"],
                                          earnings.get(sym), today, spy_dd)] if r]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("rs_63d", ascending=False)
    return df
