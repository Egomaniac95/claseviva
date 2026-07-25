# init.ps1 - preflight local, read-only y compatible con Windows PowerShell 5.1.
param(
    [switch]$WithNetwork,
    [switch]$RecordResult
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$fallo = $false

function Falla($msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; $script:fallo = $true }
function Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Aviso($msg) { Write-Host "[AVISO] $msg" -ForegroundColor Yellow }

Write-Host "=== Preflight de instalacion ClaseViva ==="

foreach ($f in @("README.md", "PROJECT_STATE.md", "requirements.txt")) {
    if (Test-Path (Join-Path $root $f)) { Ok "$f presente" }
    else { Falla "No se encuentra $f en la raiz." }
}
foreach ($d in @("src", "tests")) {
    if (Test-Path (Join-Path $root $d)) { Ok "carpeta $d presente" }
    else { Falla "Estructura rota: falta carpeta /$d" }
}

$py312 = $null
try { $py312 = & py -3.12 --version 2>$null } catch {}
if ($py312) { Ok "Python 3.12 disponible: $py312" }
else { Falla "Python 3.12 no está disponible; no se modifica Python global." }

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Falla "No existe .venv; créalo explícitamente e instala requirements.txt."
} else {
    $version = & $venvPy --version
    if ($version -match "3\.12") { Ok ".venv es $version" }
    else { Falla ".venv debe usar Python 3.12 (actual: $version)." }

    $imports = & $venvPy -c "import flask, httpx, latex2mathml, markdown, mss, numpy, openai, PIL, pyaudiowpatch, send2trash; print('DEPS_OK')" 2>$null
    if ("$imports" -match "DEPS_OK") { Ok "runtime base importable" }
    else { Falla "Faltan dependencias base; instala requirements.txt únicamente en .venv." }

}

$envFile = Join-Path $root ".env"
$deepseekKey = $false
$sttKeys = @{}
if (Test-Path $envFile) {
    $deepseekKey = Select-String -Path $envFile -Pattern "^DEEPSEEK_API_KEY=.+" -Quiet
    foreach ($name in @("ASSEMBLYAI_API_KEY", "DEEPGRAM_API_KEY", "SPEECHMATICS_API_KEY", "GROQ_API_KEY")) {
        $sttKeys[$name] = Select-String -Path $envFile -Pattern ("^" + $name + "=.+") -Quiet
    }
}
if ($deepseekKey) { Ok "DEEPSEEK_API_KEY configurada (valor oculto)" }
else { Aviso "DEEPSEEK_API_KEY no detectada; resumen/copiloto requerirán configuración." }
$configuredStt = 0
foreach ($name in @("ASSEMBLYAI_API_KEY", "DEEPGRAM_API_KEY", "SPEECHMATICS_API_KEY", "GROQ_API_KEY")) {
    if ($sttKeys[$name]) { Ok "$name configurada (valor oculto)"; $configuredStt++ }
    else { Aviso "$name no detectada; ese proveedor se omitirá." }
}
if ($configuredStt -eq 0) { Falla "STT remoto requiere al menos una clave de proveedor en .env." }

if ($WithNetwork) {
    if ((Test-Path $venvPy) -and $deepseekKey) {
        $ping = & $venvPy -m src.deepseek_client --ping
        if ("$ping" -match "PING_OK") { Ok "DeepSeek responde" }
        else { Falla "DeepSeek no respondió al ping autorizado." }
    } else { Falla "-WithNetwork requiere .venv y DEEPSEEK_API_KEY." }
} else {
    Aviso "Ping DeepSeek omitido; -WithNetwork consume API y requiere autorización."
}

$smokeTest = Join-Path $root "tests\test_smoke.py"
if (-not (Test-Path $smokeTest)) {
    Falla "No existe tests\test_smoke.py; determina si es preexistente y repáralo si el alcance lo permite."
} elseif (Test-Path $venvPy) {
    $previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
    try {
        $env:PYTHONDONTWRITEBYTECODE = "1"
        & $venvPy $smokeTest
        if ($LASTEXITCODE -eq 0) { Ok "Smoke pasó" }
        else { Falla "test_smoke.py falló." }
    } finally {
        $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
    }
}

if ($RecordResult) {
    Aviso "-RecordResult no escribe historial; conserva la salida en el checkpoint de la tarea."
}
if ($fallo) {
    Write-Host "== BASELINE EN ROJO: clasifica el fallo; no se tocaron rutas protegidas. ==" -ForegroundColor Red
    exit 1
}
Write-Host "== Instalacion local lista. ==" -ForegroundColor Green
exit 0
