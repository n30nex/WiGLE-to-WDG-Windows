@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto use_py

where python >nul 2>nul
if not errorlevel 1 goto use_python

echo Python 3 was not found. Install Python 3 and enable "Add Python to PATH".
exit /b 1

:use_py
py -3 wigle_to_wdg.py %*
set "EXITCODE=%ERRORLEVEL%"
goto done

:use_python
python wigle_to_wdg.py %*
set "EXITCODE=%ERRORLEVEL%"
goto done

:done
if not "%EXITCODE%"=="0" echo Transfer failed. See wigle_to_wdg.log for details.
exit /b %EXITCODE%
