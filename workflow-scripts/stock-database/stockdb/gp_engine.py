"""Genetic Programming engine for evolving trading strategies.

Evolves mathematical expressions (trees) that take scale-normalized features
as input and output a trading signal. Fitness is scored on actual target/stop
trade outcomes — not forward returns. Two modes: scalp (25pt NQ) and swing
(100pt NQ), optimized in parallel.

Architecture:
    - Each individual is an expression tree (e.g., gap_atr * rel_volume - atr_pct)
    - Fitness = based on target/stop hit simulation (did price hit +25 before -25?)
    - Two strategy profiles: scalp (1:1 R:R, 2-bar hold) and swing (2:1 R:R, 5-bar hold)
    - Multi-threaded: each CPU thread runs an independent population
    - Results display NQ dollar P&L ($20/point)

Usage:
    from stockdb.gp_engine import GPEngine
    engine = GPEngine(n_workers=10)
    results = engine.train(feature_df, strategy="both")
"""

import copy
import json
import logging
import math
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from pathlib import Path

import warnings
import numpy as np
import pandas as pd

# Suppress numpy divide-by-zero warnings (we handle them with _safe_div)
warnings.filterwarnings("ignore", category=RuntimeWarning,
                        message=".*divide by zero.*")
warnings.filterwarnings("ignore", category=RuntimeWarning,
                        message=".*invalid value.*")

from .config import DATA_DIR

log = logging.getLogger(__name__)

RESULTS_DIR = DATA_DIR / "gp_results"

# ═══════════════════════════════════════════════════════════════════════════
# NQ contract specs & strategy profiles
# ═══════════════════════════════════════════════════════════════════════════

NQ_POINT_VALUE = 20.0    # $/point for E-mini NQ
NQ_REF_PRICE = 25000.0   # approximate current NQ price for display conversion

STRATEGY_PROFILES = {
    "scalp": {
        "target_pct": 0.0016,    # ~40pts NQ
        "stop_pct": 0.0008,      # ~20pts NQ (2:1 R:R)
        "max_hold": 3,           # 3 hours max
        "nq_target_pts": 40,
        "nq_stop_pts": 20,
        "long_col": "scalp_long",
        "short_col": "scalp_short",
    },
    "momentum": {
        "target_pct": 0.0025,    # ~50pts NQ
        "stop_pct": 0.00125,     # ~25pts NQ (2:1 R:R)
        "max_hold": 4,           # 4 hours max
        "nq_target_pts": 50,
        "nq_stop_pts": 25,
        "long_col": "momentum_long",
        "short_col": "momentum_short",
    },
    "swing": {
        "target_pct": 0.004,     # ~100pts NQ
        "stop_pct": 0.002,       # ~50pts NQ (2:1 R:R)
        "max_hold": 5,           # 5 hours max
        "nq_target_pts": 100,
        "nq_stop_pts": 50,
        "long_col": "swing_long",
        "short_col": "swing_short",
    },
}

# Default worker distribution: 50/50 swing + momentum
DEFAULT_STRATEGY_WEIGHTS = {"swing": 0.5, "momentum": 0.5}


def distribute_workers(n_workers: int,
                       weights: dict[str, float] | None = None) -> list[str]:
    """Assign each worker a strategy profile based on weight distribution."""
    if weights is None:
        weights = DEFAULT_STRATEGY_WEIGHTS

    assignments = []
    remaining = n_workers
    sorted_strats = sorted(weights.items(), key=lambda x: x[1], reverse=True)

    for i, (strat, weight) in enumerate(sorted_strats):
        if i == len(sorted_strats) - 1:
            count = remaining
        else:
            count = max(1, round(n_workers * weight))
            count = min(count, remaining)
        assignments.extend([strat] * count)
        remaining -= count
        if remaining <= 0:
            break

    return assignments[:n_workers]


def pct_to_nq_dollars(pct: float) -> float:
    """Convert a percentage return to NQ dollar P&L per contract."""
    points = pct * NQ_REF_PRICE
    return points * NQ_POINT_VALUE


# ═══════════════════════════════════════════════════════════════════════════
# Expression Tree
# ═══════════════════════════════════════════════════════════════════════════

def _safe_div(a, b):
    return np.where(np.abs(b) > 1e-10, a / b, 0.0)

def _safe_sqrt(a):
    return np.sqrt(np.abs(a))

def _neg(a):
    return -a

def _abs(a):
    return np.abs(a)

def _square(a):
    return a * a

def _clip(a):
    return np.clip(a, -1, 1)


BINARY_OPS = [
    ("add", np.add, 2),
    ("sub", np.subtract, 2),
    ("mul", np.multiply, 2),
    ("div", _safe_div, 2),
    ("max", np.maximum, 2),
    ("min", np.minimum, 2),
]

