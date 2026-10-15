"""Scale-normalized feature engine for GP training (1-hour timeframe).

Every feature is relative/normalized so that a $50 stock in 2015 and a $500
stock in 2025 produce comparable values. This is the key insight: history
repeats, only scale changes. Remove scale → unlock the full dataset.

Features are computed per-ticker and returned as a DataFrame ready for GP input.
Tuned for 1h bars: return periods = 1h, 3h, half-day, full-day, next-day.
"""

import numpy as np
import pandas as pd


# ── Core price features (all scale-normalized) ─────────────────────────────

def pct_return(close: pd.Series, period: int = 1) -> pd.Series:
    """% return over N bars."""
    return close.pct_change(period)


def bar_range_pct(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Bar range as % of close price (scale-normalized volatility)."""
    return (high - low) / close


def body_pct(open_: pd.Series, close: pd.Series) -> pd.Series:
    """Candle body as % of close (signed: positive = bullish)."""
    return (close - open_) / close


def upper_wick_pct(high: pd.Series, open_: pd.Series, close: pd.Series) -> pd.Series:
    """Upper wick as % of close."""
    body_top = pd.concat([open_, close], axis=1).max(axis=1)
    return (high - body_top) / close


def lower_wick_pct(low: pd.Series, open_: pd.Series, close: pd.Series) -> pd.Series:
    """Lower wick as % of close."""
    body_bottom = pd.concat([open_, close], axis=1).min(axis=1)
    return (body_bottom - low) / close


# ── ATR (the universal normalizer) ──────────────────────────────────────────

def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range in price units."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    """Average True Range (EMA-smoothed)."""
    tr = true_range(high, low, close)
    return tr.ewm(span=period, adjust=False).mean()


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series,
            period: int = 14) -> pd.Series:
    """ATR as % of close (scale-normalized volatility measure)."""
    return atr(high, low, close, period) / close


# ── Gap features (session open gaps for intraday) ─────────────────────────

def gap_pct(open_: pd.Series, close: pd.Series) -> pd.Series:
    """Bar-to-bar gap as % of previous close."""
    prev_close = close.shift(1)
    return (open_ - prev_close) / prev_close


def gap_atr(open_: pd.Series, close: pd.Series, high: pd.Series,
            low: pd.Series, atr_period: int = 14) -> pd.Series:
    """Bar-to-bar gap as multiple of ATR (scale-normalized gap size)."""
    prev_close = close.shift(1)
    gap = open_ - prev_close
    atr_val = atr(high, low, close, atr_period).shift(1)
    return gap / atr_val


# ── Volume features (relative, never absolute) ─────────────────────────────

def relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume as multiple of N-bar average. 1.0 = normal, 2.0 = 2x average."""
    avg = volume.rolling(period).mean()
    return volume / avg


def volume_change(volume: pd.Series) -> pd.Series:
    """Volume % change bar-to-bar."""
    return volume.pct_change()


# ── Swing highs/lows and level detection ────────────────────────────────────

def swing_high(high: pd.Series, lookback: int = 5) -> pd.Series:
    """Mark swing highs: high price at swing points, NaN elsewhere."""
    rolling_max = high.rolling(2 * lookback + 1, center=True).max()
    is_swing = (high == rolling_max).astype(float)
    return high.where(is_swing == 1.0)


def swing_low(low: pd.Series, lookback: int = 5) -> pd.Series:
    """Mark swing lows: the low price at swing points, NaN elsewhere."""
    rolling_min = low.rolling(2 * lookback + 1, center=True).min()
    is_swing = (low == rolling_min).astype(float)
    return low.where(is_swing == 1.0)


def dist_to_swing_high(close: pd.Series, high: pd.Series,
                       lookback: int = 5, atr_period: int = 14,
                       low: pd.Series = None) -> pd.Series:
    """Distance to nearest prior swing high, in ATR units."""
    swings = swing_high(high, lookback)
    nearest = swings.ffill()
    dist = nearest - close
    if low is not None:
        atr_val = atr(high, low, close, atr_period)
        return dist / atr_val
    return dist / close


def dist_to_swing_low(close: pd.Series, low: pd.Series,
                      lookback: int = 5, atr_period: int = 14,
                      high: pd.Series = None) -> pd.Series:
    """Distance to nearest prior swing low, in ATR units."""
    swings = swing_low(low, lookback)
    nearest = swings.ffill()
    dist = close - nearest
    if high is not None:
        atr_val = atr(high, low, close, atr_period)
        return dist / atr_val
    return dist / close


# ── Candle classification ───────────────────────────────────────────────────

def candle_type(open_: pd.Series, high: pd.Series, low: pd.Series,
                close: pd.Series) -> pd.DataFrame:
    """Classify each bar into structural types. Returns DataFrame of booleans."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_open = open_.shift(1)
    prev_close = close.shift(1)

    body = (close - open_).abs()
    full_range = high - low
    prev_body = (prev_close - prev_open).abs()

    result = pd.DataFrame(index=open_.index)

    result["inside_bar"] = ((high <= prev_high) & (low >= prev_low)).astype(float)
    result["outside_bar"] = ((high > prev_high) & (low < prev_low)).astype(float)
    result["doji"] = (body < 0.1 * full_range).astype(float)

    result["bullish_engulf"] = (
        (close > open_) &
        (open_ < prev_close) &
        (close > prev_open) &
        (body > prev_body)
    ).astype(float)

    result["bearish_engulf"] = (
        (close < open_) &
        (open_ > prev_close) &
        (close < prev_open) &
        (body > prev_body)
    ).astype(float)

    lower_wick = pd.concat([open_, close], axis=1).min(axis=1) - low
    upper_wick = high - pd.concat([open_, close], axis=1).max(axis=1)
    result["hammer"] = (
        (lower_wick > 2 * body) &
        (upper_wick < body) &
        (full_range > 0)
    ).astype(float)

    result["shooting_star"] = (
        (upper_wick > 2 * body) &
        (lower_wick < body) &
        (full_range > 0)
    ).astype(float)

    return result


# ── Momentum / trend context ───────────────────────────────────────────────

def ema_distance(close: pd.Series, period: int = 21) -> pd.Series:
    """Distance from EMA as % of price (how stretched is price from trend?)."""
    ema = close.ewm(span=period, adjust=False).mean()
    return (close - ema) / close


def trend_strength(close: pd.Series, fast: int = 21, slow: int = 50) -> pd.Series:
    """Fast EMA - Slow EMA as % of price. Positive = uptrend, negative = down."""
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    return (fast_ema - slow_ema) / close


# ── Range/squeeze detection ─────────────────────────────────────────────────

def range_compression(high: pd.Series, low: pd.Series, close: pd.Series,
                      short: int = 5, long: int = 20) -> pd.Series:
    """Ratio of short-term ATR to long-term ATR. < 1.0 = compressing (squeeze)."""
    atr_short = atr(high, low, close, short)
    atr_long = atr(high, low, close, long)
    return atr_short / atr_long


def inside_bar_count(high: pd.Series, low: pd.Series, window: int = 10) -> pd.Series:
    """Count of inside bars in last N bars (compression indicator)."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    is_inside = ((high <= prev_high) & (low >= prev_low)).astype(float)
    return is_inside.rolling(window).sum()


# ── Multi-bar patterns ──────────────────────────────────────────────────────

def consecutive_direction(close: pd.Series, open_: pd.Series) -> pd.Series:
    """Count consecutive bars in same direction. Positive = up streak, negative = down."""
    bullish = (close > open_).astype(int)
    bearish = (close < open_).astype(int)

    result = pd.Series(0, index=close.index, dtype=float)
    streak = 0
    prev_dir = 0

    for i in range(len(close)):
        if bullish.iloc[i]:
            if prev_dir >= 0:
                streak += 1
            else:
                streak = 1
            prev_dir = 1
        elif bearish.iloc[i]:
            if prev_dir <= 0:
                streak -= 1
            else:
                streak = -1
            prev_dir = -1
        else:
            streak = 0
            prev_dir = 0
        result.iloc[i] = streak

    return result


# ── Classic indicators (all scale-normalized) ──────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI normalized to 0-1 range (0.5 = neutral, >0.7 overbought, <0.3 oversold)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 1.0 - (1.0 / (1.0 + rs))  # standard RSI / 100 → 0-1


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               k_period: int = 14, d_period: int = 3) -> tuple[pd.Series, pd.Series]:
    """Stochastic %K and %D, normalized to 0-1."""
    lowest = low.rolling(k_period).min()
    highest = high.rolling(k_period).max()
    k = (close - lowest) / (highest - lowest)  # already 0-1
    d = k.rolling(d_period).mean()
    return k, d


