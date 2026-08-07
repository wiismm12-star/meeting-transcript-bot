@echo off
REM 以完全脫離方式啟動 keep_server_alive 看門狗，使其不依賴 Hermes 終端機存活。
cd /d "C:\Users\lan\Documents\開會語音逐字稿"
start "" /B ".venv\Scripts\python.exe" keep_server_alive.py
echo watchdog launched via start /B
