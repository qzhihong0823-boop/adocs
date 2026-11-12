@echo off
chcp 65001 >nul
title WINDO AI 스마트 가상 창문 서버
cd /d "%~dp0"

echo ========================================================
echo   🪟 WINDO AI 스마트 가상 창문 서버를 시작합니다...
echo   잠시 후 웹 브라우저(http://127.0.0.1:5001)가 자동으로 열립니다.
echo ========================================================

REM 기존에 5001 포트를 점유 중인 이전 서버가 있으면 깔끔하게 종료 후 새로 시작 (항상 기본 대기 화면에서 시작 보장)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5001 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

set WINDO_AUTO_BROWSER=1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)
pause
