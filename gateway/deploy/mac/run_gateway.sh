#!/bin/bash
# 在 Mac mini 上启动网关：密钥由 1Password 注入（op run），不落盘。
# secrets.env 每行「变量名=op://库ID/条目ID/字段」，见 secrets.env.example。
# 无人值守（launchd）时用 1Password 服务账号：token 存在登录钥匙串
# （服务名 wxbot-op-service-account，账户 wxbot）；取不到就退回桌面版授权。
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
  if token="$(security find-generic-password -s wxbot-op-service-account -a wxbot -w 2>/dev/null)"; then
    export OP_SERVICE_ACCOUNT_TOKEN="$token"
  fi
  unset token
fi
# 服务账号 token 只给 op 用，不传给网关和 MCP 子进程
exec op run --env-file deploy/mac/secrets.env -- env -u OP_SERVICE_ACCOUNT_TOKEN .venv/bin/python -X utf8 gateway.py