UNARY_OPS = [
    ("neg", _neg, 1),
    ("abs", _abs, 1),
    ("sqrt", _safe_sqrt, 1),
    ("square", _square, 1),
    ("clip", _clip, 1),
]

ALL_OPS = BINARY_OPS + UNARY_OPS
_OP_LOOKUP = {name: func for name, func, _ in ALL_OPS}


@dataclass
class Node:
    op: str | None = None
    children: list = field(default_factory=list)
    feature: str | None = None
    constant: float | None = None

    def is_terminal(self) -> bool:
        return self.op is None

    def depth(self) -> int:
        if self.is_terminal():
            return 0
        return 1 + max(c.depth() for c in self.children)

    def size(self) -> int:
        if self.is_terminal():
            return 1
        return 1 + sum(c.size() for c in self.children)

    def evaluate(self, data: dict[str, np.ndarray]) -> np.ndarray:
        if self.feature is not None:
            return data[self.feature]
        if self.constant is not None:
            return np.full(len(next(iter(data.values()))), self.constant)

        op_func = _OP_LOOKUP[self.op]
        child_vals = [c.evaluate(data) for c in self.children]
        result = op_func(*child_vals)
        result = np.where(np.isfinite(result), result, 0.0)
        return result

    def to_string(self) -> str:
        if self.feature is not None:
            return self.feature
        if self.constant is not None:
            return f"{self.constant:.4f}"
        if len(self.children) == 1:
            return f"{self.op}({self.children[0].to_string()})"
        return f"({self.children[0].to_string()} {self.op} {self.children[1].to_string()})"

    def copy(self):
        return copy.deepcopy(self)


# ═══════════════════════════════════════════════════════════════════════════
# Tree generation and mutation
# ═══════════════════════════════════════════════════════════════════════════

def random_terminal(features: list[str]) -> Node:
    if random.random() < 0.7:
        return Node(feature=random.choice(features))
    else:
        return Node(constant=round(random.uniform(-2, 2), 4))


def random_tree(features: list[str], max_depth: int = 4, depth: int = 0) -> Node:
    if depth >= max_depth or (depth > 1 and random.random() < 0.3):
        return random_terminal(features)
    op_name, _, arity = random.choice(ALL_OPS)
    children = [random_tree(features, max_depth, depth + 1) for _ in range(arity)]
    return Node(op=op_name, children=children)


def mutate(tree: Node, features: list[str], max_depth: int = 4) -> Node:
    tree = tree.copy()
    nodes = _collect_nodes(tree)
    if not nodes:
        return random_tree(features, max_depth)
    target = random.choice(nodes)
    replacement = random_tree(features, max_depth=2)
    target.op = replacement.op
    target.children = replacement.children
    target.feature = replacement.feature
    target.constant = replacement.constant
    if tree.depth() > max_depth:
        return random_tree(features, max_depth)
    return tree


def crossover(parent1: Node, parent2: Node, max_depth: int = 4) -> Node:
    child = parent1.copy()
    donor = parent2.copy()
    child_nodes = _collect_nodes(child)
    donor_nodes = _collect_nodes(donor)
    if not child_nodes or not donor_nodes:
        return child
    target = random.choice(child_nodes)
    source = random.choice(donor_nodes)
    target.op = source.op
    target.children = source.children
    target.feature = source.feature
    target.constant = source.constant
    if child.depth() > max_depth:
        return parent1.copy()
    return child


def _collect_nodes(tree: Node) -> list[Node]:
    nodes = [tree]
    for c in tree.children:
        nodes.extend(_collect_nodes(c))
    return nodes


# ═══════════════════════════════════════════════════════════════════════════
# Fitness evaluation (target/stop based)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TradeResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    win_rate: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    fitness: float = 0.0
    trade_ratio: float = 0.0


