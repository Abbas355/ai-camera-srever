@echo off
cd /d "%~dp0"
set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo Installing deps (if needed)...
"%PY%" -m pip install -r requirements.txt
echo.
echo Starting Optiview BACKEND (API on port 8080).
echo Logs also go to: data\server.log
echo Use the Flutter app as the UI.
echo Close this window or press Ctrl+C to stop (if this process owns the port).
echo.
"%PY%" app.py
echo.
echo Backend exited. See data\server.log for details.
pause
