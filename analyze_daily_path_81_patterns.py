#!/usr/bin/env python3
from __future__ import annotations

import csv
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "local_data" / "output" / "stock_analysis_with_daily_path_latest.csv"
DEFAULT_CLASSIFIED = ROOT / "local_data" / "output" / "daily_path_81_pattern_classified.csv"
DEFAULT_SUMMARY = ROOT / "local_data" / "output" / "daily_path_81_pattern_summary.csv"
DEFAULT_REPORT = ROOT / "local_data" / "output" / "daily_path_81_pattern_report.txt"


COL = {
    "ticker": "銘柄",
    "name": "名称",
    "post_drawdown": "候補後下落率",
    "success": "成否",
    "status": "日足特徴_取得状態",
    "abs_mean": "日足特徴_変化率絶対値平均",
    "abs_median": "日足特徴_変化率絶対値中央値",
    "avg_down_return": "日足特徴_下落日の平均下落率",
    "avg_up_return": "日足特徴_上昇日の平均上昇率",
}

FEATURES = [
    ("abs_mean", "日足特徴_変化率絶対値平均"),
    ("abs_median", "日足特徴_変化率絶対値中央値"),
    ("avg_up_return", "日足特徴_上昇日の平均上昇率"),
    ("avg_down_return", "日足特徴_下落日の平均下落率"),
]

BIN_LABELS = {
    "L": "下位20%",
    "M": "中位60%",
    "H": "上位20%",
}


def to_float(value: str) -> float | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        return float(text)
    except ValueError:
        return None


def row_value(row: list[str], header_index: dict[str, int], key: str) -> str:
    idx = header_index[COL[key]]
    return row[idx] if idx < len(row) else ""