def evaluate_fitness(tree: Node, data: dict[str, np.ndarray],
                     long_outcomes: np.ndarray,
                     short_outcomes: np.ndarray,
                     min_trades: int = 30) -> TradeResult:
    """Score a GP tree using precomputed target/stop trade outcomes.

    The tree produces a signal. Z-score > 1.0 → long entry, < -1.0 → short.
    For each triggered trade, the outcome comes from the precomputed
    target/stop simulation (not raw forward returns).
    """
    result = TradeResult()
    try:
        signal = tree.evaluate(data)
    except Exception:
        return result

    valid = (np.isfinite(signal) & np.isfinite(long_outcomes)
             & np.isfinite(short_outcomes))
    signal = signal[valid]
    long_out = long_outcomes[valid]
    short_out = short_outcomes[valid]
    n_bars = len(signal)

    if n_bars < min_trades:
        return result

    # Z-score thresholding — only trade statistically extreme signals
    sig_mean = signal.mean()
    sig_std = signal.std()
    if sig_std < 1e-10:
        return result

    z_signal = (signal - sig_mean) / sig_std
    long_mask = z_signal > 1.0
    short_mask = z_signal < -1.0

    if long_mask.sum() + short_mask.sum() < min_trades:
        return result

    # Combine: longs use long outcomes, shorts use short outcomes
    trade_returns = np.concatenate([
        long_out[long_mask],
        short_out[short_mask],
    ])

    result.total_trades = len(trade_returns)
    result.trade_ratio = result.total_trades / n_bars if n_bars > 0 else 0.0
    wins = trade_returns[trade_returns > 0]
    losses = trade_returns[trade_returns <= 0]

    result.wins = len(wins)
    result.losses = len(losses)
    result.win_rate = result.wins / result.total_trades if result.total_trades > 0 else 0
    result.avg_win = wins.mean() if len(wins) > 0 else 0.0
    result.avg_loss = abs(losses.mean()) if len(losses) > 0 else 0.0

    gross_profit = wins.sum() if len(wins) > 0 else 0.0
    gross_loss = abs(losses.sum()) if len(losses) > 0 else 1e-10

    result.profit_factor = gross_profit / gross_loss
    result.total_return = trade_returns.sum()
    result.expectancy = trade_returns.mean()

    equity = np.cumsum(trade_returns)
    running_max = np.maximum.accumulate(equity)
    drawdown = running_max - equity
    result.max_drawdown = drawdown.max() if len(drawdown) > 0 else 0.0

    # ── Fitness function ──
    pf = min(result.profit_factor, 5.0)

    # Trade count reward saturates at 15% of bars
    trade_cap = max(int(n_bars * 0.15), min_trades * 2)
    effective_trades = min(result.total_trades, trade_cap)
    result.fitness = pf * math.sqrt(effective_trades)

    # Over-trading penalty
    if result.trade_ratio > 0.30:
        result.fitness *= 0.30 / result.trade_ratio

    # Complexity penalty
    complexity = tree.size()
    if complexity > 20:
        result.fitness *= 0.8
    if complexity > 30:
        result.fitness *= 0.5

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Worker progress message
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class WorkerUpdate:
    worker_id: int
    generation: int
    total_generations: int
    best_fitness: float
    best_pf: float
    best_wr: float
    best_trades: int
    best_trade_ratio: float
    best_expr: str
    evaluations: int
    elapsed: float
    strategy: str = ""
    status: str = "running"


# ═══════════════════════════════════════════════════════════════════════════
# Population evolution (worker process)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Individual:
    tree: Node
    fitness: TradeResult = field(default_factory=TradeResult)


def _tournament_select(population: list[Individual],
                       tournament_size: int) -> Individual:
    contestants = random.sample(
        population, min(tournament_size, len(population))
    )
    return max(contestants, key=lambda x: x.fitness.fitness)


