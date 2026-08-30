@echo off
setlocal
cd /d "%~dp0"
call run_wigle_to_wdg.bat %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo Transfer failed. See wigle_to_wdg.log for details.
) else (
    echo Transfer finished successfully.
)
pause
exit /b %EXITCODE%
