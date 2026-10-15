"""Polygon.io API wrapper with rate limiting and auto-chunking."""

import time
from datetime import datetime, timedelta

import pandas as pd
from polygon import RESTClient

from .config import (
    POLYGON_API_KEY,
    INTERVALS,
    RATE_LIMIT_PAUSE,
    CHUNK_YEARS,
    MARKET_TZ,
)


class PolygonClient:
    """Wraps polygon.RESTClient with rate limiting and chunking."""

    def __init__(self, api_key: str | None = None):
        self._key = api_key or POLYGON_API_KEY
        if not self._key or self._key == "your_key_here":
            raise ValueError(
                "POLYGON_API_KEY not set. Add it to .env or pass api_key=."
            )
        self._client = RESTClient(self._key)
        self._last_call = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_call
        if elapsed < RATE_LIMIT_PAUSE:
            time.sleep(RATE_LIMIT_PAUSE - elapsed)
        self._last_call = time.time()

    def get_bars(
        self,
        ticker: str,
        interval: str,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars, auto-chunking if needed.

        Args:
            ticker: e.g. "SPY", "I:VIX"
            interval: "daily", "1hour", or "5min"
            start: "YYYY-MM-DD"
            end: "YYYY-MM-DD"

        Returns:
            DataFrame with columns: timestamp, open, high, low, close,
            volume, vwap, transactions
        """
        if interval not in INTERVALS:
            raise ValueError(f"Unknown interval {interval!r}. Use: {list(INTERVALS)}")

        cfg = INTERVALS[interval]
        chunk_years = CHUNK_YEARS[interval]

        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        all_bars = []
        chunk_start = start_dt

        while chunk_start < end_dt:
            chunk_end = min(
                chunk_start + timedelta(days=chunk_years * 365),
                end_dt,
            )

            self._rate_limit()
            bars = self._fetch_chunk(
                ticker,
                cfg["multiplier"],
                cfg["timespan"],
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
            )
            all_bars.extend(bars)
            chunk_start = chunk_end + timedelta(days=1)

        if not all_bars:
            return pd.DataFrame(
                columns=[
                    "timestamp", "open", "high", "low", "close",
                    "volume", "vwap", "transactions",
                ]
            )

        return self._to_dataframe(all_bars, interval)

    def _fetch_chunk(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        start: str,
        end: str,
    ) -> list:
        """Fetch a single chunk from Polygon, handling pagination."""
        results = []
        for bar in self._client.list_aggs(
            ticker=ticker,
            multiplier=multiplier,
            timespan=timespan,
            from_=start,
            to=end,
            limit=50000,
            sort="asc",
        ):
            results.append(bar)
        return results

    def _to_dataframe(self, bars: list, interval: str) -> pd.DataFrame:
        """Convert Polygon Agg objects to a clean DataFrame."""
        rows = []
        for b in bars:
            rows.append({
                "timestamp": b.timestamp,  # unix ms
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume or 0,
                "vwap": getattr(b, "vwap", None),
                "transactions": getattr(b, "transactions", None),
            })

        df = pd.DataFrame(rows)

        # Convert unix ms → datetime
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

        if interval == "daily":
            df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d")
        else:
            df["timestamp"] = (
                df["timestamp"]
                .dt.tz_convert(MARKET_TZ)
                .dt.strftime("%Y-%m-%d %H:%M:%S")
            )

        df = df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
        return df

    def get_all_tickers(self, market: str = "stocks") -> list[str]:
        """Fetch all active US equity tickers from Polygon."""
        tickers = []
        self._rate_limit()
        for t in self._client.list_tickers(
            market=market, active=True, limit=1000
        ):
            tickers.append(t.ticker)
        return sorted(tickers)
