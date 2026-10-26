@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python environment not found: %~dp0.venv\Scripts\python.exe
  echo Run install.cmd first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m material_library serve %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] The server stopped with exit code %EXIT_CODE%.
  echo Keep this window open and read the error above.
  pause
)
exit /b %EXIT_CODE%
