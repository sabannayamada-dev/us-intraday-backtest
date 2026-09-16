# Daily Path Features

`stock_analysis_全部入り.csv` に、ピークから底打ち候補日までの日足推移特徴量を追加するPCローカル用ツールです。

今後の通常運用はVPSではなく、このフォルダ内だけで完結します。

## ローカル実行

PowerShellで実行:

```powershell
cd "C:\Users\saban\Documents\Codex\2026-07-18\new-chat\outputs\daily_path_features"

.\run_daily_path_features_local.ps1 `
  -CsvPath "C:\Users\saban\Downloads\stock_analysis_全部入り.csv"
```

`-CsvPath` を省略すると、`Downloads` 内の `stock_analysis_*.csv` から、`全部入り` を優先して自動選択します。

## ローカル保存場所

```text
outputs\daily_path_features\
  daily_path_feature_enricher.py
  run_daily_path_features_local.ps1
  requirements.txt
  .venv\                         # ローカルPython環境
  local_data\
    input\                       # 実行時にCSVをコピー
    output\                      # 加工済みCSV
    cache\                       # yfinance価格キャッシュ
    logs\                        # 実行ログ
```

最新出力は常にここへコピーされます。

```text
local_data\output\stock_analysis_with_daily_path_latest.csv
```

日時付きの出力も残ります。

```text
local_data\output\stock_analysis_with_daily_path_YYYYMMDD_HHMMSS.csv
```

## 使う列

- `銘柄`
- `判定前最高値日`
- `底打ち候補日`

通常は底打ち候補日がある行だけ処理します。未検出行も含めたい場合:

```powershell
.\run_daily_path_features_local.ps1 `
  -CsvPath "C:\Users\saban\Downloads\stock_analysis_全部入り.csv" `
  -AllRows
```

## 追加する主な列

- `日足特徴_変化率絶対値平均`
- `日足特徴_変化率絶対値最小`
- `日足特徴_変化率絶対値中央値`
- `日足特徴_変化率絶対値最大`
- `日足特徴_下落日数`
- `日足特徴_上昇日数`
- `日足特徴_横ばい日数`
- `日足特徴_累積変化率`
- `日足特徴_変化率標準偏差`
- `日足特徴_年率換算ボラ`
- `日足特徴_最大連続下落日数`
- `日足特徴_最大連続上昇日数`
- `日足特徴_符号反転回数`
- `日足特徴_大幅上昇3pct日数`
- `日足特徴_大幅下落3pct日数`
- `日足特徴_大幅上昇5pct日数`
- `日足特徴_大幅下落5pct日数`
- `日足特徴_大幅上昇10pct日数`
- `日足特徴_大幅下落10pct日数`
- `日足特徴_経路効率`
- `日足特徴_荒さ指数`
- `日足特徴_出来高急増日数`

## サーバ側を止める場合

VPSで自動実行を止めるだけなら、PowerShellから以下を実行します。

```powershell
$Key = (Get-ChildItem -LiteralPath "C:\Users\saban\Downloads" -Recurse -Force -File |
  Where-Object Name -eq "patent-news-monitor-key.pem" |
  Select-Object -First 1).FullName

ssh.exe -t -i $Key app@160.251.252.249 "sudo systemctl disable --now stock-daily-path-features.timer; sudo systemctl stop stock-daily-path-features.service; systemctl status stock-daily-path-features.timer --no-pager -l"
```

サーバ上のファイルを消さなくても、timerを止めれば勝手には動きません。

## 分類別の指値バックテスト

`daily_path_81_pattern_classified.csv` を使い、分類ごとに「底検知価格から何%下に指値を置くか」を探索します。

```powershell
cd "C:\Users\saban\Documents\Codex\2026-07-18\new-chat\outputs\daily_path_features"

.\run_limit_order_backtest_local.ps1
```

主なオプション:

```powershell
.\run_limit_order_backtest_local.ps1 `
  -Benchmark "ACWI" `
  -MaxDepth 70 `
  -MinTrades 5 `
  -MinFillRate 0.05 `
  -MinStep 0.1 `
  -StableRounds 3
```

出力:

```text
local_data\backtest\pattern_limit_best.csv    # 分類ごとの最適指値
local_data\backtest\pattern_limit_trials.csv  # 試行履歴
local_data\backtest\pattern_limit_trades.csv  # 約定した取引明細
local_data\backtest\pattern_limit_report.txt  # 要約
```

比較対象のオルカン代理には、デフォルトで yfinance の `ACWI` を使います。必要なら `-Benchmark "VT"` などに変更できます。
