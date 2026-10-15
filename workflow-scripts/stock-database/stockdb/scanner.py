"""Daily scanner — runs trained GP equations across the full universe.

After GP training produces winning equations, the scanner:
1. Loads latest data for all tickers
2. Computes scale-normalized features
3. Evaluates each GP equation on each ticker
4. Outputs a ranked watchlist of tickers with active signals

Usage:
    from stockdb.scanner import Scanner
    scanner = Scanner()
    watchlist = scanner.scan()
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_DIR, INTERVALS
from .features import compute_and_clean
from .gp_engine import (
    Node, RESULTS_DIR, _OP_LOOKUP,
    BINARY_OPS, UNARY_OPS, ALL_OPS,
)
from .metadata import all_entries, csv_path

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Expression parser — rebuild trees from saved string expressions
# ═══════════════════════════════════════════════════════════════════════════

def parse_expression(expr: str, feature_names: list[str]) -> Node:
    """Parse a GP expression string back into a Node tree.

    Handles expressions like:
        (gap_atr add dist_swing_low)
        neg(gap_atr)
        sqrt(clip(ret_1))
        ((A sub B) mul C)
        1.2345
        -0.5678
    """
    tokens = _tokenize(expr)
    tree, _ = _parse_tokens(tokens, 0, feature_names)
    return tree


def _tokenize(expr: str) -> list[str]:
    """Split expression into tokens."""
    tokens = []
    i = 0
    while i < len(expr):
        c = expr[i]
        if c in "()":
            tokens.append(c)
            i += 1
        elif c in " ,":
            i += 1
        else:
            # Read a word or number
            j = i
            while j < len(expr) and expr[j] not in "() ,":
                j += 1
            tokens.append(expr[i:j])
            i = j
    return tokens


_BINARY_OP_NAMES = {name for name, _, _ in BINARY_OPS}
_UNARY_OP_NAMES = {name for name, _, _ in UNARY_OPS}
_ALL_OP_NAMES = _BINARY_OP_NAMES | _UNARY_OP_NAMES


def _parse_tokens(tokens: list[str], pos: int,
                  feature_names: list[str]) -> tuple[Node, int]:
    """Recursive descent parser for GP expressions."""
    if pos >= len(tokens):
        return Node(constant=0.0), pos

    token = tokens[pos]

    # Parenthesized expression: (A op B) or (expr)
    if token == "(":
        pos += 1  # skip (
        left, pos = _parse_tokens(tokens, pos, feature_names)

        if pos < len(tokens) and tokens[pos] in _BINARY_OP_NAMES:
            op = tokens[pos]
            pos += 1
            right, pos = _parse_tokens(tokens, pos, feature_names)
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return Node(op=op, children=[left, right]), pos
        else:
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return left, pos

    # Unary function: func(arg)
    if token in _UNARY_OP_NAMES:
        pos += 1
        if pos < len(tokens) and tokens[pos] == "(":
            pos += 1  # skip (
            child, pos = _parse_tokens(tokens, pos, feature_names)
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return Node(op=token, children=[child]), pos
        else:
            # Unary applied to next token
            child, pos = _parse_tokens(tokens, pos, feature_names)
            return Node(op=token, children=[child]), pos

    # Feature name
    if token in feature_names:
        return Node(feature=token), pos + 1

    # Constant (number)
    try:
        val = float(token)
        return Node(constant=val), pos + 1
    except ValueError:
        pass

    # Unknown token — treat as constant 0
    log.warning(f"Unknown token in expression: {token!r}")
    return Node(constant=0.0), pos + 1


# ═══════════════════════════════════════════════════════════════════════════
# Signal
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    ticker: str
    direction: str          # "long" or "short"
    signal_strength: float  # raw GP output
    equation_id: str        # which equation produced this
    profit_factor: float    # equation's training PF
    win_rate: float         # equation's training win rate
    timestamp: str          # bar timestamp
    close: float            # current close price
    atr_pct: float          # current ATR as % of price
    target: str = "fwd_ret_5"  # which forward return horizon


# ═══════════════════════════════════════════════════════════════════════════
# Scanner
# ═══════════════════════════════════════════════════════════════════════════

class Scanner:
    """Scans the universe with trained GP equations to produce a daily watchlist."""

    def __init__(self, interval: str = "daily", target_col: str = "fwd_ret_5",
                 min_pf: float = 1.3, min_trades: int = 50,
                 signal_threshold: float = 0.0):
        self.interval = interval
        self.target_col = target_col
        self.min_pf = min_pf
        self.min_trades = min_trades
        self.signal_threshold = signal_threshold

    def scan(self, tickers: list[str] | None = None) -> list[Signal]:
        """Run scan across universe. Returns ranked list of signals.

        Args:
            tickers: Specific tickers to scan. If None, scans all available.

        Returns:
            List of Signal objects, ranked by signal strength.
        """
        # Load trained equations
        equations = self._load_equations()
        if not equations:
            log.warning("No trained equations found. Run `stockdb train` first.")
            return []

        log.info(f"Loaded {len(equations)} equations (PF >= {self.min_pf})")

        # Get tickers to scan
        if tickers is None:
            tickers = self._available_tickers()

        log.info(f"Scanning {len(tickers)} tickers on {self.interval} timeframe")

        signals = []
        errors = 0

        for ticker in tickers:
            try:
                ticker_signals = self._scan_ticker(ticker, equations)
                signals.extend(ticker_signals)
            except Exception as e:
                errors += 1
                if errors <= 5:
                    log.warning(f"Error scanning {ticker}: {e}")

        # Rank by signal strength (absolute value)
        signals.sort(key=lambda s: abs(s.signal_strength), reverse=True)

        log.info(f"Scan complete: {len(signals)} signals from {len(tickers)} tickers "
                 f"({errors} errors)")

        return signals

    def _scan_ticker(self, ticker: str, equations: list[dict]) -> list[Signal]:
        """Scan a single ticker against all equations."""
        path = csv_path(ticker, self.interval)
        if not path.exists():
            return []

        df = pd.read_csv(path)
        if len(df) < 50:  # not enough data for features
            return []

        feat = compute_and_clean(df)
        if feat.empty:
            return []

        # Get feature columns
        non_feature = {"timestamp", "ticker", "symbol", "date"}
        feature_cols = [c for c in feat.columns
                        if c not in non_feature
                        and not c.startswith("fwd_")
                        and feat[c].dtype in ("float64", "float32", "int64", "int32")]

        # Use the LAST bar (most recent) for signal generation
        last_row = feat.iloc[-1]
        last_data = {col: np.array([last_row[col]]) for col in feature_cols}

        signals = []

        for eq in equations:
            try:
                tree = parse_expression(eq["expression"], feature_cols)
                output = tree.evaluate(last_data)[0]

                if not np.isfinite(output):
                    continue

                if output > self.signal_threshold:
                    signals.append(Signal(
                        ticker=ticker,
                        direction="long",
                        signal_strength=float(output),
                        equation_id=eq["expression"][:50],
                        profit_factor=eq["profit_factor"],
                        win_rate=eq["win_rate"],
                        timestamp=str(last_row.get("timestamp", "")),
                        close=float(df["close"].iloc[-1]),
                        atr_pct=float(last_row.get("atr_pct", 0)),
                    ))
                elif output < -self.signal_threshold:
                    signals.append(Signal(
                        ticker=ticker,
                        direction="short",
                        signal_strength=float(output),
                        equation_id=eq["expression"][:50],
                        profit_factor=eq["profit_factor"],
                        win_rate=eq["win_rate"],
                        timestamp=str(last_row.get("timestamp", "")),
                        close=float(df["close"].iloc[-1]),
                        atr_pct=float(last_row.get("atr_pct", 0)),
                    ))

            except Exception:
                continue

        return signals

    def _load_equations(self) -> list[dict]:
        """Load equations from the most recent GP run, filtered by quality."""
        if not RESULTS_DIR.exists():
            return []

        if self.target_col in ("multi", "all"):
            files = sorted(
                RESULTS_DIR.glob("gp_run_multi_*.json"), reverse=True)
            if not files:
                files = sorted(
                    RESULTS_DIR.glob("gp_run_*.json"), reverse=True)
        else:
            files = sorted(
                RESULTS_DIR.glob(f"gp_run_{self.target_col}_*.json"),
                reverse=True
            )
        if not files:
            return []

        data = json.loads(files[0].read_text())
        results = data.get("results", [])

        # Filter by minimum profit factor and trade count
        qualified = [
            r for r in results
            if r.get("profit_factor", 0) >= self.min_pf
            and r.get("total_trades", 0) >= self.min_trades
        ]

        return qualified

    def _available_tickers(self) -> list[str]:
        """Get all tickers with data for this interval."""
        entries = all_entries()
        return sorted(set(
            e["ticker"] for e in entries.values()
            if e["interval"] == self.interval
        ))

    def format_watchlist(self, signals: list[Signal], top_n: int = 20) -> str:
        """Format signals into a readable watchlist string."""
        if not signals:
            return "No signals found."

        # Group by ticker, keep strongest signal per ticker
        by_ticker = {}
        for sig in signals:
            key = (sig.ticker, sig.direction)
            if key not in by_ticker or abs(sig.signal_strength) > abs(by_ticker[key].signal_strength):
                by_ticker[key] = sig

        ranked = sorted(by_ticker.values(),
                        key=lambda s: abs(s.signal_strength), reverse=True)

        lines = []
        lines.append(f"\n{'='*70}")
        lines.append(f"WATCHLIST — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"{'='*70}")
        lines.append(f"{'Ticker':>8s}  {'Dir':>5s}  {'Strength':>10s}  {'Close':>10s}  "
                      f"{'ATR%':>6s}  {'PF':>5s}  {'WR':>5s}")
        lines.append("-" * 70)

        for sig in ranked[:top_n]:
            lines.append(
                f"{sig.ticker:>8s}  {sig.direction:>5s}  {sig.signal_strength:>10.4f}  "
                f"${sig.close:>9.2f}  {sig.atr_pct:>5.2%}  "
                f"{sig.profit_factor:>5.2f}  {sig.win_rate:>4.1%}"
            )

        lines.append(f"\n{len(ranked)} unique ticker/direction signals from "
                      f"{len(signals)} total signals")
        return "\n".join(lines)