def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ADX, +DI, -DI all normalized to 0-1."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    plus_dm = (high - prev_high).where((high - prev_high) > (prev_low - low), 0.0)
    plus_dm = plus_dm.where(plus_dm > 0, 0.0)
    minus_dm = (prev_low - low).where((prev_low - low) > (high - prev_high), 0.0)
    minus_dm = minus_dm.where(minus_dm > 0, 0.0)

    tr = true_range(high, low, close)
    atr_val = tr.ewm(span=period, adjust=False).mean()
    plus_di = plus_dm.ewm(span=period, adjust=False).mean() / atr_val
    minus_di = minus_dm.ewm(span=period, adjust=False).mean() / atr_val

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_val = dx.ewm(span=period, adjust=False).mean()

    return adx_val, plus_di, minus_di  # all 0-1 range


def macd_hist_pct(close: pd.Series, fast: int = 12, slow: int = 26,
                  signal: int = 9) -> pd.Series:
    """MACD histogram as % of close (scale-normalized)."""
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return histogram / close  # scale-normalized


def bollinger_pct_b(close: pd.Series, period: int = 20,
                    num_std: float = 2.0) -> pd.Series:
    """Bollinger %B — position within bands. 0 = lower band, 1 = upper band."""
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    return (close - lower) / (upper - lower)