def run_population(
    feature_data: dict[str, np.ndarray],
    long_outcomes: np.ndarray,
    short_outcomes: np.ndarray,
    feature_names: list[str],
    excluded_features: list[str] | None = None,
    pop_size: int = 200,
    generations: int = 50,
    max_depth: int = 5,
    tournament_size: int = 5,
    mutation_rate: float = 0.3,
    crossover_rate: float = 0.6,
    elite_count: int = 5,
    min_trades: int = 30,
    strategy_name: str = "scalp",
    worker_id: int = 0,
    seed: int | None = None,
    progress_queue: Any = None,
) -> list[dict]:
    """Run a single GP population through evolution. Called per-worker."""
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    # Apply feature exclusion — forces this worker to find edge without
    # certain features, producing diverse strategies across workers.
    if excluded_features:
        feature_names = [f for f in feature_names if f not in excluded_features]
        feature_data = {k: v for k, v in feature_data.items()
                        if k not in excluded_features}

    population = [
        Individual(tree=random_tree(feature_names, max_depth))
        for _ in range(pop_size)
    ]

    best_ever_fitness = 0.0
    stagnation = 0
    total_evals = 0
    start_time = time.time()

    for gen in range(generations):
        for ind in population:
            ind.fitness = evaluate_fitness(
                ind.tree, feature_data, long_outcomes, short_outcomes,
                min_trades=min_trades
            )
            total_evals += 1

        population.sort(key=lambda x: x.fitness.fitness, reverse=True)
        best = population[0]

        if best.fitness.fitness > best_ever_fitness:
            best_ever_fitness = best.fitness.fitness
            stagnation = 0
        else:
            stagnation += 1

        if progress_queue is not None:
            elapsed = time.time() - start_time
            update = WorkerUpdate(
                worker_id=worker_id,
                generation=gen + 1,
                total_generations=generations,
                best_fitness=best.fitness.fitness,
                best_pf=best.fitness.profit_factor,
                best_wr=best.fitness.win_rate,
                best_trades=best.fitness.total_trades,
                best_trade_ratio=best.fitness.trade_ratio,
                best_expr=best.tree.to_string()[:70],
                evaluations=total_evals,
                elapsed=elapsed,
                strategy=strategy_name,
                status="running",
            )
            progress_queue.put(update)

        if stagnation > 15:
            if progress_queue is not None:
                elapsed = time.time() - start_time
                progress_queue.put(WorkerUpdate(
                    worker_id=worker_id, generation=gen + 1,
                    total_generations=generations,
                    best_fitness=best.fitness.fitness,
                    best_pf=best.fitness.profit_factor,
                    best_wr=best.fitness.win_rate,
                    best_trades=best.fitness.total_trades,
                    best_trade_ratio=best.fitness.trade_ratio,
                    best_expr=best.tree.to_string()[:70],
                    evaluations=total_evals, elapsed=elapsed,
                    strategy=strategy_name,
                    status="stagnant",
                ))
            break

        # Selection + reproduction
        new_pop = []
        for i in range(min(elite_count, len(population))):
            new_pop.append(Individual(tree=population[i].tree.copy(),
                                     fitness=population[i].fitness))

        while len(new_pop) < pop_size:
            r = random.random()
            if r < crossover_rate:
                p1 = _tournament_select(population, tournament_size)
                p2 = _tournament_select(population, tournament_size)
                child_tree = crossover(p1.tree, p2.tree, max_depth)
                new_pop.append(Individual(tree=child_tree))
            elif r < crossover_rate + mutation_rate:
                parent = _tournament_select(population, tournament_size)
                child_tree = mutate(parent.tree, feature_names, max_depth)
                new_pop.append(Individual(tree=child_tree))
            else:
                parent = _tournament_select(population, tournament_size)
                new_pop.append(Individual(tree=parent.tree.copy()))

        population = new_pop

    # Final evaluation
    for ind in population:
        ind.fitness = evaluate_fitness(
            ind.tree, feature_data, long_outcomes, short_outcomes,
            min_trades=min_trades
        )
        total_evals += 1

    population.sort(key=lambda x: x.fitness.fitness, reverse=True)

    if progress_queue is not None:
        elapsed = time.time() - start_time
        best = population[0]
        progress_queue.put(WorkerUpdate(
            worker_id=worker_id, generation=generations,
            total_generations=generations,
            best_fitness=best.fitness.fitness,
            best_pf=best.fitness.profit_factor,
            best_wr=best.fitness.win_rate,
            best_trades=best.fitness.total_trades,
            best_trade_ratio=best.fitness.trade_ratio,
            best_expr=best.tree.to_string()[:70],
            evaluations=total_evals, elapsed=elapsed,
            strategy=strategy_name,
            status="done",
        ))

    # Return top 10
    profile = STRATEGY_PROFILES[strategy_name]
    top_results = []
    for ind in population[:10]:
        if ind.fitness.total_trades > 0:
            nq_per_trade = pct_to_nq_dollars(ind.fitness.expectancy)
            nq_total = pct_to_nq_dollars(ind.fitness.total_return)
            top_results.append({
                "expression": ind.tree.to_string(),
                "fitness": ind.fitness.fitness,
                "profit_factor": ind.fitness.profit_factor,
                "win_rate": ind.fitness.win_rate,
                "total_trades": ind.fitness.total_trades,
                "trade_ratio": ind.fitness.trade_ratio,
                "avg_win": ind.fitness.avg_win,
                "avg_loss": ind.fitness.avg_loss,
                "expectancy": ind.fitness.expectancy,
                "total_return": ind.fitness.total_return,
                "max_drawdown": ind.fitness.max_drawdown,
                "tree_size": ind.tree.size(),
                "worker_id": worker_id,
                "strategy": strategy_name,
                "excluded_features": excluded_features or [],
                "n_features": len(feature_names),
                "nq_per_trade": nq_per_trade,
                "nq_total": nq_total,
                "nq_target_pts": profile["nq_target_pts"],
                "nq_stop_pts": profile["nq_stop_pts"],
            })

    return top_results


# ═══════════════════════════════════════════════════════════════════════════
# Worker wrapper for multiprocessing
# ═══════════════════════════════════════════════════════════════════════════

def _worker_task(args: dict) -> list[dict]:
    return run_population(**args)


# ═══════════════════════════════════════════════════════════════════════════
# Rich Live Dashboard
# ═══════════════════════════════════════════════════════════════════════════