def quantile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("empty values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[int(pos)]
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def bin_value(value: float, low_cut: float, high_cut: float) -> str:
    if value <= low_cut:
        return "L"
    if value >= high_cut:
        return "H"
    return "M"


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def one_way_eta2(groups: dict[str, list[float]]) -> tuple[float, float]:
    populated = [values for values in groups.values() if values]
    all_values = [value for values in populated for value in values]
    if len(all_values) < 2 or len(populated) < 2:
        return 0.0, 0.0
    grand_mean = mean(all_values)
    ss_between = sum(len(values) * (mean(values) - grand_mean) ** 2 for values in populated)
    ss_total = sum((value - grand_mean) ** 2 for value in all_values)
    eta2 = ss_between / ss_total if ss_total else 0.0
    df_between = len(populated) - 1
    df_within = len(all_values) - len(populated)
    ss_within = ss_total - ss_between
    f_stat = (ss_between / df_between) / (ss_within / df_within) if df_between and df_within and ss_within else 0.0
    return eta2, f_stat


def permutation_p_value(group_keys: list[str], y_values: list[float], iterations: int = 10000) -> tuple[float, float, float]:
    groups: dict[str, list[float]] = defaultdict(list)
    for key, y in zip(group_keys, y_values):
        groups[key].append(y)
    observed_eta2, observed_f = one_way_eta2(groups)
    rng = random.Random(20260722)
    shuffled = list(y_values)
    ge = 0
    for _ in range(iterations):
        rng.shuffle(shuffled)
        perm_groups: dict[str, list[float]] = defaultdict(list)
        for key, y in zip(group_keys, shuffled):
            perm_groups[key].append(y)
        eta2, _ = one_way_eta2(perm_groups)
        if eta2 >= observed_eta2:
            ge += 1
    p_value = (ge + 1) / (iterations + 1)
    return observed_eta2, observed_f, p_value


def analyze() -> None:
    with DEFAULT_INPUT.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)

    header_index = {name: idx for idx, name in enumerate(header)}
    missing = [name for name in COL.values() if name not in header_index]
    if missing:
        raise SystemExit("Missing required columns: " + ", ".join(missing))

    ok_rows = [
        row for row in rows
        if row_value(row, header_index, "status") == "ok"
        and to_float(row_value(row, header_index, "post_drawdown")) is not None
        and all(to_float(row_value(row, header_index, key)) is not None for key, _ in FEATURES)
    ]

    cuts: dict[str, tuple[float, float]] = {}
    for key, _ in FEATURES:
        values = [to_float(row_value(row, header_index, key)) for row in ok_rows]
        numeric = [value for value in values if value is not None]
        cuts[key] = (quantile(numeric, 0.20), quantile(numeric, 0.80))

    classified_rows: list[list[str]] = []
    group_values: dict[str, list[float]] = defaultdict(list)
    group_success: dict[str, Counter[str]] = defaultdict(Counter)
    group_bins: dict[str, dict[str, str]] = {}

    extra_header = []
    for key, label in FEATURES:
        extra_header.extend([f"{label}_3分類", f"{key}_bin"])
    extra_header.append("日足特徴_81分類コード")

    for row in rows:
        out = list(row)
        can_classify = (
            row_value(row, header_index, "status") == "ok"
            and to_float(row_value(row, header_index, "post_drawdown")) is not None
            and all(to_float(row_value(row, header_index, key)) is not None for key, _ in FEATURES)
        )
        if not can_classify:
            out.extend([""] * len(extra_header))
            classified_rows.append(out)
            continue

        bins: dict[str, str] = {}
        extras: list[str] = []
        for key, label in FEATURES:
            value = to_float(row_value(row, header_index, key))
            assert value is not None
            low_cut, high_cut = cuts[key]
            code = bin_value(value, low_cut, high_cut)
            bins[key] = code
            extras.extend([BIN_LABELS[code], code])
        pattern = "-".join(bins[key] for key, _ in FEATURES)
        extras.append(pattern)
        out.extend(extras)
        classified_rows.append(out)
        y = to_float(row_value(row, header_index, "post_drawdown"))
        assert y is not None
        group_values[pattern].append(y)
        group_success[pattern][row_value(row, header_index, "success")] += 1
        group_bins[pattern] = bins

    DEFAULT_CLASSIFIED.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_CLASSIFIED.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header + extra_header)
        writer.writerows(classified_rows)

    summary_header = [
        "pattern",
        "abs_mean_bin",
        "abs_median_bin",
        "avg_up_return_bin",
        "avg_down_return_bin",
        "n",
        "mean_post_drawdown",
        "median_post_drawdown",
        "std_post_drawdown",
        "min_post_drawdown",
        "max_post_drawdown",
        "success_count",
        "acceptable_count",
        "too_early_count",
    ]
    summary_rows: list[list[str]] = []
    for pattern, values in sorted(group_values.items(), key=lambda item: mean(item[1])):
        bins = group_bins[pattern]
        counts = group_success[pattern]
        summary_rows.append([
            pattern,
            bins["abs_mean"],
            bins["abs_median"],
            bins["avg_up_return"],
            bins["avg_down_return"],
            str(len(values)),
            f"{mean(values):.6f}",
            f"{statistics.median(values):.6f}",
            f"{stdev(values):.6f}",
            f"{min(values):.6f}",
            f"{max(values):.6f}",
            str(counts.get("成功", 0)),
            str(counts.get("許容", 0)),
            str(counts.get("早すぎ", 0)),
        ])
    with DEFAULT_SUMMARY.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(summary_header)
        writer.writerows(summary_rows)

    group_keys = []
    y_values = []
    for pattern, values in group_values.items():
        for value in values:
            group_keys.append(pattern)
            y_values.append(value)
    eta2_all, f_all, p_all = permutation_p_value(group_keys, y_values, 10000)

    supported_patterns = {pattern for pattern, values in group_values.items() if len(values) >= 10}
    supported_keys = []
    supported_y = []
    for pattern, values in group_values.items():
        if pattern not in supported_patterns:
            continue
        for value in values:
            supported_keys.append(pattern)
            supported_y.append(value)
    eta2_supported, f_supported, p_supported = permutation_p_value(supported_keys, supported_y, 10000)

    nonempty = len(group_values)
    n_ge_5 = sum(1 for values in group_values.values() if len(values) >= 5)
    n_ge_10 = sum(1 for values in group_values.values() if len(values) >= 10)
    worst = sorted(group_values.items(), key=lambda item: mean(item[1]))[:10]
    best = sorted(group_values.items(), key=lambda item: mean(item[1]), reverse=True)[:10]

    lines: list[str] = []
    lines.append("Daily path 81-pattern analysis")
    lines.append(f"input={DEFAULT_INPUT}")
    lines.append(f"classified_csv={DEFAULT_CLASSIFIED}")
    lines.append(f"summary_csv={DEFAULT_SUMMARY}")
    lines.append("")
    lines.append(f"rows={len(rows)}")
    lines.append(f"ok_analysis_rows={len(ok_rows)}")
    lines.append(f"nonempty_patterns={nonempty}/81")
    lines.append(f"patterns_n_ge_5={n_ge_5}")
    lines.append(f"patterns_n_ge_10={n_ge_10}")
    lines.append("")
    lines.append("cut points:")
    for key, label in FEATURES:
        low_cut, high_cut = cuts[key]
        lines.append(f"- {key} ({label}): low<= {low_cut:.6f}, high>= {high_cut:.6f}")
    lines.append("")
    lines.append("permutation one-way group difference test:")
    lines.append(f"- all nonempty patterns: eta2={eta2_all:.6f}, F={f_all:.6f}, p={p_all:.6f}")
    lines.append(f"- patterns with n>=10 only: eta2={eta2_supported:.6f}, F={f_supported:.6f}, p={p_supported:.6f}, patterns={len(supported_patterns)} rows={len(supported_y)}")
    lines.append("")
    lines.append("worst mean post-drawdown patterns:")
    for pattern, values in worst:
        lines.append(f"- {pattern}: n={len(values)}, mean={mean(values):.3f}, median={statistics.median(values):.3f}")
    lines.append("")
    lines.append("best mean post-drawdown patterns:")
    for pattern, values in best:
        lines.append(f"- {pattern}: n={len(values)}, mean={mean(values):.3f}, median={statistics.median(values):.3f}")

    DEFAULT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    analyze()
