param(
    [switch]$Install,
    [switch]$Coverage
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "No existe .venv\Scripts\python.exe. Sigue la instalacion de README.md."
}

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "ClaseViva"
$BaseTemp = Join-Path $TempRoot "dev-checks-$PID-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$PreviousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$PreviousHypothesis = $env:HYPOTHESIS_STORAGE_DIRECTORY
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:HYPOTHESIS_STORAGE_DIRECTORY = Join-Path $BaseTemp "hypothesis"
Push-Location $Root
try {
    if ($Install) {
        & $Python -m pip install -r .\requirements-dev.txt
        if ($LASTEXITCODE -ne 0) { throw "pip install fallo con codigo $LASTEXITCODE" }
    }

    $LintTargets = @("src", "scripts", "tests")
    & $Python -m ruff check --no-cache @LintTargets
    if ($LASTEXITCODE -ne 0) { throw "ruff fallo con codigo $LASTEXITCODE" }

    New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
    & $Python -m pytest tests -q -p no:cacheprovider `
        --timeout=90 --timeout-method=thread --basetemp $BaseTemp
    if ($LASTEXITCODE -ne 0) { throw "pytest fallo con codigo $LASTEXITCODE" }

    if ($Coverage) {
        $CoverageBaseTemp = "$BaseTemp-coverage"
        & $Python -m pytest tests -q -p no:cacheprovider `
            --timeout=90 --timeout-method=thread `
            --basetemp $CoverageBaseTemp `
            --cov=src --cov-report=term-missing:skip-covered `
            --cov-report=html:_debug\coverage_html
        if ($LASTEXITCODE -ne 0) { throw "pytest con cobertura fallo con codigo $LASTEXITCODE" }
    }

    $SmokeTest = Join-Path $Root "tests\test_smoke.py"
    if (Test-Path $SmokeTest) {
        & $Python $SmokeTest
        if ($LASTEXITCODE -ne 0) { throw "test_smoke.py fallo con codigo $LASTEXITCODE" }
    } else {
        Write-Warning "tests\test_smoke.py no existe. Pytest discovery ya se ejecuto; registrar la ausencia como baseline reparable."
    }
} finally {
    Pop-Location
    $env:PYTHONDONTWRITEBYTECODE = $PreviousNoBytecode
    $env:HYPOTHESIS_STORAGE_DIRECTORY = $PreviousHypothesis
    $ExpectedRoot = [System.IO.Path]::GetFullPath($TempRoot).TrimEnd("\") + "\"
    foreach ($Candidate in @($BaseTemp, "$BaseTemp-coverage")) {
        if (-not (Test-Path -LiteralPath $Candidate)) { continue }
        $Resolved = (Resolve-Path -LiteralPath $Candidate).Path
        if ($Resolved.StartsWith($ExpectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $Resolved -Recurse -Force
        }
    }
}
