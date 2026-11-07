#!/usr/bin/env bash
cd "$(dirname "$0")"
if ! python3 -c 'import openpyxl, xlrd' >/dev/null 2>&1; then
  echo "Installing the Python dependencies required for XLS/XLSX import..."
  python3 -m pip install -r requirements.txt || { echo "Could not install dependencies. Run: python3 -m pip install -r requirements.txt"; exit 1; }
fi
python3 server.py
