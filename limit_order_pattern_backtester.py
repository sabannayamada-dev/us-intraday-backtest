#!/usr/bin/env python3
"""
Backtest limit-order depths after bottom detection.

Default mode fits a simple linear depth model:

    depth = intercept + w1*x1 + w2*x2 + w3*x3 + w4*x4

where x1..x4 are standardized daily-path features. The predicted depth is
clipped to a bounded range and evaluated against a benchmark from the actual
fill date to the row's current date.

The older per-81-pattern optimizer remains available with --model pattern.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import math
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import numpy as np
    import pandas as pd
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency. Install with: python -m pip install pandas numpy") from exc

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "local_data" / "output" / "daily_path_81_pattern_classified.csv"
DEFAULT_OUTPUT_DIR = ROOT / "local_data" / "backtest"
DEFAULT_CACHE_DIR = ROOT / "local_data" / "backtest_price_cache"

FEATURE_COLUMNS = [
    ("abs_mean", "日足特徴_変化率絶対値平均"),
    ("abs_median", "日足特徴_変化率絶対値中央値"),
    ("avg_up_return", "日足特徴_上昇日の平均上昇率"),
    ("avg_down_return", "日足特徴_下落日の平均下落率"),
    ("last30_avg_up_return", "直前30日特徴_上昇日の平均上昇率"),
    ("last30_abs_max", "直前30日特徴_変化率絶対値最大"),
    ("last30_abs_mean", "直前30日特徴_変化率絶対値平均"),
    ("last30_big_up_5pct_days", "直前30日特徴_大幅上昇5pct日数"),
    ("last30_cumulative_return", "直前30日特徴_累積騰落率"),
    ("last30_volume_ratio_max", "直前30日特徴_出来高中央値比最大"),
    ("fear_pct3y", "恐怖指数_採用過去3年パーセンタイル"),
    ("fear_pct_all", "恐怖指数_採用全期間パーセンタイル"),
    ("fear_chg20", "恐怖指数_採用20日変化率"),
    ("fear_low20", "恐怖指数_採用低位20pctフラグ"),
    ("fear_high25", "恐怖指数_採用高位25pctフラグ"),
]

LINEAR_PARAM_NAMES = ["intercept", *[f"weight_{key}" for key, _ in FEATURE_COLUMNS]]

COL = {
    "ticker": "銘柄",
    "name": "名称",
    "signal_date": "底打ち候補日",
    "current_date": "現在日",
    "current_price": "現在価格",
    "current_return": "現在まで保有リターン",
    "post_low_drawdown": "候補後下落率",
    "pattern": "日足特徴_81分類コード",
    "feature_status": "日足特徴_取得状態",
}


@dataclass(frozen=True)
class Event:
    row_index: int
    ticker: str
    name: str
    pattern: str
    features: tuple[float, ...]
    signal_date: dt.date
    current_date: dt.date
    current_price: float
    signal_price: float
    post_low_drawdown: float


@dataclass
class EventPrices:
    stock: pd.DataFrame
    benchmark: pd.DataFrame
    stock_window_dates: tuple[dt.date, ...] | None = None
    stock_window_lows: Any = None
    stock_end_close: float | None = None
    benchmark_dates: tuple[dt.date, ...] | None = None
    benchmark_closes: tuple[float, ...] | None = None
    benchmark_end_close: float | None = None


def parse_date(value: str) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_float(value: str) -> float | None:
    try:
        text = str(value or "").strip().replace(",", "")
        if not text:
            return None
        return float(text)
    except ValueError:
        return None


def safe_name(ticker: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ".-_=^" else "_" for ch in ticker)


def normalize_download_frame(frame: Any) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        return pd.DataFrame()
    if isinstance(frame.columns, pd.MultiIndex):
        expected = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        columns: list[str] = []
        for item in frame.columns:
            parts = [str(part) for part in item if str(part)]
            matched = next((part for part in parts if part in expected), "")
            columns.append(matched or (parts[-1] if parts else ""))
        frame.columns = columns
    frame = frame.reset_index()
    if "Date" not in frame.columns and "Datetime" in frame.columns:
        frame = frame.rename(columns={"Datetime": "Date"})
    keep = [name for name in ["Date", "Open", "High", "Low", "Close", "Volume"] if name in frame.columns]
    frame = frame[keep].copy()
    if "Date" not in frame.columns:
        return pd.DataFrame()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.date
    return frame.sort_values("Date").drop_duplicates("Date")


def load_prices(ticker: str, start: dt.date, end: dt.date, cache_dir: Path, sleep_seconds: float) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{safe_name(ticker)}.csv"
    if path.exists():
        try:
            cached = pd.read_csv(path, parse_dates=["Date"])
            cached["Date"] = cached["Date"].dt.date
            if {"Date", "Close"}.issubset(set(cached.columns)) and not cached.empty:
                if min(cached["Date"]) <= start and max(cached["Date"]) >= end:
                    return cached.sort_values("Date")
                if yf is None:
                    print(
                        f"prices {ticker} cache_partial "
                        f"{min(cached['Date'])}..{max(cached['Date'])}; "
                        "events outside this range will be skipped",
                        flush=True,
                    )
                    return cached.sort_values("Date")
        except Exception:
            pass
    if yf is None:
        print(
            f"prices {ticker} cache_missing {path}; events for this ticker will be skipped "
            "because yfinance is not installed",
            flush=True,
        )
        return pd.DataFrame()

    fetch_start = start - dt.timedelta(days=10)
    fetch_end = end + dt.timedelta(days=7)
    kwargs = {
        "start": fetch_start.isoformat(),
        "end": fetch_end.isoformat(),
        "auto_adjust": True,
        "progress": False,
        "threads": False,
        "raise_errors": False,
    }
    try:
        frame = yf.download(ticker, **kwargs)
    except TypeError:
        kwargs.pop("raise_errors", None)
        frame = yf.download(ticker, **kwargs)
    prices = normalize_download_frame(frame)
    if not prices.empty:
        prices.to_csv(path, index=False, encoding="utf-8")
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    return prices


def price_on_or_after(prices: pd.DataFrame, date: dt.date, column: str = "Close") -> tuple[dt.date, float] | None:
    if prices.empty or column not in prices.columns:
        return None
    subset = prices[prices["Date"] >= date]
    if subset.empty:
        return None
    row = subset.iloc[0]
    return row["Date"], float(row[column])


def price_on_or_before(prices: pd.DataFrame, date: dt.date, column: str = "Close") -> tuple[dt.date, float] | None:
    if prices.empty or column not in prices.columns:
        return None
    subset = prices[prices["Date"] <= date]
    if subset.empty:
        return None
    row = subset.iloc[-1]
    return row["Date"], float(row[column])


def price_date_range(prices: pd.DataFrame) -> tuple[dt.date, dt.date] | None:
    if prices.empty or "Date" not in prices.columns:
        return None
    return min(prices["Date"]), max(prices["Date"])


def has_price_coverage(
    prices: pd.DataFrame,
    start: dt.date,
    end: dt.date,
    *,
    start_tolerance_days: int = 10,
    end_tolerance_days: int = 10,
) -> bool:
    bounds = price_date_range(prices)
    if bounds is None:
        return False
    first_date, last_date = bounds
    return (
        first_date <= start + dt.timedelta(days=start_tolerance_days)
        and last_date >= end - dt.timedelta(days=end_tolerance_days)
    )


def first_fill(prices: pd.DataFrame, signal_date: dt.date, current_date: dt.date, limit_price: float) -> tuple[dt.date, float] | None:
    if prices.empty or "Low" not in prices.columns:
        return None
    subset = prices[(prices["Date"] >= signal_date) & (prices["Date"] <= current_date)]
    if subset.empty:
        return None
    hit = subset[pd.to_numeric(subset["Low"], errors="coerce") <= limit_price]
    if hit.empty:
        return None
    row = hit.iloc[0]
    return row["Date"], limit_price


def read_events(path: Path) -> list[Event]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    fieldnames = set(rows[0].keys()) if rows else set()
    required_columns = list(COL.values()) + [column for _, column in FEATURE_COLUMNS]
    missing_columns = [column for column in required_columns if column not in fieldnames]
    if missing_columns:
        raise SystemExit(
            "Missing required columns for limit-order backtest: "
            + ", ".join(missing_columns)
            + "\nRun run_daily_path_features_local.ps1 with -FearIndexCsv first, then rerun the backtest."
        )
    events: list[Event] = []
    for i, row in enumerate(rows, 2):
        if str(row.get(COL["feature_status"]) or "") != "ok":
            continue
        pattern = str(row.get(COL["pattern"]) or "").strip()
        if not pattern:
            continue
        features: list[float] = []
        missing_feature = False
        for _, column in FEATURE_COLUMNS:
            value = parse_float(str(row.get(column) or ""))
            if value is None:
                missing_feature = True
                break
            features.append(value)
        if missing_feature:
            continue
        signal_date = parse_date(str(row.get(COL["signal_date"]) or ""))
        current_date = parse_date(str(row.get(COL["current_date"]) or ""))
        current_price = parse_float(str(row.get(COL["current_price"]) or ""))
        current_return = parse_float(str(row.get(COL["current_return"]) or ""))
        post_low_drawdown = parse_float(str(row.get(COL["post_low_drawdown"]) or ""))
        ticker = str(row.get(COL["ticker"]) or "").strip()
        if not ticker or not signal_date or not current_date:
            continue
        if current_price is None or current_return is None or post_low_drawdown is None:
            continue
        if current_return <= -99.9:
            continue
        signal_price = current_price / (1.0 + current_return / 100.0)
        if signal_price <= 0:
            continue
        events.append(
            Event(
                row_index=i,
                ticker=ticker,
                name=str(row.get(COL["name"]) or "").strip(),
                pattern=pattern,
                features=tuple(features),
                signal_date=signal_date,
                current_date=current_date,
                current_price=current_price,
                signal_price=signal_price,
                post_low_drawdown=post_low_drawdown,
            )
        )
    return events


def prepare_price_cache(events: list[Event], benchmark: str, cache_dir: Path, sleep_seconds: float) -> dict[str, pd.DataFrame]:
    ranges: dict[str, tuple[dt.date, dt.date]] = {}
    for event in events:
        old = ranges.get(event.ticker)
        if old is None:
            ranges[event.ticker] = (event.signal_date, event.current_date)
        else:
            ranges[event.ticker] = (min(old[0], event.signal_date), max(old[1], event.current_date))
    if events:
        ranges[benchmark] = (
            min(event.signal_date for event in events),
            max(event.current_date for event in events),
        )

    prices: dict[str, pd.DataFrame] = {}
    for idx, (ticker, (start, end)) in enumerate(sorted(ranges.items()), 1):
        print(f"prices {idx}/{len(ranges)} ticker={ticker} {start}..{end}", flush=True)
        prices[ticker] = load_prices(ticker, start, end, cache_dir, sleep_seconds)
        print(f"prices {ticker} rows={len(prices[ticker])}", flush=True)
    return prices


def filter_price_covered_events(
    events: list[Event],
    event_prices: dict[int, EventPrices],
) -> tuple[list[Event], dict[str, int]]:
    kept: list[Event] = []
    skipped = {
        "stock_missing_or_out_of_range": 0,
        "benchmark_missing_or_out_of_range": 0,
    }
    for event in events:
        prices = event_prices[event.row_index]
        if not has_price_coverage(prices.stock, event.signal_date, event.current_date):
            skipped["stock_missing_or_out_of_range"] += 1
            continue
        if not has_price_coverage(prices.benchmark, event.signal_date, event.current_date):
            skipped["benchmark_missing_or_out_of_range"] += 1
            continue
        kept.append(event)
    return kept, skipped


def prepare_event_price_views(events: list[Event], event_prices: dict[int, EventPrices]) -> None:
    for event in events:
        prices = event_prices[event.row_index]
        stock_window = prices.stock[
            (prices.stock["Date"] >= event.signal_date) & (prices.stock["Date"] <= event.current_date)
        ].copy()
        if not stock_window.empty and "Low" in stock_window.columns:
            lows = pd.to_numeric(stock_window["Low"], errors="coerce")
            valid = lows.notna()
            stock_window = stock_window[valid]
            lows = lows[valid]
            prices.stock_window_dates = tuple(stock_window["Date"].tolist())
            prices.stock_window_lows = lows.to_numpy(dtype=float)

        stock_end = price_on_or_before(prices.stock, event.current_date, "Close")
        if stock_end is not None:
            prices.stock_end_close = stock_end[1]

        benchmark = prices.benchmark.copy()
        if not benchmark.empty and "Close" in benchmark.columns:
            benchmark = benchmark.sort_values("Date")
            closes = pd.to_numeric(benchmark["Close"], errors="coerce")
            valid = closes.notna()
            benchmark = benchmark[valid]
            closes = closes[valid]
            prices.benchmark_dates = tuple(benchmark["Date"].tolist())
            prices.benchmark_closes = tuple(float(value) for value in closes.tolist())

        benchmark_end = price_on_or_before(prices.benchmark, event.current_date, "Close")
        if benchmark_end is not None:
            prices.benchmark_end_close = benchmark_end[1]


def evaluate_event(event: Event, depth_pct: float, prices: EventPrices) -> dict[str, Any] | None:
    if not has_price_coverage(prices.stock, event.signal_date, event.current_date):
        return None
    if not has_price_coverage(prices.benchmark, event.signal_date, event.current_date):
        return None
    limit_price = event.signal_price * (1.0 - depth_pct / 100.0)
    if limit_price <= 0:
        return None
    # Fast rejection from known post-signal max drawdown.
    if event.post_low_drawdown > -depth_pct:
        return None
    if (
        prices.stock_window_dates is not None
        and prices.stock_window_lows is not None
        and prices.stock_end_close is not None
        and prices.benchmark_dates is not None
        and prices.benchmark_closes is not None
        and prices.benchmark_end_close is not None
    ):
        hit_indexes = np.flatnonzero(prices.stock_window_lows <= limit_price)
        if len(hit_indexes) == 0:
            return None
        fill_date = prices.stock_window_dates[int(hit_indexes[0])]
        bench_idx = bisect.bisect_left(prices.benchmark_dates, fill_date)
        if bench_idx >= len(prices.benchmark_dates):
            return None
        bench_buy_price = prices.benchmark_closes[bench_idx]
        if bench_buy_price <= 0:
            return None
        stock_return = (prices.stock_end_close / limit_price - 1.0) * 100.0
        benchmark_return = (prices.benchmark_end_close / bench_buy_price - 1.0) * 100.0
        return {
            "row_index": event.row_index,
            "ticker": event.ticker,
            "name": event.name,
            "pattern": event.pattern,
            "depth_pct": depth_pct,
            "signal_date": event.signal_date,
            "fill_date": fill_date,
            "buy_price": limit_price,
            "stock_return": stock_return,
            "benchmark_return": benchmark_return,
            "excess_return": stock_return - benchmark_return,
            "win": stock_return > benchmark_return,
        }
    fill = first_fill(prices.stock, event.signal_date, event.current_date, limit_price)
    if fill is None:
        return None
    fill_date, buy_price = fill
    stock_end = price_on_or_before(prices.stock, event.current_date, "Close")
    bench_buy = price_on_or_after(prices.benchmark, fill_date, "Close")
    bench_end = price_on_or_before(prices.benchmark, event.current_date, "Close")
    if stock_end is None or bench_buy is None or bench_end is None:
        return None
    stock_return = (stock_end[1] / buy_price - 1.0) * 100.0
    benchmark_return = (bench_end[1] / bench_buy[1] - 1.0) * 100.0
    return {
        "row_index": event.row_index,
        "ticker": event.ticker,
        "name": event.name,
        "pattern": event.pattern,
        "depth_pct": depth_pct,
        "signal_date": event.signal_date,
        "fill_date": fill_date,
        "buy_price": buy_price,
        "stock_return": stock_return,
        "benchmark_return": benchmark_return,
        "excess_return": stock_return - benchmark_return,
        "win": stock_return > benchmark_return,
    }


def evaluate_depth(events: list[Event], event_prices: dict[int, EventPrices], depth_pct: float) -> dict[str, Any]:
    trades = []
    for event in events:
        result = evaluate_event(event, depth_pct, event_prices[event.row_index])
        if result is not None:
            trades.append(result)
    n_events = len(events)
    n_trades = len(trades)
    wins = sum(1 for trade in trades if trade["win"])
    excess = [float(trade["excess_return"]) for trade in trades]
    stock_returns = [float(trade["stock_return"]) for trade in trades]
    return {
        "depth_pct": depth_pct,
        "events": n_events,
        "trades": n_trades,
        "fill_rate": n_trades / n_events if n_events else 0.0,
        "wins": wins,
        "win_rate": wins / n_trades if n_trades else 0.0,
        "avg_excess": statistics.mean(excess) if excess else 0.0,
        "median_excess": statistics.median(excess) if excess else 0.0,
        "avg_stock_return": statistics.mean(stock_returns) if stock_returns else 0.0,
        "trades_detail": trades,
    }


def score_result(result: dict[str, Any], min_trades: int, min_fill_rate: float) -> float:
    if int(result["trades"]) < min_trades or float(result["fill_rate"]) < min_fill_rate:
        return -1e9
    # Primary: beat benchmark often. Secondary: excess return and fill rate.
    return (
        float(result["win_rate"]) * 1000.0
        + max(-200.0, min(200.0, float(result["avg_excess"])))
        + float(result["fill_rate"]) * 10.0
    )


def optimize_pattern(
    pattern: str,
    events: list[Event],
    event_prices: dict[int, EventPrices],
    max_depth: float,
    min_trades: int,
    min_fill_rate: float,
    stable_rounds: int,
    min_step: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    history: list[dict[str, Any]] = []
    center = max_depth / 2.0
    step = 5.0
    best: dict[str, Any] | None = None
    stable = 0
    round_no = 0
    seen_depths: set[float] = set()

    while True:
        round_no += 1
        if best is None:
            candidates = [round(x, 4) for x in frange(0.0, max_depth, step)]
        else:
            center = float(best["depth_pct"])
            candidates = [
                round(max(0.0, min(max_depth, center + offset * step)), 4)
                for offset in range(-5, 6)
            ]
        candidates = sorted({value for value in candidates if value not in seen_depths})
        if not candidates:
            step /= 2.0
            if step < min_step:
                break
            continue

        before_depth = None if best is None else float(best["depth_pct"])
        before_score = None if best is None else float(best["score"])
        round_best = best
        for depth in candidates:
            seen_depths.add(depth)
            result = evaluate_depth(events, event_prices, depth)
            result["pattern"] = pattern
            result["round"] = round_no
            result["score"] = score_result(result, min_trades, min_fill_rate)
            result.pop("trades_detail", None)
            history.append(result)
            if round_best is None or float(result["score"]) > float(round_best["score"]):
                round_best = result
        best = round_best
        after_depth = float(best["depth_pct"])
        after_score = float(best["score"])
        depth_delta = 999.0 if before_depth is None else abs(after_depth - before_depth)
        score_delta = 999.0 if before_score is None else abs(after_score - before_score)
        print(
            f"opt {pattern} round={round_no} step={step:.4f} "
            f"best_depth={after_depth:.4f} score={after_score:.4f} "
            f"trades={best['trades']} win_rate={best['win_rate']:.3f}",
            flush=True,
        )
        if depth_delta <= min_step and score_delta <= 0.001:
            stable += 1
        else:
            stable = 0
        if stable >= stable_rounds:
            break
        step /= 2.0
        if step < min_step:
            break
    assert best is not None
    return best, history


@dataclass(frozen=True)
class FeatureScaler:
    means: tuple[float, ...]
    stdevs: tuple[float, ...]


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clipped_mean(values: list[float], low: float, high: float) -> float:
    if not values:
        return 0.0
    return statistics.mean(clip(value, low, high) for value in values)


def build_feature_scaler(events: list[Event]) -> FeatureScaler:
    means: list[float] = []
    stdevs: list[float] = []
    for idx in range(len(FEATURE_COLUMNS)):
        values = [event.features[idx] for event in events]
        mean = statistics.mean(values) if values else 0.0
        stdev = statistics.stdev(values) if len(values) >= 2 else 1.0
        if stdev <= 1e-12:
            stdev = 1.0
        means.append(mean)
        stdevs.append(stdev)
    return FeatureScaler(means=tuple(means), stdevs=tuple(stdevs))


def standardize_event_features(event: Event, scaler: FeatureScaler) -> tuple[float, ...]:
    return tuple(
        (event.features[idx] - scaler.means[idx]) / scaler.stdevs[idx]
        for idx in range(len(FEATURE_COLUMNS))
    )


def predict_linear_raw_depth(params: list[float], event: Event, scaler: FeatureScaler) -> float:
    standardized = standardize_event_features(event, scaler)
    return params[0] + sum(params[idx + 1] * standardized[idx] for idx in range(len(standardized)))


def predict_linear_depth(params: list[float], event: Event, scaler: FeatureScaler, max_depth: float) -> float:
    return clip(predict_linear_raw_depth(params, event, scaler), 0.0, max_depth)


def evaluate_linear_model(
    events: list[Event],
    event_prices: dict[int, EventPrices],
    params: list[float],
    scaler: FeatureScaler,
    max_depth: float,
    skip_nonpositive_raw_depth: bool = False,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    predicted_depths: list[float] = []
    for event in events:
        raw_depth = predict_linear_raw_depth(params, event, scaler)
        depth = clip(raw_depth, 0.0, max_depth)
        predicted_depths.append(depth)
        base_row: dict[str, Any] = {
            "row_index": event.row_index,
            "ticker": event.ticker,
            "name": event.name,
            "pattern": event.pattern,
            "signal_date": event.signal_date,
            "current_date": event.current_date,
            "signal_price": event.signal_price,
            "raw_predicted_depth_pct": raw_depth,
            "predicted_depth_pct": depth,
            "post_low_drawdown": event.post_low_drawdown,
        }
        for idx, (key, _) in enumerate(FEATURE_COLUMNS):
            base_row[f"feature_{key}"] = event.features[idx]
            base_row[f"z_{key}"] = standardize_event_features(event, scaler)[idx]
        if skip_nonpositive_raw_depth and raw_depth <= 0.0:
            base_row["filled"] = False
            base_row["skip_reason"] = "nonpositive_raw_depth"
            event_rows.append(base_row)
            continue
        result = evaluate_event(event, depth, event_prices[event.row_index])
        if result is None:
            base_row["filled"] = False
            event_rows.append(base_row)
            continue
        result["predicted_depth_pct"] = depth
        result["model"] = "linear"
        trades.append(result)
        event_rows.append({**base_row, **result, "filled": True})

    n_events = len(events)
    n_trades = len(trades)
    wins = sum(1 for trade in trades if trade["win"])
    excess = [float(trade["excess_return"]) for trade in trades]
    stock_returns = [float(trade["stock_return"]) for trade in trades]
    return {
        "events": n_events,
        "trades": n_trades,
        "fill_rate": n_trades / n_events if n_events else 0.0,
        "wins": wins,
        "win_rate": wins / n_trades if n_trades else 0.0,
        "bayes_win_rate": (wins + 3.0) / (n_trades + 6.0) if n_trades else 0.5,
        "avg_excess": statistics.mean(excess) if excess else 0.0,
        "median_excess": statistics.median(excess) if excess else 0.0,
        "winsor_avg_excess": clipped_mean(excess, -200.0, 200.0),
        "avg_stock_return": statistics.mean(stock_returns) if stock_returns else 0.0,
        "avg_predicted_depth": statistics.mean(predicted_depths) if predicted_depths else 0.0,
        "min_predicted_depth": min(predicted_depths) if predicted_depths else 0.0,
        "max_predicted_depth": max(predicted_depths) if predicted_depths else 0.0,
        "trades_detail": trades,
        "events_detail": event_rows,
    }


def score_linear_model(
    result: dict[str, Any],
    params: list[float],
    min_trades: int,
    min_fill_rate: float,
    regularization: float,
) -> float:
    if int(result["trades"]) < min_trades or float(result["fill_rate"]) < min_fill_rate:
        return -1e9
    weight_l2 = math.sqrt(sum(value * value for value in params[1:]))
    fill_shortfall = max(0.0, min_fill_rate - float(result["fill_rate"]))
    bayes_win = float(result["bayes_win_rate"])
    median_excess = float(result["median_excess"])
    return (
        bayes_win * 2000.0
        + float(result["winsor_avg_excess"]) * 2.0
        + clip(median_excess, -200.0, 200.0) * 15.0
        + float(result["fill_rate"]) * 25.0
        - max(0.0, 0.50 - bayes_win) * 4000.0
        - max(0.0, -median_excess) * 20.0
        - float(result["avg_predicted_depth"]) * 0.15
        - weight_l2 * regularization
        - fill_shortfall * 1000.0
    )


def bound_linear_params(params: list[float], max_depth: float, max_weight: float) -> list[float]:
    bounded = [clip(params[0], 0.0, max_depth)]
    bounded.extend(clip(value, -max_weight, max_weight) for value in params[1:])
    return bounded


def optimize_linear_model(
    events: list[Event],
    event_prices: dict[int, EventPrices],
    max_depth: float,
    min_trades: int,
    min_fill_rate: float,
    stable_rounds: int,
    min_step: float,
    max_weight: float,
    random_trials: int,
    regularization: float,
    seed: int,
) -> tuple[list[float], dict[str, Any], list[dict[str, Any]], FeatureScaler]:
    scaler = build_feature_scaler(events)
    rng = random.Random(seed)
    trial_rows: list[dict[str, Any]] = []

    best_params: list[float] | None = None
    best_result: dict[str, Any] | None = None
    best_score = -1e18
    trial_no = 0

    initial_intercepts = sorted({0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, max_depth / 2.0, max_depth})
    for intercept in initial_intercepts:
        params = [clip(intercept, 0.0, max_depth)] + [0.0] * len(FEATURE_COLUMNS)
        result = evaluate_linear_model(
            events,
            event_prices,
            params,
            scaler,
            max_depth,
            skip_nonpositive_raw_depth=True,
        )
        score = score_linear_model(result, params, min_trades, min_fill_rate, regularization)
        trial_no += 1
        trial_rows.append(linear_trial_row(trial_no, 0, "initial", params, score, result))
        if score > best_score:
            best_params, best_result, best_score = params, result, score

    seeded_params = [
        # Best 4-feature model found before adding last30 features. This prevents
        # the wider 10D search from getting trapped at the all-zero model.
        [0.0, -2.213415877344657, -1.6192246384835276, 2.88867937073047, 3.341906693686016],
        [0.0, -2.213415877344657, -1.6192246384835276, 2.88867937073047, 3.341906693686016, 0.75, 0.75, 0.75, 0.5, 0.75, 0.5],
        [0.0, 0.0, 0.0, 0.0, 0.0, 1.5, 1.5, 1.5, 1.0, 1.0, 0.75],
    ]
    for raw_params in seeded_params:
        params = raw_params + [0.0] * (len(FEATURE_COLUMNS) + 1 - len(raw_params))
        params = bound_linear_params(params[: len(FEATURE_COLUMNS) + 1], max_depth, max_weight)
        result = evaluate_linear_model(
            events,
            event_prices,
            params,
            scaler,
            max_depth,
            skip_nonpositive_raw_depth=True,
        )
        score = score_linear_model(result, params, min_trades, min_fill_rate, regularization)
        trial_no += 1
        trial_rows.append(linear_trial_row(trial_no, 0, "seed", params, score, result))
        if score > best_score:
            best_params, best_result, best_score = params, result, score

    assert best_params is not None and best_result is not None
    step = max(2.0, min_step)
    round_no = 0
    stable = 0
    seen: set[tuple[float, ...]] = set()

    def try_params(source: str, params: list[float]) -> bool:
        nonlocal best_params, best_result, best_score, trial_no
        params = bound_linear_params(params, max_depth, max_weight)
        key = tuple(round(value, 6) for value in params)
        if key in seen:
            return False
        seen.add(key)
        result = evaluate_linear_model(
            events,
            event_prices,
            params,
            scaler,
            max_depth,
            skip_nonpositive_raw_depth=True,
        )
        score = score_linear_model(result, params, min_trades, min_fill_rate, regularization)
        trial_no += 1
        trial_rows.append(linear_trial_row(trial_no, round_no, source, params, score, result))
        if score > best_score + 1e-9:
            best_params, best_result, best_score = params, result, score
            return True
        return False

    while step >= min_step:
        round_no += 1
        before_score = best_score
        improvements = 0

        # Coordinate descent: tune one coefficient at a time with small moves.
        # This is better for the current non-smooth objective than large random
        # jumps, because fills/wins change only when a depth crosses a price path.
        for idx in range(len(best_params)):
            coordinate_improved = True
            while coordinate_improved:
                coordinate_improved = False
                local_best_params = list(best_params)
                local_best_score = best_score
                local_best_result = best_result
                for offset in (-3, -2, -1, 1, 2, 3):
                    candidate = list(best_params)
                    candidate[idx] += offset * step
                    params = bound_linear_params(candidate, max_depth, max_weight)
                    key = tuple(round(value, 6) for value in params)
                    if key in seen:
                        continue
                    seen.add(key)
                    result = evaluate_linear_model(
                        events,
                        event_prices,
                        params,
                        scaler,
                        max_depth,
                        skip_nonpositive_raw_depth=True,
                    )
                    score = score_linear_model(result, params, min_trades, min_fill_rate, regularization)
                    trial_no += 1
                    trial_rows.append(linear_trial_row(trial_no, round_no, f"coord_{idx}_{offset:+d}", params, score, result))
                    if score > local_best_score + 1e-9:
                        local_best_params = params
                        local_best_result = result
                        local_best_score = score
                if local_best_score > best_score + 1e-9:
                    best_params = local_best_params
                    best_result = local_best_result
                    best_score = local_best_score
                    coordinate_improved = True
                    improvements += 1

        # Small joint perturbations can catch interactions after coordinate
        # descent has found a reasonable local neighborhood.
        for _ in range(random_trials):
            candidate = [best_params[0] + rng.uniform(-step, step)]
            candidate.extend(
                best_params[idx] + rng.uniform(-step, step)
                for idx in range(1, len(best_params))
            )
            if try_params("random_small", candidate):
                improvements += 1

        print(
            f"linear round={round_no} step={step:.4f} score={best_score:.4f} "
            f"improvements={improvements} intercept={best_params[0]:.4f} "
            f"weights={','.join(f'{v:.4f}' for v in best_params[1:])} "
            f"trades={best_result['trades']} fill={best_result['fill_rate']:.2%} "
            f"bayes_win={best_result['bayes_win_rate']:.2%}",
            flush=True,
        )
        if best_score <= before_score + 0.001:
            stable += 1
        else:
            stable = 0
        if stable >= stable_rounds and step <= min_step:
            break
        step /= 2.0

    return best_params, best_result, trial_rows, scaler


def linear_trial_row(
    trial_no: int,
    round_no: int,
    source: str,
    params: list[float],
    score: float,
    result: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trial": trial_no,
        "round": round_no,
        "source": source,
        "score": score,
    }
    for name, value in zip(LINEAR_PARAM_NAMES, params):
        row[name] = value
    for key in [
        "events",
        "trades",
        "fill_rate",
        "wins",
        "win_rate",
        "bayes_win_rate",
        "avg_excess",
        "median_excess",
        "winsor_avg_excess",
        "avg_stock_return",
        "avg_predicted_depth",
        "min_predicted_depth",
        "max_predicted_depth",
    ]:
        row[key] = result[key]
    return row


def target_drawdown_depth(event: Event, max_depth: float) -> float:
    return clip(-float(event.post_low_drawdown), 0.0, max_depth)


def fit_drawdown_linear_model(
    events: list[Event],
    scaler: FeatureScaler,
    max_depth: float,
    ridge_alpha: float,
) -> list[float]:
    x_rows = []
    y_values = []
    for event in events:
        x_rows.append([1.0, *standardize_event_features(event, scaler)])
        y_values.append(target_drawdown_depth(event, max_depth))
    x = np.asarray(x_rows, dtype=float)
    y = np.asarray(y_values, dtype=float)
    penalty = np.eye(x.shape[1], dtype=float) * ridge_alpha
    penalty[0, 0] = 0.0
    try:
        beta = np.linalg.solve(x.T @ x + penalty, x.T @ y)
    except np.linalg.LinAlgError:
        beta = np.linalg.lstsq(x.T @ x + penalty, x.T @ y, rcond=None)[0]
    return bound_linear_params([float(value) for value in beta], max_depth, max_weight=max_depth)


def spearman_lists(left: list[float], right: list[float]) -> float:
    if len(left) < 2 or len(right) < 2:
        return 0.0
    left_rank = pd.Series(left).rank(method="average")
    right_rank = pd.Series(right).rank(method="average")
    corr = left_rank.corr(right_rank)
    return float(corr) if corr == corr else 0.0


def evaluate_drawdown_prediction(
    events: list[Event],
    params: list[float],
    scaler: FeatureScaler,
    max_depth: float,
) -> dict[str, Any]:
    targets: list[float] = []
    predictions: list[float] = []
    errors: list[float] = []
    rows: list[dict[str, Any]] = []
    for event in events:
        target = target_drawdown_depth(event, max_depth)
        prediction = predict_linear_depth(params, event, scaler, max_depth)
        error = prediction - target
        targets.append(target)
        predictions.append(prediction)
        errors.append(error)
        row: dict[str, Any] = {
            "row_index": event.row_index,
            "ticker": event.ticker,
            "name": event.name,
            "pattern": event.pattern,
            "signal_date": event.signal_date,
            "post_low_drawdown": event.post_low_drawdown,
            "target_drawdown_depth_pct": target,
            "predicted_depth_pct": prediction,
            "prediction_error_pct": error,
            "abs_prediction_error_pct": abs(error),
        }
        for idx, (key, _) in enumerate(FEATURE_COLUMNS):
            row[f"feature_{key}"] = event.features[idx]
            row[f"z_{key}"] = standardize_event_features(event, scaler)[idx]
        rows.append(row)

    mae = statistics.mean(abs(value) for value in errors) if errors else 0.0
    rmse = math.sqrt(statistics.mean(value * value for value in errors)) if errors else 0.0
    median_abs = statistics.median(abs(value) for value in errors) if errors else 0.0
    pearson = float(np.corrcoef(predictions, targets)[0, 1]) if len(predictions) >= 2 else 0.0
    if pearson != pearson:
        pearson = 0.0
    return {
        "target_avg_depth": statistics.mean(targets) if targets else 0.0,
        "target_median_depth": statistics.median(targets) if targets else 0.0,
        "prediction_avg_depth": statistics.mean(predictions) if predictions else 0.0,
        "prediction_median_depth": statistics.median(predictions) if predictions else 0.0,
        "prediction_min_depth": min(predictions) if predictions else 0.0,
        "prediction_max_depth": max(predictions) if predictions else 0.0,
        "drawdown_mae": mae,
        "drawdown_rmse": rmse,
        "drawdown_median_abs_error": median_abs,
        "drawdown_pearson": pearson,
        "drawdown_spearman": spearman_lists(predictions, targets),
        "prediction_detail": rows,
    }


def split_train_eval_events(events: list[Event], args: argparse.Namespace) -> tuple[list[Event], list[Event]]:
    train_end = parse_date(args.train_end_date) if args.train_end_date else None
    test_start = parse_date(args.test_start_date) if args.test_start_date else None
    train_events = [event for event in events if train_end is None or event.signal_date <= train_end]
    eval_events = [event for event in events if test_start is None or event.signal_date >= test_start]
    if not train_events:
        raise SystemExit(f"No training events matched --train-end-date={args.train_end_date!r}")
    if not eval_events:
        raise SystemExit(f"No evaluation events matched --test-start-date={args.test_start_date!r}")
    return train_events, eval_events


def write_split_linear_outputs(
    output_dir: Path,
    train_result: dict[str, Any],
    eval_result: dict[str, Any],
    eval_prediction_result: dict[str, Any] | None = None,
) -> None:
    write_dict_csv(output_dir / "linear_limit_events_train.csv", train_result["events_detail"])
    write_dict_csv(output_dir / "linear_limit_trades_train.csv", train_result["trades_detail"])
    write_dict_csv(output_dir / "linear_limit_events_test.csv", eval_result["events_detail"])
    write_dict_csv(output_dir / "linear_limit_trades_test.csv", eval_result["trades_detail"])
    # Keep the legacy filenames pointed at the evaluation side so existing
    # inspection commands continue to show the out-of-sample result.
    write_dict_csv(output_dir / "linear_limit_events.csv", eval_result["events_detail"])
    write_dict_csv(output_dir / "linear_limit_trades.csv", eval_result["trades_detail"])
    if eval_prediction_result is not None:
        write_dict_csv(
            output_dir / "linear_drawdown_predictions_test.csv",
            eval_prediction_result["prediction_detail"],
        )
        write_dict_csv(
            output_dir / "linear_drawdown_predictions.csv",
            eval_prediction_result["prediction_detail"],
        )


def append_result_block(report: list[str], title: str, result: dict[str, Any]) -> None:
    report.extend(
        [
            "",
            title,
            f"events={result['events']}",
            f"trades={result['trades']}",
            f"fill_rate={result['fill_rate']:.6f}",
            f"win_rate={result['win_rate']:.6f}",
            f"bayes_win_rate={result['bayes_win_rate']:.6f}",
            f"avg_excess={result['avg_excess']:.6f}",
            f"median_excess={result['median_excess']:.6f}",
            f"winsor_avg_excess={result['winsor_avg_excess']:.6f}",
            f"avg_stock_return={result['avg_stock_return']:.6f}",
            f"avg_predicted_depth={result['avg_predicted_depth']:.6f}",
            f"min_predicted_depth={result['min_predicted_depth']:.6f}",
            f"max_predicted_depth={result['max_predicted_depth']:.6f}",
        ]
    )


def run_linear_drawdown_backtest(
    events: list[Event],
    event_prices: dict[int, EventPrices],
    input_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> None:
    train_events, eval_events = split_train_eval_events(events, args)
    scaler = build_feature_scaler(train_events)
    params = fit_drawdown_linear_model(train_events, scaler, args.max_depth, args.drawdown_ridge_alpha)
    train_prediction_result = evaluate_drawdown_prediction(train_events, params, scaler, args.max_depth)
    eval_prediction_result = evaluate_drawdown_prediction(eval_events, params, scaler, args.max_depth)
    train_result = evaluate_linear_model(
        train_events,
        event_prices,
        params,
        scaler,
        args.max_depth,
    )
    final_result = evaluate_linear_model(
        eval_events,
        event_prices,
        params,
        scaler,
        args.max_depth,
    )

    # Keep a trade-oriented score for side-by-side comparison with the return objective.
    trade_score = score_linear_model(
        final_result,
        params,
        args.min_trades,
        args.min_fill_rate,
        args.linear_regularization,
    )
    prediction_score = -float(eval_prediction_result["drawdown_rmse"])

    model_row: dict[str, Any] = {
        "model": "linear",
        "linear_objective": "drawdown",
        "train_end_date": args.train_end_date,
        "test_start_date": args.test_start_date,
        "train_events": len(train_events),
        "test_events": len(eval_events),
        "prediction_score": prediction_score,
        "trade_score": trade_score,
        "max_depth": args.max_depth,
        "drawdown_ridge_alpha": args.drawdown_ridge_alpha,
    }
    for name, value in zip(LINEAR_PARAM_NAMES, params):
        model_row[name] = value
    for idx, (key, _) in enumerate(FEATURE_COLUMNS):
        model_row[f"mean_{key}"] = scaler.means[idx]
        model_row[f"stdev_{key}"] = scaler.stdevs[idx]
    for key, value in eval_prediction_result.items():
        if key != "prediction_detail":
            model_row[key] = value
    for key, value in final_result.items():
        if key not in {"trades_detail", "events_detail"}:
            model_row[key] = value

    trial_row = dict(model_row)
    trial_row["trial"] = 1
    trial_row["round"] = 0
    trial_row["source"] = "ridge_drawdown_fit"

    write_dict_csv(output_dir / "linear_limit_model.csv", [model_row])
    write_dict_csv(output_dir / "linear_limit_trials.csv", [trial_row])
    write_split_linear_outputs(output_dir, train_result, final_result, eval_prediction_result)
    write_dict_csv(output_dir / "linear_drawdown_predictions_train.csv", train_prediction_result["prediction_detail"])
    write_dict_csv(output_dir / "linear_drawdown_model.csv", [model_row])
    write_dict_csv(output_dir / "linear_drawdown_trials.csv", [trial_row])
    write_dict_csv(output_dir / "linear_drawdown_events_train.csv", train_result["events_detail"])
    write_dict_csv(output_dir / "linear_drawdown_trades_train.csv", train_result["trades_detail"])
    write_dict_csv(output_dir / "linear_drawdown_events_test.csv", final_result["events_detail"])
    write_dict_csv(output_dir / "linear_drawdown_trades_test.csv", final_result["trades_detail"])

    report = [
        "Linear limit-order backtest",
        "objective=drawdown",
        f"input={input_path}",
        f"benchmark={args.benchmark}",
        f"events={len(events)}",
        f"train_end_date={args.train_end_date or 'none'}",
        f"test_start_date={args.test_start_date or 'none'}",
        f"train_events={len(train_events)}",
        f"test_events={len(eval_events)}",
        f"max_depth={args.max_depth:.2f}",
        f"drawdown_ridge_alpha={args.drawdown_ridge_alpha:.6f}",
        f"prediction_score={prediction_score:.6f}",
        f"trade_score={trade_score:.6f}",
        "",
        "Drawdown prediction on train:",
        f"target_avg_depth={train_prediction_result['target_avg_depth']:.6f}",
        f"target_median_depth={train_prediction_result['target_median_depth']:.6f}",
        f"prediction_avg_depth={train_prediction_result['prediction_avg_depth']:.6f}",
        f"drawdown_mae={train_prediction_result['drawdown_mae']:.6f}",
        f"drawdown_rmse={train_prediction_result['drawdown_rmse']:.6f}",
        f"drawdown_median_abs_error={train_prediction_result['drawdown_median_abs_error']:.6f}",
        f"drawdown_pearson={train_prediction_result['drawdown_pearson']:.6f}",
        f"drawdown_spearman={train_prediction_result['drawdown_spearman']:.6f}",
        "",
        "Drawdown prediction on test:",
        f"target_avg_depth={eval_prediction_result['target_avg_depth']:.6f}",
        f"target_median_depth={eval_prediction_result['target_median_depth']:.6f}",
        f"prediction_avg_depth={eval_prediction_result['prediction_avg_depth']:.6f}",
        f"prediction_median_depth={eval_prediction_result['prediction_median_depth']:.6f}",
        f"prediction_min_depth={eval_prediction_result['prediction_min_depth']:.6f}",
        f"prediction_max_depth={eval_prediction_result['prediction_max_depth']:.6f}",
        f"drawdown_mae={eval_prediction_result['drawdown_mae']:.6f}",
        f"drawdown_rmse={eval_prediction_result['drawdown_rmse']:.6f}",
        f"drawdown_median_abs_error={eval_prediction_result['drawdown_median_abs_error']:.6f}",
        f"drawdown_pearson={eval_prediction_result['drawdown_pearson']:.6f}",
        f"drawdown_spearman={eval_prediction_result['drawdown_spearman']:.6f}",
    ]
    append_result_block(report, "Trade evaluation on train:", train_result)
    append_result_block(report, "Trade evaluation on test:", final_result)
    report.extend(["", "Model coefficients:", f"- intercept: {params[0]:.6f}"])
    for idx, (key, label) in enumerate(FEATURE_COLUMNS):
        report.append(
            f"- {key} ({label}): weight={params[idx + 1]:.6f} "
            f"mean={scaler.means[idx]:.6f} stdev={scaler.stdevs[idx]:.6f}"
        )
    text = "\n".join(report) + "\n"
    (output_dir / "linear_limit_report.txt").write_text(text, encoding="utf-8")
    (output_dir / "linear_drawdown_report.txt").write_text(text, encoding="utf-8")
    print(text)


def run_linear_backtest(
    events: list[Event],
    event_prices: dict[int, EventPrices],
    input_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> None:
    if args.linear_objective == "drawdown":
        run_linear_drawdown_backtest(events, event_prices, input_path, output_dir, args)
        return

    train_events, eval_events = split_train_eval_events(events, args)
    params, result, trial_rows, scaler = optimize_linear_model(
        train_events,
        event_prices,
        args.max_depth,
        args.min_trades,
        args.min_fill_rate,
        args.stable_rounds,
        args.min_step,
        args.linear_max_weight,
        args.linear_random_trials,
        args.linear_regularization,
        args.seed,
    )
    train_result = evaluate_linear_model(
        train_events,
        event_prices,
        params,
        scaler,
        args.max_depth,
        skip_nonpositive_raw_depth=True,
    )
    final_result = evaluate_linear_model(
        eval_events,
        event_prices,
        params,
        scaler,
        args.max_depth,
        skip_nonpositive_raw_depth=True,
    )
    score = score_linear_model(final_result, params, args.min_trades, args.min_fill_rate, args.linear_regularization)

    model_row: dict[str, Any] = {
        "model": "linear",
        "linear_objective": "return",
        "skip_nonpositive_raw_depth": True,
        "train_end_date": args.train_end_date,
        "test_start_date": args.test_start_date,
        "train_events": len(train_events),
        "test_events": len(eval_events),
        "score": score,
        "max_depth": args.max_depth,
    }
    for name, value in zip(LINEAR_PARAM_NAMES, params):
        model_row[name] = value
    for idx, (key, _) in enumerate(FEATURE_COLUMNS):
        model_row[f"mean_{key}"] = scaler.means[idx]
        model_row[f"stdev_{key}"] = scaler.stdevs[idx]
    for key, value in final_result.items():
        if key not in {"trades_detail", "events_detail"}:
            model_row[key] = value

    write_dict_csv(output_dir / "linear_limit_model.csv", [model_row])
    write_dict_csv(output_dir / "linear_limit_trials.csv", trial_rows)
    write_split_linear_outputs(output_dir, train_result, final_result)
    write_dict_csv(output_dir / "linear_return_model.csv", [model_row])
    write_dict_csv(output_dir / "linear_return_trials.csv", trial_rows)
    write_dict_csv(output_dir / "linear_return_events_train.csv", train_result["events_detail"])
    write_dict_csv(output_dir / "linear_return_trades_train.csv", train_result["trades_detail"])
    write_dict_csv(output_dir / "linear_return_events_test.csv", final_result["events_detail"])
    write_dict_csv(output_dir / "linear_return_trades_test.csv", final_result["trades_detail"])

    report = [
        "Linear limit-order backtest",
        f"input={input_path}",
        f"benchmark={args.benchmark}",
        f"events={len(events)}",
        f"train_end_date={args.train_end_date or 'none'}",
        f"test_start_date={args.test_start_date or 'none'}",
        f"train_events={len(train_events)}",
        f"test_events={len(eval_events)}",
        "skip_nonpositive_raw_depth=true",
        f"max_depth={args.max_depth:.2f}",
        f"score={score:.6f}",
    ]
    append_result_block(report, "Trade evaluation on train:", train_result)
    append_result_block(report, "Trade evaluation on test:", final_result)
    report.extend(["", "Model coefficients:", f"- intercept: {params[0]:.6f}"])
    for idx, (key, label) in enumerate(FEATURE_COLUMNS):
        report.append(
            f"- {key} ({label}): weight={params[idx + 1]:.6f} "
            f"mean={scaler.means[idx]:.6f} stdev={scaler.stdevs[idx]:.6f}"
        )
    text = "\n".join(report) + "\n"
    (output_dir / "linear_limit_report.txt").write_text(text, encoding="utf-8")
    (output_dir / "linear_return_report.txt").write_text(text, encoding="utf-8")
    print(text)


def run_pattern_backtest(
    events: list[Event],
    event_prices: dict[int, EventPrices],
    input_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> None:
    by_pattern: dict[str, list[Event]] = {}
    for event in events:
        by_pattern.setdefault(event.pattern, []).append(event)

    best_rows: list[dict[str, Any]] = []
    trial_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    for pattern, pattern_events in sorted(by_pattern.items()):
        if len(pattern_events) < args.min_trades:
            continue
        best, history = optimize_pattern(
            pattern,
            pattern_events,
            event_prices,
            args.max_depth,
            args.min_trades,
            args.min_fill_rate,
            args.stable_rounds,
            args.min_step,
        )
        best_rows.append(best)
        trial_rows.extend(history)
        final_eval = evaluate_depth(pattern_events, event_prices, float(best["depth_pct"]))
        for trade in final_eval["trades_detail"]:
            trade_rows.append(trade)

    best_rows = sorted(best_rows, key=lambda row: (float(row["score"]), int(row["trades"])), reverse=True)
    write_dict_csv(output_dir / "pattern_limit_best.csv", best_rows)
    write_dict_csv(output_dir / "pattern_limit_trials.csv", trial_rows)
    write_dict_csv(output_dir / "pattern_limit_trades.csv", trade_rows)

    all_wins = sum(1 for row in trade_rows if row["win"])
    all_trades = len(trade_rows)
    all_excess = [float(row["excess_return"]) for row in trade_rows]
    report = [
        "Pattern limit-order backtest",
        f"input={input_path}",
        f"benchmark={args.benchmark}",
        f"events={len(events)}",
        f"optimized_patterns={len(best_rows)}",
        f"total_trades={all_trades}",
        f"overall_win_rate={all_wins / all_trades if all_trades else 0:.6f}",
        f"overall_avg_excess={statistics.mean(all_excess) if all_excess else 0:.6f}",
        "",
        "Top pattern settings:",
    ]
    for row in best_rows[:20]:
        report.append(
            f"- {row['pattern']}: depth={float(row['depth_pct']):.2f}% "
            f"events={row['events']} trades={row['trades']} fill={float(row['fill_rate']):.2%} "
            f"win={float(row['win_rate']):.2%} avg_excess={float(row['avg_excess']):.2f}"
        )
    text = "\n".join(report) + "\n"
    (output_dir / "pattern_limit_report.txt").write_text(text, encoding="utf-8")
    print(text)


def frange(start: float, stop: float, step: float) -> list[float]:
    values = []
    current = start
    while current <= stop + 1e-9:
        values.append(current)
        current += step
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--benchmark", default="ACWI")
    parser.add_argument("--model", choices=["linear", "pattern"], default="linear")
    parser.add_argument("--linear-objective", choices=["drawdown", "return"], default="drawdown")
    parser.add_argument("--max-depth", type=float, default=50.0)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-fill-rate", type=float, default=0.20)
    parser.add_argument("--stable-rounds", type=int, default=3)
    parser.add_argument("--min-step", type=float, default=0.25)
    parser.add_argument("--sleep-seconds", type=float, default=0.1)
    parser.add_argument("--linear-max-weight", type=float, default=15.0)
    parser.add_argument("--linear-random-trials", type=int, default=24)
    parser.add_argument("--linear-regularization", type=float, default=2.0)
    parser.add_argument("--drawdown-ridge-alpha", type=float, default=20.0)
    parser.add_argument("--train-end-date", default="")
    parser.add_argument("--test-start-date", default="")
    parser.add_argument("--seed", type=int, default=20260722)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    cache_dir = Path(args.cache_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    events = read_events(input_path)
    print(f"events={len(events)} patterns={len(set(event.pattern for event in events))}", flush=True)
    prices = prepare_price_cache(events, args.benchmark, cache_dir, args.sleep_seconds)
    benchmark_prices = prices.get(args.benchmark, pd.DataFrame())
    event_prices = {
        event.row_index: EventPrices(stock=prices.get(event.ticker, pd.DataFrame()), benchmark=benchmark_prices)
        for event in events
    }
    events, skipped_by_coverage = filter_price_covered_events(events, event_prices)
    print(
        "price-covered "
        f"events={len(events)} skipped={skipped_by_coverage}",
        flush=True,
    )
    prepare_event_price_views(events, event_prices)

    if args.model == "linear":
        run_linear_backtest(events, event_prices, input_path, output_dir, args)
    else:
        run_pattern_backtest(events, event_prices, input_path, output_dir, args)
    return 0


def write_dict_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: serialize(value) for key, value in row.items()})


def serialize(value: Any) -> Any:
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


if __name__ == "__main__":
    raise SystemExit(main())