def vwap_distance(close: pd.Series, volume: pd.Series, high: pd.Series,
                  low: pd.Series, atr_period: int = 14,
                  vwap_period: int = 24) -> pd.Series:
    """Distance from rolling VWAP in ATR units (scale-normalized).

    Uses a rolling window VWAP (default 24 bars = 1 trading day on 1h).
    """
    typical_price = (high + low + close) / 3.0
    cum_tp_vol = (typical_price * volume).rolling(vwap_period).sum()
    cum_vol = volume.rolling(vwap_period).sum()
    rolling_vwap = cum_tp_vol / cum_vol
    atr_val = atr(high, low, close, atr_period)
    return (close - rolling_vwap) / atr_val


# ── Trade outcome simulation (target/stop hit detection) ───────────────

# Strategy profiles: percentage-based so they work across all instruments.
# ~25pts NQ at 25000 = 0.1%, ~100pts = 0.4%, ~50pts = 0.2%
SCALP_PROFILE = {"target_pct": 0.0016, "stop_pct": 0.0008, "max_hold": 3}
MOMENTUM_PROFILE = {"target_pct": 0.0025, "stop_pct": 0.00125, "max_hold": 4}
SWING_PROFILE = {"target_pct": 0.004, "stop_pct": 0.002, "max_hold": 5}


