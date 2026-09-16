param(
    [string]$Server = "app@160.251.252.249",
    [string]$CsvPath = "",
    [switch]$RunOnce
)

$ErrorActionPreference = "Stop"

$Downloads = "C:\Users\saban\Downloads"
$KeyCandidates = Get-ChildItem -LiteralPath $Downloads -Recurse -Force -File -ErrorAction SilentlyContinue |
    Where-Object Name -eq "patent-news-monitor-key.pem"

if ($KeyCandidates.Count -eq 0) {
    throw "SSH private key was not found under: $Downloads"
}
if ($KeyCandidates.Count -gt 1) {
    $FoundPaths = ($KeyCandidates.FullName -join [Environment]::NewLine)
    throw "Multiple SSH private keys were found. Remove duplicates or set the path explicitly:`n$FoundPaths"
}
if (-not $CsvPath) {
    $CsvCandidates = Get-ChildItem -LiteralPath $Downloads -Force -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "stock_analysis_*.csv" } |
        Sort-Object LastWriteTime -Descending
    if ($CsvCandidates.Count -eq 0) {
        throw "stock_analysis CSV was not found under: $Downloads"
    }
    $CsvPath = $CsvCandidates[0].FullName
    Write-Host "CsvPath was not specified. Using latest candidate: $CsvPath"
}
if (-not (Test-Path -LiteralPath $CsvPath)) {
    throw "Input CSV was not found: $CsvPath"
}

$Key = $KeyCandidates[0].FullName
$Source = $PSScriptRoot

& scp.exe -i $Key `
    "$Source\daily_path_feature_enricher.py" `
    "$Source\requirements.txt" `
    "${Server}:/tmp/"
if ($LASTEXITCODE -ne 0) {
    throw "Program upload failed."
}

& scp.exe -i $Key "$CsvPath" "${Server}:/tmp/stock_analysis_input.csv"
if ($LASTEXITCODE -ne 0) {
    throw "CSV upload failed."
}

$RemoteScript = @'
set -e
APP=/opt/stock-daily-path-features
DATA=/var/lib/stock-daily-path-features
SERVICE=stock-daily-path-features.service
TIMER=stock-daily-path-features.timer

sudo mkdir -p "$APP" "$DATA/input" "$DATA/output" "$DATA/cache" /var/log/stock-daily-path-features
if systemctl list-units --full --all | grep -q 'stock-daily-path-features.service'; then
    sudo systemctl stop stock-daily-path-features.service || true
fi
sudo install -o app -g app -m 0755 /tmp/daily_path_feature_enricher.py "$APP/daily_path_feature_enricher.py"
sudo install -o app -g app -m 0644 /tmp/requirements.txt "$APP/requirements.txt"
sudo install -o app -g app -m 0644 /tmp/stock_analysis_input.csv "$DATA/input/stock_analysis_input.csv"

if [ ! -x "$APP/venv/bin/python" ]; then
    sudo python3 -m venv "$APP/venv"
    sudo chown -R app:app "$APP/venv"
fi
sudo -u app "$APP/venv/bin/python" -m pip install --upgrade pip wheel
sudo -u app "$APP/venv/bin/python" -m pip install -r "$APP/requirements.txt"

sudo tee "/etc/systemd/system/$SERVICE" >/dev/null <<EOF
[Unit]
Description=Add yfinance daily path features to stock_analysis CSV
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=app
Group=app
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/daily_path_feature_enricher.py --input $DATA/input/stock_analysis_input.csv --output $DATA/output/stock_analysis_with_daily_path.csv --cache-dir $DATA/cache --only-detected --progress-every 50
StandardOutput=append:/var/log/stock-daily-path-features/run.log
StandardError=append:/var/log/stock-daily-path-features/run.log
EOF

sudo tee "/etc/systemd/system/$TIMER" >/dev/null <<EOF
[Unit]
Description=Run stock daily path feature enrichment every morning

[Timer]
OnCalendar=*-*-* 06:20:00
Persistent=true
Unit=$SERVICE

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$TIMER"
sudo systemctl status "$TIMER" --no-pager
'@

& ssh.exe -t -i $Key $Server $RemoteScript
if ($LASTEXITCODE -ne 0) {
    throw "Remote deployment failed."
}

if ($RunOnce) {
    & ssh.exe -t -i $Key $Server "sudo systemctl start stock-daily-path-features.service; sleep 3; sudo systemctl status stock-daily-path-features.service --no-pager; tail -n 40 /var/log/stock-daily-path-features/run.log"
    if ($LASTEXITCODE -ne 0) {
        throw "Remote run failed."
    }
}

Write-Host ""
Write-Host "Installed."
Write-Host "Output CSV on VPS: /var/lib/stock-daily-path-features/output/stock_analysis_with_daily_path.csv"
Write-Host "Log on VPS       : /var/log/stock-daily-path-features/run.log"
