@echo off
cd /d "%~dp0\.."
set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo Building Optiview backend (console EXE)...
"%PY%" -m pip install -r requirements.txt pyinstaller
"%PY%" -m PyInstaller --noconfirm --clean --console --onefile --name OptiviewBackend --collect-submodules v380 --add-data "audio;audio" optiview_backend.py
echo.
echo Backend EXE: dist\OptiviewBackend.exe
echo Use the Flutter app as the client UI.
pause