def compute_trade_outcomes(high: pd.Series, low: pd.Series, close: pd.Series,
                           target_pct: float, stop_pct: float,
                           max_hold: int) -> tuple[np.ndarray, np.ndarray]:
    """Simulate target/stop trades for every bar. Vectorized.

    For each bar, checks if price hits target or stop first within max_hold
    bars. Stop checked first on same-bar hits (conservative).

    Returns:
        long_outcomes: +target_pct (win), -stop_pct (loss), or actual % at time exit
        short_outcomes: same but for short entries
    """
    c = close.values.astype(np.float64)
    h = high.values.astype(np.float64)
    l = low.values.astype(np.float64)
    n = len(c)

    # Entry prices and target/stop levels
    entry = c.copy()
    long_target_p = entry * (1.0 + target_pct)
    long_stop_p = entry * (1.0 - stop_pct)
    short_target_p = entry * (1.0 - target_pct)
    short_stop_p = entry * (1.0 + stop_pct)

    # Track first bar (offset) where target/stop is hit
    never = max_hold + 1
    lt_bar = np.full(n, never, dtype=np.int32)  # long target
    ls_bar = np.full(n, never, dtype=np.int32)  # long stop
    st_bar = np.full(n, never, dtype=np.int32)  # short target
    ss_bar = np.full(n, never, dtype=np.int32)  # short stop

    for offset in range(1, max_hold + 1):
        fwd_h = np.full(n, np.nan)
        fwd_l = np.full(n, np.nan)
        if offset < n:
            fwd_h[:n - offset] = h[offset:]
            fwd_l[:n - offset] = l[offset:]

        # Long: target hit when forward high >= target price (first time only)
        mask = (fwd_h >= long_target_p) & (lt_bar > offset)
        lt_bar[mask] = offset
        # Long: stop hit when forward low <= stop price
        mask = (fwd_l <= long_stop_p) & (ls_bar > offset)
        ls_bar[mask] = offset
        # Short: target hit when forward low <= target price
        mask = (fwd_l <= short_target_p) & (st_bar > offset)
        st_bar[mask] = offset
        # Short: stop hit when forward high >= stop price
        mask = (fwd_h >= short_stop_p) & (ss_bar > offset)
        ss_bar[mask] = offset

    # Forward close for time exits
    fwd_close = np.full(n, np.nan)
    if max_hold < n:
        fwd_close[:n - max_hold] = c[max_hold:]

    # Long outcomes
    long_out = np.full(n, np.nan)
    long_neither = (lt_bar > max_hold) & (ls_bar > max_hold)
    long_stop_first = (ls_bar <= lt_bar) & ~long_neither
    long_target_first = (lt_bar < ls_bar) & ~long_neither

    long_out[long_stop_first] = -stop_pct
    long_out[long_target_first] = target_pct
    long_out[long_neither] = (fwd_close[long_neither] - entry[long_neither]) / entry[long_neither]

    # Short outcomes
    short_out = np.full(n, np.nan)
    short_neither = (st_bar > max_hold) & (ss_bar > max_hold)
    short_stop_first = (ss_bar <= st_bar) & ~short_neither
    short_target_first = (st_bar < ss_bar) & ~short_neither

    short_out[short_stop_first] = -stop_pct
    short_out[short_target_first] = target_pct
    short_out[short_neither] = (entry[short_neither] - fwd_close[short_neither]) / entry[short_neither]

    return long_out, short_out


# ═══════════════════════════════════════════════════════════════════════════
# Master feature builder
# ═══════════════════════════════════════════════════════════════════════════

