#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "[1/3] Creating virtual environment..."
python3 -m venv "$VENV_DIR"

echo "[2/3] Installing dependencies..."
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "[3/3] Adding to PATH..."

if [ -f "$HOME/.zshrc" ]; then
    RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    RC="$HOME/.bashrc"
else
    RC="$HOME/.profile"
fi

if grep -qF "$SCRIPT_DIR" "$RC" 2>/dev/null; then
    echo "Already in PATH ($RC), skipping."
else
    echo "" >> "$RC"
    echo "# gitscan" >> "$RC"
    echo "export PATH=\"\$PATH:$SCRIPT_DIR\"" >> "$RC"
    echo "Added to $RC."
    echo ">>> Run: source $RC"
fi

echo ""
echo "Done! Run 'gitscan' from any git repository."
