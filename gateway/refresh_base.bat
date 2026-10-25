@echo off
REM 云谷美食底库每周更新：用 Windows 计划任务每周一凌晨运行（见 README「美食底库」）
REM 高德 Key 需要设成系统环境变量 AMAP_KEY
cd /d %~dp0
set PYTHONIOENCODING=utf-8
if not exist data mkdir data
python refresh_base.py %* >> data\refresh_base.log 2>&1
