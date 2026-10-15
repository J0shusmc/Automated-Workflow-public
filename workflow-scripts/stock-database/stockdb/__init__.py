"""stockdb — Centralized market data infrastructure powered by Polygon.io.

Usage:
    from stockdb import get_bars
    df = get_bars("SPY", "daily", start="2020-01-01")
    data = get_bars(["SPY", "QQQ"], "1hour")  # returns dict of DataFrames
"""

from .query import get_bars, get_bars_wide, available_tickers, coverage

__all__ = ["get_bars", "get_bars_wide", "available_tickers", "coverage"]