def _run_live_dashboard(progress_queue: Any, n_workers: int,
                        generations: int, pop_size: int,
                        n_tickers: int, n_bars: int, n_features: int,
                        strategy: str, worker_strategies: list[str],
                        worker_exclusions: list[list[str]] | None = None):
    """Render a live rich dashboard from worker progress updates."""
    from collections import Counter
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text

    console = Console()

    worker_state = {}
    state = {"best_fitness": 0.0, "total_evals": 0}
    start_time = time.time()
    workers_done = 0

    def build_display():
        elapsed = time.time() - start_time
        eps = state["total_evals"] / elapsed if elapsed > 0 else 0

        # ── Header ──
        hdr = Text()
        hdr.append("STOCKDB GP ENGINE", style="bold cyan")
        hdr.append(" — TARGET/STOP MODE\n", style="bold yellow")
        hdr.append(f"{n_workers} workers", style="bold yellow")
        hdr.append(f" | {pop_size} pop | {generations} gens | ", style="dim")
        hdr.append(f"{n_tickers} tickers", style="green")
        hdr.append(f" | {n_bars:,} bars | {n_features} feat", style="dim")

        dist = Counter(worker_strategies)
        strat_str = " | ".join(
            f"{s.upper()}×{c}" for s, c in sorted(dist.items())
        )
        hdr.append(f" | [{strat_str}]\n", style="bold green")

        # NQ bracket info
        for s_name, s_info in STRATEGY_PROFILES.items():
            if s_name in dist:
                hdr.append(
                    f"  {s_name.upper()}: +{s_info['nq_target_pts']}pt"
                    f"/-{s_info['nq_stop_pts']}pt"
                    f" ({s_info['max_hold']}bar max)",
                    style="dim cyan"
                )
                hdr.append("  ", style="dim")
        hdr.append("\n")

        hdr.append(f"Elapsed ", style="dim")
        hdr.append(f"{elapsed:.1f}s", style="bold white")
        hdr.append(f"  Evals ", style="dim")
        hdr.append(f"{state['total_evals']:,}", style="bold white")
        hdr.append(f"  Speed ", style="dim")
        hdr.append(f"{eps:,.0f}/s", style="bold cyan")
        hdr.append(f"  Done ", style="dim")
        done_count = sum(1 for w in worker_state.values()
                         if w.status in ("done", "stagnant"))
        d_style = "bold green" if done_count == n_workers else "bold yellow"
        hdr.append(f"{done_count}/{n_workers}", style=d_style)

        # ── Worker table ──
        tbl = Table(show_header=True, header_style="bold magenta",
                    border_style="dim", expand=False, padding=(0, 1),
                    min_width=90)
        tbl.add_column("W", style="cyan", no_wrap=True)
        tbl.add_column("Strat", no_wrap=True, style="dim cyan")
        tbl.add_column("Gen", no_wrap=True)
        tbl.add_column("Progress", min_width=22, no_wrap=True)
        tbl.add_column("Fit", justify="right", no_wrap=True)
        tbl.add_column("PF", justify="right", no_wrap=True)
        tbl.add_column("WR", justify="right", no_wrap=True)
        tbl.add_column("Select", justify="right", no_wrap=True)
        tbl.add_column("Ev/s", justify="right", no_wrap=True)
        tbl.add_column("Status", justify="center", no_wrap=True)

        for wid in range(n_workers):
            w_strat = worker_strategies[wid].upper() if wid < len(worker_strategies) else "?"
            excl = ""
            if worker_exclusions and wid < len(worker_exclusions):
                excl = ", ".join(worker_exclusions[wid][:3])

            ws = worker_state.get(wid)
            if ws is None:
                tbl.add_row(str(wid + 1), w_strat, "-", "", "-", "-", "-",
                            "-", "-", Text("wait", style="dim"))
                continue

            pct = ws.generation / ws.total_generations
            filled = int(pct * 16)
            bar = Text(f"{'█' * filled}{'░' * (16 - filled)} {pct:>3.0%}",
                       style="cyan")

            w_eps = ws.evaluations / ws.elapsed if ws.elapsed > 0 else 0

            fit_s = ("bold green" if ws.best_fitness >= state["best_fitness"] * 0.95
                     else "yellow" if ws.best_fitness >= state["best_fitness"] * 0.8
                     else "white")
            pf_s = ("bold green" if ws.best_pf >= 1.5
                    else "yellow" if ws.best_pf >= 1.2 else "red")

            st = (Text("DONE", style="bold green") if ws.status == "done"
                  else Text("STALL", style="bold yellow") if ws.status == "stagnant"
                  else Text("EVOLVE", style="bold cyan"))

            sel_pct = ws.best_trade_ratio
            sel_s = ("bold green" if sel_pct <= 0.20 else
                     "yellow" if sel_pct <= 0.35 else "red")

            tbl.add_row(
                str(wid + 1),
                w_strat,
                f"{ws.generation}/{ws.total_generations}",
                bar,
                Text(f"{ws.best_fitness:.1f}", style=fit_s),
                Text(f"{ws.best_pf:.2f}", style=pf_s),
                f"{ws.best_wr:.0%}",
                Text(f"{sel_pct:.0%}", style=sel_s),
                f"{w_eps:,.0f}",
                st,
            )

        return Group(
            Panel(hdr, border_style="cyan",
                  title="[bold cyan]GP Training — Target/Stop Mode[/bold cyan]"),
            tbl,
        )

    with Live(console=console, refresh_per_second=6, transient=False) as live:
        workers_done = 0
        while workers_done < n_workers:
            try:
                while True:
                    u = progress_queue.get_nowait()
                    worker_state[u.worker_id] = u
                    state["total_evals"] = sum(w.evaluations
                                               for w in worker_state.values())
                    if u.best_fitness > state["best_fitness"]:
                        state["best_fitness"] = u.best_fitness
                    if u.status in ("done", "stagnant"):
                        workers_done = sum(
                            1 for w in worker_state.values()
                            if w.status in ("done", "stagnant")
                        )
            except Exception:
                pass

            live.update(build_display())
            time.sleep(0.15)


