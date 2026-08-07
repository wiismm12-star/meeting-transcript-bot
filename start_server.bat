@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo [清理] 關閉所有舊的 transcript_bot 伺服器...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*transcript_bot.web*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" 2>nul
timeout /t 2 >nul

echo [清理] 確認 port 8765 已釋放...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 >nul

echo [啟動] 啟動 transcript_bot 伺服器 (http://127.0.0.1:8765)...
.venv\Scripts\python.exe -m transcript_bot.web
