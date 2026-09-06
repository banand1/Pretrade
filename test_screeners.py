"""Offline unit tests for screeners.py — pure compute, no network."""
import datetime as dt
import numpy as np
import pandas as pd
import pytest

import screeners as S

TODAY = dt.date(2026, 9, 8)  # a Tuesday, plain trading day


def _ohlcv(rows, start="2020-01-01"):
    n = len(rows)
    dates = pd.bdate_range(start, periods=n)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    df.insert(0, "date", [d.date() for d in dates])
    return df


def flat_df(n, price=100.0, rng=2.0, vol=1_000_000):
    """No-trend chop — fails the leader gate's momentum test."""
    rows = []
    for i in range(n):
        c = price + (0.15 if i % 2 == 0 else -0.15)
        rows.append((price, price + rng / 2, price - rng / 2, c, vol))
    return _ohlcv(rows)


def uptrend_df(n, start=100.0, daily_ret=0.006, rng_pct=0.01, vol=1_000_000):
    """Smooth leader-shaped uptrend: clears both 63d/126d return thresholds
    (>= ~0.6%%/day) and keeps sma50 > sma200 with sma200 rising throughout."""
    rows = []
    close = start
    for i in range(n):
        o = close
        close = close * (1 + daily_ret)
        h = close * (1 + rng_pct / 2)
        l = close * (1 - rng_pct / 2)
        rows.append((o, h, l, close, vol))
    return _ohlcv(rows)


def append_bars(df, rows):
    n0 = len(df)
    tail = _ohlcv(rows, start=str(df["date"].iloc[-1] + dt.timedelta(days=1)))
    tail = tail.reset_index(drop=True)
    return pd.concat([df, tail], ignore_index=True)


LEADER_UNIVERSE_LEN = 230  # comfortably over MIN_BARS_FOR_LEADER (206)


# --------------------------------------------------------------------------- #
# 1. Non-leader excluded from all three panels
# --------------------------------------------------------------------------- #
def test_non_leader_excluded_from_all_panels():
    prices = {"FLAT": flat_df(LEADER_UNIVERSE_LEN)}
    sectors = {"XLK": flat_df(LEADER_UNIVERSE_LEN)}
    universe = S.build_universe(prices, sectors, spy_df=flat_df(LEADER_UNIVERSE_LEN))
    assert universe["FLAT"]["leader"] is False
    assert S.screen_leg_down(universe, {}, TODAY).empty
    assert S.screen_tightness(universe, {}, TODAY).empty
    assert S.screen_doji(universe, {}, TODAY, spy_dd=5).empty  # armed, still excluded


def test_leader_gate_passes_on_strong_uptrend():
    prices = {"LEAD": uptrend_df(LEADER_UNIVERSE_LEN)}
    sectors = {"XLK": flat_df(LEADER_UNIVERSE_LEN)}
    universe = S.build_universe(prices, sectors, spy_df=flat_df(LEADER_UNIVERSE_LEN))
    assert universe["LEAD"]["leader"] is True


# --------------------------------------------------------------------------- #
# 2. Tightness: tiny consecutive candles qualify; expanding range/volume fails
# --------------------------------------------------------------------------- #
def _tight_base(n_normal=27, price=100.0):
    df = flat_df(n_normal, price=price, rng=2.0, vol=1_000_000)
    df["atr14"] = S.atr(df, 14)
    return df

def test_tiny_candles_qualify_tightness():
    df = _tight_base()
    tiny = [
        (100.0, 100.3, 100.0, 100.2, 350_000),
        (100.2, 100.4, 100.1, 100.3, 340_000),
        (100.3, 100.5, 100.2, 100.4, 360_000),
    ]
    df = append_bars(df, tiny)
    df["atr14"] = S.atr(df, 14)
    i = len(df) - 1
    ok, metrics = S.three_tight_ok(df, i)
    assert ok is True
    assert metrics["vol_dry_ratio"] <= 0.80

def test_expanding_range_fails_tightness():
    df = _tight_base()
    tiny = [
        (100.0, 100.3, 100.0, 100.2, 350_000),
        (100.2, 100.4, 100.1, 100.3, 340_000),
        (100.3, 103.0, 100.2, 102.8, 1_800_000),  # expanding range + volume
    ]
    df = append_bars(df, tiny)
    df["atr14"] = S.atr(df, 14)
    i = len(df) - 1
    ok, _ = S.three_tight_ok(df, i)
    assert ok is False

def test_expanding_volume_alone_fails_tightness():
    df = _tight_base()
    tiny = [
        (100.0, 100.3, 100.0, 100.2, 350_000),
        (100.2, 100.4, 100.1, 100.3, 340_000),
        (100.3, 100.5, 100.2, 100.4, 2_200_000),  # tiny range, huge volume
    ]
    df = append_bars(df, tiny)
    df["atr14"] = S.atr(df, 14)
    i = len(df) - 1
    ok, _ = S.three_tight_ok(df, i)
    assert ok is False


