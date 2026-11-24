@echo off
setlocal
set "SENDTOCOPYQ_DATA_DIR=%LOCALAPPDATA%\SendToCopyQ-PairingTest"
start "" "%~dp0SendToCopyQ\SendToCopyQ.exe"
