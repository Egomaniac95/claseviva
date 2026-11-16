param(
    [string]$OutputDirectory = "",
    [string]$BuildPython = "",
    [switch]$KeepWork
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if ($env:OS -ne "Windows_NT") { throw "Esta distribución requiere Windows x64." }
if (-not $BuildPython) { $BuildPython = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path ([IO.Directory]::GetParent($Root).FullName) "ClaseViva-Windows"
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $OutputDirectory) {
    throw "La salida ya existe. Elige otra carpeta vacía/nueva; no se sobrescriben clases o claves."
}
& $BuildPython -B -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8, 'Se requiere Python 3.12 x64'"
if ($LASTEXITCODE -ne 0) { throw "Intérprete de build no compatible." }
$Work = Join-Path ([IO.Path]::GetTempPath()) ("ClaseViva\build-" + [Guid]::NewGuid().ToString('N'))
$EnvDir = Join-Path $Work "venv"
$Python = Join-Path $EnvDir "Scripts\python.exe"
New-Item -ItemType Directory -Path $Work -Force | Out-Null
Write-Host "Build aislado: $Work"
& $BuildPython -B -m venv $EnvDir
if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el entorno de build." }
& $Python -m pip install --only-binary=:all: -r (Join-Path $Root "requirements-windows.lock") -r (Join-Path $Root "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "No se pudo instalar el entorno fijado; se conserva $Work para diagnóstico." }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Dependencias incompatibles." }
$PreviousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = "1"
$BuildSucceeded = $false
try {
    & $Python -B -m PyInstaller --noconfirm --distpath $OutputDirectory --workpath (Join-Path $Work "analysis") (Join-Path $Root "ClaseViva.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló. No distribuir; revisar $Work." }
    $Bundle = Join-Path $OutputDirectory "ClaseViva"
    Copy-Item -LiteralPath (Join-Path $Root ".env.example") -Destination (Join-Path $Bundle ".env.example")
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Configurar claves.cmd") -Destination $Bundle
    Copy-Item -LiteralPath (Join-Path $Root "WINDOWS_PORTABLE.md") -Destination (Join-Path $Bundle "LEEME.md")
    & $Python -B (Join-Path $PSScriptRoot "collect_windows_notices.py") $Bundle (Join-Path $Root "requirements-windows.lock")
    if ($LASTEXITCODE -ne 0) { throw "No se pudieron conservar avisos de terceros." }
    & $Python -B (Join-Path $PSScriptRoot "verify_windows_bundle.py") $Bundle
    if ($LASTEXITCODE -ne 0) { throw "El paquete no superó el diagnóstico offline; no distribuir." }
    Write-Host "BUILD_OK: $Bundle"
    $BuildSucceeded = $true
} finally {
    $env:PYTHONDONTWRITEBYTECODE = $PreviousNoBytecode
    if ($BuildSucceeded -and -not $KeepWork) {
        $TemporaryRoot = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) "ClaseViva"))
        $ResolvedWork = [IO.Path]::GetFullPath($Work)
        if ([IO.Directory]::GetParent($ResolvedWork).FullName -ne $TemporaryRoot -or
            [IO.Path]::GetFileName($ResolvedWork) -notmatch '^build-[a-f0-9]{32}$') {
            throw "La carpeta temporal no está dentro de la raíz de build esperada."
        }
        Remove-Item -LiteralPath $ResolvedWork -Recurse -Force
        Write-Host "Entorno de build temporal eliminado."
    } elseif (Test-Path -LiteralPath $Work) {
        Write-Host "Entorno conservado para diagnóstico: $Work"
    }
}
