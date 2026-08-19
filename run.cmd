@echo off
rem Wrapper that runs run.ps1 with the execution policy bypassed for this
rem process only. Lets people launch the app on default Windows without
rem having to Set-ExecutionPolicy first. Double-click or run:  .\run.cmd
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