def compute_features(df: pd.DataFrame, atr_period: int = 14,
                     vol_period: int = 20, swing_lookback: int = 5) -> pd.DataFrame:
    """Compute all scale-normalized features from raw OHLCV data.

    Tuned for 1h bars. Return periods: 1, 3, 6, 12, 24 bars
    (= 1h, 3h, half-day, full-day, next-day).

    Args:
        df: DataFrame with columns: timestamp, open, high, low, close, volume
        atr_period: ATR lookback period
        vol_period: Volume average lookback
        swing_lookback: Swing high/low detection lookback

    Returns:
        DataFrame with all computed features. Ready for GP after dropping NaN warmup.
    """
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]

    feat = pd.DataFrame(index=df.index)

    # Keep timestamp for reference (not a GP input)
    feat["timestamp"] = df["timestamp"]

    # ── Price structure ──────────────────────────────────────────────────
    feat["open_pct"] = (o - c) / c
    feat["bar_range_pct"] = bar_range_pct(h, l, c)
    feat["body_pct"] = body_pct(o, c)
    feat["upper_wick_pct"] = upper_wick_pct(h, o, c)
    feat["lower_wick_pct"] = lower_wick_pct(l, o, c)

    # ── Returns (tuned for 1h: 1h, 3h, half-day, full-day, next-day) ────
    for period in [1, 3, 6, 12, 24]:
        feat[f"ret_{period}"] = pct_return(c, period)

    # ── Volatility ───────────────────────────────────────────────────────
    feat["atr_pct"] = atr_pct(h, l, c, atr_period)
    feat["range_compression"] = range_compression(h, l, c)

    # ── Gaps ─────────────────────────────────────────────────────────────
    feat["gap_pct"] = gap_pct(o, c)
    feat["gap_atr"] = gap_atr(o, c, h, l, atr_period)

    # ── Volume ───────────────────────────────────────────────────────────
    feat["rel_volume"] = relative_volume(v, vol_period)
    feat["vol_change"] = volume_change(v)

    # ── Levels ───────────────────────────────────────────────────────────
    feat["dist_swing_high"] = dist_to_swing_high(c, h, swing_lookback, atr_period, l)
    feat["dist_swing_low"] = dist_to_swing_low(c, l, swing_lookback, atr_period, h)

    # ── Candle types ─────────────────────────────────────────────────────
    candles = candle_type(o, h, l, c)
    for col in candles.columns:
        feat[col] = candles[col]

    # ── Momentum / trend ─────────────────────────────────────────────────
    feat["ema_dist_21"] = ema_distance(c, 21)
    feat["ema_dist_50"] = ema_distance(c, 50)
    feat["trend_strength"] = trend_strength(c)

    # ── Compression ──────────────────────────────────────────────────────
    feat["inside_bar_count"] = inside_bar_count(h, l)

    # ── Multi-bar patterns ───────────────────────────────────────────────
    feat["consec_dir"] = consecutive_direction(c, o)

    # ── Classic indicators (all scale-normalized) ────────────────────────
    feat["rsi_14"] = rsi(c, 14)
    feat["rsi_7"] = rsi(c, 7)

    stoch_k, stoch_d = stochastic(h, l, c)
    feat["stoch_k"] = stoch_k
    feat["stoch_d"] = stoch_d

    adx_val, plus_di, minus_di = adx(h, l, c)
    feat["adx"] = adx_val
    feat["plus_di"] = plus_di
    feat["minus_di"] = minus_di

    feat["macd_hist"] = macd_hist_pct(c)
    feat["boll_pct_b"] = bollinger_pct_b(c)
    feat["vwap_dist"] = vwap_distance(c, v, h, l, atr_period)

    # ── Trade outcomes (target/stop simulation for GP fitness) ──────────
    scalp_l, scalp_s = compute_trade_outcomes(
        h, l, c, **SCALP_PROFILE)  # 25pt target, 25pt stop, 2-bar hold
    feat["scalp_long"] = scalp_l
    feat["scalp_short"] = scalp_s

    mom_l, mom_s = compute_trade_outcomes(
        h, l, c, **MOMENTUM_PROFILE)  # 50pt target, 25pt stop, 4-bar hold
    feat["momentum_long"] = mom_l
    feat["momentum_short"] = mom_s

    swing_l, swing_s = compute_trade_outcomes(
        h, l, c, **SWING_PROFILE)  # 100pt target, 50pt stop, 5-bar hold
    feat["swing_long"] = swing_l
    feat["swing_short"] = swing_s

    # ── Forward returns (kept for analysis / scanner compat) ─────────────
    feat["fwd_ret_1"] = pct_return(c, 1).shift(-1)
    feat["fwd_ret_3"] = pct_return(c, 3).shift(-3)
    feat["fwd_ret_6"] = pct_return(c, 6).shift(-6)

    return feat


# Outcome and forward-return column prefixes (excluded from NaN-dropping)
_TARGET_PREFIXES = ("fwd_", "scalp_", "momentum_", "swing_")


def compute_and_clean(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """Compute features and drop warmup NaN rows. Ready for GP."""
    feat = compute_features(df, **kwargs)
    feature_cols = [c for c in feat.columns
                    if c != "timestamp"
                    and not any(c.startswith(p) for p in _TARGET_PREFIXES)]
    feat = feat.dropna(subset=feature_cols, how="any")
    return feat.reset_index(drop=True)
