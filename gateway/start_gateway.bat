@echo off
REM 启动自建 AI 网关（DeepSeek Key 建议设成系统环境变量 DEEPSEEK_API_KEY）
cd /d %~dp0
python gateway.py
pause