# --------------------------------------------------------------------------- #
# 3. No look-ahead: appending future bars cannot alter a historical signal
# --------------------------------------------------------------------------- #
def test_no_lookahead_in_tightness_signal():
    df = _tight_base()
    tiny = [
        (100.0, 100.3, 100.0, 100.2, 350_000),
        (100.2, 100.4, 100.1, 100.3, 340_000),
        (100.3, 100.5, 100.2, 100.4, 360_000),
    ]
    short_df = append_bars(df, tiny)
    short_df["atr14"] = S.atr(short_df, 14)
    i = len(short_df) - 1

    future = [(100.4 + k * 0.5, 100.4 + k * 0.5 + 1, 100.4 + k * 0.5 - 1, 100.4 + k * 0.5, 900_000)
              for k in range(1, 8)]
    long_df = append_bars(short_df, future)
    long_df["atr14"] = S.atr(long_df, 14)

    ok_short, m_short = S.three_tight_ok(short_df, i)
    ok_long, m_long = S.three_tight_ok(long_df, i)
    assert ok_short == ok_long
    assert m_short == m_long

def test_no_lookahead_dd_days():
    close = pd.Series([100, 101, 102, 101.5, 101, 100.5, 100.2])
    dd_before = S.dd_days(close)
    close_extended = pd.concat([close, pd.Series([99, 98, 103])], ignore_index=True)
    dd_recomputed_at_same_point = S.dd_days(close_extended.iloc[:len(close)])
    assert dd_before == dd_recomputed_at_same_point


# --------------------------------------------------------------------------- #
# 4. Synthetic tight -> breakout moves BUILDING -> TRIGGERED
# --------------------------------------------------------------------------- #
def _tight_indicator_df():
    df = flat_df(28, price=100.0, rng=2.0, vol=1_000_000)
    tiny = [
        (100.0, 100.3, 100.0, 100.2, 350_000),
        (100.2, 100.4, 100.1, 100.3, 340_000),
        (100.3, 100.5, 100.2, 100.4, 360_000),
        (100.4, 100.6, 100.3, 100.5, 355_000),
        (100.5, 100.7, 100.4, 100.6, 345_000),
        (100.6, 100.8, 100.5, 100.7, 350_000),
    ]
    df = append_bars(df, tiny)
    return S.compute_indicators(df)

def test_tightness_building_then_triggered():
    d = _tight_indicator_df()
    row = S.screen_tightness_symbol("COIL", d, "XLK", None, None, TODAY)
    assert row is not None
    assert row["status"] == "BUILDING"
    assert row["tight_days"] >= 3
    breakout = row["breakout"]

    d2 = compute_after_breakout = S.compute_indicators(
        append_bars(d[["date", "open", "high", "low", "close", "volume"]],
                    [(100.7, breakout + 2.5, 100.6, breakout + 2.2, 3_000_000)]))
    row2 = S.screen_tightness_symbol("COIL", d2, "XLK", None, None, TODAY)
    assert row2 is not None
    assert row2["status"] == "TRIGGERED"


# --------------------------------------------------------------------------- #
# 5. Synthetic 3-day pullback to the 20-EMA -> up-close moves PULLBACK -> RESUMING
# --------------------------------------------------------------------------- #
def _legdown_base_df():
    """~33-bar uptrend, then a 3-session pullback landing in the legdown band
    (dd_days=3, off_high_ATR~3.3, dist_20ema_ATR~0.0 — verified numerically)."""
    base = uptrend_df(30, start=100.0, daily_ret=0.004, rng_pct=0.01, vol=1_000_000)
    last_close = base["close"].iloc[-1]
    mults = (0.995, 0.99, 0.985)
    rows, prev = [], last_close
    for m in mults:
        c = prev * m
        rows.append((prev, prev * 1.001, c * 0.997, c, 950_000))
        prev = c
    return append_bars(base, rows)

def test_legdown_pullback_then_resuming():
    df = _legdown_base_df()
    d = S.compute_indicators(df)
    row = S.screen_leg_down_symbol("LEAD", d, "XLK", 5.0, 2.0, None, TODAY)
    assert row is not None, "expected an eligible pullback row"
    assert row["status"] == "PULLBACK"
    assert 2 <= row["dd_days"] <= 5

    prior_high = df["high"].iloc[-1]
    reclaim = [(df["close"].iloc[-1], prior_high * 1.02, df["close"].iloc[-1] * 0.999,
                prior_high * 1.015, 2_500_000)]  # closes above prior high, big volume
    df2 = append_bars(df, reclaim)
    d2 = S.compute_indicators(df2)
    row2 = S.screen_leg_down_symbol("LEAD", d2, "XLK", 5.0, 2.0, None, TODAY)
    assert row2 is not None
    assert row2["status"] == "RESUMING"


