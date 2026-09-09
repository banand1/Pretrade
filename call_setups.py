"""Call Setups tab for the pretrade dashboard.

Wraps exhaustion_trigger.py (leg-down + 1+ tight/doji days on leaders).
Drop both files next to dashboard.py, then in dashboard.py add:

    import call_setups
    ...
    tab_cs = st.tabs([...existing..., "Call Setups"])[-1]
    with tab_cs:
        call_setups.render()

Data: reads daily OHLCV from the app's DuckDB. Adjust DB below if your
table/column names differ; falls back to yfinance for missing tickers.
"""
import duckdb
import pandas as pd
import streamlit as st

import config as C
import exhaustion_trigger as et

DB = dict(
    path=C.DB_PATH,                # same store ingest.py writes (read-only here)
    table="prices",                # ingest.py table; column is `symbol`, aliased to `ticker`
    cols="symbol as ticker, date, open, high, low, close, volume",
)


@st.cache_data(ttl=3600)
def load_panel(tickers: tuple[str, ...]) -> pd.DataFrame:
    try:
        con = duckdb.connect(DB["path"], read_only=True)
        ph = ",".join("?" * len(tickers))
        df = con.execute(
            f"select {DB['cols']} from {DB['table']} "
            f"where symbol in ({ph}) order by symbol, date", list(tickers)
        ).df()
        con.close()
        have = set(df.ticker.unique()) if len(df) else set()
    except Exception:
        df, have = pd.DataFrame(), set()
    missing = [t for t in tickers if t not in have]
    if missing:
        try:
            df = pd.concat([df, et.load_yf(missing)], ignore_index=True)
        except Exception as e:
            st.warning(f"yfinance fallback failed for {missing}: {e}")
    if len(df):
        df["date"] = pd.to_datetime(df["date"])
    return df


def render():
    st.subheader("Call Setups — leg-down exhaustion on leaders")
    with st.expander("Call Setups params", expanded=False):
        p = dict(et.P)
        p["nr_frac"] = st.slider("Tight day: range ≤ x·ATR20", 0.3, 0.9, p["nr_frac"], 0.05)
        p["min_tight_run"] = st.slider("Min consecutive tight days", 1, 4, p["min_tight_run"])
        p["down_days"] = st.slider("Red days in last 4", 2, 4, p["down_days"])
        p["off_high_max"] = st.slider("Min % off 52w high", -0.20, -0.03, p["off_high_max"], 0.01)
        p["off_high_min"] = st.slider("Max % off 52w high", -0.45, -0.15, p["off_high_min"], 0.01)
        p["min_dollar_vol"] = st.number_input("Min avg $vol (M)", 10, 500, int(p["min_dollar_vol"] / 1e6)) * 1e6

    default_wl = "CRWD,AVGO,ANET,PANW,TENB,QLYS,S,TOST,BRZE,NBIS,CRWV,IREN,APLD,OKLO,RGTI"
    raw = st.text_area("Universe (comma/space separated, or paste DeepVue export)", default_wl, height=80)
    tickers = tuple(sorted({t.strip().upper() for t in raw.replace("\n", ",").replace(" ", ",").split(",") if t.strip()}))
    if not tickers:
        return
    panel = load_panel(tickers)
    if panel.empty:
        st.error("No OHLCV data for the universe.")
        return

    hits = et.todays_hits(panel, p)
    if len(hits):
        st.dataframe(hits, use_container_width=True, hide_index=True)
        st.caption("entry = break of tight-day high · stop = tight-day low · size per your 0.25–0.50% premium rule")
    else:
        st.info("No signals today. Watchlist below shows who's closest.")
        # nearest-miss table: leaders in a leg down, tightness building
        rows = []
        for tkr, g in panel.groupby("ticker"):
            g = g.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
            if len(g) < 260:
                continue
            d = et.annotate(g, p)
            r = d.iloc[-1]
            if r.ctx and d.leg.iloc[-4:].any():
                rows.append(dict(ticker=tkr, close=round(r.close, 2),
                                 tight_run=int(r.tight_run),
                                 rng_vs_atr=round((r.high - r.low) / r.atr20, 2) if r.atr20 else None))
        if rows:
            st.dataframe(pd.DataFrame(rows).sort_values("rng_vs_atr"),
                         use_container_width=True, hide_index=True)

    if st.button("Backtest universe (run locally, not on Cloud)"):
        with st.spinner("Backtesting…"):
            bt, stats = et.backtest(panel, p)
        st.write(stats)
        if isinstance(bt, pd.DataFrame) and len(bt):
            st.dataframe(bt.tail(50), use_container_width=True, hide_index=True)
            st.download_button("Download trades CSV", bt.to_csv(index=False), "exhaustion_backtest.csv")
