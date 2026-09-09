"""annotate_panel() must reproduce the per-symbol annotate() exactly — no network."""
import numpy as np
import pandas as pd

import exhaustion_trigger as et


def _synthetic(ticker, n=400, seed=0, start=50.0):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(0.0008, 0.02, n))
    o = close * (1 + rng.normal(0, 0.005, n))
    h = np.maximum(o, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    l = np.minimum(o, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    v = rng.integers(1_000_000, 5_000_000, n).astype(float)
    # sprinkle exact dojis / inside days so the quiet-candle branches are exercised
    for i in range(20, n, 37):
        o[i] = close[i]
    for i in range(30, n, 41):
        h[i] = h[i - 1] * 0.999; l[i] = l[i - 1] * 1.001
    return pd.DataFrame(dict(ticker=ticker, date=pd.bdate_range("2025-01-01", periods=n),
                             open=o, high=h, low=l, close=close, volume=v))


def test_annotate_panel_matches_per_symbol_annotate():
    panel = pd.concat([_synthetic("AAA", seed=1), _synthetic("BBB", seed=2, start=200.0),
                       _synthetic("CCC", seed=3, start=20.0)], ignore_index=True)
    wide = et.annotate_panel(panel)
    checked = 0
    for tkr, g in panel.groupby("ticker"):
        single = et.annotate(g.set_index("date")[["open", "high", "low", "close", "volume"]])
        w = wide[wide.ticker == tkr].set_index("date")
        for col in ["ctx", "leg", "nr", "inside", "doji", "dfly", "signal"]:
            assert (single[col].fillna(False).astype(bool).values
                    == w[col].fillna(False).astype(bool).values).all(), (tkr, col)
        assert (single["tight_run"].values == w["tight_run"].values).all(), (tkr, "tight_run")
        np.testing.assert_allclose(single["atr20"].values, w["atr20"].values, equal_nan=True)
        checked += 1
    assert checked == 3


def test_annotate_panel_drops_short_histories():
    panel = pd.concat([_synthetic("LONG", n=400), _synthetic("SHORT", n=100)], ignore_index=True)
    wide = et.annotate_panel(panel)
    assert set(wide.ticker) == {"LONG"}


def test_todays_hits_layout():
    panel = pd.concat([_synthetic("AAA", seed=1), _synthetic("BBB", seed=2)], ignore_index=True)
    hits = et.todays_hits(panel)
    if len(hits):
        assert list(hits.columns) == ["ticker", "date", "close", "tight_run", "doji", "dfly",
                                      "inside", "entry", "stop"]
        assert (hits.entry > hits.stop).all()
