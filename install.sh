#!/usr/bin/env bash
# Cairn installer (macOS / Linux) — build knowledge, leave signals.
# Run from the cairn folder:  ./install.sh
# Installs the package + dependencies, then verifies with `cairn doctor`.
set -euo pipefail
cd "$(dirname "$0")"   # the repo root (where pyproject.toml lives)

echo ""
echo "  CAIRN // foundation for lost knowledge"
echo "  laying the first stone..."
echo ""

# 1. find a Python 3.11+
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PY="$c"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "  ERROR: Python 3.11+ not found. Install it, then re-run." >&2
    exit 1
fi
echo "  python: $($PY --version)"

# 2. PyTorch - install the CPU-only build first on Linux/WSL so a normal install
#    stays ~1.5 GB instead of pulling a ~4.6 GB CUDA stack a CPU box never uses.
#    Any existing torch (incl. a GPU build) is preserved. macOS already ships a
#    lean CPU/MPS wheel by default, so it is left to the normal path in step 3.
if "$PY" -c 'import torch' >/dev/null 2>&1; then
    echo "  pytorch: existing install detected - keeping it (no CPU override)"
elif [ "$(uname -s)" = "Linux" ]; then
    echo "  pytorch: installing CPU-only build (GPU? pre-install your torch, then re-run)..."
    if ! "$PY" -m pip install torch --index-url https://download.pytorch.org/whl/cpu; then
        if [ "${CAIRN_ALLOW_CUDA:-}" = "1" ]; then
            echo "  pytorch: CPU-only install failed; CAIRN_ALLOW_CUDA=1 set - continuing (step 3 may pull a large CUDA build)" >&2
        else
            echo "" >&2
            echo "  ERROR: CPU-only PyTorch install failed. Stopping here so the next step does" >&2
            echo "  NOT silently pull a multi-GB CUDA build. Choose one, then re-run:" >&2
            echo "    - restore access to https://download.pytorch.org/whl/cpu, or" >&2
            echo "    - pre-install the torch you want (it will be kept), or" >&2
            echo "    - accept the default (possibly CUDA) torch with the opt-in:" >&2
            echo "        CAIRN_ALLOW_CUDA=1 ./install.sh" >&2
            exit 1
        fi
    fi
else
    echo "  pytorch: leaving the default install path for this OS ($(uname -s))"
fi

# 3. editable install + all extras (embedder + dashboard)
echo "  installing cairn + dependencies (first run downloads PyTorch - a few minutes)..."
_log="$(mktemp)"
if ! "$PY" -m pip install -e ".[all]" 2>&1 | tee "$_log"; then
    echo "" >&2
    if grep -q "externally-managed-environment" "$_log"; then
        echo "  install failed: your system Python is protected by PEP 668 (common on" >&2
        echo "  Ubuntu/Debian/Fedora, Homebrew, and WSL). Install into a virtual" >&2
        echo "  environment instead, then re-run:" >&2
        echo "" >&2
        echo "      python3 -m venv .venv && source .venv/bin/activate" >&2
        echo "      ./install.sh" >&2
    else
        echo "  install failed - see the output above." >&2
    fi
    rm -f "$_log"
    exit 1
fi
rm -f "$_log"

# 4. verify — the vault auto-creates on this first call
echo ""
"$PY" -m cairn doctor

# 5. next steps
echo ""
echo "  next steps:"
echo "    python3 -m cairn setup         # turn on memory (recommended) - handles Claude + Codex"
echo "    python3 -m cairn dashboard     # the brain at http://localhost:7331"
echo "    python3 -m cairn note 'first signal'"
echo ""
echo "  build knowledge. leave signals."
echo ""
