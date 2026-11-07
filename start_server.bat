@echo off
cd /d "%~dp0"
python -c "import openpyxl, xlrd" >nul 2>nul
if errorlevel 1 (
  echo Excel import requires openpyxl and xlrd. Installing the listed Python dependencies...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Could not install dependencies. Run: python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)
python server.py
pause
