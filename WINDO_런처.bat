@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "launcher.py"
) else (
    start "" python "launcher.py"
)
exit
