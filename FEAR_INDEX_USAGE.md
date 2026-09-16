# Fear Index CSV Usage

`daily_path_feature_enricher.py` can now add fear-index values at each bottom signal date.

## Input Format

Prepare a CSV with one date column and one numeric value column.

Supported date column names include:

- `Date`
- `date`
- `日付`
- `年月日`

Supported value column names include:

- `Close`
- `Value`
- `終値`
- `指数値`
- `VI`
- `VXJ`

If the value column name is not recognized, the script uses the numeric column with the most usable rows.

## Run Example

```powershell
cd "C:\Users\saban\Documents\Codex\2026-07-18\new-chat\outputs\daily_path_features"

.\run_daily_path_features_local.ps1 `
  -CsvPath "C:\Users\saban\Downloads\stock_analysis_全部入り.csv" `
  -FearIndexCsv "VXJ=C:\Users\saban\Downloads\vxj.csv", "NikkeiVI=C:\Users\saban\Downloads\nikkei_vi.csv"
```

`-FearIndexCsv` can be repeated. Use `NAME=PATH`.

For VIX, you can also use yfinance instead of a local CSV:

```powershell
.\run_daily_path_features_local.ps1 `
  -CsvPath "C:\Users\saban\Downloads\stock_analysis_全部入り.csv" `
  -FearIndexCsv "VIX=yf:^VIX"
```

You can mix both formats:

```powershell
.\run_daily_path_features_local.ps1 `
  -CsvPath "C:\Users\saban\Downloads\stock_analysis_全部入り.csv" `
  -FearIndexCsv "VXJ=C:\Users\saban\Downloads\vxj.csv", "NikkeiVI=C:\Users\saban\Downloads\nikkei_vi.csv", "VIX=yf:^VIX"
```

## Added Columns

For `VXJ`, columns are named like this:

- `恐怖指数_VXJ_取得状態`
- `恐怖指数_VXJ_参照日`
- `恐怖指数_VXJ_底検知日差`
- `恐怖指数_VXJ_当日値`
- `恐怖指数_VXJ_5日変化率`
- `恐怖指数_VXJ_20日変化率`
- `恐怖指数_VXJ_過去1年パーセンタイル`
- `恐怖指数_VXJ_過去3年パーセンタイル`
- `恐怖指数_VXJ_全期間パーセンタイル`
- `恐怖指数_VXJ_低位20pctフラグ`
- `恐怖指数_VXJ_高位25pctフラグ`
- `恐怖指数_VXJ_エラー`

The script uses the latest fear-index date on or before `底打ち候補日`. If the latest available value is too old, `取得状態` becomes `stale`.

The default maximum lag is 7 calendar days. Change it with:

```powershell
-FearMaxLagDays 14
```

## Auto-Selected Fear Index

When multiple fear indexes are supplied, the script also writes one normalized set of columns named `恐怖指数_採用...`.

Examples:

- `恐怖指数_採用対象`
- `恐怖指数_採用名`
- `恐怖指数_採用取得状態`
- `恐怖指数_採用参照日`
- `恐怖指数_採用底検知日差`
- `恐怖指数_採用当日値`
- `恐怖指数_採用5日変化率`
- `恐怖指数_採用20日変化率`
- `恐怖指数_採用過去1年パーセンタイル`
- `恐怖指数_採用過去3年パーセンタイル`
- `恐怖指数_採用低位20pctフラグ`
- `恐怖指数_採用高位25pctフラグ`

Selection rules:

- Japanese stocks, such as `2413.T`: `NikkeiVI` -> `VXJ` -> `VIX`
- US stocks/ETFs, such as `AAPL` or `QQQ`: `VIX` -> `VXJ`
- Crypto, such as `BTC-USD`: crypto-specific index if supplied -> `VIX` -> `VXJ`
- Oil/gas futures, such as `CL=F`: `OVX` if supplied -> `VIX` -> `VXJ`
- Gold/silver futures, such as `GC=F`: `GVZ` if supplied -> `VIX` -> `VXJ`
- Bond futures: `MOVE` if supplied -> `VIX` -> `VXJ`

If the first preferred index is stale or missing for that signal date, the script uses the next available index whose `取得状態` is `ok`.
