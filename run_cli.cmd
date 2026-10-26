@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python environment not found: %~dp0.venv\Scripts\python.exe
  echo Run install.cmd first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m material_library %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Command stopped with exit code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
