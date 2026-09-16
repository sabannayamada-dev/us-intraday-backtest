#!/usr/bin/env python3
"""
Add daily price path features from the pre-signal peak date to the bottom-signal date.

Input CSV is expected to contain these Japanese columns:
  - 銘柄
  - 底打ち候補日
  - 判定前最高値日

Data source: yfinance.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import sys
import time
import traceback
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - runtime dependency guard
    raise SystemExit(
        "Missing dependency. Install with: python -m pip install pandas"
    ) from exc

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - optional when all data is cached
    yf = None


SOURCE_TICKER_COL = "銘柄"
SOURCE_SIGNAL_DATE_COL = "底打ち候補日"
SOURCE_PEAK_DATE_COL = "判定前最高値日"

FEATURE_COLUMNS = [
    "日足特徴_取得状態",
    "日足特徴_開始日",
    "日足特徴_終了日",
    "日足特徴_営業日数",
    "日足特徴_変化率絶対値平均",
    "日足特徴_変化率絶対値最小",
    "日足特徴_変化率絶対値中央値",
    "日足特徴_変化率絶対値最大",
    "日足特徴_下落日数",
    "日足特徴_上昇日数",
    "日足特徴_横ばい日数",
    "日足特徴_下落日率",
    "日足特徴_累積変化率",
    "日足特徴_平均変化率",
    "日足特徴_変化率標準偏差",
    "日足特徴_年率換算ボラ",
    "日足特徴_下落日の平均下落率",
    "日足特徴_上昇日の平均上昇率",
    "日足特徴_下落率合計",
    "日足特徴_上昇率合計",
    "日足特徴_最大連続下落日数",
    "日足特徴_最大連続上昇日数",
    "日足特徴_符号反転回数",
    "日足特徴_大幅上昇3pct日数",
    "日足特徴_大幅下落3pct日数",
    "日足特徴_大幅上昇5pct日数",
    "日足特徴_大幅下落5pct日数",
    "日足特徴_大幅上昇10pct日数",
    "日足特徴_大幅下落10pct日数",
    "日足特徴_経路効率",
    "日足特徴_荒さ指数",
    "日足特徴_出来高中央値比最大",
    "日足特徴_出来高急増日数",
    "日足特徴_安値更新日数",
    "日足特徴_終値位置",
    "日足特徴_エラー",
]

LAST30_FEATURE_COLUMNS = [
    "直前30日特徴_取得状態",
    "直前30日特徴_開始日",
    "直前30日特徴_終了日",
    "直前30日特徴_営業日数",
    "直前30日特徴_累積騰落率",
    "直前30日特徴_変化率絶対値平均",
    "直前30日特徴_変化率絶対値中央値",
    "直前30日特徴_変化率絶対値最大",
    "直前30日特徴_下落日数",
    "直前30日特徴_上昇日数",
    "直前30日特徴_横ばい日数",
    "直前30日特徴_下落日率",
    "直前30日特徴_上昇日の平均上昇率",
    "直前30日特徴_下落日の平均下落率",
    "直前30日特徴_最大連続下落日数",
    "直前30日特徴_最大連続上昇日数",
    "直前30日特徴_符号反転回数",
    "直前30日特徴_大幅上昇3pct日数",
    "直前30日特徴_大幅下落3pct日数",
    "直前30日特徴_大幅上昇5pct日数",
    "直前30日特徴_大幅下落5pct日数",
    "直前30日特徴_出来高中央値比最大",
    "直前30日特徴_出来高急増日数",
    "直前30日特徴_安値更新日数",
    "直前30日特徴_終値位置",
    "直前30日特徴_エラー",
]

FEAR_INDEX_METRICS = [
    "取得状態",
    "参照日",
    "底検知日差",
    "当日値",
    "5日変化率",
    "20日変化率",
    "過去1年パーセンタイル",
    "過去3年パーセンタイル",
    "全期間パーセンタイル",
    "低位20pctフラグ",
    "高位25pctフラグ",
    "エラー",
]

ADOPTED_FEAR_COLUMNS = [
    "恐怖指数_採用対象",
    "恐怖指数_採用名",
    *[f"恐怖指数_採用{metric}" for metric in FEAR_INDEX_METRICS],
]

ALL_FEATURE_COLUMNS = FEATURE_COLUMNS + LAST30_FEATURE_COLUMNS


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


def fmt_num(value: float | int | None, digits: int = 6) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number) or math.isinf(number):
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def pct_change_from_positions(values: pd.Series, current_pos: int, lookback: int) -> str:
    prev_pos = current_pos - lookback
    if prev_pos < 0:
        return ""
    current = float(values.iloc[current_pos])
    previous = float(values.iloc[prev_pos])
    if previous == 0:
        return ""
    return fmt_num((current / previous - 1.0) * 100.0)


def percentile_rank(values: pd.Series, current_value: float) -> str:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return ""
    rank = float((clean <= current_value).sum()) / float(len(clean)) * 100.0
    return fmt_num(rank)


def parse_fear_index_spec(spec: str) -> tuple[str, str]:
    text = str(spec or "").strip()
    if not text:
        raise ValueError("empty fear index spec")
    if "=" in text:
        name, path_text = text.split("=", 1)
        name = name.strip()
        path_text = path_text.strip()
    else:
        path = Path(text)
        name = path.stem
        path_text = text
    if not name:
        raise ValueError(f"fear index name is empty: {spec}")
    if not path_text:
        raise ValueError(f"fear index path is empty: {spec}")
    return name, path_text


def fear_index_columns(names: list[str]) -> list[str]:
    columns: list[str] = []
    for name in names:
        safe_name = str(name).strip()
        for metric in FEAR_INDEX_METRICS:
            columns.append(f"恐怖指数_{safe_name}_{metric}")
    return columns


def normalize_fear_name(name: str) -> str:
    text = str(name or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def infer_fear_asset_class(ticker: str) -> str:
    text = str(ticker or "").strip().upper()
    if not text:
        return "UNKNOWN"
    if text.endswith(".T") or text.isdigit():
        return "JP"
    if text.endswith("-USD") or text.endswith("-JPY") or text in {"BTC", "ETH"}:
        return "CRYPTO"
    if "=" in text:
        if text in {"CL=F", "BZ=F", "HO=F", "RB=F", "NG=F"}:
            return "COMMODITY_ENERGY"
        if text in {"GC=F", "SI=F", "PL=F", "PA=F"}:
            return "COMMODITY_METAL"
        if text in {"ZB=F", "ZN=F", "ZF=F", "ZT=F"}:
            return "BOND_FUTURES"
        return "FUTURES"
    if "." in text:
        return "OTHER_LISTED"
    return "US"


def find_fear_name(available_names: list[str], aliases: list[str]) -> str | None:
    normalized_available = [(name, normalize_fear_name(name)) for name in available_names]
    normalized_aliases = [normalize_fear_name(alias) for alias in aliases]
    for alias in normalized_aliases:
        for name, normalized_name in normalized_available:
            if normalized_name == alias or alias in normalized_name:
                return name
    return None


def preferred_fear_names(asset_class: str, available_names: list[str]) -> list[str]:
    preference_aliases: list[list[str]]
    if asset_class == "JP":
        preference_aliases = [
            ["NikkeiVI", "Nikkei225VI", "NKVI", "NK225VI", "日経VI", "日経平均VI"],
            ["VXJ"],
            ["VIX"],
        ]
    elif asset_class == "US":
        preference_aliases = [
            ["VIX", "CBOEVIX"],
            ["VXJ"],
            ["NikkeiVI", "Nikkei225VI", "日経VI", "日経平均VI"],
        ]
    elif asset_class == "CRYPTO":
        preference_aliases = [
            ["CryptoFear", "CryptoFearGreed", "BTCFear", "BitcoinFear"],
            ["VIX"],
            ["VXJ"],
        ]
    elif asset_class == "COMMODITY_ENERGY":
        preference_aliases = [
            ["OVX", "OilVIX"],
            ["VIX"],
            ["VXJ"],
        ]
    elif asset_class == "COMMODITY_METAL":
        preference_aliases = [
            ["GVZ", "GoldVIX"],
            ["VIX"],
            ["VXJ"],
        ]
    elif asset_class in {"BOND_FUTURES"}:
        preference_aliases = [
            ["MOVE"],
            ["VIX"],
            ["VXJ"],
        ]
    else:
        preference_aliases = [
            ["VIX"],
            ["VXJ"],
            ["NikkeiVI", "Nikkei225VI", "日経VI", "日経平均VI"],
        ]

    preferred: list[str] = []
    for aliases in preference_aliases:
        name = find_fear_name(available_names, aliases)
        if name and name not in preferred:
            preferred.append(name)
    for name in available_names:
        if name not in preferred:
            preferred.append(name)
    return preferred


def compute_adopted_fear_features(
    ticker: str,
    fear_names: list[str],
    row_features: dict[str, str],
) -> dict[str, str]:
    result = {column: "" for column in ADOPTED_FEAR_COLUMNS}
    asset_class = infer_fear_asset_class(ticker)
    result["恐怖指数_採用対象"] = asset_class
    if not fear_names:
        result["恐怖指数_採用取得状態"] = "no_fear_indexes"
        return result

    preferred = preferred_fear_names(asset_class, fear_names)
    selected = ""
    for name in preferred:
        if row_features.get(f"恐怖指数_{name}_取得状態") == "ok":
            selected = name
            break
    if not selected:
        for name in preferred:
            status = row_features.get(f"恐怖指数_{name}_取得状態")
            if status:
                selected = name
                break
    if not selected:
        result["恐怖指数_採用取得状態"] = "no_matching_index"
        return result

    result["恐怖指数_採用名"] = selected
    for metric in FEAR_INDEX_METRICS:
        result[f"恐怖指数_採用{metric}"] = row_features.get(f"恐怖指数_{selected}_{metric}", "")
    return result


def pick_column(columns: list[str], candidates: list[str]) -> str | None:
    normalized = {str(column).strip().lower(): column for column in columns}
    for candidate in candidates:
        hit = normalized.get(candidate.strip().lower())
        if hit is not None:
            return hit
    return None


def parse_date_series(values: pd.Series) -> pd.Series:
    text = values.astype(str).str.strip()
    text = text.str.replace(r"\.0$", "", regex=True)

    result = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    missing = result.isna()
    if missing.any():
        result.loc[missing] = pd.to_datetime(text[missing], errors="coerce")
    return result


def load_fear_index_from_yfinance(symbol: str) -> pd.DataFrame:
    text = str(symbol or "").strip()
    if not text:
        raise ValueError("yfinance fear index symbol is empty")
    if yf is None:
        raise SystemExit(
            f"yfinance is not installed, so fear index cannot be downloaded from yfinance: {text}. "
            "Use a local CSV instead, for example VXJ=C:\\path\\vxj.csv."
        )
    download_kwargs = {
        "period": "max",
        "auto_adjust": True,
        "progress": False,
        "threads": False,
        "raise_errors": False,
    }
    try:
        frame = yf.download(text, **download_kwargs)
    except TypeError:
        download_kwargs.pop("raise_errors", None)
        frame = yf.download(text, **download_kwargs)
    prices = normalize_download_frame(frame)
    if prices.empty or "Close" not in prices.columns:
        raise ValueError(f"yfinance returned no usable data for fear index: {text}")
    result = pd.DataFrame({"Date": prices["Date"], "Value": pd.to_numeric(prices["Close"], errors="coerce")})
    result = result.dropna(subset=["Date", "Value"]).sort_values("Date").drop_duplicates("Date")
    if result.empty:
        raise ValueError(f"yfinance returned no numeric close values for fear index: {text}")
    return result.reset_index(drop=True)


def load_fear_index_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"fear index CSV was not found: {path}. "
            "Download or create the CSV first, then pass the real path with --fear-index-csv NAME=PATH."
        )
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            frame = pd.read_csv(path, encoding=encoding)
            break
        except Exception as exc:
            last_error = exc
    else:
        raise ValueError(f"failed to read fear index CSV: {last_error}")

    columns = [str(column) for column in frame.columns]
    date_col = pick_column(columns, ["date", "Date", "DATE", "日付", "年月日", "基準日"])
    if date_col is None:
        date_col = columns[0] if columns else None
    if date_col is None:
        raise ValueError("date column was not found")

    value_col = pick_column(
        columns,
        ["value", "Value", "VALUE", "close", "Close", "CLOSE", "終値", "指数値", "VI", "VXJ"],
    )
    if value_col is None:
        numeric_candidates: list[tuple[str, int]] = []
        for column in columns:
            if column == date_col:
                continue
            numeric = pd.to_numeric(frame[column], errors="coerce")
            numeric_candidates.append((column, int(numeric.notna().sum())))
        numeric_candidates.sort(key=lambda item: item[1], reverse=True)
        value_col = numeric_candidates[0][0] if numeric_candidates and numeric_candidates[0][1] > 0 else None
    if value_col is None:
        raise ValueError("value column was not found")

    result = pd.DataFrame(
        {
            "Date": parse_date_series(frame[date_col]).dt.date,
            "Value": pd.to_numeric(frame[value_col], errors="coerce"),
        }
    )
    result = result.dropna(subset=["Date", "Value"]).sort_values("Date").drop_duplicates("Date")
    if result.empty:
        raise ValueError("fear index CSV has no usable rows")
    return result.reset_index(drop=True)


def load_fear_indexes(specs: list[str]) -> dict[str, pd.DataFrame]:
    indexes: dict[str, pd.DataFrame] = {}
    for spec in specs:
        name, source = parse_fear_index_spec(spec)
        source_text = str(source).strip()
        if source_text.lower().startswith(("yf:", "yfinance:")):
            symbol = source_text.split(":", 1)[1].strip()
            indexes[name] = load_fear_index_from_yfinance(symbol)
            print(f"fear_index name={name} yfinance={symbol} rows={len(indexes[name])}", flush=True)
        else:
            path = Path(source_text)
            indexes[name] = load_fear_index_csv(path)
            print(f"fear_index name={name} path={path} rows={len(indexes[name])}", flush=True)
    return indexes


def compute_fear_features(
    fear_indexes: dict[str, pd.DataFrame],
    signal_date: dt.date | None,
    max_lag_days: int,
) -> dict[str, str]:
    result = {column: "" for column in fear_index_columns(list(fear_indexes.keys()))}
    for name, frame in fear_indexes.items():
        prefix = f"恐怖指数_{name}_"
        if signal_date is None:
            result[prefix + "取得状態"] = "missing_signal_date"
            continue
        try:
            available = frame[frame["Date"] <= signal_date].copy()
            if available.empty:
                result[prefix + "取得状態"] = "no_prior_value"
                continue
            pos = len(available) - 1
            ref_date = available["Date"].iloc[pos]
            lag_days = (signal_date - ref_date).days
            result[prefix + "参照日"] = ref_date.isoformat()
            result[prefix + "底検知日差"] = str(lag_days)
            if lag_days > max_lag_days:
                result[prefix + "取得状態"] = "stale"
                continue

            value = float(available["Value"].iloc[pos])
            values = available["Value"]
            one_year_start = signal_date - dt.timedelta(days=365)
            three_year_start = signal_date - dt.timedelta(days=365 * 3)
            one_year_values = available[available["Date"] >= one_year_start]["Value"]
            three_year_values = available[available["Date"] >= three_year_start]["Value"]
            p1 = percentile_rank(one_year_values, value)
            p3 = percentile_rank(three_year_values, value)

            result[prefix + "取得状態"] = "ok"
            result[prefix + "当日値"] = fmt_num(value)
            result[prefix + "5日変化率"] = pct_change_from_positions(values, pos, 5)
            result[prefix + "20日変化率"] = pct_change_from_positions(values, pos, 20)
            result[prefix + "過去1年パーセンタイル"] = p1
            result[prefix + "過去3年パーセンタイル"] = p3
            result[prefix + "全期間パーセンタイル"] = percentile_rank(values, value)
            result[prefix + "低位20pctフラグ"] = "1" if p3 and float(p3) <= 20.0 else "0"
            result[prefix + "高位25pctフラグ"] = "1" if p3 and float(p3) >= 75.0 else "0"
        except Exception as exc:
            result[prefix + "取得状態"] = "error"
            result[prefix + "エラー"] = str(exc)[:300]
    return result


def cache_path(cache_dir: Path, ticker: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in ticker)
    return cache_dir / f"{safe}.csv"


def normalize_download_frame(frame: Any) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        return pd.DataFrame()
    if isinstance(frame.columns, pd.MultiIndex):
        expected = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        normalized_columns: list[str] = []
        for item in frame.columns:
            parts = [str(part) for part in item if str(part)]
            matched = next((part for part in parts if part in expected), "")
            normalized_columns.append(matched or (parts[-1] if parts else ""))
        frame.columns = normalized_columns
    frame = frame.reset_index()
    if "Date" not in frame.columns and "Datetime" in frame.columns:
        frame = frame.rename(columns={"Datetime": "Date"})
    keep = [name for name in ["Date", "Open", "High", "Low", "Close", "Volume"] if name in frame.columns]
    frame = frame[keep].copy()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.date
    return frame.sort_values("Date").drop_duplicates("Date")


def load_prices(
    ticker: str,
    start: dt.date,
    end: dt.date,
    cache_dir: Path,
    sleep_seconds: float,
    cache_ttl_days: int,
) -> tuple[pd.DataFrame, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, ticker)
    today = dt.datetime.now().date()

    if path.exists():
        try:
            age_days = (today - dt.datetime.fromtimestamp(path.stat().st_mtime).date()).days
            cached = pd.read_csv(path, parse_dates=["Date"])
            cached["Date"] = cached["Date"].dt.date
            if (
                not cached.empty
                and {"Date", "Close"}.issubset(set(cached.columns))
                and age_days <= cache_ttl_days
            ):
                min_date = min(cached["Date"])
                max_date = max(cached["Date"])
                if min_date <= start and max_date >= end:
                    return cached.sort_values("Date"), "cache"
        except Exception:
            pass
    if yf is None:
        raise SystemExit(
            f"Price cache is missing or incomplete for {ticker}: {path}. "
            "yfinance is not installed in this Python runtime, so uncached prices cannot be downloaded."
        )

    fetch_start = start - dt.timedelta(days=14)
    fetch_end = end + dt.timedelta(days=7)
    download_kwargs = {
        "start": fetch_start.isoformat(),
        "end": fetch_end.isoformat(),
        "auto_adjust": True,
        "progress": False,
        "threads": False,
        "raise_errors": False,
    }
    try:
        frame = yf.download(ticker, **download_kwargs)
    except TypeError:
        download_kwargs.pop("raise_errors", None)
        frame = yf.download(ticker, **download_kwargs)
    prices = normalize_download_frame(frame)
    if prices.empty:
        return prices, "empty"
    prices.to_csv(path, index=False, encoding="utf-8")
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    return prices, "download"


def max_run(flags: list[bool]) -> int:
    best = 0
    current = 0
    for flag in flags:
        if flag:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def sign_changes(values: list[float]) -> int:
    signs: list[int] = []
    for value in values:
        if value > 0:
            signs.append(1)
        elif value < 0:
            signs.append(-1)
    return sum(1 for prev, cur in zip(signs, signs[1:]) if prev != cur)


def compute_features(prices: pd.DataFrame, peak_date: dt.date, signal_date: dt.date) -> dict[str, str]:
    result = {column: "" for column in FEATURE_COLUMNS}
    result["日足特徴_開始日"] = peak_date.isoformat()
    result["日足特徴_終了日"] = signal_date.isoformat()

    if peak_date > signal_date:
        result["日足特徴_取得状態"] = "date_error"
        result["日足特徴_エラー"] = "peak_date_after_signal_date"
        return result

    if prices.empty or "Date" not in prices.columns or "Close" not in prices.columns:
        result["日足特徴_取得状態"] = "empty_prices"
        result["日足特徴_エラー"] = "empty_or_invalid_price_frame"
        return result

    frame = prices[(prices["Date"] >= peak_date) & (prices["Date"] <= signal_date)].copy()
    if len(frame) < 2:
        result["日足特徴_取得状態"] = "no_enough_prices"
        result["日足特徴_エラー"] = "fewer_than_2_price_rows"
        return result

    frame["ret"] = frame["Close"].pct_change() * 100.0
    returns = [float(x) for x in frame["ret"].dropna().tolist()]
    if not returns:
        result["日足特徴_取得状態"] = "no_returns"
        return result

    ret_series = pd.Series(returns)
    abs_series = ret_series.abs()
    down = [x for x in returns if x < 0]
    up = [x for x in returns if x > 0]
    flat = [x for x in returns if x == 0]
    trading_days = len(returns)
    cumulative_return = (float(frame["Close"].iloc[-1]) / float(frame["Close"].iloc[0]) - 1.0) * 100.0
    path_abs_sum = float(abs_series.sum())
    efficiency = abs(cumulative_return) / path_abs_sum if path_abs_sum else None
    roughness = path_abs_sum / abs(cumulative_return) if cumulative_return else None

    volume_ratio_max = None
    volume_spike_days = ""
    if "Volume" in frame.columns:
        volume = pd.to_numeric(frame["Volume"], errors="coerce").dropna()
        median_volume = float(volume.median()) if len(volume) else 0.0
        if median_volume > 0:
            ratios = volume / median_volume
            volume_ratio_max = float(ratios.max())
            volume_spike_days = str(int((ratios >= 2.0).sum()))

    low_source = "Low" if "Low" in frame.columns else "Close"
    high_source = "High" if "High" in frame.columns else "Close"
    lows = pd.to_numeric(frame[low_source], errors="coerce").dropna()
    highs = pd.to_numeric(frame[high_source], errors="coerce").dropna()
    closes = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    low_update_days = ""
    close_position = None
    if len(lows) >= 2:
        running_lows = lows.cummin()
        low_update_days = str(int((lows.iloc[1:].to_numpy() < running_lows.iloc[:-1].to_numpy()).sum()))
    if len(lows) and len(highs) and len(closes):
        window_low = float(lows.min())
        window_high = float(highs.max())
        denominator = window_high - window_low
        if denominator > 0:
            close_position = (float(closes.iloc[-1]) - window_low) / denominator

    result.update(
        {
            "日足特徴_取得状態": "ok",
            "日足特徴_営業日数": str(trading_days),
            "日足特徴_変化率絶対値平均": fmt_num(float(abs_series.mean())),
            "日足特徴_変化率絶対値最小": fmt_num(float(abs_series.min())),
            "日足特徴_変化率絶対値中央値": fmt_num(float(abs_series.median())),
            "日足特徴_変化率絶対値最大": fmt_num(float(abs_series.max())),
            "日足特徴_下落日数": str(len(down)),
            "日足特徴_上昇日数": str(len(up)),
            "日足特徴_横ばい日数": str(len(flat)),
            "日足特徴_下落日率": fmt_num(len(down) / trading_days),
            "日足特徴_累積変化率": fmt_num(cumulative_return),
            "日足特徴_平均変化率": fmt_num(float(ret_series.mean())),
            "日足特徴_変化率標準偏差": fmt_num(float(ret_series.std(ddof=0))),
            "日足特徴_年率換算ボラ": fmt_num(float(ret_series.std(ddof=0)) * math.sqrt(252.0)),
            "日足特徴_下落日の平均下落率": fmt_num(float(pd.Series(down).mean()) if down else None),
            "日足特徴_上昇日の平均上昇率": fmt_num(float(pd.Series(up).mean()) if up else None),
            "日足特徴_下落率合計": fmt_num(sum(down)),
            "日足特徴_上昇率合計": fmt_num(sum(up)),
            "日足特徴_最大連続下落日数": str(max_run([x < 0 for x in returns])),
            "日足特徴_最大連続上昇日数": str(max_run([x > 0 for x in returns])),
            "日足特徴_符号反転回数": str(sign_changes(returns)),
            "日足特徴_大幅上昇3pct日数": str(sum(1 for x in returns if x >= 3.0)),
            "日足特徴_大幅下落3pct日数": str(sum(1 for x in returns if x <= -3.0)),
            "日足特徴_大幅上昇5pct日数": str(sum(1 for x in returns if x >= 5.0)),
            "日足特徴_大幅下落5pct日数": str(sum(1 for x in returns if x <= -5.0)),
            "日足特徴_大幅上昇10pct日数": str(sum(1 for x in returns if x >= 10.0)),
            "日足特徴_大幅下落10pct日数": str(sum(1 for x in returns if x <= -10.0)),
            "日足特徴_経路効率": fmt_num(efficiency),
            "日足特徴_荒さ指数": fmt_num(roughness),
            "日足特徴_出来高中央値比最大": fmt_num(volume_ratio_max),
            "日足特徴_出来高急増日数": volume_spike_days,
            "日足特徴_安値更新日数": low_update_days,
            "日足特徴_終値位置": fmt_num(close_position),
        }
    )
    return result


def compute_last30_features(prices: pd.DataFrame, signal_date: dt.date, window_days: int = 30) -> dict[str, str]:
    result = {column: "" for column in LAST30_FEATURE_COLUMNS}
    result["直前30日特徴_終了日"] = signal_date.isoformat()

    if prices.empty or "Date" not in prices.columns or "Close" not in prices.columns:
        result["直前30日特徴_取得状態"] = "empty_prices"
        result["直前30日特徴_エラー"] = "empty_or_invalid_price_frame"
        return result

    frame = prices[prices["Date"] <= signal_date].copy().tail(window_days + 1)
    if len(frame) < 2:
        result["直前30日特徴_取得状態"] = "no_enough_prices"
        result["直前30日特徴_エラー"] = "fewer_than_2_price_rows"
        return result

    result["直前30日特徴_開始日"] = frame["Date"].iloc[0].isoformat()
    frame["ret"] = frame["Close"].pct_change() * 100.0
    returns = [float(x) for x in frame["ret"].dropna().tolist()]
    if not returns:
        result["直前30日特徴_取得状態"] = "no_returns"
        return result

    ret_series = pd.Series(returns)
    abs_series = ret_series.abs()
    down = [x for x in returns if x < 0]
    up = [x for x in returns if x > 0]
    flat = [x for x in returns if x == 0]
    trading_days = len(returns)
    cumulative_return = (float(frame["Close"].iloc[-1]) / float(frame["Close"].iloc[0]) - 1.0) * 100.0

    volume_ratio_max = None
    volume_spike_days = ""
    if "Volume" in frame.columns:
        volume = pd.to_numeric(frame["Volume"], errors="coerce").dropna()
        median_volume = float(volume.median()) if len(volume) else 0.0
        if median_volume > 0:
            ratios = volume / median_volume
            volume_ratio_max = float(ratios.max())
            volume_spike_days = str(int((ratios >= 2.0).sum()))

    low_source = "Low" if "Low" in frame.columns else "Close"
    high_source = "High" if "High" in frame.columns else "Close"
    lows = pd.to_numeric(frame[low_source], errors="coerce").dropna()
    highs = pd.to_numeric(frame[high_source], errors="coerce").dropna()
    closes = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    low_update_days = ""
    close_position = None
    if len(lows) >= 2:
        running_lows = lows.cummin()
        low_update_days = str(int((lows.iloc[1:].to_numpy() < running_lows.iloc[:-1].to_numpy()).sum()))
    if len(lows) and len(highs) and len(closes):
        window_low = float(lows.min())
        window_high = float(highs.max())
        denominator = window_high - window_low
        if denominator > 0:
            close_position = (float(closes.iloc[-1]) - window_low) / denominator

    result.update(
        {
            "直前30日特徴_取得状態": "ok" if trading_days >= window_days else "ok_short",
            "直前30日特徴_営業日数": str(trading_days),
            "直前30日特徴_累積騰落率": fmt_num(cumulative_return),
            "直前30日特徴_変化率絶対値平均": fmt_num(float(abs_series.mean())),
            "直前30日特徴_変化率絶対値中央値": fmt_num(float(abs_series.median())),
            "直前30日特徴_変化率絶対値最大": fmt_num(float(abs_series.max())),
            "直前30日特徴_下落日数": str(len(down)),
            "直前30日特徴_上昇日数": str(len(up)),
            "直前30日特徴_横ばい日数": str(len(flat)),
            "直前30日特徴_下落日率": fmt_num(len(down) / trading_days),
            "直前30日特徴_上昇日の平均上昇率": fmt_num(float(pd.Series(up).mean()) if up else None),
            "直前30日特徴_下落日の平均下落率": fmt_num(float(pd.Series(down).mean()) if down else None),
            "直前30日特徴_最大連続下落日数": str(max_run([x < 0 for x in returns])),
            "直前30日特徴_最大連続上昇日数": str(max_run([x > 0 for x in returns])),
            "直前30日特徴_符号反転回数": str(sign_changes(returns)),
            "直前30日特徴_大幅上昇3pct日数": str(sum(1 for x in returns if x >= 3.0)),
            "直前30日特徴_大幅下落3pct日数": str(sum(1 for x in returns if x <= -3.0)),
            "直前30日特徴_大幅上昇5pct日数": str(sum(1 for x in returns if x >= 5.0)),
            "直前30日特徴_大幅下落5pct日数": str(sum(1 for x in returns if x <= -5.0)),
            "直前30日特徴_出来高中央値比最大": fmt_num(volume_ratio_max),
            "直前30日特徴_出来高急増日数": volume_spike_days,
            "直前30日特徴_安値更新日数": low_update_days,
            "直前30日特徴_終値位置": fmt_num(close_position),
        }
    )
    return result


def enrich_csv(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output)
    cache_dir = Path(args.cache_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fear_indexes = load_fear_indexes(args.fear_index_csv or [])
    fear_columns = fear_index_columns(list(fear_indexes.keys()))

    with input_path.open("r", encoding=args.encoding, newline="") as handle:
        rows = list(csv.DictReader(handle))
        if not rows:
            raise SystemExit("Input CSV is empty.")
        fieldnames = list(rows[0].keys())

    missing = [
        column
        for column in [SOURCE_TICKER_COL, SOURCE_SIGNAL_DATE_COL, SOURCE_PEAK_DATE_COL]
        if column not in fieldnames
    ]
    if missing:
        raise SystemExit("Missing required columns: " + ", ".join(missing))

    for column in ALL_FEATURE_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
    for column in fear_columns:
        if column not in fieldnames:
            fieldnames.append(column)
    if fear_indexes:
        for column in ADOPTED_FEAR_COLUMNS:
            if column not in fieldnames:
                fieldnames.append(column)
        print(
            f"fear_output_columns raw={len(fear_columns)} adopted={len(ADOPTED_FEAR_COLUMNS)} "
            f"total_fieldnames={len(fieldnames)}",
            flush=True,
        )

    price_cache: dict[str, pd.DataFrame] = {}
    status_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    error_samples: list[str] = []
    download_counts: dict[str, int] = {}
    detected_rows = sum(1 for row in rows if parse_date(str(row.get(SOURCE_SIGNAL_DATE_COL) or "")))
    unique_detected_tickers = {
        str(row.get(SOURCE_TICKER_COL) or "").strip()
        for row in rows
        if str(row.get(SOURCE_TICKER_COL) or "").strip()
        and parse_date(str(row.get(SOURCE_SIGNAL_DATE_COL) or ""))
    }
    ticker_ranges: dict[str, tuple[dt.date, dt.date]] = {}
    for row in rows:
        ticker = str(row.get(SOURCE_TICKER_COL) or "").strip()
        signal_date = parse_date(str(row.get(SOURCE_SIGNAL_DATE_COL) or ""))
        peak_date = parse_date(str(row.get(SOURCE_PEAK_DATE_COL) or ""))
        if not ticker or not signal_date or not peak_date:
            continue
        start = min(peak_date, signal_date - dt.timedelta(days=max(45, args.last30_calendar_buffer_days)))
        end = signal_date
        old = ticker_ranges.get(ticker)
        if old is None:
            ticker_ranges[ticker] = (start, end)
        else:
            ticker_ranges[ticker] = (min(old[0], start), max(old[1], end))
    print(
        f"start rows={len(rows)} detected_rows={detected_rows} "
        f"unique_detected_tickers={len(unique_detected_tickers)}",
        flush=True,
    )
    print(f"input={input_path}", flush=True)
    print(f"output={output_path}", flush=True)
    print(f"cache_dir={cache_dir}", flush=True)
    if fear_indexes:
        print(f"fear_indexes={list(fear_indexes.keys())}", flush=True)

    for index, row in enumerate(rows, 1):
        ticker = str(row.get(SOURCE_TICKER_COL) or "").strip()
        signal_date = parse_date(str(row.get(SOURCE_SIGNAL_DATE_COL) or ""))
        peak_date = parse_date(str(row.get(SOURCE_PEAK_DATE_COL) or ""))
        if fear_indexes:
            fear_features = compute_fear_features(fear_indexes, signal_date, args.fear_max_lag_days)
            fear_features.update(
                compute_adopted_fear_features(ticker, list(fear_indexes.keys()), fear_features)
            )
            row.update(fear_features)

        if args.only_detected and not signal_date:
            row.update({column: "" for column in ALL_FEATURE_COLUMNS})
            row["日足特徴_取得状態"] = "skipped_not_detected"
            row["直前30日特徴_取得状態"] = "skipped_not_detected"
            status_counts["skipped_not_detected"] = status_counts.get("skipped_not_detected", 0) + 1
            continue
        if not ticker or not signal_date or not peak_date:
            row.update({column: "" for column in ALL_FEATURE_COLUMNS})
            row["日足特徴_取得状態"] = "missing_required_value"
            row["直前30日特徴_取得状態"] = "missing_required_value"
            status_counts["missing_required_value"] = status_counts.get("missing_required_value", 0) + 1
            continue

        try:
            if ticker not in price_cache:
                load_start, load_end = ticker_ranges.get(ticker, (peak_date, signal_date))
                print(
                    f"price {index}/{len(rows)} ticker={ticker} "
                    f"range={load_start}..{load_end} signal={signal_date} loading",
                    flush=True,
                )
                prices, source = load_prices(
                    ticker,
                    load_start,
                    load_end,
                    cache_dir,
                    args.sleep_seconds,
                    args.cache_ttl_days,
                )
                price_cache[ticker] = prices
                download_counts[source] = download_counts.get(source, 0) + 1
                print(
                    f"price {index}/{len(rows)} ticker={ticker} source={source} "
                    f"rows={len(prices)}",
                    flush=True,
                )
            features = compute_features(price_cache[ticker], peak_date, signal_date)
            features.update(compute_last30_features(price_cache[ticker], signal_date, args.last30_trading_days))
            row.update(features)
            status = str(features.get("日足特徴_取得状態") or "unknown")
            last30_status = str(features.get("直前30日特徴_取得状態") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            status_counts[f"last30_{last30_status}"] = status_counts.get(f"last30_{last30_status}", 0) + 1
            if status != "ok":
                print(
                    f"feature {index}/{len(rows)} ticker={ticker} status={status} "
                    f"error={features.get('日足特徴_エラー', '')}",
                    flush=True,
                )
        except Exception as exc:
            error_key = f"{type(exc).__name__}: {str(exc)[:120]}"
            error_counts[error_key] = error_counts.get(error_key, 0) + 1
            if len(error_samples) < args.error_sample_limit:
                error_samples.append(
                    f"row={index} ticker={ticker} peak={peak_date} signal={signal_date} "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
            row.update({column: "" for column in ALL_FEATURE_COLUMNS})
            row["日足特徴_取得状態"] = "error"
            row["日足特徴_エラー"] = (str(exc) or traceback.format_exc(limit=1))[:300]
            row["直前30日特徴_取得状態"] = "error"
            row["直前30日特徴_エラー"] = (str(exc) or traceback.format_exc(limit=1))[:300]
            status_counts["error"] = status_counts.get("error", 0) + 1

        if args.progress_every and index % args.progress_every == 0:
            print(
                f"processed {index}/{len(rows)} "
                f"status={status_counts} price_sources={download_counts} "
                f"cached_tickers={len(price_cache)}",
                flush=True,
            )
            if error_counts:
                top_errors = sorted(error_counts.items(), key=lambda item: item[1], reverse=True)[:3]
                print(f"top_errors={top_errors}", flush=True)
            for sample in error_samples[-min(len(error_samples), 3):]:
                print(f"error_sample={sample}", flush=True)

    with output_path.open("w", encoding=args.encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"input={input_path}")
    print(f"output={output_path}")
    print(f"rows={len(rows)}")
    print(f"status={status_counts}")
    print(f"price_sources={download_counts}")
    print(f"cached_tickers={len(price_cache)}")
    if error_counts:
        print(f"error_counts={error_counts}")
    for sample in error_samples:
        print(f"error_sample={sample}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Input stock_analysis CSV")
    parser.add_argument("--output", required=True, help="Output enriched CSV")
    parser.add_argument("--cache-dir", default="data/yfinance_cache", help="Ticker price cache directory")
    parser.add_argument("--encoding", default="utf-8-sig", help="CSV encoding")
    parser.add_argument("--only-detected", action="store_true", help="Skip rows without 底打ち候補日")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Sleep after each yfinance download")
    parser.add_argument("--cache-ttl-days", type=int, default=30, help="Reuse ticker cache for this many days")
    parser.add_argument("--progress-every", type=int, default=50, help="Print progress every N rows")
    parser.add_argument("--error-sample-limit", type=int, default=12, help="Print up to N error samples")
    parser.add_argument(
        "--fear-index-csv",
        action="append",
        default=[],
        help="Optional fear index CSV. Use NAME=PATH, for example VXJ=C:\\path\\vxj.csv. Can be repeated.",
    )
    parser.add_argument(
        "--fear-max-lag-days",
        type=int,
        default=7,
        help="Maximum allowed calendar-day lag when matching a signal date to the latest prior fear index date",
    )
    parser.add_argument("--last30-trading-days", type=int, default=30, help="Trading-return days used for 直前30日特徴")
    parser.add_argument(
        "--last30-calendar-buffer-days",
        type=int,
        default=75,
        help="Calendar-day lookback buffer to ensure 30 trading days before the signal date are cached",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return enrich_csv(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
