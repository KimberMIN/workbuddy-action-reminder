#!/usr/bin/env bash
# WorkBuddy Action Reminder — 飞书通知通道
# 中文标点前使用 ${TITLE} 明确变量边界，避免被当成变量名的一部分。
set -u
CONFIG="${ACTION_REMINDER_CONFIG:-$HOME/.workbuddy/action-reminder/config.json}"
TITLE="${1:-WorkBuddy 待操作提醒}"
BODY="${2:-}"
PYTHON="${ACTION_REMINDER_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x /usr/bin/python3 ]; then
    PYTHON=/usr/bin/python3
  else
    PYTHON="$(command -v python3 2>/dev/null)"
  fi
fi
[ -n "$PYTHON" ] || { printf '%s\n' 'feishu failed: Python 不可用' >&2; exit 1; }
TEXT="【${TITLE}】
${BODY}"
exec "$PYTHON" - "$CONFIG" "$TEXT" <<'PY'
import json
import os
import shutil
import subprocess
import sys


def fail(reason, code=1):
    # 不回显 CLI 原始输出、命令参数或配置，防止凭据进入日志。
    print('feishu failed: ' + reason, file=sys.stderr)
    sys.exit(code)


try:
    with open(sys.argv[1], encoding='utf-8') as f:
        cfg = json.load(f).get('feishu', {})
except (OSError, ValueError, AttributeError):
    fail('配置文件无法读取或格式错误')
if not cfg.get('target_id'):
    print('feishu skipped: target_id 未配置')
    sys.exit(0)
cli = os.environ.get('ACTION_REMINDER_LARK_CLI') or cfg.get('cli_path')
if cli:
    if not isinstance(cli, str) or not os.path.isabs(cli) or not os.access(cli, os.X_OK):
        fail('指定 CLI 必须是可执行文件的绝对路径')
else:
    cli = shutil.which('lark-cli')
    if not cli:
        candidate = os.path.expanduser('~/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli')
        if os.access(candidate, os.X_OK):
            cli = candidate
if not cli:
    fail('lark-cli 不可用')
kind = cfg.get('target_type', 'user')
identity = cfg.get('as', 'bot')
if kind not in ('user', 'chat') or identity not in ('bot', 'user'):
    fail('target_type 或发送身份无效')
try:
    result = subprocess.run([cli, 'im', '+messages-send', '--as', identity,
                             '--' + kind + '-id', str(cfg['target_id']),
                             '--text', sys.argv[2], '--format', 'json'],
                            capture_output=True, timeout=12)
except subprocess.TimeoutExpired:
    fail('CLI 调用超时')
except OSError:
    fail('CLI 无法启动，请检查路径、权限和 CPU 架构')
if result.returncode:
    fail('CLI 非零退出（exit=%d；原始输出已隐藏）' % result.returncode,
         result.returncode if result.returncode > 0 else 1)
try:
    data = json.loads(result.stdout)
except (ValueError, UnicodeDecodeError):
    fail('CLI 返回非 JSON 内容（原始输出已隐藏）')
if not isinstance(data, dict) or data.get('ok') is not True:
    fail('CLI 未确认成功（ok 非 true；错误详情已隐藏）')
print('feishu ok=True')
PY
