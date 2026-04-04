@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Virtual environment not found at ".venv". Run "scripts\bootstrap_windows.cmd" first.
  exit /b 1
)

set "XDG_DATA_HOME=%PROJECT_ROOT%.data"
set "TEMP=%PROJECT_ROOT%.tmp"
set "TMP=%PROJECT_ROOT%.tmp"
set "COQUI_TOS_AGREED=1"

if not exist "%XDG_DATA_HOME%" mkdir "%XDG_DATA_HOME%"
if not exist "%TEMP%" mkdir "%TEMP%"

"%PYTHON_EXE%" -m tts_win %*
exit /b %ERRORLEVEL%
