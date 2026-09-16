param(
    [string]$CsvPath = "",
    [string]$OutputDir = "",
    [string[]]$FearIndexCsv = @(),
    [int]$FearMaxLagDays = 7,
    [switch]$AllRows,
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
if (-not $OutputDir) {
    $OutputDir = Join-Path $Root "local_data\output"
}
$InputDir = Join-Path $Root "local_data\input"
$CacheDir = Join-Path $Root "local_data\cache"
$LogDir = Join-Path $Root "local_data\logs"
$VenvDir = Join-Path $Root ".venv"

New-Item -ItemType Directory -Force -Path $InputDir, $OutputDir, $CacheDir, $LogDir | Out-Null

if (-not $CsvPath) {
    $Downloads = Join-Path $env:USERPROFILE "Downloads"
    $Candidates = Get-ChildItem -LiteralPath $Downloads -Force -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "stock_analysis_*.csv" } |
        Sort-Object @{Expression = { if ($_.Name -like "*全部入り*") { 0 } else { 1 } }}, LastWriteTime -Descending
    if ($Candidates.Count -eq 0) {
        throw "stock_analysis_*.csv was not found under: $Downloads"
    }
    $CsvPath = $Candidates[0].FullName
    Write-Host "CsvPath was not specified. Using: $CsvPath"
}
if (-not (Test-Path -LiteralPath $CsvPath)) {
    throw "Input CSV was not found: $CsvPath"
}

function Get-BasePython {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            & $py.Source -3 -c "import sys; print(sys.executable)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return @($py.Source, "-3")
            }
        }
        catch {}
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        try {
            & $python.Source -c "import sys; print(sys.executable)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return @($python.Source)
            }
        }
        catch {}
    }
    $BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $BundledPython) {
        return @($BundledPython)
    }
    throw "Python was not found. Install Python 3 or make python/py available in PATH."
}

function New-LocalVenv {
    $BasePython = @(Get-BasePython)
    $BaseExe = $BasePython[0]
    $BaseArgs = @()
    if ($BasePython.Count -gt 1) {
        $BaseArgs = $BasePython[1..($BasePython.Count - 1)]
    }
    & $BaseExe @BaseArgs -m venv $VenvDir
}

$BasePython = @(Get-BasePython)
$BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$UseBundledDirect = ($BasePython.Count -eq 1 -and $BasePython[0] -eq $BundledPython)

if ($UseBundledDirect) {
    $Python = $BundledPython
    Write-Host "Using bundled Python directly: $Python"
}
else {
    if (-not (Test-Path -LiteralPath (Join-Path $VenvDir "Scripts\python.exe"))) {
        New-LocalVenv
    }

    $Python = Join-Path $VenvDir "Scripts\python.exe"
    $VenvWorks = $false
    try {
        & $Python -c "import sys; print(sys.executable)" *> $null
        $VenvWorks = ($LASTEXITCODE -eq 0)
    }
    catch {
        $VenvWorks = $false
    }
    if (-not $VenvWorks) {
        Write-Host "Existing .venv is broken. Recreating: $VenvDir"
        $ResolvedVenv = [System.IO.Path]::GetFullPath($VenvDir)
        $ResolvedRoot = [System.IO.Path]::GetFullPath($Root)
        if (-not $ResolvedVenv.StartsWith($ResolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to recreate venv outside project root: $ResolvedVenv"
        }
        if (Test-Path -LiteralPath $VenvDir) {
            Remove-Item -LiteralPath $VenvDir -Recurse -Force
        }
        New-LocalVenv
        $Python = Join-Path $VenvDir "Scripts\python.exe"
    }
    if (-not $NoInstall) {
        & $Python -m pip install --upgrade pip wheel
        & $Python -m pip install -r (Join-Path $Root "requirements.txt")
    }
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$InputCopy = Join-Path $InputDir ("stock_analysis_input_$Timestamp.csv")
$OutputPath = Join-Path $OutputDir ("stock_analysis_with_daily_path_$Timestamp.csv")
$LatestPath = Join-Path $OutputDir "stock_analysis_with_daily_path_latest.csv"
$LogPath = Join-Path $LogDir ("run_$Timestamp.log")

Copy-Item -LiteralPath $CsvPath -Destination $InputCopy -Force

$Args = @(
    (Join-Path $Root "daily_path_feature_enricher.py"),
    "--input", $InputCopy,
    "--output", $OutputPath,
    "--cache-dir", $CacheDir,
    "--progress-every", "25",
    "--fear-max-lag-days", "$FearMaxLagDays"
)
if (-not $AllRows) {
    $Args += "--only-detected"
}
foreach ($FearSpec in $FearIndexCsv) {
    if ($FearSpec) {
        $FearPathText = $FearSpec
        if ($FearSpec.Contains("=")) {
            $FearPathText = $FearSpec.Substring($FearSpec.IndexOf("=") + 1)
        }
        $FearPathText = $FearPathText.Trim().Trim('"')
        $IsYFinanceSource = $FearPathText -match "^(?i:yf|yfinance):"
        if ((-not $IsYFinanceSource) -and -not (Test-Path -LiteralPath $FearPathText)) {
            $Downloads = Join-Path $env:USERPROFILE "Downloads"
            $Nearby = Get-ChildItem -LiteralPath $Downloads -Force -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match "vxj|vix|vi|nikkei|日経|fear" -or $_.Extension -eq ".csv" } |
                Sort-Object LastWriteTime -Descending |
                Select-Object -First 12 -ExpandProperty FullName
            $Hint = ""
            if ($Nearby) {
                $Hint = "`nDownloads内の候補:`n  " + ($Nearby -join "`n  ")
            }
            throw "FearIndexCsv source was not found: $FearPathText`nUse an existing CSV path, or use yfinance for VIX like: -FearIndexCsv `"VIX=yf:^VIX`"$Hint"
        }
        $Args += @("--fear-index-csv", $FearSpec)
    }
}

Write-Host "Input : $InputCopy"
Write-Host "Output: $OutputPath"
Write-Host "Cache : $CacheDir"
if ($FearIndexCsv.Count -gt 0) {
    Write-Host "Fear  : $($FearIndexCsv -join ', ')"
}
Write-Host "Log   : $LogPath"

$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Python @Args 2>&1 | Tee-Object -FilePath $LogPath
    $ExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
}
if ($ExitCode -ne 0) {
    throw "daily_path_feature_enricher.py failed. See log: $LogPath"
}

Copy-Item -LiteralPath $OutputPath -Destination $LatestPath -Force

Write-Host ""
Write-Host "Done."
Write-Host "Output latest: $LatestPath"
Write-Host "Output dated : $OutputPath"
Write-Host "Log          : $LogPath"
