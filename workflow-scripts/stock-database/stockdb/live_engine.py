"""Live trading engine — runs top GP equations as an ensemble on incoming bar data.

Loads the best N equations per strategy (swing + momentum) from the latest
GP run, evaluates them on each new bar, and uses majority-vote consensus
to generate trade signals.

Usage:
    from stockdb.live_engine import LiveEngine
    engine = LiveEngine()
    engine.run("data/1hour/NQ.csv")  # backtest / replay mode
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from .features import compute_and_clean
from .gp_engine import RESULTS_DIR, STRATEGY_PROFILES, NQ_POINT_VALUE, NQ_REF_PRICE
from .scanner import parse_expression

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Equation:
    """A trained GP equation with its metadata."""
    expression: str
    strategy: str
    val_pf: float
    val_wr: float
    val_trades: int
    val_per_trade: float
    tree: object = None  # parsed Node tree, set after loading

    @property
    def bracket(self) -> str:
        p = STRATEGY_PROFILES.get(self.strategy, {})
        return f"+{p.get('nq_target_pts', '?')}/-{p.get('nq_stop_pts', '?')}"


@dataclass
class TradeSignal:
    """A consensus trade signal from the ensemble."""
    timestamp: str
    direction: Literal["LONG", "SHORT", "FLAT"]
    strategy: str
    votes_for: int
    votes_total: int
    avg_strength: float
    equations: list[str]
    close: float
    target_pts: int
    stop_pts: int


@dataclass
class Trade:
    """An active or completed trade."""
    entry_time: str
    direction: Literal["LONG", "SHORT"]
    strategy: str
    entry_price: float
    target_price: float
    stop_price: float
    target_pts: int
    stop_pts: int
    votes: int
    total_eq: int
    exit_time: str | None = None
    exit_price: float | None = None
    result: Literal["WIN", "LOSS", "TIME_EXIT"] | None = None
    pnl_pts: float = 0.0
    bars_held: int = 0
    max_hold: int = 5


# ═══════════════════════════════════════════════════════════════════════════
# Equation loader
# ═══════════════════════════════════════════════════════════════════════════

def load_top_equations(top_n: int = 3, run_file: str | None = None) -> dict[str, list[Equation]]:
    """Load top N equations per strategy from the latest GP run.

    Returns:
        Dict mapping strategy name -> list of Equation objects.
    """
    if run_file:
        path = Path(run_file)
    else:
        # Find latest 'both' run, fall back to any run
        if not RESULTS_DIR.exists():
            return {}
        files = sorted(RESULTS_DIR.glob("gp_run_both_*.json"), reverse=True)
        if not files:
            files = sorted(RESULTS_DIR.glob("gp_run_*.json"), reverse=True)
        if not files:
            return {}
        path = files[0]

    data = json.loads(path.read_text())
    results = data.get("results", [])

    # Group by strategy, sort by val_pf descending
    by_strategy: dict[str, list] = {}
    for r in results:
        strat = r.get("strategy", "unknown")
        if strat not in by_strategy:
            by_strategy[strat] = []
        by_strategy[strat].append(r)

    equations: dict[str, list[Equation]] = {}
    for strat, eqs in by_strategy.items():
        # Sort by validation PF
        eqs.sort(key=lambda x: x.get("val_pf", 0), reverse=True)
        top = eqs[:top_n]
        equations[strat] = [
            Equation(
                expression=e["expression"],
                strategy=strat,
                val_pf=e.get("val_pf", 0),
                val_wr=e.get("val_wr", 0),
                val_trades=e.get("val_trades", 0),
                val_per_trade=e.get("val_nq_per_trade", 0),
            )
            for e in top
        ]

    return equations


# ═══════════════════════════════════════════════════════════════════════════
# Live Engine
# ═══════════════════════════════════════════════════════════════════════════

class LiveEngine:
    """Ensemble engine: evaluates top equations with z-score thresholding.

    Matches training logic: raw equation output is z-scored across all bars,
    then z > 1.0 → long, z < -1.0 → short.  Consensus voting requires
    min_votes equations to agree before a trade triggers.
    """

    Z_LONG = 1.0    # matches training: z_signal > 1.0
    Z_SHORT = -1.0   # matches training: z_signal < -1.0

    def __init__(self, top_n: int = 1, min_votes: int = 1,
                 run_file: str | None = None):
        self.top_n = top_n
        self.min_votes = min_votes
        self.run_file = run_file

        self.equations: dict[str, list[Equation]] = {}
        self.feature_cols: list[str] = []
        self.trades: list[Trade] = []
        self.active_trades: dict[str, Trade] = {}  # strategy -> active trade

    def load(self, feature_names: list[str] | None = None):
        """Load equations and parse expression trees.

        Args:
            feature_names: Feature column names from the dataset. If None,
                           trees are parsed lazily during backtest.
        """
        self.equations = load_top_equations(self.top_n, self.run_file)
        self._feature_names = feature_names

        if feature_names:
            self._parse_trees(feature_names)

        return self.equations

    def _parse_trees(self, feature_names: list[str]):
        """Parse all expression strings into Node trees."""
        for strat, eqs in self.equations.items():
            for eq in eqs:
                eq.tree = parse_expression(eq.expression, feature_names)

    def backtest(self, csv_path: str) -> list[Trade]:
        """Run the engine across a full CSV file in replay mode.

        Uses the same z-score thresholding as GP training:
        1. Compute raw equation output for ALL bars
        2. Z-score the output (mean/std across full dataset)
        3. z > 1.0 → long signal, z < -1.0 → short signal
        4. Walk forward bar-by-bar managing trades with target/stop brackets
        """
        raw_df = pd.read_csv(csv_path)
        feat_df = compute_and_clean(raw_df)

        # Align raw_df to feat_df (features drop warmup rows)
        raw_aligned = raw_df.iloc[-len(feat_df):].reset_index(drop=True)
        feat_df = feat_df.reset_index(drop=True)

        # Identify feature columns
        non_feature = {"timestamp", "ticker", "symbol", "date"}
        target_prefixes = ("fwd_", "scalp_", "momentum_", "swing_")
        feat_cols = [
            c for c in feat_df.columns
            if c not in non_feature
            and not any(c.startswith(p) for p in target_prefixes)
            and feat_df[c].dtype in ("float64", "float32", "int64", "int32")
        ]

        # Parse trees with actual feature names if not already done
        if not any(eq.tree for eqs in self.equations.values() for eq in eqs):
            self._parse_trees(feat_cols)

        # Pre-compute all equation outputs and z-scores across full dataset
        all_data = {col: feat_df[col].values.astype(np.float64) for col in feat_cols}
        n_bars = len(feat_df)

        # z_signals[strat][eq_idx] = array of z-scores per bar
        z_signals: dict[str, list[np.ndarray]] = {}

        for strat, eqs in self.equations.items():
            z_signals[strat] = []
            for eq in eqs:
                raw_output = eq.tree.evaluate(all_data)
                mean = np.nanmean(raw_output)
                std = np.nanstd(raw_output)
                if std < 1e-10:
                    z = np.zeros(n_bars)
                else:
                    z = (raw_output - mean) / std
                z = np.where(np.isfinite(z), z, 0.0)
                z_signals[strat].append(z)

        # Walk forward bar-by-bar
        for i in range(n_bars):
            raw = raw_aligned.iloc[i]
            timestamp = str(raw.get("timestamp", i))
            close = float(raw["close"])
            high = float(raw["high"])
            low = float(raw["low"])

            # 1. Update active trades
            for strat in list(self.active_trades.keys()):
                trade = self.active_trades[strat]
                trade.bars_held += 1
                profile = STRATEGY_PROFILES.get(strat, {})
                max_hold = profile.get("max_hold", 5)

                if trade.direction == "LONG":
                    hit_stop = low <= trade.stop_price
                    hit_target = high >= trade.target_price
                else:
                    hit_stop = high >= trade.stop_price
                    hit_target = low <= trade.target_price

                # Stop checked first (conservative)
                if hit_stop:
                    trade.exit_time = timestamp
                    trade.exit_price = trade.stop_price
                    trade.result = "LOSS"
                    trade.pnl_pts = -trade.stop_pts
                    self.trades.append(trade)
                    del self.active_trades[strat]
                elif hit_target:
                    trade.exit_time = timestamp
                    trade.exit_price = trade.target_price
                    trade.result = "WIN"
                    trade.pnl_pts = trade.target_pts
                    self.trades.append(trade)
                    del self.active_trades[strat]
                elif trade.bars_held >= max_hold:
                    trade.exit_time = timestamp
                    trade.exit_price = close
                    if trade.direction == "LONG":
                        trade.pnl_pts = (close - trade.entry_price) / trade.entry_price * NQ_REF_PRICE
                    else:
                        trade.pnl_pts = (trade.entry_price - close) / trade.entry_price * NQ_REF_PRICE
                    trade.result = "TIME_EXIT"
                    self.trades.append(trade)
                    del self.active_trades[strat]

            # 2. Check for new signals via z-score consensus
            for strat, eqs in self.equations.items():
                if strat in self.active_trades:
                    continue

                profile = STRATEGY_PROFILES.get(strat, {})
                long_votes = []
                short_votes = []

                for eq_idx, eq in enumerate(eqs):
                    z = z_signals[strat][eq_idx][i]
                    if z > self.Z_LONG:
                        long_votes.append((z, eq.expression[:60]))
                    elif z < self.Z_SHORT:
                        short_votes.append((z, eq.expression[:60]))

                total = len(eqs)
                direction = None
                votes = []

                if len(long_votes) >= self.min_votes:
                    direction = "LONG"
                    votes = long_votes
                elif len(short_votes) >= self.min_votes:
                    direction = "SHORT"
                    votes = short_votes

                if direction is None:
                    continue

                # Open trade
                if direction == "LONG":
                    target_price = close * (1 + profile.get("target_pct", 0.004))
                    stop_price = close * (1 - profile.get("stop_pct", 0.002))
                else:
                    target_price = close * (1 - profile.get("target_pct", 0.004))
                    stop_price = close * (1 + profile.get("stop_pct", 0.002))

                trade = Trade(
                    entry_time=timestamp,
                    direction=direction,
                    strategy=strat,
                    entry_price=close,
                    target_price=target_price,
                    stop_price=stop_price,
                    target_pts=profile.get("nq_target_pts", 0),
                    stop_pts=profile.get("nq_stop_pts", 0),
                    votes=len(votes),
                    total_eq=total,
                    max_hold=profile.get("max_hold", 5),
                )
                self.active_trades[strat] = trade

        # Close any remaining active trades at last bar
        last_close = float(raw_aligned["close"].iloc[-1])
        last_ts = str(raw_aligned.iloc[-1].get("timestamp", ""))
        for strat, trade in self.active_trades.items():
            if trade.direction == "LONG":
                trade.pnl_pts = (last_close - trade.entry_price) / trade.entry_price * NQ_REF_PRICE
            else:
                trade.pnl_pts = (trade.entry_price - last_close) / trade.entry_price * NQ_REF_PRICE
            trade.exit_time = last_ts
            trade.exit_price = last_close
            trade.result = "TIME_EXIT"
            self.trades.append(trade)

        self.active_trades.clear()
        return self.trades

    def get_stats(self) -> dict:
        """Compute aggregate performance stats from completed trades."""
        if not self.trades:
            return {}

        trades = self.trades
        wins = [t for t in trades if t.result == "WIN"]
        losses = [t for t in trades if t.result == "LOSS"]
        time_exits = [t for t in trades if t.result == "TIME_EXIT"]

        total_pnl = sum(t.pnl_pts for t in trades)
        gross_win = sum(t.pnl_pts for t in trades if t.pnl_pts > 0)
        gross_loss = abs(sum(t.pnl_pts for t in trades if t.pnl_pts < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

        # Per-strategy breakdown
        strats = sorted(set(t.strategy for t in trades))
        by_strat = {}
        for s in strats:
            st = [t for t in trades if t.strategy == s]
            sw = [t for t in st if t.result == "WIN"]
            sl = [t for t in st if t.result == "LOSS"]
            s_pnl = sum(t.pnl_pts for t in st)
            s_gw = sum(t.pnl_pts for t in st if t.pnl_pts > 0)
            s_gl = abs(sum(t.pnl_pts for t in st if t.pnl_pts < 0))
            by_strat[s] = {
                "trades": len(st),
                "wins": len(sw),
                "losses": len(sl),
                "win_rate": len(sw) / len(st) if st else 0,
                "pf": s_gw / s_gl if s_gl > 0 else float("inf"),
                "total_pts": s_pnl,
                "total_dollars": s_pnl * NQ_POINT_VALUE,
                "avg_pts": s_pnl / len(st) if st else 0,
            }

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "time_exits": len(time_exits),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "profit_factor": pf,
            "total_pts": total_pnl,
            "total_dollars": total_pnl * NQ_POINT_VALUE,
            "avg_pts_per_trade": total_pnl / len(trades) if trades else 0,
            "avg_dollars_per_trade": (total_pnl * NQ_POINT_VALUE) / len(trades) if trades else 0,
            "by_strategy": by_strat,
        }

    # ═════════════════════════════════════════════════════════════════════
    # Live watch mode — tail a CSV for new bars
    # ═════════════════════════════════════════════════════════════════════

    def watch(self, csv_path: str, poll_interval: float = 5.0,
              on_signal=None, on_trade_close=None, on_bar=None):
        """Watch a CSV file for new bars and process them in real-time.

        1. Reads existing data to warm up z-score baselines
        2. Polls for new rows appended by NinjaTrader
        3. Recomputes features + z-scores incrementally
        4. Fires callbacks on signals and trade closures

        Args:
            csv_path: Path to the live CSV (written by NinjaTrader).
            poll_interval: Seconds between file checks.
            on_signal: Callback(TradeSignal, Trade) when a new trade opens.
            on_trade_close: Callback(Trade) when a trade closes.
            on_bar: Callback(timestamp, close, active_trades) every new bar.
        """
        path = Path(csv_path)
        last_row_count = 0

        # Initial warmup — read everything currently in the file
        raw_df = pd.read_csv(path)
        last_row_count = len(raw_df)

        warmup_result = self._warmup(raw_df)
        if warmup_result is None:
            log.error("Not enough data for warmup. Need at least 50 bars.")
            return

        feat_cols, z_means, z_stds = warmup_result
        log.info(f"Warmup complete: {last_row_count} bars, "
                 f"{len(self.active_trades)} active trades")

        # Main watch loop
        while True:
            time.sleep(poll_interval)

            try:
                raw_df = pd.read_csv(path)
            except Exception as e:
                log.warning(f"Error reading CSV: {e}")
                continue

            current_count = len(raw_df)
            if current_count <= last_row_count:
                continue

            # New bars arrived
            new_rows = raw_df.iloc[last_row_count:]
            last_row_count = current_count

            # Recompute features on full dataset (needed for lookback indicators)
            feat_df = compute_and_clean(raw_df)
            feat_df = feat_df.reset_index(drop=True)
            raw_aligned = raw_df.iloc[-len(feat_df):].reset_index(drop=True)

            # Update z-score baselines with new data
            all_data = {col: feat_df[col].values.astype(np.float64)
                        for col in feat_cols}

            for strat, eqs in self.equations.items():
                for eq_idx, eq in enumerate(eqs):
                    raw_output = eq.tree.evaluate(all_data)
                    z_means[strat][eq_idx] = np.nanmean(raw_output)
                    z_stds[strat][eq_idx] = np.nanstd(raw_output)

            # Process only the new bars
            n_total = len(feat_df)
            n_new = min(len(new_rows), n_total)
            start_idx = n_total - n_new

            for i in range(start_idx, n_total):
                raw = raw_aligned.iloc[i]
                timestamp = str(raw.get("timestamp", i))
                close = float(raw["close"])
                high = float(raw["high"])
                low = float(raw["low"])

                # Update active trades
                closed_trades = self._update_trades(timestamp, close, high, low)
                for t in closed_trades:
                    if on_trade_close:
                        on_trade_close(t)

                # Evaluate signals on this bar
                feat_row = {col: np.array([feat_df.iloc[i][col]])
                            for col in feat_cols}

                for strat, eqs in self.equations.items():
                    if strat in self.active_trades:
                        continue

                    profile = STRATEGY_PROFILES.get(strat, {})
                    long_votes = []
                    short_votes = []

                    for eq_idx, eq in enumerate(eqs):
                        raw_val = eq.tree.evaluate(feat_row)[0]
                        mean = z_means[strat][eq_idx]
                        std = z_stds[strat][eq_idx]
                        if std < 1e-10:
                            continue
                        z = (raw_val - mean) / std
                        if not np.isfinite(z):
                            continue
                        if z > self.Z_LONG:
                            long_votes.append((z, eq.expression[:60]))
                        elif z < self.Z_SHORT:
                            short_votes.append((z, eq.expression[:60]))

                    direction = None
                    votes = []
                    if len(long_votes) >= self.min_votes:
                        direction = "LONG"
                        votes = long_votes
                    elif len(short_votes) >= self.min_votes:
                        direction = "SHORT"
                        votes = short_votes

                    if direction is None:
                        continue

                    if direction == "LONG":
                        target_price = close * (1 + profile.get("target_pct", 0.004))
                        stop_price = close * (1 - profile.get("stop_pct", 0.002))
                    else:
                        target_price = close * (1 - profile.get("target_pct", 0.004))
                        stop_price = close * (1 + profile.get("stop_pct", 0.002))

                    sig = TradeSignal(
                        timestamp=timestamp,
                        direction=direction,
                        strategy=strat,
                        votes_for=len(votes),
                        votes_total=len(eqs),
                        avg_strength=float(np.mean([v[0] for v in votes])),
                        equations=[v[1] for v in votes],
                        close=close,
                        target_pts=profile.get("nq_target_pts", 0),
                        stop_pts=profile.get("nq_stop_pts", 0),
                    )

                    trade = Trade(
                        entry_time=timestamp,
                        direction=direction,
                        strategy=strat,
                        entry_price=close,
                        target_price=target_price,
                        stop_price=stop_price,
                        target_pts=sig.target_pts,
                        stop_pts=sig.stop_pts,
                        votes=len(votes),
                        total_eq=len(eqs),
                        max_hold=profile.get("max_hold", 5),
                    )
                    self.active_trades[strat] = trade

                    if on_signal:
                        on_signal(sig, trade)

                if on_bar:
                    on_bar(timestamp, close, dict(self.active_trades))

    def _warmup(self, raw_df: pd.DataFrame):
        """Warm up z-score baselines from existing data.

        Returns (feat_cols, z_means, z_stds) or None if insufficient data.
        """
        if len(raw_df) < 50:
            return None

        feat_df = compute_and_clean(raw_df)
        feat_df = feat_df.reset_index(drop=True)
        raw_aligned = raw_df.iloc[-len(feat_df):].reset_index(drop=True)

        non_feature = {"timestamp", "ticker", "symbol", "date"}
        target_prefixes = ("fwd_", "scalp_", "momentum_", "swing_")
        feat_cols = [
            c for c in feat_df.columns
            if c not in non_feature
            and not any(c.startswith(p) for p in target_prefixes)
            and feat_df[c].dtype in ("float64", "float32", "int64", "int32")
        ]

        # Parse trees if needed
        if not any(eq.tree for eqs in self.equations.values() for eq in eqs):
            self._parse_trees(feat_cols)

        all_data = {col: feat_df[col].values.astype(np.float64) for col in feat_cols}
        n_bars = len(feat_df)

        # Compute z-score baselines
        z_means: dict[str, list[float]] = {}
        z_stds: dict[str, list[float]] = {}

        for strat, eqs in self.equations.items():
            z_means[strat] = []
            z_stds[strat] = []
            for eq in eqs:
                raw_output = eq.tree.evaluate(all_data)
                z_means[strat].append(float(np.nanmean(raw_output)))
                z_stds[strat].append(float(np.nanstd(raw_output)))

        # Replay existing bars to populate active trades
        z_signals: dict[str, list[np.ndarray]] = {}
        for strat, eqs in self.equations.items():
            z_signals[strat] = []
            for eq_idx, eq in enumerate(eqs):
                raw_output = eq.tree.evaluate(all_data)
                mean = z_means[strat][eq_idx]
                std = z_stds[strat][eq_idx]
                if std < 1e-10:
                    z = np.zeros(n_bars)
                else:
                    z = (raw_output - mean) / std
                z = np.where(np.isfinite(z), z, 0.0)
                z_signals[strat].append(z)

        for i in range(n_bars):
            raw = raw_aligned.iloc[i]
            timestamp = str(raw.get("timestamp", i))
            close = float(raw["close"])
            high = float(raw["high"])
            low = float(raw["low"])

            self._update_trades(timestamp, close, high, low)

            for strat, eqs in self.equations.items():
                if strat in self.active_trades:
                    continue

                profile = STRATEGY_PROFILES.get(strat, {})
                long_votes = []
                short_votes = []

                for eq_idx, eq in enumerate(eqs):
                    z = z_signals[strat][eq_idx][i]
                    if z > self.Z_LONG:
                        long_votes.append((z, eq.expression[:60]))
                    elif z < self.Z_SHORT:
                        short_votes.append((z, eq.expression[:60]))

                direction = None
                votes = []
                if len(long_votes) >= self.min_votes:
                    direction = "LONG"
                    votes = long_votes
                elif len(short_votes) >= self.min_votes:
                    direction = "SHORT"
                    votes = short_votes

                if direction is None:
                    continue

                if direction == "LONG":
                    target_price = close * (1 + profile.get("target_pct", 0.004))
                    stop_price = close * (1 - profile.get("stop_pct", 0.002))
                else:
                    target_price = close * (1 - profile.get("target_pct", 0.004))
                    stop_price = close * (1 + profile.get("stop_pct", 0.002))

                self.active_trades[strat] = Trade(
                    entry_time=timestamp,
                    direction=direction,
                    strategy=strat,
                    entry_price=close,
                    target_price=target_price,
                    stop_price=stop_price,
                    target_pts=profile.get("nq_target_pts", 0),
                    stop_pts=profile.get("nq_stop_pts", 0),
                    votes=len(votes),
                    total_eq=len(eqs),
                    max_hold=profile.get("max_hold", 5),
                )

        return feat_cols, z_means, z_stds

    def _update_trades(self, timestamp: str, close: float,
                       high: float, low: float) -> list[Trade]:
        """Update active trades against current bar. Returns closed trades."""
        closed = []
        for strat in list(self.active_trades.keys()):
            trade = self.active_trades[strat]
            trade.bars_held += 1
            profile = STRATEGY_PROFILES.get(strat, {})
            max_hold = profile.get("max_hold", 5)

            if trade.direction == "LONG":
                hit_stop = low <= trade.stop_price
                hit_target = high >= trade.target_price
            else:
                hit_stop = high >= trade.stop_price
                hit_target = low <= trade.target_price

            if hit_stop:
                trade.exit_time = timestamp
                trade.exit_price = trade.stop_price
                trade.result = "LOSS"
                trade.pnl_pts = -trade.stop_pts
                self.trades.append(trade)
                del self.active_trades[strat]
                closed.append(trade)
            elif hit_target:
                trade.exit_time = timestamp
                trade.exit_price = trade.target_price
                trade.result = "WIN"
                trade.pnl_pts = trade.target_pts
                self.trades.append(trade)
                del self.active_trades[strat]
                closed.append(trade)
            elif trade.bars_held >= max_hold:
                trade.exit_time = timestamp
                trade.exit_price = close
                if trade.direction == "LONG":
                    trade.pnl_pts = (close - trade.entry_price) / trade.entry_price * NQ_REF_PRICE
                else:
                    trade.pnl_pts = (trade.entry_price - close) / trade.entry_price * NQ_REF_PRICE
                trade.result = "TIME_EXIT"
                self.trades.append(trade)
                del self.active_trades[strat]
                closed.append(trade)

        return closed
