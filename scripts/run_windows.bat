@echo off
chcp 65001 >nul
cd /d %~dp0\..
if not exist .venv (
  py -3.12 -m venv .venv
  .venv\Scripts\pip install -e ".[wx,dev]"
)
.venv\Scripts\python -m wxbot run %*
pause
