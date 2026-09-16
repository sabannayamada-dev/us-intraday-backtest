param(
    [string]$InputCsv = "",
    [string]$Benchmark = "ACWI",
    [ValidateSet("linear", "pattern")]
    [string]$Model = "linear",
    [ValidateSet("drawdown", "return")]
    [string]$LinearObjective = "drawdown",
    [double]$MaxDepth = 50.0,
    [int]$MinTrades = 20,
    [double]$MinFillRate = 0.20,
    [double]$MinStep = 0.25,
    [int]$StableRounds = 3,
    [double]$LinearMaxWeight = 15.0,
    [int]$LinearRandomTrials = 24,
    [double]$LinearRegularization = 2.0,
    [double]$DrawdownRidgeAlpha = 20.0,
    [string]$TrainEndDate = "",
    [string]$TestStartDate = "",
    [int]$Seed = 20260722,
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$VenvDir = Join-Path $Root ".venv"
$OutputDir = Join-Path $Root "local_data\backtest"
$CacheDir = Join-Path $Root "local_data\backtest_price_cache"
$LogDir = Join-Path $Root "local_data\logs"
New-Item -ItemType Directory -Force -Path $OutputDir, $CacheDir, $LogDir | Out-Null

$UsingDefaultInputCsv = -not $InputCsv
if ($UsingDefaultInputCsv) {
    $InputCsv = Join-Path $Root "local_data\output\daily_path_81_pattern_classified.csv"
}
if (-not (Test-Path -LiteralPath $InputCsv)) {
    throw "Input classified CSV was not found: $InputCsv"
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

function Test-PythonHasCoreDeps {
    param([string]$PythonPath)
    try {
        & $PythonPath -c "import pandas, numpy" *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
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
    if (-not (Test-PythonHasCoreDeps $Python)) {
        if ((Test-Path -LiteralPath $BundledPython) -and (Test-PythonHasCoreDeps $BundledPython)) {
            Write-Host "Local .venv lacks pandas/numpy. Using bundled Python directly: $BundledPython"
            $Python = $BundledPython
        }
        else {
            throw "Python lacks required packages: pandas, numpy. Run without -NoInstall, or install them with: python -m pip install pandas numpy"
        }
    }
}

if ($UsingDefaultInputCsv) {
    Write-Host "Refreshing classified CSV from latest daily-path features..."
    & $Python (Join-Path $Root "analyze_daily_path_81_patterns.py")
    if ($LASTEXITCODE -ne 0) {
        throw "analyze_daily_path_81_patterns.py failed."
    }
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogDir ("limit_backtest_$Timestamp.log")

$Args = @(
    (Join-Path $Root "limit_order_pattern_backtester.py"),
    "--input", $InputCsv,
    "--output-dir", $OutputDir,
    "--cache-dir", $CacheDir,
    "--benchmark", $Benchmark,
    "--model", $Model,
    "--linear-objective", $LinearObjective,
    "--max-depth", "$MaxDepth",
    "--min-trades", "$MinTrades",
    "--min-fill-rate", "$MinFillRate",
    "--min-step", "$MinStep",
    "--stable-rounds", "$StableRounds",
    "--linear-max-weight", "$LinearMaxWeight",
    "--linear-random-trials", "$LinearRandomTrials",
    "--linear-regularization", "$LinearRegularization",
    "--drawdown-ridge-alpha", "$DrawdownRidgeAlpha",
    "--seed", "$Seed"
)
if ($TrainEndDate) {
    $Args += @("--train-end-date", $TrainEndDate)
}
if ($TestStartDate) {
    $Args += @("--test-start-date", $TestStartDate)
}

Write-Host "Input    : $InputCsv"
Write-Host "Benchmark: $Benchmark"
Write-Host "Model    : $Model"
Write-Host "Objective: $LinearObjective"
if ($TrainEndDate -or $TestStartDate) {
    Write-Host "Split    : train <= $TrainEndDate / test >= $TestStartDate"
}
Write-Host "Output   : $OutputDir"
Write-Host "Cache    : $CacheDir"
Write-Host "Log      : $LogPath"

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
    throw "limit_order_pattern_backtester.py failed. See log: $LogPath"
}

Write-Host ""
Write-Host "Done."
Write-Host "Best settings : $(Join-Path $OutputDir 'pattern_limit_best.csv')"
Write-Host "Trial history : $(Join-Path $OutputDir 'pattern_limit_trials.csv')"
Write-Host "Trade details : $(Join-Path $OutputDir 'pattern_limit_trades.csv')"
Write-Host "Report        : $(Join-Path $OutputDir 'pattern_limit_report.txt')"
Write-Host "Linear model  : $(Join-Path $OutputDir 'linear_limit_model.csv')"
Write-Host "Linear trials : $(Join-Path $OutputDir 'linear_limit_trials.csv')"
Write-Host "Linear events : $(Join-Path $OutputDir 'linear_limit_events.csv')"
Write-Host "Linear trades : $(Join-Path $OutputDir 'linear_limit_trades.csv')"
Write-Host "Drawdown pred : $(Join-Path $OutputDir 'linear_drawdown_predictions.csv')"
Write-Host "Linear report : $(Join-Path $OutputDir 'linear_limit_report.txt')"
Write-Host "Log           : $LogPath"
