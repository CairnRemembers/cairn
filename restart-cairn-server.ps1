# Restart Cairn Server — desktop button.
# Kills whatever holds port 7331, starts a fresh detached dashboard, waits until
# /garden genuinely answers, then closes. Stays open (with the reason) on failure.
$ErrorActionPreference = 'Stop'
$py    = "C:\Users\sinng\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$cairn = "C:\Users\sinng\OneDrive\Desktop\cairn"
$port  = 7331
$Host.UI.RawUI.WindowTitle = "Restart Cairn Server"
Write-Host ""
Write-Host "  Restarting Cairn server..." -ForegroundColor Cyan
try {
    # 1) stop the old server (anything listening on the port)
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        try { Stop-Process -Id $c -Force -ErrorAction SilentlyContinue; Write-Host "  Stopped old server (PID $c)" -ForegroundColor DarkGray } catch {}
    }
    Start-Sleep -Milliseconds 900

    # 2) start a fresh, detached dashboard (survives this window closing)
    $p = Start-Process -FilePath $py `
        -ArgumentList '-X','utf8','-m','cairn','dashboard',"--port=$port",'--no-browser' `
        -WorkingDirectory $cairn -WindowStyle Hidden -PassThru
    Write-Host "  Started new server (PID $($p.Id)). Waiting for it to answer..." -ForegroundColor DarkGray

    # 3) wait until /garden really responds
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $r = Invoke-WebRequest "http://localhost:$port/garden" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch {}
        Start-Sleep -Seconds 1
    }

    if ($ok) {
        Write-Host ""
        Write-Host "  Server is up  ->  http://localhost:$port/garden" -ForegroundColor Green
        Write-Host "  (this window closes on its own)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 2
    } else {
        Write-Host ""
        Write-Host "  Server did NOT come up after 60s." -ForegroundColor Red
        Write-Host "  Check that Python is installed and Cairn is at $cairn" -ForegroundColor Yellow
        Read-Host "  Press Enter to close"
    }
} catch {
    Write-Host ""
    Write-Host "  ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host "  Press Enter to close"
}
