@echo off
setlocal
chcp 65001 >nul

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo [Error] Python virtual environment not found.
  echo Run setup.bat in this windows folder to prepare it.
  pause
  exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [Error] The virtual environment is missing python.exe.
  echo Run setup.bat in this windows folder to repair it.
  pause
  exit /b 1
)

start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop_app.py"
exit /b 0
