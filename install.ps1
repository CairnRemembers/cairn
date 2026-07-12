# Cairn installer (Windows) — build knowledge, leave signals.
# Run from the cairn folder:  .\install.ps1
# Installs the package + dependencies, then verifies with `cairn doctor`.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot   # the repo root (where pyproject.toml lives)

Write-Host ""
Write-Host "  CAIRN // foundation for lost knowledge" -ForegroundColor DarkYellow
Write-Host "  laying the first stone..." -ForegroundColor DarkGray
Write-Host ""

# 1. find a Python 3.11+
$py = $null
foreach ($c in 'python', 'python3', 'py') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        $ok = & $cmd.Source -c "import sys; print(1 if sys.version_info>=(3,11) else 0)" 2>$null
        if ($ok -eq '1') { $py = $cmd.Source; break }
    }
}
if (-not $py) {
    Write-Host "  ERROR: Python 3.11+ not found. Install it from https://python.org and re-run." -ForegroundColor Red
    exit 1
}
Write-Host "  python: $py"

# 2. editable install + all extras (embedder + dashboard)
Write-Host "  installing cairn + dependencies (first run downloads PyTorch - a few minutes)..."
& $py -X utf8 -m pip install -e ".[all]"
if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: install failed - see output above." -ForegroundColor Red; exit 1 }

# 3. verify — the vault auto-creates on this first call
Write-Host ""
& $py -X utf8 -m cairn doctor

# 4. next steps
Write-Host ""
Write-Host "  next steps:" -ForegroundColor DarkYellow
Write-Host "    python -X utf8 -m cairn dashboard    # the brain at http://localhost:7331"
Write-Host "    python -X utf8 -m cairn connect       # ambient capture (optional, off by default)"
Write-Host "    python -X utf8 -m cairn note 'first signal'"
Write-Host ""
Write-Host "  build knowledge. leave signals." -ForegroundColor DarkGray
Write-Host ""
