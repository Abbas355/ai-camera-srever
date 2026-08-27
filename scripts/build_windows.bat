@echo off
cd /d "%~dp0\.."
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name V380Studio app.py
echo.
echo EXE is in dist\V380Studio\V380Studio.exe
pause
