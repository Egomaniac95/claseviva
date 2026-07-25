# Utilidad OPCIONAL. ClaseViva escucha en 127.0.0.1 y normalmente no necesita reglas
# de entrada. Ejecutar una sola vez como Administrador únicamente si Windows Firewall
# bloquea de forma comprobada las conexiones de Python de este entorno.
$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
$exes = @("$raiz\.venv\Scripts\python.exe", "$raiz\.venv\Scripts\pythonw.exe")

$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
    Write-Host "Este script debe correr como ADMINISTRADOR." -ForegroundColor Red
    Write-Host "Clic derecho > 'Ejecutar como administrador', o ábrelo desde una consola admin."
    exit 1
}

foreach ($exe in $exes) {
    if (-not (Test-Path $exe)) { Write-Host "(omito, no existe: $exe)"; continue }
    foreach ($dir in @("Inbound", "Outbound")) {
        $nombre = "clase-viva $dir $(Split-Path $exe -Leaf)"
        Get-NetFirewallRule -DisplayName $nombre -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName $nombre -Direction $dir -Program $exe `
            -Action Allow -Profile Any -Enabled True | Out-Null
        Write-Host "[OK] regla $dir para $(Split-Path $exe -Leaf)" -ForegroundColor Green
    }
}
Write-Host "`nListo. Ya no debería aparecer el diálogo de firewall durante la clase." -ForegroundColor Green
