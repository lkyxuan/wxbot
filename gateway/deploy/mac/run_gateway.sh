#!/bin/bash
# 在 Mac mini 上启动网关：密钥由 1Password 注入（op run），不落盘。
# secrets.env 每行「变量名=op://库ID/条目ID/字段」，见 secrets.env.example。
# 只用 1Password 服务账号，不走桌面版授权（无人值守时没人点弹窗）：
# token 存在登录钥匙串（服务名 wxbot-op-service-account，账户 wxbot）。
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
  OP_SERVICE_ACCOUNT_TOKEN="$(security find-generic-password -s wxbot-op-service-account -a wxbot -w)"
  export OP_SERVICE_ACCOUNT_TOKEN
fi
# op 启动时会去读桌面版的设置文件（~/Library/Group Containers/...1password），
# 在 launchd 下这一步可能被系统隐私保护卡住。给 op 一个空的 HOME 让它不碰桌面版，
# 网关进程仍用真实 HOME；服务账号 token 只给 op 用，不传给网关和 MCP 子进程。
REAL_HOME="$HOME"
OP_HOME="$PWD/.op-home"
mkdir -p "$OP_HOME"
HOME="$OP_HOME" exec op run --env-file deploy/mac/secrets.env -- \
  env -u OP_SERVICE_ACCOUNT_TOKEN HOME="$REAL_HOME" .venv/bin/python -X utf8 gateway.py
