@echo off
cd /d "%~dp0\.."
set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo Building V380Studio.exe ...
"%PY%" -m pip install -r requirements.txt pyinstaller
"%PY%" -m PyInstaller --noconfirm --clean --windowed --name V380Studio --collect-submodules v380 --add-data "audio;audio" app.py
if errorlevel 1 goto :fail

echo Compiling Inno installer ...
"C:\Program Files\Inno Setup 7\ISCC.exe" "installer\V380Studio.iss"
if errorlevel 1 goto :fail

echo.
echo Installer: installer\V380StudioSetup.exe
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
