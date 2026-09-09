"""Dynamic US-stock universe (keyless).

Source: NASDAQ's public screener feed (all US-listed common stocks with last price,
volume, market cap, sector, country). Filtered to liquid US names at/above
config.MIN_PRICE and stored in the `universe` table so every screen (tight flags,
call setups, leader screens, scanner, brief) can run over 1000+ symbols locally.

    refresh(con, today)      -> DataFrame of qualifying symbols (also upserts `universe`)
    symbols(con, today=None) -> sorted list: curated config lists ∪ latest universe
    sector_lookup(con)       -> {symbol: SPDR sector ETF} for the leader gate

Pure helpers (`parse_rows`, `qualify`) take plain data so they are testable offline.
"""
from __future__ import annotations
import datetime as dt, re, sys
import pandas as pd
import config as C

SCREENER_URL = ("https://api.nasdaq.com/api/screener/stocks"
                "?tableonly=true&limit=10000&offset=0&download=true")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# NASDAQ sector label -> SPDR sector ETF (config.SECTORS keys) for the leader gate's
# sector-relative-strength test. Unmapped sectors fall back to config.sector_of().
SECTOR_ETF = {
    "Technology": "XLK", "Telecommunications": "XLC", "Health Care": "XLV",
    "Finance": "XLF", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Industrials": "XLI", "Energy": "XLE", "Utilities": "XLU", "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}

# Names that are not operating common stocks even though they sit in the stocks feed.
_EXCLUDE_NAME = re.compile(
    r"\b(?:warrant|warrants|unit|units|right|rights|preferred|depositary|notes?|debentures?|"
    r"acquisition corp|acquisition corporation|acquisition co|SPAC|trust|fund|ETF|ETN|"
    r"\d+(?:\.\d+)?%)\b", re.I)   # trailing \b: 'unit' must not match Unity / United
_BAD_SYMBOL = re.compile(r"[^A-Z]")          # keep plain tickers only (no ^ / . - suffixes)

UNIVERSE_COLS = ["as_of", "symbol", "name", "sector", "industry", "country",
                 "last", "volume", "market_cap", "dollar_vol"]


def _num(x) -> float | None:
    if x is None: return None
    s = str(x).replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "NA", "N/A"): return None
    try: return float(s)
    except ValueError: return None


def parse_rows(rows: list[dict]) -> pd.DataFrame:
    """Raw screener rows -> tidy frame (pure)."""
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        last, vol, mc = _num(r.get("lastsale")), _num(r.get("volume")), _num(r.get("marketCap"))
        out.append(dict(symbol=sym, name=(r.get("name") or "").strip(),
                        sector=(r.get("sector") or "").strip(),
                        industry=(r.get("industry") or "").strip(),
                        country=(r.get("country") or "").strip(),
                        last=last, volume=vol, market_cap=mc,
                        dollar_vol=(last * vol) if last is not None and vol is not None else None))
    return pd.DataFrame(out, columns=UNIVERSE_COLS[1:])


def qualify(df: pd.DataFrame, min_price: float | None = None,
            min_dollar_vol: float | None = None, min_market_cap: float | None = None,
            us_only: bool | None = None) -> pd.DataFrame:
    """Liquid US operating companies at/above the price floor (pure)."""
    min_price = C.MIN_PRICE if min_price is None else min_price
    min_dollar_vol = getattr(C, "UNIVERSE_MIN_DOLLAR_VOL", 10e6) if min_dollar_vol is None else min_dollar_vol
    min_market_cap = getattr(C, "UNIVERSE_MIN_MARKET_CAP", 300e6) if min_market_cap is None else min_market_cap
    us_only = getattr(C, "UNIVERSE_US_ONLY", True) if us_only is None else us_only
    m = (df["symbol"].str.len().between(1, 5)
         & ~df["symbol"].str.contains(_BAD_SYMBOL)
         & ~df["name"].str.contains(_EXCLUDE_NAME)
         & df["last"].ge(min_price)
         & df["dollar_vol"].ge(min_dollar_vol)
         & df["market_cap"].ge(min_market_cap))
    if us_only:
        m &= df["country"].eq("United States")
    return (df[m].drop_duplicates("symbol").sort_values("dollar_vol", ascending=False)
            .reset_index(drop=True))


def fetch_screener(timeout: int = 60) -> list[dict]:
    import requests
    r = requests.get(SCREENER_URL, headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=timeout)
    r.raise_for_status()
    d = r.json().get("data") or {}
    return d.get("rows") or (d.get("table") or {}).get("rows") or []


def create_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS universe(as_of DATE, symbol VARCHAR, name VARCHAR,
        sector VARCHAR, industry VARCHAR, country VARCHAR, last DOUBLE, volume DOUBLE,
        market_cap DOUBLE, dollar_vol DOUBLE, PRIMARY KEY(as_of, symbol))""")


def refresh(con, today: dt.date | None = None) -> pd.DataFrame:
    """Fetch, filter, and upsert today's universe. Returns the qualifying frame.
    On a network failure returns the most recent stored universe instead."""
    today = today or dt.date.today()
    create_table(con)
    try:
        q = qualify(parse_rows(fetch_screener()))
    except Exception as e:
        print(f"  ! universe refresh failed ({e}); using last stored universe", file=sys.stderr)
        return latest(con)
    q.insert(0, "as_of", today)
    con.execute("DELETE FROM universe WHERE as_of = ?", [today])
    con.register("_u", q[UNIVERSE_COLS]); con.execute("INSERT INTO universe SELECT * FROM _u")
    con.unregister("_u")
    print(f"  universe: {len(q)} US stocks >= ${C.MIN_PRICE:.0f} "
          f"(dollar-vol >= ${getattr(C, 'UNIVERSE_MIN_DOLLAR_VOL', 10e6)/1e6:.0f}M)")
    return q


def latest(con) -> pd.DataFrame:
    try:
        d = con.execute("SELECT max(as_of) FROM universe").fetchone()[0]
        if d is None: return pd.DataFrame(columns=UNIVERSE_COLS)
        return con.execute("SELECT * FROM universe WHERE as_of = ? ORDER BY dollar_vol DESC", [d]).df()
    except Exception:
        return pd.DataFrame(columns=UNIVERSE_COLS)


def symbols(con, today: dt.date | None = None) -> list[str]:
    """Screen universe = curated config lists ∪ latest stored universe."""
    u = latest(con)
    dyn = set(u["symbol"]) if not u.empty else set()
    return sorted(set(C.SCREENER_UNIVERSE) | dyn)


def sector_lookup(con) -> dict[str, str]:
    u = latest(con)
    if u.empty: return {}
    return {r.symbol: SECTOR_ETF[r.sector] for r in u.itertuples() if r.sector in SECTOR_ETF}


if __name__ == "__main__":
    q = qualify(parse_rows(fetch_screener()))
    print(len(q), "symbols"); print(q.head(20).to_string(index=False))
