@echo off
rem Cherry Remote App launcher (C-end)
rem Uses %~dp0 so it works regardless of install directory.
cd /d "%~dp0"
set PYTHONPATH=src
".venv\Scripts\python.exe" -m cherry_remote_app -c config.yaml
if errorlevel 1 pause
