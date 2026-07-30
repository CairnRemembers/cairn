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

# 2. PyTorch - install the CPU-only build first so a normal Windows install stays
#    ~1.5 GB instead of pulling a ~4.6 GB CUDA stack a CPU box never uses. Any
#    existing torch (incl. a GPU build) is preserved.
# relax EAP here: a failing torch probe (or a missing CPU wheel) writes to stderr and
# would otherwise abort the whole installer under $ErrorActionPreference='Stop'.
$eapSave = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
& $py -c "import torch" 2>$null
$torchPresent = ($LASTEXITCODE -eq 0)
if ($torchPresent) {
    Write-Host "  pytorch: existing install detected - keeping it (no CPU override)"
} else {
    Write-Host "  pytorch: installing CPU-only build (GPU? pre-install your torch, then re-run)..."
    & $py -X utf8 -m pip install torch --index-url https://download.pytorch.org/whl/cpu
    $cpuRc = $LASTEXITCODE
    if ($cpuRc -ne 0) {
        if ($env:CAIRN_ALLOW_CUDA -eq '1') {
            Write-Host "  pytorch: CPU-only install failed; CAIRN_ALLOW_CUDA=1 set - continuing (step 3 may pull a large CUDA build)" -ForegroundColor DarkYellow
        } else {
            $ErrorActionPreference = $eapSave
            Write-Host ""
            Write-Host "  ERROR: CPU-only PyTorch install failed. Stopping here so the next step does" -ForegroundColor Red
            Write-Host "  NOT silently pull a multi-GB CUDA build. Choose one, then re-run:" -ForegroundColor Red
            Write-Host "    - restore access to https://download.pytorch.org/whl/cpu, or"
            Write-Host "    - pre-install the torch you want (it will be kept), or"
            Write-Host "    - accept the default (possibly CUDA) torch with the opt-in:"
            Write-Host "        `$env:CAIRN_ALLOW_CUDA='1'; .\install.ps1"
            exit 1
        }
    }
}
$ErrorActionPreference = $eapSave

# 3. editable install + all extras (embedder + dashboard)
Write-Host "  installing cairn + dependencies (first run downloads PyTorch - a few minutes)..."
& $py -X utf8 -m pip install -e ".[all]"
if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: install failed - see output above." -ForegroundColor Red; exit 1 }

# 4. verify — the vault auto-creates on this first call
Write-Host ""
& $py -X utf8 -m cairn doctor

# 5. next steps
Write-Host ""
Write-Host "  next steps:" -ForegroundColor DarkYellow
Write-Host "    python -X utf8 -m cairn setup         # turn on memory (recommended) - handles Claude + Codex"
Write-Host "    python -X utf8 -m cairn dashboard     # the brain at http://localhost:7331"
Write-Host "    python -X utf8 -m cairn note 'first signal'"
Write-Host ""
Write-Host "  build knowledge. leave signals." -ForegroundColor DarkGray
Write-Host ""
