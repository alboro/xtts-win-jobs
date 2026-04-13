@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PROJECT_ROOT=%~dp0"
set "VENV_DIR=%PROJECT_ROOT%.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "VENV_CFG=%VENV_DIR%\pyvenv.cfg"
set "VENV_SITE_PACKAGES=%VENV_DIR%\Lib\site-packages"

if not exist "%PYTHON_EXE%" (
  echo Virtual environment not found at ".venv". Run "scripts\bootstrap_windows.cmd" first.
  exit /b 1
)

set "BASE_PYTHON="
if exist "%VENV_CFG%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%VENV_CFG%") do (
    set "CFG_KEY=%%~A"
    set "CFG_VALUE=%%~B"
    call :trim_var CFG_KEY
    call :trim_var CFG_VALUE
    if /I "!CFG_KEY!"=="executable" set "BASE_PYTHON=!CFG_VALUE!"
    if /I "!CFG_KEY!"=="home" if not defined BASE_PYTHON set "BASE_PYTHON=!CFG_VALUE!\python.exe"
  )
)

set "XDG_DATA_HOME=%PROJECT_ROOT%.data"
set "TEMP=%PROJECT_ROOT%.tmp"
set "TMP=%PROJECT_ROOT%.tmp"
set "COQUI_TOS_AGREED=1"

if not exist "%XDG_DATA_HOME%" mkdir "%XDG_DATA_HOME%"
if not exist "%TEMP%" mkdir "%TEMP%"

if defined BASE_PYTHON if exist "!BASE_PYTHON!" if exist "%VENV_SITE_PACKAGES%" (
  if defined PYTHONPATH (
    set "PYTHONPATH=%PROJECT_ROOT%src;%VENV_SITE_PACKAGES%;%PYTHONPATH%"
  ) else (
    set "PYTHONPATH=%PROJECT_ROOT%src;%VENV_SITE_PACKAGES%"
  )
  set "VIRTUAL_ENV=%VENV_DIR%"
  "!BASE_PYTHON!" -m tts_win %*
) else (
  "%PYTHON_EXE%" -m tts_win %*
)
exit /b %ERRORLEVEL%

:trim_var
setlocal EnableDelayedExpansion
set "VALUE=!%~1!"
for /f "tokens=* delims= " %%Z in ("!VALUE!") do set "VALUE=%%Z"
:trim_loop
if defined VALUE if "!VALUE:~-1!"==" " (
  set "VALUE=!VALUE:~0,-1!"
  goto trim_loop
)
endlocal & set "%~1=%VALUE%"
goto :eof
