#!/usr/bin/env bash
set -e

echo "=========================================="
echo " GATE CSE Tracker — Automated Setup"
echo "=========================================="

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_DIR="$SCRIPT_DIR/tracker_app"

echo "[1/3] Checking Python 3..."
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 could not be found. Please install Python 3.8+."
    exit 1
fi

echo "[2/3] Setting up Python virtual environment..."
python3 -m venv "$APP_DIR/.venv"
source "$APP_DIR/.venv/bin/activate"

echo "[3/3] Installing required packages (PyMuPDF, ReportLab)..."
pip install --upgrade pip
pip install -r "$APP_DIR/requirements.txt"

# Check for Tkinter
python3 -c "import tkinter" 2>/dev/null || {
    echo ""
    echo "--------------------------------------------------------"
    echo "WARNING: Tkinter GUI module is missing on your system!"
    echo "If you are on Debian/Ubuntu/Mint, install it with:"
    echo "    sudo apt update && sudo apt install python3-tk"
    echo "If you are on Fedora, install it with:"
    echo "    sudo dnf install python3-tkinter"
    echo "--------------------------------------------------------"
    echo ""
}

echo "=========================================="
echo " Setup complete!"
echo " To start the app, run: ./run.sh"
echo "=========================================="