# ═══════════════════════════════════════════════════════════════════════════
# Multi-threaded training engine
# ═══════════════════════════════════════════════════════════════════════════

class GPEngine:
    """Multi-threaded GP training engine with target/stop fitness."""

    def __init__(self, n_workers: int = 10, pop_size: int = 200,
                 generations: int = 50, max_depth: int = 5,
                 min_trades: int = 30):
        self.n_workers = n_workers
        self.pop_size = pop_size
        self.generations = generations
        self.max_depth = max_depth
        self.min_trades = min_trades

    def train(self, feature_df: pd.DataFrame,
              val_df: pd.DataFrame | None = None,
              strategy: str = "both",
              custom_weights: dict[str, float] | None = None) -> list[dict]:
        """Train GP across multiple workers with live dashboard.

        Args:
            feature_df: Training features (all tickers combined).
            val_df: External validation DataFrame (e.g. NQ futures).
            strategy: "scalp", "swing", or "both".
            custom_weights: Override strategy weight distribution.
        """
        from rich.console import Console

        console = Console()

        # Identify feature columns (exclude timestamps, targets, outcomes)
        non_feature = {"timestamp", "ticker", "symbol", "date"}
        target_prefixes = ("fwd_", "scalp_", "momentum_", "swing_")
        feature_cols = [
            c for c in feature_df.columns
            if c not in non_feature
            and not any(c.startswith(p) for p in target_prefixes)
            and feature_df[c].dtype in ("float64", "float32", "int64", "int32")
        ]

        n_tickers = (feature_df["ticker"].nunique()
                     if "ticker" in feature_df.columns else 1)

        # Worker strategy assignments
        if strategy == "both":
            weights = custom_weights or DEFAULT_STRATEGY_WEIGHTS
        else:
            weights = {strategy: 1.0}

        worker_strategies = distribute_workers(self.n_workers, weights)
        unique_strats = sorted(set(worker_strategies))

        # Validate required outcome columns exist
        for strat in unique_strats:
            profile = STRATEGY_PROFILES[strat]
            for col in (profile["long_col"], profile["short_col"]):
                if col not in feature_df.columns:
                    raise ValueError(
                        f"Missing column '{col}' in feature_df. "
                        f"Recompute features with latest features.py."
                    )

        # Prepare training data — use 100% when external validation provided
        if val_df is not None:
            console.print("[bold green]External validation — training on 100% of data[/bold green]")
            train_split = feature_df
            val_split = val_df
            console.print(
                f"[dim]Train: {len(train_split):,} bars | "
                f"Val (external): {len(val_split):,} bars[/dim]"
            )
        else:
            split_idx = int(len(feature_df) * 0.7)
            train_split = feature_df.iloc[:split_idx]
            val_split = feature_df.iloc[split_idx:]
            console.print(
                f"[dim]Train: {len(train_split):,} bars | "
                f"Val (30% holdout): {len(val_split):,} bars[/dim]"
            )

        # Feature arrays (shared by all workers)
        train_data = {
            col: train_split[col].values.astype(np.float64)
            for col in feature_cols
        }
        val_data = {
            col: val_split[col].values.astype(np.float64)
            for col in feature_cols
        }

        # Outcome arrays per strategy
        train_outcomes = {}
        val_outcomes = {}
        for strat in unique_strats:
            profile = STRATEGY_PROFILES[strat]
            train_outcomes[strat] = {
                "long": train_split[profile["long_col"]].values.astype(np.float64),
                "short": train_split[profile["short_col"]].values.astype(np.float64),
            }
            val_outcomes[strat] = {
                "long": val_split[profile["long_col"]].values.astype(np.float64),
                "short": val_split[profile["short_col"]].values.astype(np.float64),
            }

        # Progress queue for live dashboard
        import multiprocessing
        progress_queue = multiprocessing.Manager().Queue()

        # Generate random feature exclusions per worker to force diversity.
        # Each worker drops 2-3 random features so they can't all converge
        # on the same dominant feature (e.g. dist_swing_high).
        worker_exclusions = []
        for i in range(self.n_workers):
            n_exclude = random.randint(2, 3)
            excluded = random.sample(feature_cols, min(n_exclude, len(feature_cols) - 5))
            worker_exclusions.append(excluded)

        console.print(f"[dim]Feature exclusion: each worker drops 2-3 features for diversity[/dim]\n")

        # Build worker args
        worker_args = []
        for i in range(self.n_workers):
            w_strat = worker_strategies[i]
            worker_args.append({
                "feature_data": train_data,
                "long_outcomes": train_outcomes[w_strat]["long"],
                "short_outcomes": train_outcomes[w_strat]["short"],
                "feature_names": feature_cols,
                "excluded_features": worker_exclusions[i],
                "pop_size": self.pop_size,
                "generations": self.generations,
                "max_depth": self.max_depth,
                "min_trades": self.min_trades,
                "strategy_name": w_strat,
                "worker_id": i,
                "seed": int(time.time() * 1000) % (2**31) + i,
                "progress_queue": progress_queue,
            })

        # Launch dashboard thread + workers
        import threading

        dashboard_thread = threading.Thread(
            target=_run_live_dashboard,
            args=(progress_queue, self.n_workers, self.generations,
                  self.pop_size, n_tickers, len(train_split),
                  len(feature_cols), strategy, worker_strategies,
                  worker_exclusions),
            daemon=True,
        )
        dashboard_thread.start()

        all_results = []
        start_time = time.time()

        if self.n_workers == 1:
            results = _worker_task(worker_args[0])
            all_results.extend(results)
        else:
            with ProcessPoolExecutor(max_workers=self.n_workers) as executor:
                futures = {
                    executor.submit(_worker_task, args): args["worker_id"]
                    for args in worker_args
                }
                for future in as_completed(futures):
                    worker_id = futures[future]
                    try:
                        results = future.result()
                        all_results.extend(results)
                    except Exception as e:
                        log.error(f"Worker {worker_id} failed: {e}")

        dashboard_thread.join(timeout=2)
        elapsed = time.time() - start_time

        if not all_results:
            console.print("\n[bold red]No viable equations found.[/bold red] "
                          "Try more generations or larger population.")
            return []

        # Validate on held-out / external data
        validated = self._validate(
            all_results, val_data, val_outcomes, feature_cols
        )

        val_source = "external_nq" if val_df is not None else "30pct_holdout"
        self._save_results(validated, strategy, elapsed,
                           worker_strategies, val_source)
        self._print_results(console, validated, elapsed)

        return validated

    def _validate(self, candidates: list[dict],
                  val_data: dict[str, np.ndarray],
                  val_outcomes: dict[str, dict[str, np.ndarray]],
                  feature_cols: list[str]) -> list[dict]:
        """Validate candidates against held-out data using target/stop fitness."""
        from .scanner import parse_expression

        validated = []
        for cand in candidates:
            strat = cand.get("strategy", "scalp")
            outcomes = val_outcomes.get(strat)
            if outcomes is None:
                continue
            try:
                tree = parse_expression(cand["expression"], feature_cols)
                val_result = evaluate_fitness(
                    tree, val_data,
                    outcomes["long"], outcomes["short"],
                    min_trades=10
                )
                cand["val_fitness"] = val_result.fitness
                cand["val_pf"] = val_result.profit_factor
                cand["val_wr"] = val_result.win_rate
                cand["val_trades"] = val_result.total_trades
                cand["val_expectancy"] = val_result.expectancy
                cand["val_total_return"] = val_result.total_return
                cand["val_nq_per_trade"] = pct_to_nq_dollars(val_result.expectancy)
                cand["val_nq_total"] = pct_to_nq_dollars(val_result.total_return)
                cand["validated"] = True
            except Exception:
                cand["val_fitness"] = 0
                cand["val_pf"] = 0
                cand["val_wr"] = 0
                cand["val_trades"] = 0
                cand["val_expectancy"] = 0
                cand["val_total_return"] = 0
                cand["val_nq_per_trade"] = 0
                cand["val_nq_total"] = 0
                cand["validated"] = False
            validated.append(cand)

        validated.sort(key=lambda x: x.get("val_fitness", 0), reverse=True)

        # Smart dedup: two equations are "same" if they produce nearly
        # identical validation metrics (same trades → same PF/WR/trades).
        # This catches functional duplicates like (a max a) vs (a).
        unique = []
        for v in validated:
            is_dup = False
            v_sig = (round(v.get("val_pf", 0), 2),
                     round(v.get("val_wr", 0), 2),
                     v.get("val_trades", 0))
            for u in unique:
                u_sig = (round(u.get("val_pf", 0), 2),
                         round(u.get("val_wr", 0), 2),
                         u.get("val_trades", 0))
                if v_sig == u_sig and v.get("strategy") == u.get("strategy"):
                    # Same metrics + same strategy = functional duplicate.
                    # Keep the simpler one.
                    if v.get("tree_size", 99) < u.get("tree_size", 99):
                        unique[unique.index(u)] = v
                    is_dup = True
                    break
            if not is_dup:
                unique.append(v)

        return unique[:20]

    def _save_results(self, results: list[dict], strategy: str,
                      elapsed: float, worker_strategies: list[str],
                      val_source: str = "holdout"):
        from collections import Counter

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RESULTS_DIR / f"gp_run_{strategy}_{timestamp}.json"

        output = {
            "timestamp": timestamp,
            "strategy": strategy,
            "strategy_profiles": {
                k: {kk: vv for kk, vv in v.items()
                     if kk not in ("long_col", "short_col")}
                for k, v in STRATEGY_PROFILES.items()
            },
            "nq_point_value": NQ_POINT_VALUE,
            "n_workers": self.n_workers,
            "pop_size": self.pop_size,
            "generations": self.generations,
            "elapsed_seconds": elapsed,
            "validation": val_source,
            "worker_distribution": dict(Counter(worker_strategies)),
            "results": results,
        }

        path.write_text(json.dumps(output, indent=2))

    def _print_results(self, console, results: list[dict], elapsed: float):
        from rich.table import Table
        from rich.panel import Panel
        from rich.text import Text

        # Results table
        table = Table(
            show_header=True, header_style="bold magenta",
            border_style="green",
            title="[bold green]Final Results — NQ Target/Stop (Validated)[/bold green]",
            expand=True, padding=(0, 1),
        )
        table.add_column("#", width=3, justify="right", style="dim")
        table.add_column("Strat", width=6, justify="center", style="dim cyan")
        table.add_column("Train PF", width=9, justify="right")
        table.add_column("Val PF", width=9, justify="right")
        table.add_column("WR", width=6, justify="right")
        table.add_column("Val WR", width=6, justify="right")
        table.add_column("Trades", width=7, justify="right")
        table.add_column("$/Trade", width=9, justify="right")
        table.add_column("NQ Total $", width=11, justify="right")
        table.add_column("Size", width=5, justify="right", style="dim")

        for i, r in enumerate(results[:10]):
            train_pf = r.get("profit_factor", 0)
            val_pf = r.get("val_pf", 0)
            strat = r.get("strategy", "?").upper()
            nq_pt = r.get("val_nq_per_trade", 0)
            nq_tot = r.get("val_nq_total", 0)

            t_style = ("bold green" if train_pf >= 1.5
                       else "yellow" if train_pf >= 1.2 else "red")
            v_style = ("bold green" if val_pf >= 1.5
                       else "yellow" if val_pf >= 1.2 else "red")
            nq_style = "bold green" if nq_pt > 0 else "red"

            table.add_row(
                str(i + 1),
                strat,
                Text(f"{train_pf:.2f}", style=t_style),
                Text(f"{val_pf:.2f}", style=v_style),
                f"{r.get('win_rate', 0):.0%}",
                f"{r.get('val_wr', 0):.0%}",
                f"{r.get('val_trades', 0):,}",
                Text(f"${nq_pt:+,.0f}", style=nq_style),
                Text(f"${nq_tot:+,.0f}", style=nq_style),
                f"{r.get('tree_size', 0)}",
            )

        console.print()
        console.print(table)

        # Print equations
        console.print()
        for i, r in enumerate(results[:10]):
            val_pf = r.get("val_pf", 0)
            strat = r.get("strategy", "?").upper()
            nq_pt = r.get("val_nq_per_trade", 0)
            pf_style = ("bold green" if val_pf >= 1.5
                        else "yellow" if val_pf >= 1.2 else "red")

            # Show bracket info
            profile = STRATEGY_PROFILES.get(r.get("strategy", "scalp"), {})
            bracket = (f"+{profile.get('nq_target_pts', '?')}pt"
                       f"/-{profile.get('nq_stop_pts', '?')}pt")

            console.print(
                f"  [dim]#{i+1}[/dim] "
                f"[bold cyan][{strat}][/bold cyan] "
                f"[dim]{bracket}[/dim] "
                f"[{pf_style}]PF={val_pf:.2f}[/{pf_style}] "
                f"[bold yellow]${nq_pt:+,.0f}/trade[/bold yellow] "
                f"[yellow]{r['expression'][:80]}[/yellow]"
            )

        console.print(
            f"\n[dim]Completed in {elapsed:.1f}s | "
            f"NQ @ ${NQ_POINT_VALUE:.0f}/pt | "
            f"Results saved to data/gp_results/[/dim]\n"
        )


def load_latest_results(strategy: str = "both") -> list[dict] | None:
    """Load most recent GP run results."""
    if not RESULTS_DIR.exists():
        return None

    if strategy == "both":
        files = sorted(RESULTS_DIR.glob("gp_run_both_*.json"), reverse=True)
        if not files:
            files = sorted(RESULTS_DIR.glob("gp_run_*.json"), reverse=True)
    else:
        files = sorted(
            RESULTS_DIR.glob(f"gp_run_{strategy}_*.json"), reverse=True
        )

    if not files:
        return None
    data = json.loads(files[0].read_text())
    return data["results"]
