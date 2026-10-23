@echo off
cd /d "%~dp0\.."
echo Starting Optiview backend in WSL...
wsl -e bash -lc "cd ~/optiview 2>/dev/null || cd '%CD:\=/%' ; bash linux/wsl-start.sh"
pause
