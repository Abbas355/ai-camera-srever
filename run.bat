@echo off
cd /d "%~dp0"
"%LocalAppData%\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt
"%LocalAppData%\Programs\Python\Python312\python.exe" app.py
pause
