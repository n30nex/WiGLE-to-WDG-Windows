@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" copy /Y ".env.example" ".env" >nul
notepad ".env"
