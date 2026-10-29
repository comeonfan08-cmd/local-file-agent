@echo off
rem Local File Agent - Windows launcher
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found.
  echo Please install Python 3.10+ from https://www.python.org/downloads/
  echo Remember to check "Add Python to PATH" during installation.
  pause
  exit /b 1
)

python -c "import flask" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo Install failed. Run manually:
    echo   python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

echo Starting... browser will open http://127.0.0.1:8000
echo Close this window to stop the server.
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
python web.py
pause
