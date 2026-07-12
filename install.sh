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

# 2. editable install + all extras (embedder + dashboard)
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

# 3. verify — the vault auto-creates on this first call
echo ""
"$PY" -m cairn doctor

# 4. next steps
echo ""
echo "  next steps:"
echo "    python3 -m cairn dashboard    # the brain at http://localhost:7331"
echo "    python3 -m cairn connect       # ambient capture (optional, off by default)"
echo "    python3 -m cairn note 'first signal'"
echo ""
echo "  build knowledge. leave signals."
echo ""
