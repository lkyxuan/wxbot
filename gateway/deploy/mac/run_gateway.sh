#!/bin/bash
# 在 Mac mini 上启动网关：密钥由 1Password 注入（op run），不落盘。
# secrets.env 每行「变量名=op://库ID/条目ID/字段」，见 secrets.env.example。
set -euo pipefail
cd "$(dirname "$0")/../.."
exec op run --env-file deploy/mac/secrets.env -- .venv/bin/python -X utf8 gateway.py
