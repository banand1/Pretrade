"""Offline tests for universe.py's pure helpers — no network."""
import pandas as pd
import pytest

import universe as U


def _row(symbol, name, last, volume, mcap, country="United States", sector="Technology"):
    return {"symbol": symbol, "name": name, "lastsale": f"${last:,.2f}", "volume": f"{volume:,}",
            "marketCap": f"{mcap:.2f}", "country": country, "sector": sector, "industry": "x"}


ROWS = [
    _row("GOOD", "Good Co. Common Stock", 42.0, 2_000_000, 5e9),
    _row("CHEAP", "Cheap Co. Common Stock", 9.5, 9_000_000, 2e9),            # < $15
    _row("THIN", "Thin Co. Common Stock", 30.0, 20_000, 1e9),                # dollar vol 600k
    _row("TINY", "Tiny Co. Common Stock", 30.0, 1_000_000, 50e6),            # mcap 50M
    _row("FRGN", "Foreign Co. Ordinary Shares", 80.0, 3_000_000, 9e9, country="Canada"),
    _row("GOODW", "Good Co. Warrants", 20.0, 1_000_000, 5e9),                # warrant
    _row("SPAC", "Fresh Acquisition Corp Class A", 20.0, 1_000_000, 5e9),    # SPAC
    _row("PFD", "Bank 6.25% Preferred Stock Series C", 25.0, 1_000_000, 5e9),
    _row("BAD.A", "Dotted Class A Common Stock", 50.0, 1_000_000, 5e9),      # bad symbol
    _row("GOOD", "Good Co. Common Stock", 42.0, 2_000_000, 5e9),             # duplicate
]


def test_parse_rows_numeric_fields():
    df = U.parse_rows(ROWS)
    g = df[df.symbol == "GOOD"].iloc[0]
    assert g["last"] == 42.0 and g["volume"] == 2_000_000 and g["market_cap"] == 5e9
    assert g["dollar_vol"] == 42.0 * 2_000_000


def test_qualify_applies_every_gate():
    q = U.qualify(U.parse_rows(ROWS), min_price=15.0, min_dollar_vol=10e6,
                  min_market_cap=300e6, us_only=True)
    assert q.symbol.tolist() == ["GOOD"]


def test_qualify_can_include_foreign_listings():
    q = U.qualify(U.parse_rows(ROWS), min_price=15.0, min_dollar_vol=10e6,
                  min_market_cap=300e6, us_only=False)
    assert set(q.symbol) == {"GOOD", "FRGN"}


def test_qualify_sorted_by_liquidity_and_deduped():
    rows = ROWS + [_row("BIG", "Big Co. Common Stock", 100.0, 50_000_000, 1e12)]
    q = U.qualify(U.parse_rows(rows), min_price=15.0, min_dollar_vol=10e6,
                  min_market_cap=300e6, us_only=True)
    assert q.symbol.tolist() == ["BIG", "GOOD"]
    assert q.symbol.is_unique


def test_exclusion_regex_needs_whole_words():
    keep = ["Unity Software Inc. Common Stock", "UnitedHealth Group Incorporated Common Stock",
            "United Airlines Holdings Inc. Common Stock", "Trustmark Corporation Common Stock",
            "Bright Horizons Family Solutions Inc. Common Stock", "Fundamental Global Inc. Common Stock"]
    drop = ["Good Co. Warrants", "Fresh Acquisition Corp Class A", "Bank 6.25% Preferred Stock Series C",
            "Real Estate Income Trust", "XYZ Fund Inc.", "Acme Class A Units", "Acme Rights"]
    for n in keep:
        assert not U._EXCLUDE_NAME.search(n), n
    for n in drop:
        assert U._EXCLUDE_NAME.search(n), n


def test_parse_rows_tolerates_missing_values():
    df = U.parse_rows([{"symbol": "NA1", "name": "x", "lastsale": "NA", "volume": None,
                        "marketCap": "", "country": "", "sector": ""}])
    assert df.iloc[0]["last"] is None or pd.isna(df.iloc[0]["last"])
    assert U.qualify(df).empty
