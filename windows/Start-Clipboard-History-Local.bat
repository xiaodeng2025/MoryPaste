@echo off
setlocal
chcp 65001 >nul
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Run setup.bat to prepare the local development environment.
    pause
    exit /b 1
)
"%PYTHON%" -c "import tkinter; import winrt.windows.applicationmodel.datatransfer; import winrt.windows.storage.streams" >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install -r "%~dp0requirements-clipboard-local.txt"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop_app.py" --clipboard-history-local
