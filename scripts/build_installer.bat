@echo off
cd /d "%~dp0\.."
set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo Building OptiviewBackend.exe ...
"%PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

"%PY%" -m PyInstaller --noconfirm --clean --console --name OptiviewBackend --collect-submodules v380 --add-data "audio;audio" optiview_backend.py
if errorlevel 1 goto :fail

echo Compiling Inno installer ...
set ISCC=
if exist "C:\Program Files\Inno Setup 7\ISCC.exe" set ISCC=C:\Program Files\Inno Setup 7\ISCC.exe
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" if not defined ISCC set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not defined ISCC (
  echo Inno Setup compiler not found. Install Inno Setup 6/7, or open installer\Optiview.iss manually.
  goto :fail
)
"%ISCC%" "installer\Optiview.iss"
if errorlevel 1 goto :fail

echo.
echo Installer: installer\OptiviewSetup.exe
echo Folder EXE: dist\OptiviewBackend\OptiviewBackend.exe
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