# --------------------------------------------------------------------------- #
# 6. A/D divergence excludes a symbol from Leg Down
# --------------------------------------------------------------------------- #
def test_ad_divergence_excludes_from_legdown():
    df = _legdown_base_df().reset_index(drop=True)
    n = len(df)
    # Inject a run of down-closes with strictly increasing volume day over day,
    # across the trailing window (distribution outweighing accumulation).
    start = n - 8
    for k, i in enumerate(range(start, n)):
        df.loc[i, "close"] = df.loc[i - 1, "close"] * 0.995
        df.loc[i, "open"] = df.loc[i - 1, "close"]
        df.loc[i, "low"] = df.loc[i, "close"] * 0.99
        df.loc[i, "high"] = df.loc[i - 1, "close"] * 1.001
        df.loc[i, "volume"] = 2_000_000 + k * 500_000
    d = S.compute_indicators(df)
    assert S.ad_diverging(d) is True
    row = S.screen_leg_down_symbol("LEAD", d, "XLK", 5.0, 2.0, None, TODAY)
    assert row is None


# --------------------------------------------------------------------------- #
# 7. Doji panel armed only when SPY dd_days >= 4
# --------------------------------------------------------------------------- #
def _doji_leader_df():
    base = uptrend_df(220, start=100.0, daily_ret=0.006, rng_pct=0.01, vol=1_000_000)
    last_close = base["close"].iloc[-1]
    doji = [(last_close, last_close * 1.003, last_close * 0.997, last_close * 1.0015, 300_000)]
    return append_bars(base, doji)

def test_doji_unarmed_below_threshold():
    prices = {"LEAD": _doji_leader_df()}
    sectors = {"XLK": flat_df(230)}
    universe = S.build_universe(prices, sectors, spy_df=flat_df(230))
    assert universe["LEAD"]["leader"] is True
    assert S.screen_doji(universe, {}, TODAY, spy_dd=3).empty
    assert S.screen_doji(universe, {}, TODAY, spy_dd=None).empty

def test_doji_armed_flags_leader_doji():
    prices = {"LEAD": _doji_leader_df()}
    sectors = {"XLK": flat_df(230)}
    universe = S.build_universe(prices, sectors, spy_df=flat_df(230))
    out = S.screen_doji(universe, {}, TODAY, spy_dd=4)
    assert not out.empty
    assert set(out["symbol"]) == {"LEAD"}
    assert out.iloc[0]["status"] == "WATCH"


# --------------------------------------------------------------------------- #
# 8. Earnings within 2 sessions greys the row and suppresses its trigger
# --------------------------------------------------------------------------- #
def test_earnings_blackout_suppresses_legdown_trigger():
    df = _legdown_base_df()
    prior_high = df["high"].iloc[-1]
    reclaim = [(df["close"].iloc[-1], prior_high * 1.02, df["close"].iloc[-1] * 0.999,
                prior_high * 1.015, 2_500_000)]
    df2 = append_bars(df, reclaim)
    d2 = S.compute_indicators(df2)

    row_clear = S.screen_leg_down_symbol("LEAD", d2, "XLK", 5.0, 2.0, None, TODAY)
    assert row_clear["status"] == "RESUMING"

    row_blackout = S.screen_leg_down_symbol("LEAD", d2, "XLK", 5.0, 2.0, TODAY, TODAY)
    assert row_blackout is not None
    assert row_blackout["earnings_blackout"] is True
    assert row_blackout["status"] != "RESUMING"


# --------------------------------------------------------------------------- #
# 9. Missing OHLCV / fundamentals render N/A without crashing
# --------------------------------------------------------------------------- #
def test_missing_data_handled_gracefully():
    assert S.compute_indicators(None) is None
    assert S.compute_indicators(pd.DataFrame()) is None
    leader, diag = S.leader_gate(None, 0.0, 0.0)
    assert leader is False
    assert diag["rs_63d"] is None
    assert S.earnings_blackout(TODAY, None) is False

    tiny_df = S.compute_indicators(flat_df(5))
    assert tiny_df is None  # fewer than 30 bars -> pure "not enough history", no crash

    short_but_valid = S.compute_indicators(flat_df(35))
    assert S.screen_leg_down_symbol("X", short_but_valid, "XLK", None, None, None, TODAY) is None
    assert S.screen_tightness_symbol("X", short_but_valid, "XLK", None, None, TODAY) is None
    assert S.screen_doji_symbol("X", short_but_valid, "XLK", None, None, TODAY, spy_dd=5) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
